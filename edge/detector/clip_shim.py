"""为 YOLO-World 提供 CLIP 文本编码器垫片（从 scripts/detect_marine_demo.py 提取）。

背景
----
ultralytics 的 YOLO-World 在 ``model.set_classes(prompts)`` 时需要一个
OpenAI 的 ``clip`` 包来做文本编码。本仓库不想为此再装一个运行时，
于是用 **transformers 版的 CLIP**（``openai/clip-vit-base-patch32``）
临时顶替 ``clip`` 模块 —— 只要 ``encode_text`` / ``tokenize`` / ``load``
三个接口对得上，ultralytics 就察觉不到区别。

这个垫片原本只写在一份演示脚本里（``scripts/detect_marine_demo.py``）。
现在边缘侧也要用，所以提取成共享模块，避免出现"两份 shim 各自演化"。

★ 依赖是可选的，导入必须安全
----------------------------
本模块**顶层不导入 torch / transformers**。在这台机器上，边缘测试环境
（``.venv-analysis``）没有 torch，如果顶层导入，``import world_detector``
会直接炸掉 ``make test-edge``。所以：

- ``clip_available()``  只做探测，不导入
- ``install_clip_shim()`` 内部才导入，缺依赖时抛 ``ClipUnavailable`` 并带可执行的提示

这是本项目"可选依赖不能把主线拖下水"的一贯做法。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

# CLIP 文本编码器的默认落盘位置（与 scripts/detect_marine_demo.py 的 --clip-dir 默认值一致）
DEFAULT_CLIP_DIR = Path.home() / ".cache" / "clip-vit-base-patch32"

# 需要存在的权重文件名。HF 上这个模型提供的是 pytorch_model.bin（不是 safetensors），
# 这也是为什么探测不能只看目录是否存在 —— 目录存在但只有一个 .incomplete 分片，
# 是"下载到一半"的典型状态，必须当成不可用。
_CLIP_WEIGHT_FILES = ("pytorch_model.bin", "model.safetensors")


class ClipUnavailable(RuntimeError):
    """CLIP 文本编码器不可用（缺少依赖或权重）。"""


def clip_available() -> tuple[bool, str]:
    """探测 CLIP 垫片能否工作。返回 ``(可用, 原因)``。

    只做 importlib 层面的探测与文件存在性检查，**不加载模型**，
    因此可以安全地在启动日志、配置校验里调用。
    """
    try:
        import torch  # noqa: F401, PLC0415
    except ImportError as exc:
        return False, f"未安装 torch（{exc}）"
    try:
        import transformers  # noqa: F401, PLC0415
    except ImportError as exc:
        return False, f"未安装 transformers（{exc}）"
    return True, "依赖就绪"


def clip_dir_status(clip_dir: Path | str | None = None) -> tuple[bool, str]:
    """检查 CLIP 权重目录是否**完整**（有真正的权重文件，不是半截下载）。

    为什么要单独查这一步：``~/.cache/huggingface/hub`` 里出现
    ``blobs/*.incomplete`` 而快照目录只有 ``config.json``，
    是"下载中断"的典型形态。此时目录**存在**，
    若不检查权重文件，报错会推迟到模型加载阶段，且信息指向完全不同的方向。
    """
    path = Path(clip_dir).expanduser() if clip_dir else DEFAULT_CLIP_DIR
    if not path.exists():
        return False, f"目录不存在：{path}"
    if not path.is_dir():
        return False, f"不是目录：{path}"
    for name in _CLIP_WEIGHT_FILES:
        if (path / name).exists():
            return True, f"权重就绪：{path / name}"
    return False, (
        f"目录存在但没有权重文件（{'/'.join(_CLIP_WEIGHT_FILES)}）：{path} "
        "—— 常见原因是下载未完成。国内网络建议走镜像，例如："
        "HF_ENDPOINT=https://hf-mirror.com python -c \"from huggingface_hub import "
        "snapshot_download; snapshot_download('openai/clip-vit-base-patch32', "
        f"local_dir=r'{path}')\""
    )


def install_clip_shim(clip_dir: Path | str | None = None) -> None:
    """把 transformers 版 CLIP 注册成 ``clip`` 模块（供 ultralytics 使用）。

    调用一次即可；重复调用是幂等的（会覆盖同名模块）。

    抛出 ``ClipUnavailable`` 表示依赖或权重缺失，调用方应据此走降级路径。
    """
    path = Path(clip_dir).expanduser() if clip_dir else DEFAULT_CLIP_DIR

    deps_ok, deps_reason = clip_available()
    if not deps_ok:
        raise ClipUnavailable(deps_reason)
    weights_ok, weights_reason = clip_dir_status(path)
    if not weights_ok:
        raise ClipUnavailable(weights_reason)

    import torch  # noqa: PLC0415
    from transformers import CLIPModel, CLIPTokenizer  # noqa: PLC0415

    class TransformersClip(torch.nn.Module):  # noqa: D101
        """Ultralytics YOLO-World 用到的最小 OpenAI CLIP 接口。"""

        def __init__(self, model: CLIPModel, device: torch.device) -> None:
            super().__init__()
            self.model = model.to(device)
            self.model.eval()

        def encode_text(
            self, token_ids: torch.Tensor, *_: Any, **__: Any
        ) -> torch.Tensor:
            token_ids = token_ids.to(self.model.device)
            return self.model.get_text_features(
                input_ids=token_ids,
                attention_mask=(token_ids != 0).long(),
            )

    tokenizer = CLIPTokenizer.from_pretrained(str(path))

    def tokenize(texts: str | list[str], truncate: bool = True) -> torch.Tensor:
        if isinstance(texts, str):
            texts = [texts]
        encoded = tokenizer(
            texts,
            padding="max_length",
            truncation=bool(truncate),
            max_length=77,
            return_tensors="pt",
        )
        return encoded["input_ids"]

    def load(
        _name: str,
        device: str | torch.device = "cpu",
        **_kwargs: Any,
    ) -> tuple[Any, None]:
        model = CLIPModel.from_pretrained(str(path))
        return TransformersClip(model, torch.device(device)), None

    clip_module = types.ModuleType("clip")
    clip_module.load = load  # type: ignore[attr-defined]
    clip_module.tokenize = tokenize  # type: ignore[attr-defined]
    sys.modules["clip"] = clip_module


def ultralytics_available() -> tuple[bool, str]:
    """探测 ultralytics 是否可用（同样不导入）。"""
    try:
        import ultralytics  # noqa: F401, PLC0415

        return True, f"ultralytics {ultralytics.__version__}"
    except ImportError as exc:
        return False, f"未安装 ultralytics（{exc}）"


def environment_report(clip_dir: Path | str | None = None) -> dict[str, Any]:
    """把"开放词汇通道能不能跑"的判定拆成可打印的一组事实。

    给启动自检和排障用 —— 与其在跑完一帧之后看到空结果，
    不如一开始就把缺什么、缺在哪说清楚。
    """
    deps_ok, deps_reason = clip_available()
    weights_ok, weights_reason = clip_dir_status(clip_dir)
    ultra_ok, ultra_reason = ultralytics_available()
    return {
        "ready": deps_ok and weights_ok and ultra_ok,
        "deps": {"ok": deps_ok, "reason": deps_reason},
        "clip_weights": {
            "ok": weights_ok,
            "reason": weights_reason,
            "dir": str(Path(clip_dir).expanduser() if clip_dir else DEFAULT_CLIP_DIR),
        },
        "ultralytics": {"ok": ultra_ok, "reason": ultra_reason},
    }
