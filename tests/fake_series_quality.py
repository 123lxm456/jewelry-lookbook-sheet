#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--product")
parser.add_argument("--spec")
parser.add_argument("--display-plan")
parser.add_argument("--attempt", type=int, required=True)
parser.add_argument("--env-file")
args = parser.parse_args()

if os.environ.get("FAKE_SERIES_STATUS") == "2":
    raise SystemExit(2)

work = args.output_dir / "work"
work.mkdir(parents=True, exist_ok=True)
if args.attempt == 1:
    (work / "series-quality.retry").write_text("04\n", encoding="ascii")
    base = (args.output_dir / "prompts/panel-04.txt").read_text(encoding="utf-8")
    (work / "panel-04-quality-retry.txt").write_text(base + "\nFake correction.\n", encoding="utf-8")
    raise SystemExit(3)
raise SystemExit(0)
