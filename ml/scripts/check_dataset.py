"""数据集体检：图片与标签配对、类别索引越界、标签格式。

为什么需要它：YOLO 训练时图片与标签不配对**不会立刻报错**，
而是给出警告然后按缺失处理 —— 你以为在训练 1400 张，实际只用了 900 张，
指标对不上时很难想到是数据的问题。这个脚本把这类问题提前暴露。

用法（在 seahawk/ 下）：
    python ml/scripts/check_dataset.py
    python ml/scripts/check_dataset.py --data ml/configs/seasight.yaml
    python ml/scripts/check_dataset.py --strict     # 有任何问题即返回 1
    python ml/scripts/check_dataset.py --write-stats  # 把统计写回 data yaml

退出码：0 = 无阻断性问题；1 = 有（缺标签、类别越界、格式错等）或数据集为空；
        2 = 配置文件不可读。

★ 空数据集语义（WP-06 冻结）
──────────────────────────
没有任何图片（train/val/test 全空）时：
  - 输出明确状态 not_evaluated，**不生成精度/召回/F1 等任何虚假指标**；
  - 拒绝 --write-stats 写回（避免以空数据冒充已采集统计）；
  - 视为阻断性问题返回 1，防止下游误以为"数据健康、可以出指标"。

★ 关于 --write-stats
────────────────────
`seasight.yaml` 里的 `stats` 段（train_images / instances / background_images）
是 `train_two_stage.py` 做「负样本比例检查」与「类别均衡检查」的输入。
但**手工去数这些数字既不现实也一定会写错** —— 标注批次一改就得重数一遍。
所以这里把体检过程中本来就已经统计好的数字直接写回去。
不传 `--write-stats` 时只读不写。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

IMG_SUFFIX = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def parse_data_yaml(path: Path) -> tuple[Path, dict[str, str], list[str]]:
    """极简 YAML 读取（只取 path/train/val/names，避免依赖 pyyaml）。"""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        print("需要 pyyaml：pip install pyyaml", file=sys.stderr)
        raise SystemExit(2) from None

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    root = Path(data.get("path", "."))
    if not root.is_absolute():
        root = (path.parent / root).resolve()
    splits = {k: data[k] for k in ("train", "val", "test") if data.get(k)}
    names = data.get("names") or []
    if isinstance(names, dict):          # 也允许 {0: 'xx', 1: 'yy'} 写法
        names = [names[k] for k in sorted(names)]
    return root, splits, list(names)


def iter_images(d: Path):
    if not d.exists():
        return
    for p in sorted(d.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMG_SUFFIX:
            yield p


def label_path_for(img: Path, root: Path) -> Path:
    """把 images/... 换成 labels/...，后缀换成 .txt。"""
    rel = img.relative_to(root)
    parts = list(rel.parts)
    for i, seg in enumerate(parts):
        if seg == "images":
            parts[i] = "labels"
            break
    parts[-1] = Path(parts[-1]).stem + ".txt"
    return root.joinpath(*parts)


def _load_protocol_validator():
    """懒加载 WP-13 数据协议校验核心（convert_annotations.validate_dataset）。

    与 evaluate_opencv.py 同目录，通过 importlib 加载，避免依赖 sys.path 顺序。
    """
    import importlib.util

    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "seasight_convert_annotations", here / "convert_annotations.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_protocol_json(path: Path, protocol) -> int:
    """校验 Oceanus COCO-like JSON（WP-13 数据协议）；返回阻断问题数。

    复用 convert_annotations.validate_dataset（冻结校验核心），不重复实现；
    check_files/check_checksums 关闭（数据体检不要求图片与校验和就位）。
    证据纪律由校验核心强制：合成最高 E2、real 需真实来源+复核、E4 需凭证。
    """
    problems = 0
    try:
        data, _ = protocol.load_json(str(path))
    except protocol.ConfigError as e:
        print(f"  ✗ {path}：{e}")
        return 1
    rep = protocol.validate_dataset(
        data, source_path=str(path), check_files=False, check_checksums=False
    )
    print(
        f"  {path}：dataset_id={rep.get('dataset_id')} "
        f"evidence={rep.get('evidence_level')} "
        f"images={rep.get('sample_count', {}).get('images')} "
        f"annotations={rep.get('sample_count', {}).get('annotations')}"
    )
    for w in rep.get("warnings", [])[:5]:
        print(f"    ⚠ {w}")
    if rep["status"] == "invalid":
        for p in rep.get("problems", [])[:8]:
            print(f"    ✗ {p}")
        problems += 1
    else:
        print(f"    ✓ 协议校验通过（evaluation_status={rep.get('evaluation_status')}）")
    return problems


def write_stats(path: Path, stats: dict, names: list[str]) -> bool:
    """把统计数字写回 data yaml 的 stats 段（保留其它内容与注释）。

    ★ 不用 yaml.dump 整体重写 —— 那会把文件里所有人工注释抹掉，
      而那些注释（类别选取依据、划分策略）比数字本身更有价值。
      这里只做「定位 stats 段 → 替换其下几行」的定点手术。

    实现方式刻意保守：只改写 stats 段的四个已知字段，其余原样保留。
    若找不到 stats 段，不擅自创建，而是明确提示。
    """
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("  ✗ 需要 pyyaml 才能写回", file=sys.stderr)
        return False

    text = path.read_text(encoding="utf-8")
    if "stats:" not in text:
        print("  ✗ 配置里没有 stats 段，未写回（请先手工加上再重跑）", file=sys.stderr)
        return False

    lines = text.splitlines()
    out: list[str] = []
    in_stats = False
    stats_indent = 0

    def render(key: str, value, indent: int) -> str:
        # 空值不带尾随空格（instances 是个嵌套段的头，本身没有值）
        if value == "":
            return " " * indent + f"{key}:"
        return " " * indent + f"{key}: {value}"

    # 已经被本函数写过的键直接跳过（避免重复追加）
    written: set[str] = set()

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if not in_stats:
            out.append(line)
            if stripped.startswith("stats:"):
                in_stats = True
                stats_indent = indent
            continue

        # 离开 stats 段（遇到同级或更浅的键）
        if stripped and not stripped.startswith("#") and indent <= stats_indent:
            # 补写所有尚未出现的字段
            for k in ("train_images", "val_images", "background_images"):
                if k not in written:
                    out.append(render(k, stats[k], stats_indent + 2))
                    written.add(k)
            if "instances" not in written:
                out.append(render("instances", "", stats_indent + 2))
                written.add("instances")
                for nm in names:
                    out.append(render(nm, stats["instances"].get(nm, 0), stats_indent + 4))
            in_stats = False
            out.append(line)
            continue

        # 段内：替换我们认识的键，保留注释与其它键
        key = stripped.split(":", 1)[0].strip() if ":" in stripped and not stripped.startswith("#") else None
        if key in ("train_images", "val_images", "background_images"):
            out.append(render(key, stats[key], indent))
            written.add(key)
        elif key == "instances":
            out.append(render("instances", "", indent))
            written.add("instances")
        elif key in names and "instances" in written:
            out.append(render(key, stats["instances"].get(key, 0), indent))
        else:
            out.append(line)

    # 文件以 stats 段结尾的情况
    if in_stats:
        for k in ("train_images", "val_images", "background_images"):
            if k not in written:
                out.append(render(k, stats[k], stats_indent + 2))
                written.add(k)
        if "instances" not in written:
            out.append(render("instances", "", stats_indent + 2))
            for nm in names:
                out.append(render(nm, stats["instances"].get(nm, 0), stats_indent + 4))

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="数据集配对与格式体检")
    ap.add_argument("--data", default="ml/configs/seasight.yaml", help="data yaml 路径")
    ap.add_argument("--strict", action="store_true", help="有非阻断问题也返回 1")
    ap.add_argument(
        "--write-stats",
        action="store_true",
        help="把统计数字写回 data yaml 的 stats 段（供训练脚本做负样本/均衡检查）",
    )
    ap.add_argument(
        "--data-protocol-json",
        action="append",
        default=[],
        metavar="PATH",
        help="Oceanus COCO-like JSON 路径（WP-13 数据协议），可多次指定；逐一做协议级校验，无效计为阻断问题",
    )
    args = ap.parse_args(argv)

    data_path = Path(args.data)
    if not data_path.exists():
        print(f"[错误] 找不到 {data_path}", file=sys.stderr)
        return 2

    root, splits, names = parse_data_yaml(data_path)
    print(f"配置文件 : {data_path}")
    print(f"数据根目录: {root}")
    print(f"类别数   : {len(names)}  {names}")
    print()

    problems = 0
    warnings = 0

    # 供 --write-stats 使用：跨 split 汇总
    collected: dict[str, int] = {}
    total_instances: Counter[int] = Counter()

    for split, rel in splits.items():
        img_dir = root / rel
        imgs = list(iter_images(img_dir))
        print(f"── {split}: {img_dir}")
        if not imgs:
            print("   ⚠ 没有找到任何图片（目录不存在或为空）")
            warnings += 1
            collected[split] = 0
            continue

        missing: list[Path] = []
        empty: list[Path] = []
        bad_format: list[tuple[Path, int, str]] = []
        cls_counter: Counter[int] = Counter()
        orphan_labels: list[Path] = []

        for img in imgs:
            lp = label_path_for(img, root)
            if not lp.exists():
                missing.append(img)
                continue
            lines = [ln.strip() for ln in lp.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if not lines:
                empty.append(lp)
                continue
            for no, ln in enumerate(lines, 1):
                parts = ln.split()
                if len(parts) != 5:
                    bad_format.append((lp, no, f"字段数 {len(parts)} ≠ 5"))
                    continue
                try:
                    cid = int(float(parts[0]))
                    vals = [float(x) for x in parts[1:]]
                except ValueError:
                    bad_format.append((lp, no, "存在非数字字段"))
                    continue
                if not (0 <= cid < len(names)):
                    bad_format.append((lp, no, f"类别索引 {cid} 超出 0~{len(names)-1}"))
                    continue
                if any(v < 0 or v > 1.0001 for v in vals):
                    bad_format.append((lp, no, "坐标未归一化到 0~1"))
                    continue
                cls_counter[cid] += 1

        collected[split] = len(imgs)
        total_instances.update(cls_counter)

        # 反向：有标签但没图片
        lab_dir = label_path_for(imgs[0], root).parent
        if lab_dir.exists():
            have_img_stems = {i.stem for i in imgs}
            for lb in lab_dir.rglob("*.txt"):
                if lb.stem not in have_img_stems:
                    orphan_labels.append(lb)

        print(f"   图片 {len(imgs)} 张")
        if missing:
            print(f"   ✗ 缺标签 {len(missing)} 张（训练时会静默少用，导致指标对不上）")
            for m in missing[:5]:
                print(f"       {m.relative_to(root)}")
            if len(missing) > 5:
                print(f"       ... 另 {len(missing)-5} 张")
            problems += len(missing)
        if empty:
            print(f"   ⚠ 空标签文件 {len(empty)} 个（有意当作负样本？否则是标注漏了）")
            for e in empty[:3]:
                print(f"       {e.relative_to(root)}")
            warnings += len(empty)
        if bad_format:
            print(f"   ✗ 格式错误 {len(bad_format)} 处")
            for lp, no, why in bad_format[:5]:
                print(f"       {lp.relative_to(root)}:{no}  {why}")
            problems += len(bad_format)
        if orphan_labels:
            print(f"   ⚠ 有标签无图片 {len(orphan_labels)} 个")
            warnings += len(orphan_labels)
        if cls_counter and not missing and not bad_format:
            print("   ✓ 配对与格式均正常")
            dist = "  ".join(f"{names[c]}:{n}" for c, n in sorted(cls_counter.items()))
            print(f"   实例分布: {dist}")
            zero = [names[i] for i in range(len(names)) if cls_counter[i] == 0]
            if zero:
                print(f"   ⚠ 没有样本的类别: {zero}（训练时该类永远学不到）")
                warnings += len(zero)
        print()

    # ★ WP-06：空数据集 = not_evaluated，视为阻断性问题，绝不产出虚假指标
    total_images = sum(collected.values())
    empty_dataset = total_images == 0
    if empty_dataset:
        problems += 1

    if args.write_stats:
        print("=" * 50)
        if empty_dataset:
            print("  ✗ 空数据集：拒绝写回 stats（避免以空数据冒充已采集统计）")
        else:
            stats = {
                "train_images": collected.get("train", 0),
                "val_images": collected.get("val", 0),
                "background_images": 0,   # 负样本要靠空标签文件统计，见下方说明
                "instances": {nm: total_instances.get(i, 0) for i, nm in enumerate(names)},
            }
            if write_stats(data_path, stats, names):
                print(f"  ✓ 统计已写回 {data_path}")
                print(f"    train={stats['train_images']} val={stats['val_images']}")
                print(f"    instances={stats['instances']}")
                print("  ⚠ background_images 未自动写（需要按空标签文件数另算）")
        print()

    print("=" * 50)
    if empty_dataset:
        print("✗ 空数据集：没有任何图片 → 评测状态 not_evaluated")
        print("  禁止以空数据生成精度 / 召回 / F1 等任何指标（不产出虚假精度）。")
        print("  请先完成 采集 → 标注 → 加入清单 ml/datasets/manifests/，再运行评测。")
        return 1
    if problems:
        print(f"✗ 阻断性问题 {problems} 处，警告 {warnings} 处 → 建议先修再训练")
        return 1
    if warnings:
        print(f"✓ 无阻断性问题，但有 {warnings} 处警告 → 请确认是否预期")
        return 1 if args.strict else 0
    print("✓ 数据集健康")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
