#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jewelry_workflow.product_spec import ProductSpec, SourceMetadata  # noqa: E402
from jewelry_workflow.qwen_vision import QwenVisionClient  # noqa: E402


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze one jewelry image with a Qwen vision model.")
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if not args.image.is_file():
        raise SystemExit(f"Missing product image: {args.image}")
    load_env(args.env_file)
    image_hash = file_sha256(args.image)
    configured_model = os.environ.get("QWEN_MODEL", "").strip()

    if args.output.exists() and not args.force:
        try:
            existing = ProductSpec.model_validate_json(args.output.read_text(encoding="utf-8"))
            model_matches = not configured_model or existing.source.qwen_model == configured_model
            if existing.source.image_sha256 == image_hash and model_matches:
                print(f"Reusing Qwen analysis: {args.output}", file=sys.stderr)
                return
        except (OSError, ValueError):
            pass

    api_key = os.environ.get("QWEN_API_KEY", "").strip()
    base_url = os.environ.get("QWEN_BASE_URL", "").strip()
    if not api_key or not base_url:
        raise SystemExit("Set QWEN_API_KEY and QWEN_BASE_URL in .env or the environment")

    client = QwenVisionClient(api_key=api_key, base_url=base_url, model=configured_model or None)
    print(f"Analyzing jewelry with Qwen model: {client.model}", file=sys.stderr)
    analysis = client.analyze(args.image)
    spec = ProductSpec(
        **analysis.model_dump(),
        source=SourceMetadata(
            image_file=args.image.name,
            image_sha256=image_hash,
            qwen_model=client.model,
            analyzed_at=datetime.now(timezone.utc),
        ),
    )
    write_json(args.output, spec.model_dump(mode="json", by_alias=True))
    print(f"Saved product specification: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
