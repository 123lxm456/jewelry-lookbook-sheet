#!/usr/bin/env python3
"""Project-owned image editing CLI used by the production workflow.

It intentionally implements the small command surface used by run_workflow.sh
so production does not depend on a Codex installation or a user's HOME.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import os
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from openai import OpenAI


@contextmanager
def global_image_slot():
    """Limit image requests across all workflow processes on this host."""
    limit = int(os.environ.get("IMAGE2_GLOBAL_PARALLELISM", "5"))
    if not 1 <= limit <= 64:
        fail("IMAGE2_GLOBAL_PARALLELISM must be between 1 and 64")
    default_root = Path(tempfile.gettempdir()) / f"jewelry-lookbook-image-slots-{os.getuid()}"
    lock_root = Path(os.environ.get("IMAGE2_GLOBAL_LIMIT_DIR", str(default_root)))
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_stat = lock_root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
        fail("IMAGE2_GLOBAL_LIMIT_DIR must be a directory owned by the service account")
    lock_root.chmod(0o700)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    handles = []
    try:
        for index in range(limit):
            descriptor = os.open(
                lock_root / f"slot-{index:02d}.lock",
                os.O_CREAT | os.O_RDWR | no_follow,
                0o600,
            )
            handles.append(os.fdopen(descriptor, "a+b"))
    except Exception:
        for handle in handles:
            handle.close()
        raise
    acquired = None
    try:
        while acquired is None:
            for handle in handles:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = handle
                    break
                except BlockingIOError:
                    continue
            if acquired is None:
                time.sleep(0.1)
        yield
    finally:
        if acquired is not None:
            fcntl.flock(acquired.fileno(), fcntl.LOCK_UN)
        for handle in handles:
            handle.close()


def fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an edited image through an OpenAI-compatible API")
    subparsers = parser.add_subparsers(dest="command", required=True)
    edit = subparsers.add_parser("edit", help="Edit one or more reference images")
    edit.add_argument("--model", default="gpt-image-2")
    edit.add_argument("--image", action="append", required=True)
    edit.add_argument("--prompt-file", required=True)
    edit.add_argument("--size", default="1024x1536")
    edit.add_argument("--quality", default="high")
    edit.add_argument("--output-format", default="png", choices=("png", "jpeg", "webp"))
    edit.add_argument("--out", required=True, type=Path)
    edit.add_argument("--no-augment", action="store_true", help="Accepted for workflow compatibility")
    edit.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def run_edit(args: argparse.Namespace) -> None:
    image_paths = [Path(value) for value in args.image]
    for image_path in image_paths:
        if not image_path.is_file():
            fail(f"Image file not found: {image_path}")
    prompt_path = Path(args.prompt_file)
    if not prompt_path.is_file():
        fail(f"Prompt file not found: {prompt_path}")
    if not args.dry_run and not args.out.parent.exists():
        args.out.parent.mkdir(parents=True, exist_ok=True)

    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        fail(f"Prompt file is empty: {prompt_path}")

    if args.dry_run:
        print(f"Dry run: edit {len(image_paths)} image(s) -> {args.out}")
        return

    if not os.environ.get("OPENAI_API_KEY"):
        fail("OPENAI_API_KEY is not set; configure IMAGE2_API_KEY in .env")

    request = {
        "model": args.model,
        "image": [],
        "prompt": prompt,
        "size": args.size,
        "quality": args.quality,
        "output_format": args.output_format,
    }
    handles = []
    try:
        for image_path in image_paths:
            handle = image_path.open("rb")
            handles.append(handle)
            request["image"].append(handle)
        if len(request["image"]) == 1:
            request["image"] = request["image"][0]
        with global_image_slot():
            result = OpenAI().images.edit(**request)
    except Exception as exc:
        fail(str(exc))
    finally:
        for handle in handles:
            handle.close()

    if not result.data or not getattr(result.data[0], "b64_json", None):
        fail("Image API returned no base64 image data")
    temporary = args.out.with_suffix(args.out.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(base64.b64decode(result.data[0].b64_json))
        temporary.replace(args.out)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        fail(f"Unable to save generated image: {exc}")
    print(f"Saved image: {args.out}")


def main() -> int:
    args = parse_args()
    if args.command == "edit":
        run_edit(args)
    return 0


if __name__ == "__main__":
    main()
