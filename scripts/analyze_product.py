#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jewelry_workflow.qwen_vision import QwenVisionClient  # noqa: E402
from product_workflow.compatibility import load_product_spec  # noqa: E402
from product_workflow.models import CopySection, MarketingCopy, ProductSpec, SourceMetadata  # noqa: E402
from product_workflow.registry import select_strategy  # noqa: E402


ANALYSIS_REVISION = "forensic-product-identity-v6"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip().strip("\"'")
        # Deployment managers commonly export optional variables as empty
        # strings. Treat empty as unset so an explicit .env value can repair
        # configuration such as QWEN_MODEL; non-empty process overrides still
        # retain their normal precedence.
        if not os.environ.get(normalized_key, "").strip():
            os.environ[normalized_key] = normalized_value


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


def analysis_cache_path(output: Path, image_hash: str, model: str) -> Path:
    configured = os.environ.get("QWEN_ANALYSIS_CACHE_DIR", "").strip()
    if configured:
        cache_dir = Path(configured).expanduser()
    else:
        owner_dir = output.parent.parent if output.parent.name.startswith("job-") else output.parent
        cache_dir = owner_dir / ".analysis-cache"
    key = hashlib.sha256(f"{image_hash}\0{model}\0{ANALYSIS_REVISION}".encode()).hexdigest()
    return cache_dir / f"{key}.json"


def reusable_spec(path: Path, image_hash: str, configured_model: str, *, require_current: bool) -> ProductSpec | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        existing = load_product_spec(path)
        offline_adapter = bool(os.environ.get("IMAGE_GEN_CLI", "").strip())
        model_matches = not configured_model or existing.source.vision_model == configured_model or offline_adapter
        revision_matches = existing.source.analysis_revision == ANALYSIS_REVISION or offline_adapter
        if existing.source.image_sha256 != image_hash:
            return None
        if require_current and (not model_matches or not revision_matches or "reference_observation" not in raw):
            return None
        return existing
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def provisional_copy(strategy: dict) -> MarketingCopy:
    """Create schema-valid copy so image prompts can start before copywriting.

    The provisional value is never used in the final long image.  A separate
    copy worker replaces it while the image workers are already running.
    """
    sections = []
    for panel in strategy["panels"]:
        label = str(panel["label"])
        sections.append(CopySection(
            panel_id=panel["id"], eyebrow="PRODUCT STORY", title=label[:16],
            body="商品视觉信息正在生成\n完整文案将同步更新",
        ))
    return MarketingCopy(sections=sections)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze one non-apparel product image with a vision model.")
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--audit-only", action="store_true", help="Re-run only the geometry audit on an existing specification")
    args = parser.parse_args()

    if not args.image.is_file():
        raise SystemExit(f"Missing product image: {args.image}")
    load_env(args.env_file)
    image_hash = file_sha256(args.image)
    configured_model = os.environ.get("QWEN_MODEL", "").strip()

    cache_path = analysis_cache_path(args.output, image_hash, configured_model)
    fallback_spec = reusable_spec(args.output, image_hash, configured_model, require_current=False) if args.output.exists() else None
    if fallback_spec is None and cache_path.exists():
        fallback_spec = reusable_spec(cache_path, image_hash, configured_model, require_current=False)
    if not args.force and not args.audit_only:
        existing = reusable_spec(args.output, image_hash, configured_model, require_current=True) if args.output.exists() else None
        if existing is not None:
            # A custom image adapter is also the supported offline/test path;
            # in that mode an existing validated spec is authoritative and
            # must not trigger a network analysis merely because .env names a
            # production Qwen model.
            print(f"Reusing Qwen analysis: {args.output}", file=sys.stderr)
            return
        cached = reusable_spec(cache_path, image_hash, configured_model, require_current=True) if cache_path.exists() else None
        if cached is not None:
            write_json(args.output, cached.model_dump(mode="json", by_alias=True))
            if any("商品视觉信息正在生成" in section.body for section in cached.marketing_copy.sections):
                args.output.with_suffix(".copy-pending").write_text("pending\n", encoding="ascii")
            print(f"Reusing cached Qwen analysis: {cache_path}", file=sys.stderr)
            return

    api_key = os.environ.get("QWEN_API_KEY", "").strip()
    base_url = os.environ.get("QWEN_BASE_URL", "").strip()
    if not api_key or not base_url:
        if fallback_spec is not None:
            write_json(args.output, fallback_spec.model_dump(mode="json", by_alias=True))
            print("Qwen is not configured; using the matching previously validated analysis.", file=sys.stderr)
            return
        raise SystemExit("Set QWEN_API_KEY and QWEN_BASE_URL in .env or the environment")

    started = time.monotonic()
    client: QwenVisionClient | None = None
    try:
        client = QwenVisionClient(api_key=api_key, base_url=base_url, model=configured_model or None)
        print(f"Analyzing product with Qwen model: {client.model}", file=sys.stderr)
        if args.audit_only:
            if not args.output.is_file():
                raise SystemExit("--audit-only requires an existing product specification")
            existing = load_product_spec(args.output)
            if existing.source.image_sha256 != image_hash:
                raise SystemExit("--audit-only specification does not match the input image")
            analysis = client.audit_product(args.image, existing)
        else:
            analysis = client.analyze(args.image)
    except Exception as exc:
        if args.audit_only or fallback_spec is None:
            raise
        print(
            f"Qwen analysis unavailable ({type(exc).__name__}); using the matching previously validated analysis.",
            file=sys.stderr,
        )
        write_json(args.output, fallback_spec.model_dump(mode="json", by_alias=True))
        retry_count = client.retry_count if client is not None else 0
        print(
            f"Qwen analysis stats: seconds={time.monotonic() - started:.1f}, retries={retry_count}, fallback=existing",
            file=sys.stderr,
        )
        return
    assert client is not None
    category, strategy = select_strategy(analysis)
    spec = ProductSpec(
        **analysis.model_dump(),
        strategy_id=strategy["id"],
        marketing_copy=provisional_copy(strategy),
        source=SourceMetadata(
            image_file=args.image.name,
            image_sha256=image_hash,
            vision_model=client.model,
            analysis_revision=ANALYSIS_REVISION,
            analyzed_at=datetime.now(timezone.utc),
        ),
    )
    write_json(args.output, spec.model_dump(mode="json", by_alias=True))
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(cache_path, spec.model_dump(mode="json", by_alias=True))
    except OSError as exc:
        print(f"Qwen analysis cache unavailable; continuing without shared cache: {exc}", file=sys.stderr)
    args.output.with_suffix(".copy-pending").write_text("pending\n", encoding="ascii")
    print(f"Saved product specification: {args.output} ({category.label}/{spec.identity.subcategory})", file=sys.stderr)
    print(
        f"Qwen analysis stats: seconds={time.monotonic() - started:.1f}, retries={client.retry_count}, fallback=none",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
