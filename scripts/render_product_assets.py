#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jewelry_workflow.product_spec import ProductSpec  # noqa: E402


TEMPLATES = {
    "01": "panel-01-wear.txt",
    "02": "panel-02-macro.txt",
    "03": "panel-03-still-life.txt",
    "04": "panel-04-gift.txt",
    "05": "panel-05-mirror.txt",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def write_json(path: Path, data: dict) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def bullet_text(values: list[str]) -> str:
    return "; ".join(values)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render image prompts and page copy from ProductSpec.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("detail", type=Path)
    parser.add_argument("style", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--template-dir", type=Path, default=ROOT / "prompts")
    parser.add_argument("--image-model", default="gpt-image-2")
    args = parser.parse_args()

    spec = ProductSpec.model_validate_json(args.spec.read_text(encoding="utf-8"))
    for path, label in ((args.detail, "detail reference"), (args.style, "style reference")):
        if not path.is_file():
            raise SystemExit(f"Missing {label}: {path}")

    values = {
        "category": spec.identity.category,
        "item_count": str(spec.identity.item_count),
        "subject_description_en": spec.generation.subject_description_en,
        "wearing_instruction_en": spec.generation.wearing_instruction_en,
        "macro_focus_en": spec.generation.macro_focus_en,
        "gift_presentation_en": spec.generation.gift_presentation_en,
        "integrity_constraints_en": bullet_text(spec.generation.integrity_constraints_en),
        "forbidden_additions_en": bullet_text(spec.generation.forbidden_additions_en),
    }

    rendered_prompts: dict[str, str] = {}
    template_hashes: dict[str, str] = {}
    for number, filename in TEMPLATES.items():
        template_path = args.template_dir / filename
        template_source = template_path.read_text(encoding="utf-8")
        rendered = Template(template_source).substitute(values).rstrip() + "\n"
        prompt_path = args.output_dir / "prompts" / f"panel-{number}.txt"
        write_text(prompt_path, rendered)
        rendered_prompts[number] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        template_hashes[number] = hashlib.sha256(template_source.encode("utf-8")).hexdigest()

    mirror_refine_template = args.template_dir / "panel-05-mirror-refine.txt"
    mirror_refine_source = mirror_refine_template.read_text(encoding="utf-8")
    mirror_refine = Template(mirror_refine_source).safe_substitute(values).rstrip() + "\n"
    write_text(args.output_dir / "work" / "panel-05-mirror-refine.txt", mirror_refine)
    template_hashes["05-mirror-refine"] = hashlib.sha256(mirror_refine_source.encode("utf-8")).hexdigest()
    rendered_prompts["05-mirror-refine"] = hashlib.sha256(mirror_refine.encode("utf-8")).hexdigest()

    page = {"sections": [section.model_dump() for section in spec.marketing_copy.sections]}
    write_json(args.output_dir / "page.json", page)

    fingerprint_input = {
        "workflow_version": 4,
        "mirror_pipeline": "scene-edit-geometry-refine-v1",
        "source_sha256": spec.source.image_sha256,
        "detail_sha256": file_sha256(args.detail),
        "style_sha256": file_sha256(args.style),
        "qwen_model": spec.source.qwen_model,
        "image_model": args.image_model,
        "analysis": spec.model_dump(mode="json", by_alias=True, exclude={"source"}),
        "templates": template_hashes,
        "rendered_prompts": rendered_prompts,
    }
    canonical = json.dumps(fingerprint_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    manifest = {"fingerprint": fingerprint, **fingerprint_input}
    write_json(args.output_dir / "generation-manifest.json", manifest)
    print(fingerprint)


if __name__ == "__main__":
    main()
