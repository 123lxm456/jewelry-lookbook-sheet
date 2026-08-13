#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
import os
import re
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
    output.with_suffix(".images").write_text(str(sys.argv.count("--image")), encoding="ascii")
    match = re.search(r"panel-(\d{2})", output.name)
    delay_key = "FAKE_IMAGE_DELAY_POSTPROCESS" if "refined" in output.name else (
        f"FAKE_IMAGE_DELAY_{match.group(1)}" if match else "FAKE_IMAGE_DELAY"
    )
    time.sleep(float(os.environ.get(delay_key, os.environ.get("FAKE_IMAGE_DELAY", "0.25"))))
    error_key = f"FAKE_IMAGE_ERROR_{match.group(1)}" if match else "FAKE_IMAGE_ERROR"
    if error := os.environ.get(error_key, os.environ.get("FAKE_IMAGE_ERROR", "")):
        print(f"Error: Error code: {error} - fake upstream failure", file=sys.stderr)
        raise SystemExit(1)
    Image.new("RGB", (64, 96), "#ded6ca").save(output, format="PNG")
    output.with_suffix(".finished").write_text(str(time.monotonic()), encoding="ascii")


if __name__ == "__main__":
    main()
