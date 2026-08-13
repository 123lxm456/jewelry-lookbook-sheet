#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.assemble_long_image import build_risk_map, cover  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Precompute one generated panel for final layout.")
    parser.add_argument("panel", type=Path)
    parser.add_argument("display_plan", type=Path)
    parser.add_argument("number")
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    plan = json.loads(args.display_plan.read_text(encoding="utf-8"))
    panel = next(item for item in plan["panels"] if f"{int(item['number']):02d}" == args.number)
    image = cover(args.panel, 1256, int(panel["crop_height"]), float(panel["focus_y"]))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prepared = args.output_dir / f"panel-{args.number}.ppm"
    risk = args.output_dir / f"risk-{args.number}.png"
    image.save(prepared, format="PPM")
    build_risk_map(image).save(risk, format="PNG")
    print(f"::workflow::layout_ready::{args.number}")


if __name__ == "__main__":
    main()
