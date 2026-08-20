#!/usr/bin/env python3
import argparse
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat


def deidentify_style_product(image: Image.Image) -> Image.Image:
    """Retain pose/layout/color grammar while making jewelry detail non-copyable."""
    small_width = max(48, image.width // 16)
    small_height = max(72, image.height // 16)
    abstract = image.resize((small_width, small_height), Image.Resampling.BOX)
    abstract = abstract.resize(image.size, Image.Resampling.BILINEAR)
    return abstract.filter(ImageFilter.GaussianBlur(max(2, image.width / 320)))


def near_white_runs(image: Image.Image) -> list[tuple[int, int]]:
    """Find horizontal white separator bands in a tall composite screenshot."""
    sample_width = 256
    sample_height = image.height
    sample = image.convert("RGB").resize((sample_width, sample_height), Image.Resampling.BOX)
    channels = [channel.point(lambda value: 255 if value >= 242 else 0) for channel in sample.split()]
    white_mask = ImageChops.darker(ImageChops.darker(channels[0], channels[1]), channels[2])
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for y in range(sample_height):
        white_fraction = ImageStat.Stat(white_mask.crop((0, y, sample_width, y + 1))).mean[0] / 255
        is_white = white_fraction >= 0.9
        if is_white and start is None:
            start = y
        elif not is_white and start is not None:
            if y - start >= 2:
                runs.append((round(start * image.height / sample_height), round(y * image.height / sample_height) - 1))
            start = None
    if start is not None and sample_height - start >= 2:
        runs.append((round(start * image.height / sample_height), image.height - 1))

    # Rounded title cards can split one header band into several white runs.
    merged: list[list[int]] = []
    merge_gap = max(8, round(image.height * 0.015))
    for left, right in runs:
        if merged and left - merged[-1][1] <= merge_gap:
            merged[-1][1] = right
        else:
            merged.append([left, right])
    return [(left, right) for left, right in merged]


def crop_bounds(image: Image.Image) -> tuple[int, int, str]:
    bands = near_white_runs(image)
    # image1.jpg has a white title band followed by five panels separated by
    # white rules. Keep all five panels and remove only the title/footer bands.
    has_five_panel_frame = (
        len(bands) >= 6
        and bands[0][0] <= round(image.height * 0.08)
        and bands[-1][0] >= round(image.height * 0.95)
    )
    if has_five_panel_frame:
        top = bands[0][1] + 1
        bottom = bands[-1][0]
        if bottom - top >= round(image.height * 0.5):
            return top, bottom, "detected separator bands"
    # Custom style references may not be composite screenshots. Preserve the
    # historical behavior as a compatible fallback for those inputs.
    return round(image.height * 0.275), round(image.height * 0.935), "percentage fallback"


def panel_bounds(image: Image.Image) -> list[tuple[int, int]]:
    """Return the five content regions in a five-panel style long image."""
    bands = near_white_runs(image)
    if (
        len(bands) >= 6
        and bands[0][0] <= round(image.height * 0.08)
        and bands[-1][0] >= round(image.height * 0.95)
    ):
        regions = [
            (bands[index][1] + 1, bands[index + 1][0])
            for index in range(5)
        ]
        if all(bottom - top >= round(image.height * 0.12) for top, bottom in regions):
            return regions
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Crop the ad-only section from the style screenshot.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--panels-dir", type=Path)
    parser.add_argument("--deidentified-panels-dir", type=Path)
    args = parser.parse_args()

    with Image.open(args.source) as image:
        width, height = image.size
        top, bottom, method = crop_bounds(image)
        crop = image.crop((0, top, width, bottom)).convert("RGB")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        crop.save(args.output, quality=94, subsampling=0)
        print(f"Saved style crop: {args.output} ({crop.width}x{crop.height}; {method}; y={top}:{bottom})")
        if args.panels_dir:
            regions = panel_bounds(image)
            if regions:
                args.panels_dir.mkdir(parents=True, exist_ok=True)
                for index, (panel_top, panel_bottom) in enumerate(regions, start=1):
                    panel = image.crop((0, panel_top, width, panel_bottom)).convert("RGB")
                    panel_path = args.panels_dir / f"panel-{index:02d}.jpg"
                    panel.save(panel_path, quality=94, subsampling=0)
                    if args.deidentified_panels_dir:
                        args.deidentified_panels_dir.mkdir(parents=True, exist_ok=True)
                        safe_path = args.deidentified_panels_dir / f"panel-{index:02d}.jpg"
                        deidentify_style_product(panel).save(safe_path, quality=90, subsampling=0)
                print(f"Saved {len(regions)} panel-specific style references: {args.panels_dir}")
                if args.deidentified_panels_dir:
                    print(f"Saved product-deidentified style references: {args.deidentified_panels_dir}")


if __name__ == "__main__":
    main()
