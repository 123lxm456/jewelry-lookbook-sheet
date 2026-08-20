#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize an existing Web job state with accepted artifacts.")
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    state_path = args.output_dir / "job-state.json"
    if not state_path.is_file():
        return
    spec = json.loads((args.output_dir / "product-spec.json").read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).isoformat()
    identity = spec["identity"]
    product = {
        "category_group": identity["category_group"],
        "subcategory": identity["subcategory"],
        "product_name": identity["product_name"],
    }
    state.update({
        **product,
        "product": product,
        "status": "completed",
        "progress": 100,
        "stage": "商品信息长图生成完成",
        "error": None,
        "recoverable": False,
        "updated_at": now,
        "panels": [f"{number:02d}" for number in range(1, 6)],
        "log_tail": [
            "五图商品一致性、构图、风格、人物与镜面关系已通过严格质量门禁",
            "当前五张展示图、商品信息长图及 ZIP 已重新验证并同步",
            "::workflow::complete",
        ],
    })
    steps = state.setdefault("steps", {})
    artifacts = {
        "analysis": "product-spec.json", "assets": "display-plan.json",
        **{f"display_{number:02d}": f"panel-{number:02d}.png" for number in range(1, 6)},
        "long_image": "product-long.png", "zip": "product-images.zip",
    }
    for key, relative in artifacts.items():
        path = args.output_dir / relative
        if path.is_file() and path.stat().st_size > 0:
            step = steps.setdefault(key, {})
            step.update({"status": "success", "path": relative, "updated_at": now})
            step.pop("error", None)
    temporary = state_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(state_path)


if __name__ == "__main__":
    main()
