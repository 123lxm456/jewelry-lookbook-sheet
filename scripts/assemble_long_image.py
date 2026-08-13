#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat

WIDTH = 1256
TITLE_HEIGHT = 0
SERIF = "/usr/share/fonts/opentype/arphic/uming.ttc"
SANS = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


@dataclass(frozen=True)
class Placement:
    x: int
    y: int
    width: int
    height: int
    variant: int = 0

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)


LAYOUT_NAMES = (
    "left_sidebar",
    "right_sidebar",
    "vertical_left",
    "vertical_right",
    "top_heading",
    "bottom_description",
    "wrap_around",
    "minimal_whitespace",
    "magazine_editorial",
)
VERTICAL_LAYOUTS = {"vertical_left", "vertical_right"}


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def fit_font(path: str, text: str, max_size: int, min_size: int, width: int) -> ImageFont.FreeTypeFont:
    for size in range(max_size, min_size - 1, -1):
        candidate = font(path, size)
        if max(candidate.getlength(line) for line in text.splitlines()) <= width:
            return candidate
    return font(path, min_size)


def cover(path: Path, width: int, height: int, focus_y: float = 0.5) -> Image.Image:
    with Image.open(path) as source:
        image = source.convert("RGB")
    scale = max(width / image.width, height / image.height)
    resized = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - width) // 2)
    top = max(0, round((resized.height - height) * focus_y))
    return resized.crop((left, top, left + width, top + height))


