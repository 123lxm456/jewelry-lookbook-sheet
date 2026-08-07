#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from pathlib import Path

from PIL import Image


def main() -> None:
    output_index = sys.argv.index("--output-dir") + 1
    output_dir = Path(sys.argv[output_index])
    output_dir.mkdir(parents=True, exist_ok=True)
    print("Analyzing jewelry with Qwen model: fake-model", flush=True)
    time.sleep(0.02)
    print("::workflow::spec_ready", flush=True)
    print("::workflow::assets_ready", flush=True)
    for number in range(1, 6):
        time.sleep(0.02)
        print(f"::workflow::panel_ready::{number:02d}", flush=True)
    print("::workflow::stage::长图排版与合成（动态避让人物和珠宝）", flush=True)
    result = output_dir / "jewelry-long.png"
    Image.new("RGB", (160, 640), "#e7e1d8").save(result, format="PNG")
    print(f"Saved jewelry details: {result} (160x640)", flush=True)
    print("::workflow::complete", flush=True)


if __name__ == "__main__":
    main()
