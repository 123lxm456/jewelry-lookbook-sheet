#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jewelry_workflow.qwen_vision import QwenVisionClient  # noqa: E402
from product_workflow.compatibility import load_product_spec  # noqa: E402
from product_workflow.models import CopySection, MarketingCopy  # noqa: E402
from product_workflow.registry import StrategyRegistry  # noqa: E402
from scripts.analyze_product import analysis_cache_path, load_env, write_json  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate final panel copy independently of image generation.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("page", type=Path)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    load_env(args.env_file)
    spec = load_product_spec(args.spec)
    strategy = StrategyRegistry().get(spec.strategy_id)
    api_key = os.environ.get("QWEN_API_KEY", "").strip()
    base_url = os.environ.get("QWEN_BASE_URL", "").strip()
    marketing_copy = None
    if api_key and base_url:
        client = QwenVisionClient(
            api_key=api_key, base_url=base_url,
            model=os.environ.get("QWEN_MODEL", "").strip() or spec.source.vision_model,
            timeout=float(os.environ.get("QWEN_COPY_TIMEOUT_SECONDS", "45")),
        )
        try:
            marketing_copy = client.create_marketing_copy(spec, strategy)
        except Exception as exc:
            print(
                f"Qwen copy request unavailable ({type(exc).__name__}); using deterministic product-grounded copy.",
                file=sys.stderr,
            )
    if marketing_copy is None:
        print(
            "Using deterministic product-grounded marketing copy fallback.",
            file=sys.stderr,
        )
        feature = next((item for item in spec.design.visual_selling_points if item.strip()), "忠于源图细节")
        sections = []
        for panel in strategy["panels"]:
            label = str(panel["label"])
            sections.append(CopySection(
                panel_id=panel["id"], eyebrow="PRODUCT STORY", title=label[:16],
                body=f"忠于源图呈现\n源图细节·{feature[:19]}",
            ))
        marketing_copy = MarketingCopy(sections=sections)
    updated = spec.model_copy(update={"marketing_copy": marketing_copy})
    write_json(args.spec, updated.model_dump(mode="json", by_alias=True))
    try:
        cache_path = analysis_cache_path(
            args.spec, spec.source.image_sha256, os.environ.get("QWEN_MODEL", "").strip(),
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(cache_path, updated.model_dump(mode="json", by_alias=True))
    except OSError as exc:
        print(f"Qwen analysis/copy cache update unavailable: {exc}", file=sys.stderr)
    write_json(args.page, {
        "strategy_id": updated.strategy_id,
        "sections": [section.model_dump(exclude={"panel_id"}) for section in marketing_copy.sections],
    })
    args.spec.with_suffix(".copy-pending").unlink(missing_ok=True)
    print("::workflow::copy_ready")


if __name__ == "__main__":
    main()