def build_risk_map(image: Image.Image) -> Image.Image:
    """Estimate regions that must remain clear of typography.

    The map intentionally combines detail, highlights, saturation, and skin
    tones. Skin-tone protection is a lightweight model-independent guard for
    faces, hands, and exposed areas when a separate segmentation service is
    unavailable; the generous blur acts as a buffer around those regions.
    """
    sample_width = 180
    sample_height = max(120, round(image.height * sample_width / image.width))
    sample = image.resize((sample_width, sample_height), Image.Resampling.LANCZOS)
    gray = sample.convert("L")
    # Expand thin high-contrast structures before scoring. Chains, prongs and
    # stone rims occupy few pixels but still need a generous typography buffer.
    edges = gray.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(1.1))
    local_contrast = ImageChops.difference(gray, gray.filter(ImageFilter.GaussianBlur(6)))

    saturation = sample.convert("HSV").getchannel("S")
    bright = gray.point(lambda value: max(0, (value - 205) * 4))
    risk = ImageChops.add(edges.point(lambda value: min(255, value * 2)), local_contrast.point(lambda value: min(255, value * 3)))
    risk = ImageChops.add(risk, bright, scale=1.35)
    risk = ImageChops.add(risk, saturation.point(lambda value: value // 3), scale=1.15)
    red, green, blue = sample.split()
    luminance, cb_channel, cr_channel = sample.convert("YCbCr").split()
    # Protect faces and hands without classifying warm ivory, wood, leather,
    # or champagne backdrops as skin. YCbCr chroma bounds are more stable under
    # editorial lighting than a broad RGB warmth test.
    skin = Image.new("L", sample.size)
    skin_pixels = []
    channels = zip(
        red.get_flattened_data(), green.get_flattened_data(), blue.get_flattened_data(),
        luminance.get_flattened_data(), cb_channel.get_flattened_data(), cr_channel.get_flattened_data(),
    )
    for r, g, b, y, cb, cr in channels:
        chroma_skin = 35 < y < 246 and 78 <= cb <= 126 and 134 <= cr <= 172
        rgb_skin = r > g > b and 7 <= r - g <= 62 and 4 <= g - b <= 58
        skin_pixels.append(190 if chroma_skin and rgb_skin else 0)
    skin.putdata(skin_pixels)
    skin = skin.filter(ImageFilter.MaxFilter(13)).filter(ImageFilter.GaussianBlur(4.0))
    risk = ImageChops.lighter(risk, skin)
    return risk.filter(ImageFilter.GaussianBlur(2.2))


def scaled_box(box: tuple[int, int, int, int], source: Image.Image, target: Image.Image, padding: int = 64) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(source.width, right + padding)
    bottom = min(source.height, bottom + padding)
    return (
        round(left * target.width / source.width),
        round(top * target.height / source.height),
        round(right * target.width / source.width),
        round(bottom * target.height / source.height),
    )


def region_risk(image: Image.Image, risk_map: Image.Image, box: tuple[int, int, int, int]) -> float:
    mapped = scaled_box(box, image, risk_map)
    crop = risk_map.crop(mapped)
    stats = ImageStat.Stat(crop)
    mean = stats.mean[0]
    # A few concentrated highlights/edges can be the product even when the rest is quiet.
    histogram = crop.histogram()
    high_pixels = sum(histogram[145:]) / max(1, crop.width * crop.height)
    return mean + high_pixels * 135


def candidates(layout: str, height: int) -> list[Placement]:
    if layout == "left_sidebar":
        return [Placement(54, y, 470, 286, i) for i, y in enumerate((80, height // 3, height - 372))]
    if layout == "right_sidebar":
        return [Placement(735, y, 465, 286, i) for i, y in enumerate((82, height // 3, height - 372))]
    if layout == "vertical_left":
        return [Placement(42, y, 330, 560, i) for i, y in enumerate((64, max(64, (height - 560) // 2), height - 624))]
    if layout == "vertical_right":
        return [Placement(884, y, 330, 560, i) for i, y in enumerate((64, max(64, (height - 560) // 2), height - 624))]
    if layout == "top_heading":
        return [Placement(x, 58, width, 250, i) for i, (x, width) in enumerate(((74, 650), (520, 660), (245, 760)))]
    if layout == "bottom_description":
        positions = ((56, 500), (700, 500), (775, 425), (72, 650), (530, 650), (250, 756))
        return [Placement(x, height - 320, width, 254, i) for i, (x, width) in enumerate(positions)]
    if layout == "wrap_around":
        return [Placement(58, 68, 1140, height - 136, i) for i in range(2)]
    if layout == "minimal_whitespace":
        positions = ((70, 120), (720, 120), (70, height - 350), (720, height - 350))
        return [Placement(x, y, 455, 265, i) for i, (x, y) in enumerate(positions)]
    if layout == "magazine_editorial":
        positions = ((66, 90, 510), (680, 90, 510), (66, height - 390, 510), (680, height - 390, 510))
        return [Placement(x, y, width, 315, i) for i, (x, y, width) in enumerate(positions)]
    raise ValueError(f"Unknown layout: {layout}")


def placement_risk(image: Image.Image, risk_map: Image.Image, layout: str, placement: Placement) -> float:
    if layout != "wrap_around":
        return region_risk(image, risk_map, placement.box)
    title_box, body_box = wrap_boxes(image, placement)
    return (region_risk(image, risk_map, title_box) + region_risk(image, risk_map, body_box)) / 2


def wrap_boxes(image: Image.Image, placement: Placement) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    top_y = 72 if placement.variant == 0 else image.height - 352
    bottom_y = image.height - 250 if placement.variant == 0 else 82
    return (58, top_y, 548, top_y + 230), (700, bottom_y, 1198, bottom_y + 188)


def semantic_safe_zone(
    index: int,
    image: Image.Image,
    configured_zones: list[list[float]] | None = None,
) -> tuple[int, int, int, int]:
    """Safe typography zones already requested when each source panel is generated."""
    zones = configured_zones or (
        (0.00, 0.06, 0.48, 0.94),  # worn product: calm left side
        (0.04, 0.62, 0.96, 0.96),  # macro: quiet lower area
        (0.55, 0.04, 0.96, 0.46),  # still life: clean upper-right
        (0.04, 0.04, 0.48, 0.46),  # gift: clean upper-left
        (0.52, 0.68, 0.96, 0.94),  # mirror: jewelry-free clothing/background
    )
    left, top, right, bottom = zones[index]
    return round(left * image.width), round(top * image.height), round(right * image.width), round(bottom * image.height)


def outside_fraction(box: tuple[int, int, int, int], zone: tuple[int, int, int, int]) -> float:
    left = max(box[0], zone[0])
    top = max(box[1], zone[1])
    right = min(box[2], zone[2])
    bottom = min(box[3], zone[3])
    overlap = max(0, right - left) * max(0, bottom - top)
    area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
    return 1 - overlap / area


def safe_zone_penalty(
    image: Image.Image,
    index: int,
    layout: str,
    placement: Placement,
    configured_zones: list[list[float]] | None = None,
) -> float:
    zone = semantic_safe_zone(index, image, configured_zones)
    boxes = wrap_boxes(image, placement) if layout == "wrap_around" else (placement.box,)
    # Generated prompts provide a semantic quiet area, so leaving it should be
    # substantially more expensive than choosing a different layout variant.
    return sum(outside_fraction(box, zone) for box in boxes) / len(boxes) * 260


def choose_layouts(
    images: list[Image.Image],
    rng: random.Random,
    configured_zones: list[list[float]] | None = None,
    prepared_risk_maps: list[Image.Image] | None = None,
) -> list[tuple[str, Placement, float, float]]:
    # Detection/risk analysis is independent per panel. Threads work well here
    # because Pillow's resize/filter primitives release the GIL.
    if prepared_risk_maps is None:
        with ThreadPoolExecutor(max_workers=min(len(images), 5)) as executor:
            risk_maps = list(executor.map(build_risk_map, images))
    else:
        if len(prepared_risk_maps) != len(images):
            raise ValueError("prepared risk-map count must match image count")
        risk_maps = prepared_risk_maps
    best: dict[tuple[int, str], tuple[Placement, float, float]] = {}
    for image_index, (image, risk_map) in enumerate(zip(images, risk_maps)):
        for layout in LAYOUT_NAMES:
            options = []
            for item in candidates(layout, image.height):
                content_risk = placement_risk(image, risk_map, layout, item)
                score = content_risk + safe_zone_penalty(image, image_index, layout, item, configured_zones)
                options.append((score, content_risk, item))
            score, content_risk, placement = min(options, key=lambda option: option[0])
            best[(image_index, layout)] = (placement, score, content_risk)

    vertical_count = 1 if rng.random() < 0.65 else 2
    assignments = []
    for layouts in itertools.permutations(LAYOUT_NAMES, len(images)):
        if sum(layout in VERTICAL_LAYOUTS for layout in layouts) != vertical_count:
            continue
        total = sum(best[(index, layout)][1] for index, layout in enumerate(layouts))
        assignments.append((total, layouts))
    assignments.sort(key=lambda item: item[0])
    safest = assignments[0][0]
    safe_pool = [item for item in assignments if item[0] <= safest * 1.08][:24]
    if len(safe_pool) < 4:
        safe_pool = assignments[:4]
    _, selected = rng.choice(safe_pool)
    return [(layout, *best[(index, layout)]) for index, layout in enumerate(selected)]


def palette(image: Image.Image, box: tuple[int, int, int, int], force: str | None = None) -> tuple[tuple[int, ...], tuple[int, ...]]:
    luminance = ImageStat.Stat(image.crop(box).convert("L")).mean[0]
    if force == "dark" or (force is None and luminance > 142):
        return (38, 33, 30, 255), (255, 255, 255, 172)
    return (255, 250, 242, 255), (22, 18, 16, 160)


def copy_fonts(section: dict, width: int, title_max: int = 50) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    return (
        fit_font(SANS, section["eyebrow"], 22, 16, width),
        fit_font(SERIF, section["title"], title_max, 28, width),
        fit_font(REGULAR, section["body"], 25, 18, width),
    )


def draw_standard_copy(draw: ImageDraw.ImageDraw, section: dict, placement: Placement, color: tuple[int, ...], title_max: int = 50) -> None:
    text_width = placement.width - 52
    eyebrow_font, title_font, body_font = copy_fonts(section, text_width, title_max)
    x, y = placement.x + 26, placement.y + 20
    draw.text((x, y), section["eyebrow"], font=eyebrow_font, fill=color)
    draw.text((x, y + 48), section["title"], font=title_font, fill=color)
    draw.multiline_text((x, y + 126), section["body"], font=body_font, fill=color, spacing=12)


def vertical_font(path: str, text: str, max_size: int, min_size: int, height: int, gap: int = 4) -> ImageFont.FreeTypeFont:
    longest = max((len(line.replace(" ", "")) for line in text.splitlines()), default=1)
    for size in range(max_size, min_size - 1, -1):
        if longest * (size + gap) <= height:
            return font(path, size)
    return font(path, min_size)


def draw_vertical_chars(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    text_font: ImageFont.FreeTypeFont,
    fill: tuple[int, ...],
    gap: int = 4,
) -> None:
    cursor = y
    for character in text.replace(" ", ""):
        draw.text((x, cursor), character, font=text_font, fill=fill, anchor="ma")
        cursor += text_font.size + gap


def draw_vertical_copy(
    draw: ImageDraw.ImageDraw,
    section: dict,
    placement: Placement,
    color: tuple[int, ...],
    align: str,
) -> None:
    title_font = vertical_font(SERIF, section["title"], 46, 30, placement.height - 82, 7)
    body_font = vertical_font(REGULAR, section["body"], 24, 17, placement.height - 94, 5)
    eyebrow_font = vertical_font(SANS, section["eyebrow"], 16, 12, placement.height - 68, 2)
    if align == "left":
        eyebrow_x = placement.x + 28
        body_start = placement.x + 112
        title_x = placement.x + 258
    else:
        title_x = placement.x + 72
        body_start = placement.x + 192
        eyebrow_x = placement.x + 302
    draw_vertical_chars(draw, section["eyebrow"], eyebrow_x, placement.y + 30, eyebrow_font, color, 2)
    draw_vertical_chars(draw, section["title"], title_x, placement.y + 28, title_font, color, 7)
    body_lines = section["body"].splitlines()
    for index, line in enumerate(body_lines[:3]):
        column_x = body_start + index * 38 if align == "left" else body_start - index * 38
        draw_vertical_chars(draw, line, column_x, placement.y + 46, body_font, color, 5)


def choose_decoration(layout: str, content_risk: float, rng: random.Random) -> str:
    if layout in {"minimal_whitespace", "wrap_around"}:
        return "none"
    return rng.choice(["none", "line"])


def render_layout(
    image: Image.Image,
    section: dict,
    layout: str,
    placement: Placement,
    decoration: str,
) -> Image.Image:
    panel = image.convert("RGBA")
    draw = ImageDraw.Draw(panel, "RGBA")
    color, contrast = palette(image, placement.box)

    if layout == "left_sidebar":
        if decoration != "none":
            draw.line((placement.x + placement.width - 2, placement.y, placement.x + placement.width - 2, placement.y + placement.height), fill=contrast, width=2)
        draw_standard_copy(draw, section, placement, color)
    elif layout == "right_sidebar":
        if decoration != "none":
            draw.line((placement.x, placement.y, placement.x, placement.y + placement.height), fill=contrast, width=2)
        draw_standard_copy(draw, section, placement, color)
    elif layout in VERTICAL_LAYOUTS:
        align = "left" if layout == "vertical_left" else "right"
        if decoration != "none":
            line_x = placement.x + placement.width if align == "left" else placement.x
            draw.line((line_x, placement.y, line_x, placement.y + placement.height), fill=contrast, width=2)
        draw_vertical_copy(draw, section, placement, color, align)
    elif layout == "top_heading":
        eyebrow_font, title_font, body_font = copy_fonts(section, placement.width - 40, 60)
        center_x = placement.x + placement.width // 2
        draw.text((center_x, placement.y + 20), section["eyebrow"], anchor="ma", font=eyebrow_font, fill=color)
        draw.text((center_x, placement.y + 72), section["title"], anchor="ma", font=title_font, fill=color)
        draw.multiline_text((placement.x + placement.width - 12, placement.y + 158), section["body"], anchor="ra", align="right", font=body_font, fill=color, spacing=10)
        if decoration != "none":
            draw.line((placement.x + placement.width // 3, placement.y + 146, placement.x + placement.width * 2 // 3, placement.y + 146), fill=color, width=1)
    elif layout == "bottom_description":
        eyebrow_font, title_font, body_font = copy_fonts(section, placement.width - 44, 48)
        draw.text((placement.x + placement.width - 22, placement.y + 18), section["eyebrow"], anchor="ra", font=eyebrow_font, fill=color)
        draw.text((placement.x + placement.width - 22, placement.y + 66), section["title"], anchor="ra", font=title_font, fill=color)
        draw.multiline_text((placement.x + 22, placement.y + 142), section["body"], font=body_font, fill=color, spacing=10)
        if decoration != "none":
            draw.line((placement.x + 20, placement.y + 128, placement.x + placement.width - 20, placement.y + 128), fill=color, width=1)
    elif layout == "minimal_whitespace":
        # Pure typography with a restrained shadow, deliberately without a box.
        shadow = (255, 255, 255, 125) if color[0] < 100 else (0, 0, 0, 110)
        shifted = Placement(placement.x + 2, placement.y + 2, placement.width, placement.height)
        draw_standard_copy(draw, section, shifted, shadow)
        draw_standard_copy(draw, section, placement, color)
    elif layout == "magazine_editorial":
        draw.line((placement.x, placement.y, placement.x + placement.width, placement.y), fill=color, width=3)
        draw.line((placement.x, placement.y + 54, placement.x + 145, placement.y + 54), fill=color, width=1)
        eyebrow_font, title_font, body_font = copy_fonts(section, placement.width - 28, 64)
        draw.text((placement.x, placement.y + 18), section["eyebrow"], font=eyebrow_font, fill=color)
        draw.text((placement.x, placement.y + 82), section["title"], font=title_font, fill=color)
        draw.multiline_text((placement.x + placement.width, placement.y + 184), section["body"], anchor="ra", align="right", font=body_font, fill=color, spacing=10)
    elif layout == "wrap_around":
        title_box, body_box = wrap_boxes(image, placement)
        title_color, _ = palette(image, title_box)
        body_color, _ = palette(image, body_box)
        eyebrow_font, title_font, _ = copy_fonts(section, title_box[2] - title_box[0] - 12, 52)
        body_font = fit_font(REGULAR, section["body"], 27, 19, body_box[2] - body_box[0] - 12)
        draw.text((title_box[0], title_box[1]), section["eyebrow"], font=eyebrow_font, fill=title_color)
        draw.text((title_box[0], title_box[1] + 48), section["title"], font=title_font, fill=title_color)
        draw.line((body_box[0], body_box[1], body_box[0] + 110, body_box[1]), fill=body_color, width=2)
        draw.multiline_text((body_box[0], body_box[1] + 24), section["body"], font=body_font, fill=body_color, spacing=13)
    else:
        raise ValueError(f"Unknown layout: {layout}")
    return panel.convert("RGB")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render strategy-driven product panels into one adaptively typeset image.")
    parser.add_argument("panel_dir", type=Path)
    parser.add_argument("page", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--display-plan", type=Path)
    parser.add_argument("--prepared-dir", type=Path)
    parser.add_argument("--seed", type=int, help="Optional deterministic seed for tests and reproducible exports.")
    args = parser.parse_args()

    data = json.loads(args.page.read_text(encoding="utf-8"))
    plan = json.loads(args.display_plan.read_text(encoding="utf-8")) if args.display_plan else None
    panels = plan["panels"] if plan else [
        {"number": index + 1, "id": f"panel-{index + 1}", "label": f"Panel {index + 1}",
         "safe_zone": zone, "crop_height": height, "focus_y": focus}
        for index, (zone, height, focus) in enumerate(zip(
            ([0.00, 0.06, 0.48, 0.94], [0.04, 0.62, 0.96, 0.96], [0.55, 0.04, 0.96, 0.46],
             [0.04, 0.04, 0.48, 0.46], [0.52, 0.68, 0.96, 0.94]),
            (1460, 1520, 1640, 1500, 1560), (0.42, 0.38, 0.72, 0.50, 0.42),
        ))
    ]
    if not isinstance(data.get("sections"), list) or len(data["sections"]) != len(panels):
        raise SystemExit("page section count must match the display plan")
    paths = [args.panel_dir / f"panel-{int(panel['number']):02d}.png" for panel in panels]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise SystemExit("Missing generated images: " + ", ".join(missing))

    panel_specs = [
        (path, int(panel["crop_height"]), float(panel["focus_y"]))
        for path, panel in zip(paths, panels)
    ]
    prepared_images = [args.prepared_dir / f"panel-{int(panel['number']):02d}.ppm" for panel in panels] if args.prepared_dir else []
    prepared_risks = [args.prepared_dir / f"risk-{int(panel['number']):02d}.png" for panel in panels] if args.prepared_dir else []
    if prepared_images and all(path.is_file() for path in (*prepared_images, *prepared_risks)):
        images = []
        risk_maps = []
        for image_path, risk_path in zip(prepared_images, prepared_risks):
            with Image.open(image_path) as opened:
                images.append(opened.convert("RGB"))
            with Image.open(risk_path) as opened:
                risk_maps.append(opened.convert("L"))
    else:
        images = [cover(path, WIDTH, height, focus_y) for path, height, focus_y in panel_specs]
        risk_maps = None
    rng = random.Random(args.seed) if args.seed is not None else random.SystemRandom()
    choices = choose_layouts(images, rng, [panel["safe_zone"] for panel in panels], risk_maps)

    rendered_panels: list[tuple[Image.Image, str]] = []
    for image, section, choice in zip(images, data["sections"], choices):
        layout, placement, _, content_risk = choice
        # Typography is always drawn directly over the photograph. Never add a
        # translucent rectangle/card behind copy, even in a busy safe zone.
        decoration = choose_decoration(layout, content_risk, rng)
        rendered_panels.append((render_layout(image, section, layout, placement, decoration), decoration))

    height = TITLE_HEIGHT + sum(panel.height for panel, _ in rendered_panels)
    canvas = Image.new("RGB", (WIDTH, height), "#f2f2f2")
    draw = ImageDraw.Draw(canvas, "RGBA")

    layout_plan = []
    y = TITLE_HEIGHT
    for index, (image, section, choice, panel_spec, rendered) in enumerate(zip(images, data["sections"], choices, panels, rendered_panels), start=1):
        layout, placement, selection_score, content_risk = choice
        rendered_image, decoration = rendered
        canvas.paste(rendered_image, (0, y))
        draw.rectangle((0, y, WIDTH, y + 8), fill="#fffaf5")
        layout_plan.append({
            "panel": index,
            "panel_id": panel_spec["id"],
            "label": panel_spec["label"],
            "layout": layout,
            "decoration": decoration,
            "safety_mode": "in_image_semantic_risk_optimized",
            "placement": {"x": placement.x, "y": placement.y, "width": placement.width, "height": placement.height},
            "content_risk": round(content_risk, 2),
            "selection_score": round(selection_score, 2),
        })
        y += rendered_image.height

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = args.output.with_suffix(args.output.suffix + ".tmp")
    canvas.save(temporary_output, format="PNG", quality=95, subsampling=0)
    temporary_output.replace(args.output)
    plan_path = args.output.with_name(f"{args.output.stem}-layout.json")
    temporary_plan = plan_path.with_suffix(plan_path.suffix + ".tmp")
    temporary_plan.write_text(json.dumps({"panels": layout_plan}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_plan.replace(plan_path)
    print(f"Saved product details: {args.output} ({canvas.width}x{canvas.height})")
    print(f"Saved adaptive layout plan: {plan_path}")


if __name__ == "__main__":
    main()
