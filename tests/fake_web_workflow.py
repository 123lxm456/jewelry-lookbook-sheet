#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
import os
from pathlib import Path

from PIL import Image


def main() -> None:
    delay = float(os.environ.get("FAKE_WEB_WORKFLOW_DELAY", "0.02"))
    output_index = sys.argv.index("--output-dir") + 1
    output_dir = Path(sys.argv[output_index])
    output_dir.mkdir(parents=True, exist_ok=True)
    print("Analyzing product with Qwen model: fake-model", flush=True)
    time.sleep(delay)
    print("::workflow::spec_ready", flush=True)
    print("::workflow::assets_ready", flush=True)
    for number in range(1, 6):
        time.sleep(delay)
        Image.new("RGB", (160, 160), f"#{number}{number}8877").save(
            output_dir / f"panel-{number:02d}.png", format="PNG"
        )
        print(f"::workflow::panel_ready::{number:02d}", flush=True)
    print("::workflow::stage::长图排版与合成（动态避让商品与人物）", flush=True)
    result = output_dir / "product-long.png"
    Image.new("RGB", (160, 640), "#e7e1d8").save(result, format="PNG")
    print(f"Saved product details: {result} (160x640)", flush=True)
    print("::workflow::complete", flush=True)


if __name__ == "__main__":
    main()
