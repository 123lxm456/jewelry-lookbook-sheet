#!/usr/bin/env python3
"""Repeatable local multi-user queue benchmark using the offline workflow."""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def jpeg_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (320, 480), "#e8e2d9").save(output, format="JPEG")
    return output.getvalue()


def available_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=12)
    parser.add_argument("--active-jobs", type=int, required=True)
    parser.add_argument("--workflow-delay", type=float, default=0.12)
    args = parser.parse_args()
    if args.users < 1 or args.active_jobs < 1:
        raise SystemExit("users and active-jobs must be positive")

    with tempfile.TemporaryDirectory() as temporary:
        port = available_port()
        base_url = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment.update({
            "APP_DB_PATH": str(Path(temporary) / "app.db"),
            "DB_DRIVER": "sqlite",
            "APP_ENV": "test",
            "APP_OUTPUT_ROOT": temporary,
            "WEB_WORKFLOW_SCRIPT": str(ROOT / "tests/fake_web_workflow.py"),
            "WECHAT_DEV_LOGIN": "true",
            "WECHAT_APP_ID": "benchmark-app-id",
            "WECHAT_APP_SECRET": "benchmark-secret",
            "COOKIE_SECURE": "false",
            "PAYMENT_REQUIRED": "false",
            "WEB_MAX_ACTIVE_JOBS": str(args.active_jobs),
            "WEB_MAX_CONCURRENT_UPLOADS": str(max(4, args.active_jobs * 2)),
            "WEB_MAX_QUEUED_JOBS": str(max(100, args.users)),
            "FAKE_WEB_WORKFLOW_DELAY": str(args.workflow_delay),
            "RATE_LIMIT_DEV_LOGIN": str(max(200, args.users * 2)),
            "RATE_LIMIT_JOB_CREATE": str(max(100, args.users * 2)),
        })
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        clients: list[httpx.Client] = []
        try:
            probe = httpx.Client(base_url=base_url, timeout=10, trust_env=False)
            for _ in range(200):
                try:
                    if probe.get("/").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.025)
            else:
                raise RuntimeError("benchmark server did not start")
            probe.close()

            for index in range(args.users):
                client = httpx.Client(base_url=base_url, timeout=30, trust_env=False)
                response = client.post("/api/auth/wechat/dev-login", data={"openid": f"bench-{index}"})
                response.raise_for_status()
                clients.append(client)

            payload = jpeg_bytes()
            started = time.perf_counter()

            def submit(item: tuple[int, httpx.Client]):
                index, client = item
                response = client.post(
                    "/api/jobs", files={"image": (f"bench-{index}.jpg", payload, "image/jpeg")},
                )
                return index, time.perf_counter(), response

            with concurrent.futures.ThreadPoolExecutor(max_workers=args.users) as executor:
                submissions = list(executor.map(submit, enumerate(clients)))
            accepted: dict[str, float] = {}
            owners: dict[str, int] = {}
            rejected = 0
            for index, accepted_at, response in submissions:
                if response.status_code != 202:
                    rejected += 1
                    continue
                job_id = str(response.json()["job_id"])
                accepted[job_id] = accepted_at
                owners[job_id] = index

            first_running: dict[str, float] = {}
            terminal: dict[str, str] = {}
            while len(terminal) < len(accepted) and time.perf_counter() - started < 60:
                now = time.perf_counter()
                for job_id, index in owners.items():
                    if job_id in terminal:
                        continue
                    data = clients[index].get(f"/api/jobs/{job_id}").json()
                    status = str(data["status"])
                    if status == "running":
                        first_running.setdefault(job_id, now)
                    elif status in {"completed", "failed"}:
                        terminal[job_id] = status
                        first_running.setdefault(job_id, accepted[job_id])
                time.sleep(0.01)

            waits = [first_running[job_id] - accepted_at for job_id, accepted_at in accepted.items()]
            completed = sum(status == "completed" for status in terminal.values())
            report = {
                "users": args.users,
                "configured_active_jobs": args.active_jobs,
                "accepted": len(accepted),
                "rejected": rejected,
                "average_wait_seconds": round(sum(waits) / len(waits), 3) if waits else None,
                "p95_wait_seconds": round(sorted(waits)[max(0, int(len(waits) * 0.95) - 1)], 3) if waits else None,
                "completed": completed,
                "success_rate_percent": round(completed / args.users * 100, 2),
                "wall_seconds": round(time.perf_counter() - started, 3),
            }
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            if completed != args.users:
                raise SystemExit(1)
        finally:
            for client in clients:
                client.close()
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
