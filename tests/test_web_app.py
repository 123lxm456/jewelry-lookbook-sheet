from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from app import public_job_error


ROOT = Path(__file__).resolve().parents[1]


def available_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def jpeg_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (320, 480), "#f2eee8").save(buffer, format="JPEG")
    return buffer.getvalue()


class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.port = available_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        environment = os.environ.copy()
        environment.update(
            {
                "APP_DB_PATH": str(Path(cls.temporary.name) / "app.db"),
                "APP_OUTPUT_ROOT": cls.temporary.name,
                "WEB_WORKFLOW_SCRIPT": str(ROOT / "tests/fake_web_workflow.py"),
            }
        )
        cls.server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.client = httpx.Client(base_url=cls.base_url, timeout=5, trust_env=False)
        for _ in range(100):
            try:
                if cls.client.get("/").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            cls.server.terminate()
            raise RuntimeError("test web server did not start")
        registration = cls.client.post(
            "/api/auth/register",
            data={"username": "testuser", "password": "test-password-123"},
        )
        if registration.status_code != 200:
            cls.server.terminate()
            raise RuntimeError(f"test user registration failed: {registration.text}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls.server.terminate()
        try:
            cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait(timeout=5)
        cls.temporary.cleanup()

    def setUp(self) -> None:
        response = self.client.post(
            "/api/auth/login",
            data={"username": "testuser", "password": "test-password-123"},
        )
        self.assertEqual(response.status_code, 200)

    def test_frontend_is_served(self) -> None:
        response = self.client.get("/app")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Jewelry Lookbook Sheet", response.text)
        self.assertIn('id="previewDialog"', response.text)
        self.assertIn('id="expandPreview"', response.text)
        self.assertIn('/app.js?v=', response.text)

    def test_unauthenticated_root_shows_login_page(self) -> None:
        unauthenticated = httpx.Client(base_url=self.base_url, timeout=5, trust_env=False)
        try:
            response = unauthenticated.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("登录系统", response.text)
        finally:
            unauthenticated.close()

    def test_root_always_requires_a_new_login(self) -> None:
        response = self.client.get("/")
        self.assertIn("登录系统", response.text)
        self.assertEqual(self.client.get("/app").status_code, 401)

    def test_unauthenticated_api_is_rejected(self) -> None:
        unauthenticated = httpx.Client(base_url=self.base_url, timeout=5, trust_env=False)
        try:
            self.assertEqual(unauthenticated.get("/api/session").status_code, 401)
            self.assertEqual(unauthenticated.get("/api/jobs/not-owned").status_code, 401)
        finally:
            unauthenticated.close()

    def test_frontend_does_not_restore_previous_job(self) -> None:
        response = self.client.get("/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("restoreJob", response.text)
        self.assertNotIn("localStorage.setItem", response.text)
        self.assertIn("cache: \"no-store\"", response.text)

    def test_frontend_session_is_scoped_to_server_process(self) -> None:
        first = self.client.get("/api/session")
        second = self.client.get("/api/session")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertTrue(first.json()["session_id"])
        self.assertEqual(first.json()["user_id"], 1)
        self.assertEqual(first.headers["cache-control"], "no-store")

        script = self.client.get("/app.js").text
        self.assertIn('fetch("/api/session", { cache: "no-store" })', script)

    def test_frontend_restores_only_current_tab_job(self) -> None:
        response = self.client.get("/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("window.sessionStorage", response.text)
        self.assertIn("restoreCurrentJob", response.text)
        self.assertIn("data.input_url", response.text)
        self.assertNotIn("clearClientState();\n  try {\n    await initializeSession", response.text)
        self.assertIn("initializePage();", response.text)

    def test_invalid_upload_is_rejected(self) -> None:
        response = self.client.post("/api/jobs", files={"image": ("bad.txt", b"not-an-image", "text/plain")})
        self.assertEqual(response.status_code, 400)

    def test_connection_error_is_safe_for_frontend(self) -> None:
        error = public_job_error(RuntimeError("traceback\nopenai.APIConnectionError: Connection error."))
        self.assertIn("图片生成服务连接失败", error)
        self.assertNotIn("traceback", error)

    def test_upload_progress_result_and_download(self) -> None:
        response = self.client.post(
            "/api/jobs",
            files={"image": ("jewelry.jpg", jpeg_bytes(), "image/jpeg")},
        )
        self.assertEqual(response.status_code, 202)
        job_id = response.json()["job_id"]

        terminal = None
        for _ in range(100):
            terminal = self.client.get(f"/api/jobs/{job_id}").json()
            if terminal["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        self.assertIsNotNone(terminal)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["progress"], 100)

        events = self.client.get(f"/api/jobs/{job_id}/events")
        self.assertEqual(events.status_code, 200)
        event_data = json.loads(events.text.split("data: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(event_data["status"], "completed")

        result = self.client.get(terminal["result_url"])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["content-type"], "image/png")

        download = self.client.get(terminal["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["content-disposition"])

        user_output_root = Path(self.temporary.name) / "testuser"
        self.assertTrue(user_output_root.is_dir())
        user_jobs = list(user_output_root.glob("job-*"))
        self.assertEqual(len(user_jobs), 1)
        self.assertTrue((user_jobs[0] / "input.jpg").is_file())
        self.assertTrue((user_jobs[0] / "jewelry-long.png").is_file())
        self.assertTrue((user_jobs[0] / "job-state.json").is_file())
        self.assertFalse(list(Path(self.temporary.name).glob("job-*")))

        other = httpx.Client(base_url=self.base_url, timeout=5, trust_env=False)
        try:
            registration = other.post(
                "/api/auth/register",
                data={"username": "otheruser", "password": "other-password-123"},
            )
            self.assertEqual(registration.status_code, 200)
            self.assertEqual(other.get(f"/api/jobs/{job_id}").status_code, 404)
            self.assertEqual(other.get(terminal["result_url"]).status_code, 404)
            self.assertEqual(other.get(terminal["download_url"]).status_code, 404)

            other_job_response = other.post(
                "/api/jobs",
                files={"image": ("other.jpg", jpeg_bytes(), "image/jpeg")},
            )
            self.assertEqual(other_job_response.status_code, 202)
            other_job_id = other_job_response.json()["job_id"]
            for _ in range(100):
                other_terminal = other.get(f"/api/jobs/{other_job_id}").json()
                if other_terminal["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.02)
            self.assertEqual(other_terminal["status"], "completed")
            other_output_root = Path(self.temporary.name) / "otheruser"
            self.assertTrue(other_output_root.is_dir())
            self.assertTrue(list(other_output_root.glob("job-*/jewelry-long.png")))
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
