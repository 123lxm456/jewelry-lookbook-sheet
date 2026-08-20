#!/usr/bin/env python3
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser(description="Atomically package the five panels and final long image.")
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    artifacts = [
        (f"商品展示图_{number:02d}.png", args.output_dir / f"panel-{number:02d}.png")
        for number in range(1, 6)
    ]
    artifacts.append(("商品信息长图.png", args.output_dir / "product-long.png"))
    for _, path in artifacts:
        with Image.open(path) as image:
            image.verify()
    destination = args.output_dir / "product-images.zip"
    temporary = destination.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for filename, path in artifacts:
                archive.write(path, arcname=filename)
        temporary.replace(destination)
        destination.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    print(f"Saved product archive: {destination}")


if __name__ == "__main__":
    main()
