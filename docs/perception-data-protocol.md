# 探海灵眸 Oceanus — 感知数据协议（WP-13）

> 版本：1.0
> 日期：2026-09-19
> 上位口径：`项目文档/探海灵眸_项目计划书.md` §6（感知、执行与数据闭环）、§7.1（感知指标）
> 关联机制：`ml/datasets/manifests/README.md`（WP-06 数据集清单）、`ml/scripts/evaluate_opencv.py`（WP-06 评测）
> 实现工具：`ml/scripts/convert_annotations.py`、`ml/scripts/validate_dataset_integrity.py`、`ml/scripts/calibrate_camera.py`

本文档冻结真实视频/图像进入系统**之前**的标准数据交换格式与校验纪律：
标注转换、完整性校验、标定与评测准备必须走同一套协议，防止「手工目录 + 自创格式」导致
评测口径不可复现。

---

## 1. 冻结格式：Oceanus COCO-like JSON

标准数据交换格式固定为 **Oceanus COCO-like JSON**（`schema_version: "1.0"`）。

最小顶层字段（12 个，全部必填）：

```text
schema_version
dataset_id
dataset_type
evidence_level
source
captured_at
camera
images
annotations
categories
calibration
checksums
```

顶层字段语义：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | str | 固定 `"1.0"`。升级必须同步更新校验器与测试。 |
| `dataset_id` | str | 数据集唯一 ID，可追溯（建议 `<类型>_<采集批次>_<地点>`）。 |
| `dataset_type` | str | `planned` \| `synthetic` \| `quasi_real` \| `real`（冻结）。 |
| `evidence_level` | str | `E0`~`E4`，按第 4 节规则解析；**脚本不自动授予 E4**。 |
| `source` | object | 采集来源：`device`、`region`、`date_range`、`license`、`acquisition_note`。`real` 必须填写真实采集来源说明（`acquisition_note` 或等价字段）。 |
| `captured_at` | str | ISO 8601 时间（含时区）。 |
| `camera` | object | 相机信息：`model`、`lens`、`resolution`（[w, h]）、`calibration_ref`（可选，指向标定输出）。 |
| `images` | list | 图片条目，见第 2 节。 |
| `annotations` | list | 标注条目，字段冻结，见第 3 节。 |
| `categories` | list | `[{id, name, supercategory}]`；顺序即 YOLO 类别索引，改动需重训并重新校验。 |
| `calibration` | object | 标定信息：`camera_matrix`、`dist_coeffs`、`homography`、`square_size_m`、`pattern`、`ref`；未标定可为 `null`/空。 |
| `checksums` | object | `{algorithm: "sha256", values: {<file_name>: <hex>}}`；与 `images[]` 一一对应，缺失须在报告中列出。 |

示例：

```json
{
  "schema_version": "1.0",
  "dataset_id": "synthetic_demo_20260919",
  "dataset_type": "synthetic",
  "evidence_level": "E1",
  "source": {
    "device": "synthetic-renderer-v1",
    "region": "lab",
    "date_range": ["2026-09-19", "2026-09-19"],
    "license": "internal",
    "acquisition_note": "程序化合成的海面漂浮物帧，非现场采集"
  },
  "captured_at": "2026-09-19T10:00:00+08:00",
  "camera": {
    "model": "synthetic-cam",
    "lens": "n/a",
    "resolution": [640, 480],
    "calibration_ref": null
  },
  "images": [
    {
      "id": 1,
      "file_name": "images/train/0001.jpg",
      "width": 640,
      "height": 480,
      "split": "train",
      "captured_at": "2026-09-19T10:00:01+08:00",
      "md5": "",
      "sha256": ""
    }
  ],
  "annotations": [
    {
      "id": 1,
      "image_id": 1,
      "category_id": 0,
      "bbox": [120.0, 180.0, 40.0, 30.0],
      "area": 1200.0,
      "iscrowd": 0,
      "source_type": "synthetic",
      "review_status": "reviewed",
      "reviewer": "pipeline",
      "note": ""
    }
  ],
  "categories": [
    {"id": 0, "name": "foam", "supercategory": "marine_litter"},
    {"id": 1, "name": "plastic", "supercategory": "marine_litter"},
    {"id": 2, "name": "fishing_gear", "supercategory": "marine_litter"},
    {"id": 3, "name": "other", "supercategory": "marine_litter"}
  ],
  "calibration": {
    "camera_matrix": null,
    "dist_coeffs": null,
    "homography": null,
    "square_size_m": null,
    "pattern": null,
    "ref": ""
  },
  "checksums": {
    "algorithm": "sha256",
    "values": {}
  }
}
```

## 2. 图片条目（`images[]`）

每条必含：

```text
id            int    图片唯一 ID（文件内唯一）
file_name     str    相对路径（相对数据集根目录），用于定位磁盘文件
width         int    像素宽
height        int    像素高
split         str    train | val | test | blind（四类分集，冻结，见 manifests/README.md）
```

