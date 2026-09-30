"""Run reproducible open-vocabulary detection for the frontend demo samples.

Ultralytics expects OpenAI's `clip` package for YOLO-World text embeddings.
This script supplies a small Transformers-backed shim so the model can run
from a local Hugging Face CLIP directory without installing another runtime.

Example:
    python scripts/detect_marine_demo.py \
      --image .wp-demo-originals/beach.jpg \
      --output artifacts/detections/beach.json
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch
from transformers import CLIPModel, CLIPTokenizer


DEFAULT_CLASSES = (
    "plastic bottle",
    "plastic waste",
    "garbage",
    "fishing net",
    "foam debris",
    "marine debris",
    "discarded shoe",
)

PREVIEW_COLORS = (
    (62, 113, 250),
    (255, 126, 52),
    (52, 199, 89),
    (255, 59, 48),
    (175, 82, 222),
    (255, 204, 0),
    (50, 173, 230),
)


class TransformersClip(torch.nn.Module):
    """Minimal OpenAI CLIP API used by Ultralytics YOLO-World."""

    def __init__(self, model: CLIPModel, device: torch.device):
        super().__init__()
        self.model = model.to(device)
        self.model.eval()

    def encode_text(self, token_ids: torch.Tensor, *_: Any, **__: Any) -> torch.Tensor:
        token_ids = token_ids.to(self.model.device)
        return self.model.get_text_features(
            input_ids=token_ids,
            attention_mask=(token_ids != 0).long(),
        )


def install_clip_shim(model_dir: Path) -> None:
    tokenizer = CLIPTokenizer.from_pretrained(str(model_dir))
    clip_module = types.ModuleType("clip")

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
    ) -> tuple[TransformersClip, None]:
        model = CLIPModel.from_pretrained(str(model_dir))
        return TransformersClip(model, torch.device(device)), None

    clip_module.load = load
    clip_module.tokenize = tokenize
    sys.modules["clip"] = clip_module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", action="append", required=True, help="Image path; repeat for batches.")
    parser.add_argument("--output", required=True, help="JSON output path.")
    parser.add_argument("--weights", default="yolov8s-worldv2.pt")
    parser.add_argument(
        "--clip-dir",
        default=str(Path.home() / ".cache" / "clip-vit-base-patch32"),
    )
    parser.add_argument("--class", dest="classes", action="append", help="Prompt; repeat to override defaults.")
    parser.add_argument("--conf", type=float, default=0.03)
    parser.add_argument("--iou", type=float, default=0.45)
    parser.add_argument("--imgsz", type=int, default=1600)
    parser.add_argument("--max-det", type=int, default=120)
    parser.add_argument("--device", default="0")
    parser.add_argument("--preview-dir", help="Optional directory for boxed review images.")
    return parser.parse_args()


def write_preview(
    image: np.ndarray,
    detections: list[dict[str, Any]],
    classes: list[str],
    output_path: Path,
) -> None:
    preview = image.copy()
    thickness = max(2, round(min(image.shape[:2]) / 700))
    font_scale = max(0.45, min(image.shape[:2]) / 1600)
    for detection in detections:
        x1, y1, x2, y2 = (round(value) for value in detection["bbox"])
        color = PREVIEW_COLORS[classes.index(detection["class"]) % len(PREVIEW_COLORS)]
        cv2.rectangle(preview, (x1, y1), (x2, y2), color, thickness)
        label = f'{detection["class"]} {detection["confidence"]:.1%}'
        (text_width, text_height), baseline = cv2.getTextSize(
            label,
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            thickness,
        )
        label_y = max(text_height + baseline, y1)
        cv2.rectangle(
            preview,
            (x1, label_y - text_height - baseline),
            (x1 + text_width + 10, label_y),
            color,
            -1,
        )
        cv2.putText(
            preview,
            label,
            (x1 + 5, label_y - baseline),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), preview, [cv2.IMWRITE_JPEG_QUALITY, 88]):
        raise SystemExit(f"Unable to write preview: {output_path}")


def main() -> int:
    args = parse_args()
    clip_dir = Path(args.clip_dir).expanduser().resolve()
    if not (clip_dir / "pytorch_model.bin").exists():
        raise SystemExit(f"CLIP weights not found: {clip_dir / 'pytorch_model.bin'}")

    install_clip_shim(clip_dir)
    from ultralytics import YOLOWorld

    classes = args.classes or list(DEFAULT_CLASSES)
    model = YOLOWorld(args.weights)
    model.set_classes(classes)

    payload: dict[str, Any] = {
        "model": Path(args.weights).name,
        "prompt_classes": classes,
        "confidence_threshold": args.conf,
        "iou_threshold": args.iou,
        "image_size": args.imgsz,
        "items": [],
    }

    for raw_path in args.image:
        image_path = Path(raw_path).resolve()
        image = cv2.imread(str(image_path))
        if image is None:
            raise SystemExit(f"Unable to read image: {image_path}")

        result = model.predict(
            image,
            conf=args.conf,
            iou=args.iou,
            imgsz=args.imgsz,
            max_det=args.max_det,
            device=args.device,
            verbose=False,
        )[0]

        detections: list[dict[str, Any]] = []
        if result.boxes is not None:
            boxes = result.boxes.xyxy.detach().cpu().tolist()
            confidences = result.boxes.conf.detach().cpu().tolist()
            class_ids = result.boxes.cls.detach().cpu().int().tolist()
            for bbox, confidence, class_id in zip(boxes, confidences, class_ids, strict=True):
                detections.append(
                    {
                        "class": classes[class_id],
                        "confidence": round(float(confidence), 6),
                        "bbox": [round(float(value), 3) for value in bbox],
                    }
                )

        height, width = image.shape[:2]
        payload["items"].append(
            {
                "path": str(image_path),
                "name": image_path.name,
                "width": int(width),
                "height": int(height),
                "count": len(detections),
                "detections": detections,
            }
        )
        if args.preview_dir:
            write_preview(
                image,
                detections,
                classes,
                Path(args.preview_dir).resolve() / f"{image_path.stem}-detected.jpg",
            )
        print(f"{image_path.name}: {len(detections)} detections")

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
