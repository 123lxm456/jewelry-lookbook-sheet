#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_workflow.compatibility import load_product_spec  # noqa: E402
from product_workflow.prompt_builder import build_display_plan, canonical_hash, render_panel_prompt  # noqa: E402
from product_workflow.registry import StrategyRegistry  # noqa: E402


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Render image prompts and page copy from ProductSpec.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("detail", type=Path)
    parser.add_argument("style", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--template", type=Path, default=ROOT / "prompts" / "base" / "product-panel.txt")
    parser.add_argument("--image-model", default="gpt-image-2")
    args = parser.parse_args()

    spec = load_product_spec(args.spec)
    for path, label in ((args.detail, "detail reference"), (args.style, "style reference")):
        if not path.is_file():
            raise SystemExit(f"Missing {label}: {path}")

    strategy = StrategyRegistry().get(spec.strategy_id)
    display_plan = build_display_plan(spec, strategy)
    write_json(args.output_dir / "display-plan.json", display_plan)

    rendered_prompts: dict[str, str] = {}
    template_hashes: dict[str, str] = {}
    postprocessors: dict[str, dict] = {}
    for panel in display_plan["panels"]:
        number = f"{panel['number']:02d}"
        rendered, template_hash = render_panel_prompt(spec, panel, args.template)
        prompt_path = args.output_dir / "prompts" / f"panel-{number}.txt"
        write_text(prompt_path, rendered)
        rendered_prompts[number] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        template_hashes[number] = template_hash
        if panel.get("postprocess"):
            postprocess = panel["postprocess"]
            source_path = ROOT / postprocess["template"]
            source = source_path.read_text(encoding="utf-8").rstrip() + "\n"
            output_name = f"panel-{number}-{postprocess['type']}.txt"
            write_text(args.output_dir / "work" / output_name, source)
            postprocessors[number] = {**postprocess, "prompt_file": f"work/{output_name}"}
            template_hashes[f"{number}-{postprocess['type']}"] = hashlib.sha256(source.encode()).hexdigest()
            rendered_prompts[f"{number}-{postprocess['type']}"] = hashlib.sha256(source.encode()).hexdigest()

    display_plan["postprocessors"] = postprocessors
    write_json(args.output_dir / "display-plan.json", display_plan)

    page = {
        "strategy_id": spec.strategy_id,
        "sections": [section.model_dump(exclude={"panel_id"}) for section in spec.marketing_copy.sections],
    }
    write_json(args.output_dir / "page.json", page)

    fingerprint_input = {
        "workflow_version": 7,
        "schema_version": spec.schema_version,
        "strategy_id": spec.strategy_id,
        "display_plan_sha256": canonical_hash(display_plan),
        "source_sha256": spec.source.image_sha256,
        "detail_sha256": file_sha256(args.detail),
        "style_sha256": file_sha256(args.style),
        "vision_model": spec.source.vision_model,
        "image_model": args.image_model,
        # Marketing copy is intentionally absent: it is generated in parallel
        # and must not invalidate or delay image-generation artifacts.
        "analysis": spec.model_dump(mode="json", by_alias=True, exclude={"source", "marketing_copy"}),
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