可选：`captured_at`、`md5`、`sha256`、`conditions`（混淆因素分层：lighting / background /
target_size / weather）、`extra`。

## 3. 标注对象（`annotations[]`）

标注对象**必须**包含以下字段（冻结，缺一即坏样本）：

```text
image_id      int     所属图片 id（必须存在于 images[]）
category_id   int     类别 id（必须存在于 categories[]）
bbox          [x, y, w, h]  绝对像素坐标：x>=0, y>=0, w>0, h>0，
                            且 (x+w)<=width、(y+h)<=height（容差内）
area          float   bbox 面积，须与 w*h 一致（容差内）
iscrowd       int     0/1
source_type   str     manual | imported | synthetic | quasi_real | real（冻结）
review_status str     unreviewed | reviewed | approved
```

可选：`id`（建议提供并保证唯一；缺失时按顺序自动编号）、`reviewer`、`note`。

### 3.1 source_type（冻结）

| source_type | 含义 | 证据约束 |
| --- | --- | --- |
| `manual` | 人工标注 | 最高 E2（需人工复核记录） |
| `imported` | 从外部格式导入（如 YOLO txt） | 导入后默认 `unreviewed`，须人工复核后方可升级证据 |
| `synthetic` | 程序合成帧 | **最高只能标 E2**，禁止 E3/E4 |
| `quasi_real` | 准真实（水槽/码头受控环境） | 最高 E2 |
| `real` | 真实设备/真实海域/真实用户 | 默认 E3；**必须带真实采集来源（`source.acquisition_note`）与复核状态（`review_status ∈ {reviewed, approved}`）**；无真实来源时不得标 `real` |

### 3.2 复核状态（review_status，冻结）

```text
unreviewed   未复核（导入/自动生成的默认状态，不得直接用于评测）
reviewed     人工复核通过
approved     复核并批准（可用于标注一致性基准）
```

`real` 标注的 `review_status` 必须为 `reviewed` 或 `approved`；`unreviewed` 的 `real`
标注视为坏样本。

## 4. 证据等级纪律（冻结，与计划书 §1、manifests/README.md 一致）

```text
planned    → E0（只有规划，没有数据）
synthetic  → E1 | E2（最高 E2，禁止 E3/E4）
quasi_real → E2
real       → E3（默认；E4 需要合同/订单/验收凭证，脚本不自动授予）
```

校验规则：

- `dataset_type=synthetic` 且 `evidence_level ∈ {E3, E4}` → 无效（越级宣称）。
- `dataset_type=real`：必须提供真实采集来源（`source.acquisition_note` 等）；`evidence_level=E4`
  只有在 JSON 内显式给出 `evidence_upgrade: {basis: "contract|order|acceptance", reference: "..."}`
  时才被接受，否则拒绝（脚本绝不自动授予 E4）。
- 数据集内任何 `source_type=synthetic` 的标注出现在 `evidence_level ≥ E3` 的数据集中 → 污染，
  视为坏样本/阻断问题（合成标注不得抬高真实数据集的证据）。

## 5. 完整性校验（validate / 导入时）

导入与校验工具必须执行以下检查，坏样本**跳过并在报告中列出**：

1. **结构**：12 个顶层字段齐全；`schema_version == "1.0"`。
2. **图片路径**：`images[].file_name` 在磁盘上存在（`--images-root` 提供时）。
3. **类别**：`category_id` 必须落在 `categories[]` 的 id 集合内。
4. **bbox**：`x>=0, y>=0, w>0, h>0`，且在图片尺寸内（容差 1e-3）。
5. **面积**：`area ≈ w*h`（相对容差 1e-3）。
6. **重复 ID**：`images[].id` 重复、`annotations[].id` 重复 → 坏样本/阻断。
7. **引用完整**：`annotation.image_id` 必须存在于 `images[]`；无标注的图片 = 负样本（合法）。
8. **跨 split 泄漏**：同一 `file_name`（或同一图片 id）同时出现在 `train|val` 与 `test|blind`
   → `isolation_violation: true`；跨文件校验时（多个 JSON 输入）同样检测。
9. **校验和**：`checksums.values[file_name]` 与磁盘文件 sha256 不一致 → 坏样本。
10. **证据等级**：见第 4 节。

校验报告（JSON）固定包含：

```text
input_files       输入文件 sha256 哈希列表
dataset_id
dataset_type
evidence_level
sample_count      {images, annotations}
category_distribution   {类别名: 实例数}
skipped_samples   [{type, id, reason}, ...]   （跳过的坏样本）
warnings          []
status            valid | invalid
isolation_violation    bool
```

**空数据集语义**：`images=[]` 时状态为 `not_evaluated`（若 `dataset_type=planned` 则结构合法，
但任何评测脚本必须输出 `not_evaluated`，不得生成精度/召回等指标）。

## 6. 标注转换

### 6.1 Oceanus JSON → YOLO txt（训练/评测准备）

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe ml\scripts\convert_annotations.py to-yolo `
  --input <dataset.json> --images-root <data_root> --output <out_root>
```

