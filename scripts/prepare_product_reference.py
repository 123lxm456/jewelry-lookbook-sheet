#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageOps


def expanded_box(box: list[float] | tuple[float, ...], width: int, height: int, padding: float = 0.06) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    span_x, span_y = right - left, bottom - top
    left = max(0.0, left - max(padding, span_x * padding))
    top = max(0.0, top - max(padding, span_y * padding))
    right = min(1.0, right + max(padding, span_x * padding))
    bottom = min(1.0, bottom + max(padding, span_y * padding))
    return round(left * width), round(top * height), round(right * width), round(bottom * height)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a primary-product authority crop from analyzed evidence.")
    parser.add_argument("image", type=Path)
    parser.add_argument("spec", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    data = json.loads(args.spec.read_text(encoding="utf-8"))
    observation = data.get("reference_observation") or {}
    box = observation.get("primary_product_bbox", [0, 0, 1, 1])
    with Image.open(args.image) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        crop = image.crop(expanded_box(box, image.width, image.height))
        # Keep fine jewelry, stitching, printed details, and small connectors at
        # useful resolution without upscaling already-large sources excessively.
        crop.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    crop.save(temporary, format="PNG", optimize=True)
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
