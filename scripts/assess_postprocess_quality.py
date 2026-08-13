#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from openai import BadRequestError
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jewelry_workflow.qwen_vision import QwenVisionClient, extract_json, image_data_url  # noqa: E402
from scripts.analyze_product import load_env  # noqa: E402


class QualityAssessment(BaseModel):
    qualified: bool
    confidence: float = Field(ge=0, le=1)
    issues: list[str]


SYSTEM_PROMPT = """You are a strict commercial image quality inspector. Return JSON only.
Assess whether the generated mirror photograph is already good enough to publish without another image edit.
It qualifies only when there is one coherent real mirror scene, one person and exactly one corresponding reflection,
matching pose/clothing/anatomy/perspective on both sides, and the same referenced jewelry is visibly and correctly
worn without duplication, disappearance, redesign, or malformed body parts. Minor stylistic differences are acceptable.
Return qualified, confidence from 0 to 1, and concise issues. When evidence is ambiguous, qualified must be false."""


def main() -> int:
    parser = argparse.ArgumentParser(description="Decide whether an expensive image postprocess is necessary.")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--type", required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    if args.type != "mirror_refine":
        print(f"No quality gate is defined for {args.type}; refinement required.")
        return 3

    load_env(args.env_file)
    api_key = os.environ.get("QWEN_API_KEY", "").strip()
    base_url = os.environ.get("QWEN_BASE_URL", "").strip()
    if not api_key or not base_url:
        print("Quality gate is unavailable; refinement required.", file=sys.stderr)
        return 2

    try:
        client = QwenVisionClient(api_key, base_url, os.environ.get("QWEN_MODEL", "").strip() or None)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "First image: generated mirror panel. Second image: authoritative product reference."},
                {"type": "image_url", "image_url": {"url": image_data_url(args.image, 140000), "detail": "high"}},
                {"type": "image_url", "image_url": {"url": image_data_url(args.product, 100000), "detail": "high"}},
            ]},
        ]
        content = None
        for mode in ("json_schema", "json_object", "none"):
            try:
                content = client._completion(messages, mode, QualityAssessment, "postprocess_quality")
                break
            except BadRequestError:
                if mode == "none":
                    raise
        if content is None:
            raise RuntimeError("quality model returned no assessment")
        result = QualityAssessment.model_validate(extract_json(content))
    except Exception as exc:
        print(f"Quality gate failed safely: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result.model_dump(), ensure_ascii=False))
    return 0 if result.qualified and result.confidence >= 0.8 and not result.issues else 3


if __name__ == "__main__":
    raise SystemExit(main())
