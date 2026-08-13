#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jewelry_workflow.qwen_vision import QwenVisionClient  # noqa: E402
from product_workflow.compatibility import load_product_spec  # noqa: E402
from product_workflow.registry import StrategyRegistry  # noqa: E402
from scripts.analyze_product import load_env, write_json  # noqa: E402


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
    if not api_key or not base_url:
        raise SystemExit("Set QWEN_API_KEY and QWEN_BASE_URL in .env or the environment")
    client = QwenVisionClient(api_key=api_key, base_url=base_url, model=os.environ.get("QWEN_MODEL", "").strip() or None)
    marketing_copy = client.create_marketing_copy(spec, strategy)
    updated = spec.model_copy(update={"marketing_copy": marketing_copy})
    write_json(args.spec, updated.model_dump(mode="json", by_alias=True))
    write_json(args.page, {
        "strategy_id": updated.strategy_id,
        "sections": [section.model_dump(exclude={"panel_id"}) for section in marketing_copy.sections],
    })
    args.spec.with_suffix(".copy-pending").unlink(missing_ok=True)
    print("::workflow::copy_ready")


if __name__ == "__main__":
    main()
