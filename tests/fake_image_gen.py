#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from pathlib import Path

from PIL import Image


def argument_value(name: str) -> str:
    index = sys.argv.index(name)
    return sys.argv[index + 1]


def main() -> None:
    if "--dry-run" in sys.argv:
        return
    output = Path(argument_value("--out"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".started").write_text(str(time.monotonic()), encoding="ascii")
    time.sleep(0.25)
    Image.new("RGB", (64, 96), "#ded6ca").save(output, format="PNG")


if __name__ == "__main__":
    main()
