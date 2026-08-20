#!/usr/bin/env python3
"""Build a reflection deterministically instead of asking a model to redraw it."""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageOps


def compose(source: Image.Image) -> Image.Image:
    image = source.convert("RGB")
    width, height = image.size
    margin = round(width * 0.035)
    top, bottom = 0, height
    # The mirror is a narrower auxiliary field. Use an equal-width crop from
    # the real-model zone so reflection scale and anatomy stay exact, while
    # the uncropped real model retains more shoulder/body area and dominates.
    mirror_box = (margin, top, round(width * 0.37), bottom)
    real_left = round(width * 0.55)
    real_box = (real_left, top, real_left + (mirror_box[2] - mirror_box[0]), bottom)

    # The generation contract places the sole real model in real_box. Copying
    # exactly those pixels guarantees identical hands, pose, product, clothing,
    # and anatomy. Horizontal reversal supplies the optical parity operation.
    real_plate = image.crop(real_box)
    reflected = ImageOps.mirror(real_plate)
    reflected = ImageEnhance.Brightness(reflected).enhance(0.965)
    reflected = ImageEnhance.Color(reflected).enhance(0.94)
    result = image.copy()
    result.paste(reflected, mirror_box)

    # A narrow vertical divider reads as an actual dressing-room mirror instead
    # of an inset picture frame and keeps the real model visually dominant.
    draw = ImageDraw.Draw(result, "RGBA")
    frame = max(7, round(width * 0.012))
    divider_x = round(width * 0.395)
    draw.rounded_rectangle(
        (divider_x - frame, 0, divider_x + frame, height),
        radius=max(2, frame // 2), fill=(170, 138, 95, 230),
    )
    draw.line((divider_x + frame, 0, divider_x + frame, height), fill=(255, 245, 224, 150), width=max(2, frame // 3))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a geometry-locked real/reflected portrait.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with Image.open(args.source) as opened:
        result = compose(opened)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    result.save(temporary, format="PNG", optimize=True)
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
