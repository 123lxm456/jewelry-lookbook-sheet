#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import BadRequestError
from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jewelry_workflow.qwen_vision import QwenVisionClient, extract_json, image_data_url  # noqa: E402
from scripts.analyze_product import load_env  # noqa: E402


class PanelResult(BaseModel):
    panel_number: int = Field(ge=1, le=5)
    product_fidelity: float = Field(ge=0, le=1)
    composition_match: float = Field(ge=0, le=1)
    style_match: float = Field(ge=0, le=1)
    product_identity_exact: bool
    product_bbox: tuple[float, float, float, float]
    physically_valid: bool
    mirror_consistent: bool
    issues: list[str]

    @field_validator("product_bbox")
    @classmethod
    def product_bbox_must_be_normalized(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        left, top, right, bottom = value
        if not all(0 <= coordinate <= 1 for coordinate in value) or left >= right or top >= bottom:
            raise ValueError("product_bbox must be normalized [left, top, right, bottom]")
        return value


class DuplicatePair(BaseModel):
    panels: tuple[int, int]
    similarity: float = Field(ge=0, le=1)
    reasons: list[str]


class SeriesAssessment(BaseModel):
    panels: list[PanelResult]
    duplicate_pairs: list[DuplicatePair] = Field(default_factory=list)


def panel_fails_gate(
    item: PanelResult, min_fidelity: float, min_composition: float, min_style: float = 0.0
) -> bool:
    """Use structured verdicts, not free-text notes, as blocking signals."""
    return (
        item.product_fidelity < min_fidelity
        or item.composition_match < min_composition
        or item.style_match < min_style
        or not item.product_identity_exact
        or not item.physically_valid
        or not item.mirror_consistent
    )


SYSTEM_PROMPT = """You are a strict product-identity and commercial-series inspector. Return JSON only.
Image 1 is the authoritative primary-product crop. Images 2-6 are generated panels 1-5 in order.
The pixels in Image 1 override every supplied product name or textual claim. First inventory Image 1 directly without using
familiar motif labels, then compare each output. If JSON evidence conflicts with Image 1, ignore the JSON conflict.
Use supplied evidence only to exclude coins, rulers, packaging, props, nearby accessories, and other products.
For every panel compare type, exact count, silhouette, proportions, material/color, motif or stone count/shape/arrangement,
attachment topology, connectors, symmetry, and category-specific construction. Penalize every invented, omitted, duplicated,
recolored, simplified, or redesigned element. Do not demand hidden structure that the source does not show.
Set product_identity_exact=false for any changed outer contour, nested motif layer, connection point, stone/pearl group,
component count, material color, or relative proportion. Such a change must score product_fidelity <= 0.79; a merely similar
generic substitute can never score above 0.70. product_bbox is the tight normalized [left,top,right,bottom] rectangle around
all visible instances of the product in that generated panel.
Check the assigned shot contract and whether the product remains the visual subject. Set style_match=1 because style is
enforced before generation and intentionally omitted from this fast identity gate. For a geometry-locked mirror panel,
require real and reflected halves to have exactly corresponding pose, head, arms, hands, clothing, product placement,
scale, and horizontal parity; set mirror_consistent=false for any mismatch. For panels 1 and 5 require a complete natural
face, head, shoulders, and plausible anatomy.
Duplicate detection is performed locally; return duplicate_pairs as an empty list. Ambiguous product identity is a failure.
Put only publication-blocking defects in issues; express minor acceptable observations through the numeric scores
and leave issues empty. An issue is publication-blocking when product_identity_exact is false, product_fidelity is below 0.94,
composition_match is below 0.80, style_match is below 0.78, physically_valid is false, or mirror_consistent is false; never put confirmations, strengths, or stylistic notes in
issues. Return exactly five panel results with unique panel_number values."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def obvious_duplicate_pairs(paths: list[Path]) -> list[DuplicatePair]:
    """Detect only near-identical full panels; prompts own semantic diversity."""
    prepared: list[tuple[list[bool], Image.Image]] = []
    hashes: list[str] = []
    for path in paths:
        hashes.append(file_sha256(path))
        with Image.open(path) as opened:
            gray = ImageOps.exif_transpose(opened).convert("L").resize((33, 32), Image.Resampling.LANCZOS)
        pixels = list(gray.tobytes())
        differences = [
            pixels[row * 33 + column] > pixels[row * 33 + column + 1]
            for row in range(32) for column in range(32)
        ]
        prepared.append((differences, gray.resize((64, 64), Image.Resampling.BILINEAR)))
    threshold = float(os.environ.get("SERIES_LOCAL_DUPLICATE_THRESHOLD", "0.94"))
    if not 0.85 <= threshold <= 1:
        raise ValueError("SERIES_LOCAL_DUPLICATE_THRESHOLD must be between 0.85 and 1")
    duplicates: list[DuplicatePair] = []
    for left in range(len(paths)):
        for right in range(left + 1, len(paths)):
            if hashes[left] == hashes[right]:
                similarity = 1.0
            else:
                bits_a, image_a = prepared[left]
                bits_b, image_b = prepared[right]
                hash_similarity = sum(a == b for a, b in zip(bits_a, bits_b)) / len(bits_a)
                pixel_difference = ImageStat.Stat(ImageChops.difference(image_a, image_b)).mean[0] / 255
                similarity = 0.65 * hash_similarity + 0.35 * (1 - pixel_difference)
            if similarity >= threshold:
                duplicates.append(DuplicatePair(
                    panels=(left + 1, right + 1), similarity=round(similarity, 4),
                    reasons=["local perceptual check found near-identical full-panel pixels"],
                ))
    return duplicates


def write_local_product_mask(image_path: Path, bbox: tuple[float, float, float, float], output: Path) -> None:
    """Limit identity repair to the generated product and a small edge-blending margin."""
    with Image.open(image_path) as opened:
        width, height = opened.size
    left, top, right, bottom = bbox
    span_x, span_y = right - left, bottom - top
    left = max(0.0, left - max(0.015, span_x * 0.12))
    top = max(0.0, top - max(0.015, span_y * 0.12))
    right = min(1.0, right + max(0.015, span_x * 0.12))
    bottom = min(1.0, bottom + max(0.015, span_y * 0.12))
    mask = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(mask)
    draw.rectangle((round(left * width), round(top * height), round(right * width), round(bottom * height)), fill=(0, 0, 0, 0))
    output.parent.mkdir(parents=True, exist_ok=True)
    mask.save(output, format="PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Quality-gate a five-panel product series.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--display-plan", type=Path, required=True)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    load_env(args.env_file)
    api_key = os.environ.get("QWEN_API_KEY", "").strip()
    base_url = os.environ.get("QWEN_BASE_URL", "").strip()

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    plan = json.loads(args.display_plan.read_text(encoding="utf-8"))
    evidence = {
        "identity": spec["identity"], "physical": spec["physical"], "details": spec["details"],
        "integrity": spec["integrity"], "reference_observation": spec.get("reference_observation", {}),
        "shot_contracts": [
            {"number": p["number"], "id": p["id"], "shot_spec": p["shot_spec"], "style_reference_en": p.get("style_reference_en")}
            for p in plan["panels"]
        ],
    }
    encode_started = time.monotonic()
    panel_paths = [args.output_dir / f"panel-{number:02d}.png" for number in range(1, 6)]
    duplicate_started = time.monotonic()
    duplicate_pairs = obvious_duplicate_pairs(panel_paths)
    duplicate_seconds = time.monotonic() - duplicate_started
    image_jobs = [(args.product, 120000, 1280)] + [
        (path, 60000, 896) for path in panel_paths
    ]
    with ThreadPoolExecutor(max_workers=6) as executor:
        encoded_images = list(executor.map(lambda item: image_data_url(*item), image_jobs))
    content: list[dict] = [{"type": "text", "text": json.dumps(evidence, ensure_ascii=False)}]
    content.extend(
        {"type": "image_url", "image_url": {"url": encoded, "detail": "high"}}
        for encoded in encoded_images
    )
    print(
        f"Prepared six quality images in {time.monotonic() - encode_started:.1f}s "
        f"({sum(len(item) for item in encoded_images) // 1000} KB encoded).",
        file=sys.stderr,
    )

    assessment: SeriesAssessment | None = None
    quality_fingerprint = hashlib.sha256(json.dumps({
        "revision": 2,
        "inputs": [file_sha256(args.product), file_sha256(args.spec), file_sha256(args.display_plan)]
                  + [file_sha256(path) for path in panel_paths],
    }, sort_keys=True).encode()).hexdigest()
    cache_path = args.output_dir / "work" / "series-quality-cache.json"
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("fingerprint") == quality_fingerprint:
            assessment = SeriesAssessment.model_validate(cached["assessment"])
            print("Reusing unchanged five-panel consistency assessment.", file=sys.stderr)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass

    try:
        if assessment is None and (not api_key or not base_url):
            raise RuntimeError("Qwen credentials are unavailable")
        quality_timeout = float(os.environ.get("QWEN_QUALITY_TIMEOUT_SECONDS", "60"))
        quality_max_tokens = int(os.environ.get("QWEN_QUALITY_MAX_TOKENS", "1400"))
        quality_attempts = int(os.environ.get("QWEN_QUALITY_REQUEST_ATTEMPTS", "1"))
        if assessment is None:
            quality_model = os.environ.get("QWEN_MODEL", "").strip() or spec.get("source", {}).get("vision_model")
            client = QwenVisionClient(api_key, base_url, quality_model or None, timeout=quality_timeout)
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}]
            response = None
            for mode in ("json_schema", "json_object", "none"):
                try:
                    response = client._completion(
                        messages, mode, SeriesAssessment, "series_quality",
                        max_tokens=quality_max_tokens, request_attempts=quality_attempts,
                    )
                    break
                except BadRequestError:
                    if mode == "none":
                        raise
            assessment = SeriesAssessment.model_validate(extract_json(response or ""))
            cache_path.write_text(json.dumps({
                "fingerprint": quality_fingerprint,
                "assessment": assessment.model_dump(mode="json"),
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if sorted(item.panel_number for item in assessment.panels) != [1, 2, 3, 4, 5]:
            raise ValueError("quality response must contain panels 1-5 exactly once")
    except Exception as exc:
        print(f"Series quality gate failed safely: {exc}", file=sys.stderr)
        duplicate_targets = sorted({max(pair.panels) for pair in duplicate_pairs})
        if duplicate_targets:
            retry_path = args.output_dir / "work" / "series-quality.retry"
            retry_path.write_text("\n".join(f"{number:02d}" for number in duplicate_targets) + "\n", encoding="ascii")
            for number in duplicate_targets:
                related = [pair for pair in duplicate_pairs if number in pair.panels]
                base = (args.output_dir / "prompts" / f"panel-{number:02d}.txt").read_text(encoding="utf-8")
                correction = (
                    "\nLOCAL DUPLICATE CORRECTION (mandatory):\n"
                    "Use a visibly different camera, elevation, arrangement, action, background, and lighting while "
                    "preserving the exact authoritative product and assigned panel purpose. Near-duplicate panels: "
                    + ", ".join(str(pair.panels) for pair in related) + ".\n"
                )
                (args.output_dir / "work" / f"panel-{number:02d}-quality-retry.txt").write_text(
                    base.rstrip() + correction, encoding="utf-8",
                )
            report_path = args.output_dir / "work" / f"series-quality-attempt-{args.attempt}.json"
            report_path.write_text(json.dumps({
                "assessment_unavailable": type(exc).__name__,
                "duplicate_pairs": [pair.model_dump(mode="json") for pair in duplicate_pairs],
                "retry_panels": duplicate_targets, "attempt": args.attempt,
                "timing": {"local_duplicate_seconds": round(duplicate_seconds, 3)},
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 3
        return 2

    retry: set[int] = set()
    issue_map: dict[int, list[str]] = {}
    min_fidelity = float(os.environ.get("SERIES_MIN_PRODUCT_FIDELITY", "0.94"))
    min_composition = float(os.environ.get("SERIES_MIN_COMPOSITION_MATCH", "0.80"))
    min_style = float(os.environ.get("SERIES_MIN_STYLE_MATCH", "0.78"))
    if not 0 <= min_fidelity <= 1 or not 0 <= min_composition <= 1 or not 0 <= min_style <= 1:
        raise ValueError("series quality thresholds must be between 0 and 1")
    for item in assessment.panels:
        # Scores and physical validity are the machine-readable gate. Some
        # vision models put positive confirmations or harmless observations in
        # `issues` despite the schema instruction; treating mere list presence
        # as failure caused four good panels to be regenerated in one observed
        # run. Real blocking issues must be reflected in these scores/flag.
        if panel_fails_gate(item, min_fidelity, min_composition, min_style):
            retry.add(item.panel_number)
            issue_map[item.panel_number] = item.issues or ["Product identity, assigned composition, or physical validity is below threshold."]
    for pair in duplicate_pairs:
        if pair.similarity >= float(os.environ.get("SERIES_LOCAL_DUPLICATE_THRESHOLD", "0.94")):
            target = max(pair.panels)
            retry.add(target)
            issue_map.setdefault(target, []).append(f"Too similar to panel {min(pair.panels)}: " + "; ".join(pair.reasons))

    report = {
        **assessment.model_dump(mode="json"),
        "duplicate_pairs": [pair.model_dump(mode="json") for pair in duplicate_pairs],
        "retry_panels": sorted(retry), "attempt": args.attempt,
        "timing": {"local_duplicate_seconds": round(duplicate_seconds, 3)},
    }
    report_path = args.output_dir / "work" / f"series-quality-attempt-{args.attempt}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    retry_path = args.output_dir / "work" / "series-quality.retry"
    retry_path.write_text("\n".join(f"{number:02d}" for number in sorted(retry)) + ("\n" if retry else ""), encoding="ascii")

    for number in retry:
        item = next(result for result in assessment.panels if result.panel_number == number)
        localized_identity_repair = (
            number != 5 and not item.product_identity_exact and item.composition_match >= min_composition
            and item.style_match >= min_style and item.physically_valid
        )
        retry_prompt = args.output_dir / "work" / f"panel-{number:02d}-quality-retry.txt"
        if localized_identity_repair:
            write_local_product_mask(
                args.output_dir / f"panel-{number:02d}.png", item.product_bbox,
                args.output_dir / "work" / f"panel-{number:02d}-product-lock-mask.png",
            )
            retry_prompt.write_text(
                "Use case: precise-object-edit\n"
                "Image 1 is the current commercial panel and edit target. Image 2 is the sole product-design authority.\n"
                "Replace only the jewelry inside the transparent mask with the exact product from Image 2. Reproduce its observed source pixels: outer contour, nested layers, proportions, metal color, stone groups, chain type, and exact attachment points. Preserve its realistic scale and contact with the wearer or support.\n"
                "Keep every pixel outside the mask unchanged, especially face, body, hands, clothing, pose, background, light, camera, and composition. Do not beautify, simplify, symmetrize, add, remove, or redesign any product element. Do not copy any style-reference jewelry.\n"
                "Blocking findings:\n- " + "\n- ".join(issue_map[number]) + "\n",
                encoding="utf-8",
            )
        else:
            base = (args.output_dir / "prompts" / f"panel-{number:02d}.txt").read_text(encoding="utf-8")
            correction = "\nQUALITY-GATE CORRECTION (mandatory):\n- " + "\n- ".join(issue_map[number])
            correction += "\nRegenerate from the authoritative product pixels and assigned shot contract. Correct only these failures; do not compensate by redesigning, cropping, adding accessories, or borrowing another panel's visual grammar.\n"
            retry_prompt.write_text(base.rstrip() + correction, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 3 if retry else 0


if __name__ == "__main__":
    raise SystemExit(main())