- 每张图输出 `labels/<split>/<stem>.txt`，每行 `class cx cy w h`（归一化到 0~1）。
- 归一化由 `bbox` 与图片 `width/height` 计算；越界（<=0 或 >1）的框 → 坏样本跳过并列出。
- 报告同时输出到 stdout 与 `--report` 指定的 JSON。

### 6.2 YOLO txt → Oceanus JSON（导入入口）

```powershell
.\.venv-analysis\Scripts\python.exe ml\scripts\convert_annotations.py from-yolo `
  --images <img_dir> --labels <lbl_dir> --dataset-type <type> --evidence-level <E0..E3> `
  --output <dataset.json> [--source-device "..." --source-region "..." --source-note "..." `
  --skip-image-hash --strict]
```

- 导入后所有标注 `source_type="imported"`、`review_status="unreviewed"`（必须人工复核）。
- 必须显式给出 `--dataset-type` 与 `--evidence-level`；越级（如 synthetic+E3）直接报错。
- 默认计算每张图 sha256 写入 `checksums.values`（`--skip-image-hash` 可跳过并记警告）。
- 坏样本（坏标签行、缺图、缺标签、图片无法读取尺寸）跳过并在报告列出。

## 7. 标定（calibrate_camera.py）

- **棋盘格内参 + 畸变**：`cv2.calibrateCamera`（OpenCV 传统视觉，非 YOLO、非训练模型）。
- **简单平面映射**：像素坐标 ↔ 平面（米制）坐标的单应矩阵，来自棋盘角点或显式对应点。
- **没有图像时必须明确失败**（退出码 1），**不输出任何默认假参数**。
- 可用图像 < 2 张（棋盘模式）同样失败。
- 报告固定包含：输入文件哈希、图像数、可用/跳过数、跳过原因、重投影误差、相机矩阵、
  畸变系数、单应矩阵（若计算）、`evidence_level`。
- 合成棋盘图默认 `evidence_level=E1/E2`；`E3` 需要 `--real-source`（真实采集说明）；
  `E4` 拒绝。

```powershell
# 棋盘格标定（内参+畸变+平面映射）
.\.venv-analysis\Scripts\python.exe ml\scripts\calibrate_camera.py chessboard `
  --images <dir_or_glob> --pattern 9x6 --square-size 0.03 `
  --output artifacts\calibration\cam_01.json

# 仅平面映射（至少 4 组对应点）
.\.venv-analysis\Scripts\python.exe ml\scripts\calibrate_camera.py plane-map `
  --points correspondences.json --output artifacts\calibration\plane_01.json
```

`correspondences.json` 格式：

```json
{"points": [
  {"pixel": [x1, y1], "plane": [X1, Y1]},
  {"pixel": [x2, y2], "plane": [X2, Y2]}
]}
```

## 8. 能力口径（冻结，不得越级）

- 本协议与工具只属于**数据准备层**：转换、校验、标定、评测准备，证据等级最高 E2
  （合成/受控），`real` 数据必须带真实采集来源与人工复核，E4 一律不自动授予。
- 未取得真实数据前，感知指标保持 `not_evaluated` 或 `null`，不生成虚假精度。
- `convert_annotations.py` / `validate_dataset_integrity.py` / `calibrate_camera.py`
  全部是确定性工具脚本，不是 YOLO、不是视觉大模型、不是已训练深度模型。
- 真实设备标定、真实海域数据与复核记录必须另立证据条目（E3/E4），由总控按
  `docs/evidence-claim-policy.md`（WP-15）登记。

## 9. 集成接口（供总控 WP-16 串行接入，本文件不直接修改共享文件）

1. `ml/configs/seasight.yaml`：新增 `data_protocol:` 段，声明
   `schema_version: "1.0"`、`convert_entry: ml/scripts/convert_annotations.py`、
   `validate_entry: ml/scripts/validate_dataset_integrity.py`、
   `calibration_dir: artifacts/calibration`、`protocol_doc: docs/perception-data-protocol.md`。
2. `ml/scripts/evaluate_opencv.py`：允许 ground-truth 来源二选一——既有 manifest 机制
   （YOLO labels）或本协议 JSON（`--gt-json <dataset.json>`）；加载时复用本协议的
   校验结果（跳过坏样本、拒绝越级证据、检测泄漏），空数据仍输出 `not_evaluated`。
3. `ml/scripts/check_dataset.py`：在既有 YOLO 体检之外，可选读取 `--data-protocol-json`
   做协议级校验；空数据语义不变。
4. `ml/datasets/manifests/**`：模板 `template_manifest.yaml` 增加可选
   `protocol_ref: <dataset.json>` 字段，把清单与协议 JSON 关联；评测脚本据此发现
   ground-truth 文件。

## 10. 测试与验收

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe -m pytest ml\tests\test_annotation_pipeline.py ml\tests\test_calibration.py -q
```

测试全部离线：合成棋盘图由 OpenCV 绘制生成；标注样例为内存/临时目录构造；
不访问公网、不调用任何真实模型。
