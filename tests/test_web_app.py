from __future__ import annotations

import base64
import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import io
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

os.environ.setdefault("DB_DRIVER", "sqlite")
os.environ.setdefault("APP_DB_PATH", str(Path(tempfile.gettempdir()) / "lookbook-test-import.db"))
os.environ.setdefault("APP_ENV", "test")

from fastapi import HTTPException

import app as app_module
from app import FairJobQueue, PAYMENT_PACKAGES, JobState, job_failure_error, paid_user, parse_workflow_line, public_job_error
from auth import AuthStore, DatabaseConfig, User


ROOT = Path(__file__).resolve().parents[1]


def available_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def jpeg_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (320, 480), "#f2eee8").save(buffer, format="JPEG")
    return buffer.getvalue()


class QueueRecoveryTests(unittest.TestCase):
    def test_series_quality_timeout_is_not_reported_as_product_analysis_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.jpg"
            input_path.write_bytes(jpeg_bytes())
            job = JobState(
                "a" * 32, 1, root, input_path, status="failed",
                stage="五图商品一致性与重复度校验：第 1 次",
            )
            job.steps = {"analysis": {"status": "success"}}
            message = job_failure_error(job, RuntimeError("APITimeoutError: Request timed out"))
            self.assertIn("五图质量校验", message)
            self.assertNotIn("商品分析服务", message)

    def test_fair_queue_prioritizes_users_with_less_running_work(self) -> None:
        queue = FairJobQueue()
        root = Path(tempfile.gettempdir())
        queued_jobs = [
            JobState("1" * 32, 10, root / "one", root / "one/input.jpg"),
            JobState("2" * 32, 10, root / "two", root / "two/input.jpg"),
            JobState("3" * 32, 20, root / "three", root / "three/input.jpg"),
        ]
        try:
            for job in queued_jobs:
                app_module.jobs[job.job_id] = job
                queue.put_nowait(job.job_id)
            self.assertEqual(queue.get_nowait(), queued_jobs[0].job_id)
            queue.task_done()
            # User 20 has not received a slot yet, so it jumps ahead of user
            # 10's second task without violating user 10's internal FIFO.
            self.assertEqual(queue.get_nowait(), queued_jobs[2].job_id)
            queue.task_done()
            self.assertEqual(queue.get_nowait(), queued_jobs[1].job_id)
            queue.task_done()
        finally:
            for job in queued_jobs:
                app_module.jobs.pop(job.job_id, None)

    def test_panel_503_log_is_reported_without_exposing_raw_provider_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.jpg"
            input_path.write_bytes(jpeg_bytes())
            logs = root / "logs"
            logs.mkdir()
            (logs / "panel-04-attempt-1.log").write_text(
                "Error: Error code: 503 - {'error': {'message': 'No available compatible accounts'}}\n",
                encoding="utf-8",
            )
            job = JobState("f" * 32, 1, root, input_path)
            parse_workflow_line(job, f"::workflow::panel_error::04::{logs / 'panel-04-attempt-1.log'}")
            parse_workflow_line(job, "::workflow::error::stage=商品展示图片生成：第 04 张/共 5 张::status=1")
            self.assertIn("第 04 张", job.error or "")
            self.assertIn("上游暂时不可用", job.error or "")
            self.assertNotIn("compatible accounts", job.error or "")

    def test_job_checkpoints_reconcile_images_and_persist_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.jpg"
            input_path.write_bytes(jpeg_bytes())
            job = JobState("d" * 32, 1, root, input_path)
            for number in range(1, 4):
                Image.new("RGB", (80, 80), "#ccbbaa").save(root / f"panel-{number:02d}.png")
            job.reconcile_steps()
            job.persist()
            state = json.loads(job.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual([state["steps"][f"display_{number:02d}"]["status"] for number in range(1, 6)],
                             ["success", "success", "success", "waiting", "waiting"])
            self.assertEqual(state["steps"]["display_01"]["path"], "panel-01.png")

            for number in range(4, 6):
                Image.new("RGB", (80, 80), "#ccbbaa").save(root / f"panel-{number:02d}.png")
            Image.new("RGB", (80, 240), "#eeeeee").save(root / "product-long.png")
            job.reconcile_steps()
            app_module.write_job_archive(job)
            self.assertTrue(job.valid_archive())
            self.assertEqual(job.steps["zip"]["status"], "success")

    def test_running_job_timeout_stops_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.jpg"
            input_path.write_bytes(jpeg_bytes())
            job = JobState("c" * 32, 1, root, input_path)
            job.persist()
            with (
                patch.object(app_module, "WORKFLOW_SCRIPT", ROOT / "tests/fake_web_workflow.py"),
                patch.object(app_module, "JOB_TIMEOUT_SECONDS", 1),
                patch.object(app_module, "PAYMENT_REQUIRED", False),
                patch.dict(os.environ, {"FAKE_WEB_WORKFLOW_DELAY": "2"}),
            ):
                asyncio.run(app_module.execute_job(job))
            self.assertEqual(job.status, "failed")
            self.assertIn("超时", job.stage)

    def test_resume_endpoint_requeues_same_job_and_preserves_successes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.jpg"
            input_path.write_bytes(jpeg_bytes())
            job = JobState("e" * 32, 7, root, input_path, status="failed", error="upstream timeout")
            for number in range(1, 4):
                Image.new("RGB", (80, 80), "#ccbbaa").save(root / f"panel-{number:02d}.png")
            job.reconcile_steps()
            job.persist()
            while not app_module.job_queue.empty():
                app_module.job_queue.get_nowait()
                app_module.job_queue.task_done()
            app_module.jobs[job.job_id] = job
            try:
                with patch.object(app_module, "PAYMENT_REQUIRED", False):
                    payload = asyncio.run(app_module.resume_job(job.job_id, User(7, "resume-user", "active")))
                self.assertEqual(payload["job_id"], job.job_id)
                self.assertEqual(payload["status"], "queued")
                self.assertEqual(job.resume_count, 1)
                self.assertEqual([job.steps[f"display_{number:02d}"]["status"] for number in range(1, 6)],
                                 ["success", "success", "success", "waiting", "waiting"])
                self.assertEqual(app_module.job_queue.get_nowait(), job.job_id)
                app_module.job_queue.task_done()
            finally:
                app_module.jobs.pop(job.job_id, None)

    def test_stale_queued_job_is_failed_by_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "wechat-1-test" / "job-stale"
            output.mkdir(parents=True)
            input_path = output / "input.jpg"
            input_path.write_bytes(jpeg_bytes())
            old = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
            job = JobState("b" * 32, 1, output, input_path, created_at=old, queued_at=old)
            job.persist()
            with (
                patch.object(app_module, "JOBS_ROOT", root),
                patch.object(app_module, "QUEUE_TIMEOUT_SECONDS", 1),
                patch.object(app_module, "PAYMENT_REQUIRED", False),
            ):
                app_module.jobs.clear()
                app_module.jobs[job.job_id] = job
                app_module.maintain_jobs()
            self.assertEqual(job.status, "failed")
            self.assertIn("排队超时", job.stage)
            app_module.jobs.clear()

    def test_running_job_is_recovered_into_persistent_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "wechat-1-test" / "job-20260810-000000-000000-recovery"
            output.mkdir(parents=True)
            input_path = output / "input.jpg"
            input_path.write_bytes(jpeg_bytes())
            job = JobState("a" * 32, 1, output, input_path, status="running", stage="生成中")
            job.persist()
            while not app_module.job_queue.empty():
                app_module.job_queue.get_nowait()
                app_module.job_queue.task_done()
            with patch.object(app_module, "JOBS_ROOT", root):
                app_module.jobs.clear()
                app_module.recover_persisted_jobs()
            recovered_id = app_module.job_queue.get_nowait()
            app_module.job_queue.task_done()
            self.assertEqual(recovered_id, job.job_id)
            self.assertEqual(app_module.jobs[job.job_id].status, "queued")
            self.assertIn("重新排队", app_module.jobs[job.job_id].stage)
            app_module.jobs.clear()


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
                "DB_DRIVER": "sqlite",
                "APP_OUTPUT_ROOT": cls.temporary.name,
                "WEB_WORKFLOW_SCRIPT": str(ROOT / "tests/fake_web_workflow.py"),
                "WECHAT_DEV_LOGIN": "true",
                "WECHAT_APP_ID": "test-app-id",
                "WECHAT_APP_SECRET": "test-app-secret",
                "COOKIE_SECURE": "false",
                "PAYMENT_REQUIRED": "false",
                "WEB_MAX_ACTIVE_JOBS": "2",
                "WEB_MAX_CONCURRENT_UPLOADS": "2",
                "FAKE_WEB_WORKFLOW_DELAY": "0.08",
                "RATE_LIMIT_DEV_LOGIN": "200",
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
        login = cls.client.post("/api/auth/wechat/dev-login", data={"openid": "test-openid-a"})
        if login.status_code != 200:
            cls.server.terminate()
            raise RuntimeError(f"test user login failed: {login.text}")

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
        response = self.client.post("/api/auth/wechat/dev-login", data={"openid": "test-openid-a"})
        self.assertEqual(response.status_code, 200)

    def test_frontend_is_served(self) -> None:
        response = self.client.get("/app")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Product Visual Studio", response.text)
        self.assertIn('id="previewDialog"', response.text)
        self.assertIn('id="expandPreview"', response.text)
        self.assertIn('id="saveDialog"', response.text)
        self.assertIn('id="rechargeDialog"', response.text)
        self.assertIn('href="/profile"', response.text)
        self.assertIn('class="mobile-nav"', response.text)
        self.assertIn('id="mobileBalanceBadge"', response.text)
        self.assertEqual(response.text.count("data-logout"), 2)
        self.assertIn("长按下方原图", response.text)
        self.assertIn('/app.js?v=', response.text)

        script = self.client.get("/app.js").text
        self.assertIn("navigator.canShare", script)
        self.assertIn("navigator.share", script)
        self.assertIn("openSaveDialog", script)
        self.assertIn("isMobileSaveContext", script)
        self.assertIn("hasAvailableUse", script)
        self.assertIn("requestedJobId", script)
        stylesheet = self.client.get("/app.css").text
        self.assertIn(".mobile-account-actions", stylesheet)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto auto", stylesheet)
        self.assertNotIn(".balance-badge { display: none", stylesheet)

        profile = self.client.get("/profile")
        self.assertEqual(profile.status_code, 200)
        self.assertIn("历史生成图片", profile.text)
        self.assertIn("充值记录", profile.text)
        self.assertIn("消费记录", profile.text)
        self.assertIn("取消排队", profile.text)

    def test_unauthenticated_root_shows_login_page(self) -> None:
        unauthenticated = httpx.Client(base_url=self.base_url, timeout=5, trust_env=False)
        try:
            response = unauthenticated.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("微信授权登录", response.text)
        finally:
            unauthenticated.close()

    def test_root_preserves_valid_wechat_login(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/app")
        self.assertEqual(self.client.get("/app").status_code, 200)

    def test_password_endpoints_are_removed(self) -> None:
        self.assertIn(self.client.post("/api/auth/login", data={"username": "x", "password": "x"}).status_code, {404, 405})
        self.assertIn(self.client.post("/api/auth/register", data={"username": "x", "password": "x"}).status_code, {404, 405})

    def test_wechat_login_starts_oauth_with_state_cookie(self) -> None:
        response = self.client.get("/api/auth/wechat/start?next=/profile")
        self.assertEqual(response.status_code, 302)
        self.assertIn("open.weixin.qq.com/connect/oauth2/authorize", response.headers["location"])
        self.assertIn("scope=snsapi_base", response.headers["location"])
        self.assertIn("wechat_oauth_state=", response.headers["set-cookie"])
        self.assertIn("lookbook_login_next=", response.headers["set-cookie"])
        self.assertIn("HttpOnly", response.headers["set-cookie"])

    def test_same_openid_can_keep_multiple_device_sessions(self) -> None:
        first_session = self.client.get("/api/session")
        self.assertEqual(first_session.status_code, 200)
        second = httpx.Client(base_url=self.base_url, timeout=5, trust_env=False)
        try:
            login = second.post("/api/auth/wechat/dev-login", data={"openid": "test-openid-a"})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(second.get("/api/session").status_code, 200)
            self.assertEqual(self.client.get("/api/session").status_code, 200)
            set_cookies = login.headers.get_list("set-cookie")
            self.assertTrue(any("Path=/jewelry-lookbook-sheet" in value and "Max-Age=0" in value for value in set_cookies))
            self.assertTrue(any("lookbook_session=" in value and "Path=/" in value and "Max-Age=604800" in value for value in set_cookies))
        finally:
            second.close()

    def test_unauthenticated_api_is_rejected(self) -> None:
        unauthenticated = httpx.Client(base_url=self.base_url, timeout=5, trust_env=False)
        try:
            self.assertEqual(unauthenticated.get("/api/session").status_code, 401)
            self.assertEqual(unauthenticated.get("/api/jobs/not-owned").status_code, 401)
            self.assertEqual(unauthenticated.post("/api/payment/authorization").status_code, 401)
            for path in ("/app", "/profile", "/pay"):
                page = unauthenticated.get(path)
                self.assertEqual(page.status_code, 303)
                self.assertTrue(page.headers["location"].startswith("/?next="))
        finally:
            unauthenticated.close()

    def test_admin_login_and_wechat_session_are_fully_isolated(self) -> None:
        # A valid WeChat user session is not an administrator session.
        self.assertEqual(self.client.get("/admin/api/users").status_code, 401)
        self.assertEqual(self.client.get("/admin").status_code, 303)
        self.assertEqual(self.client.get("/admin.html").status_code, 303)

        admin = httpx.Client(base_url=self.base_url, timeout=5, trust_env=False)
        try:
            self.assertEqual(
                admin.post("/admin/api/login", data={"username": "ltd", "password": "wrong"}).status_code,
                401,
            )
            login = admin.post(
                "/admin/api/login", data={"username": "ltd", "password": "ltd123456"}
            )
            self.assertEqual(login.status_code, 200)
            self.assertIn("lookbook_admin_session=", login.headers["set-cookie"])
            self.assertIn("Path=/admin", login.headers["set-cookie"])
            self.assertEqual(admin.get("/admin").status_code, 200)
            self.assertIn("商品视觉后台", admin.get("/admin").text)
            self.assertEqual(admin.get("/api/session").status_code, 401)

            users = admin.get("/admin/api/users?page=1&page_size=10")
            self.assertEqual(users.status_code, 200)
            self.assertTrue(any(item["openid"] == "test-openid-a" for item in users.json()["items"]))
            self.assertIn("remaining_uses", users.json()["items"][0])
            self.assertEqual(admin.get("/admin/api/payments").status_code, 200)
            self.assertEqual(admin.get("/admin/api/jobs").status_code, 200)

            self.assertEqual(admin.post("/admin/api/logout").status_code, 200)
            self.assertEqual(admin.get("/admin/api/users").status_code, 401)
        finally:
            admin.close()

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
        self.assertEqual(first.json()["openid_masked"], "***id-a")
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertFalse(first.json()["payment_required"])

        payment = self.client.get("/api/payment/status")
        self.assertEqual(payment.status_code, 200)
        self.assertTrue(payment.json()["paid"])
        self.assertEqual(
            [(item["id"], item["uses"], item["amount_cent"]) for item in payment.json()["packages"]],
            [("trial_1", 1, 1), ("small_5", 5, 2900), ("standard_10", 10, 5600),
             ("premium_20", 20, 10600), ("business_50", 50, 25000),
             ("enterprise_100", 100, 47500)],
        )

        authorization = self.client.post("/api/payment/authorization")
        self.assertEqual(authorization.status_code, 200)
        encoded, encoded_signature = authorization.json()["authorization"].split(".")
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(b"test-app-secret", encoded.encode("ascii"), hashlib.sha256).digest()
        self.assertTrue(hmac.compare_digest(signature, expected))
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(payload["sub"], "test-openid-a")
        self.assertEqual(len(payload["sid"]), 64)

        script = self.client.get("/app.js").text
        self.assertIn('fetch("/api/session", { cache: "no-store", credentials: "same-origin" })', script)
        payment_page = self.client.get("/pay").text
        self.assertIn("/api/payment/authorization", payment_page)
        self.assertIn("authorization:authorizationData.authorization", payment_page)
        self.assertIn("package_id:packageId", payment_page)
        self.assertNotIn("amount_cent:selected", payment_page)
        self.assertIn("function clearQr(){qr.classList.remove('visible');qrImage.removeAttribute('src')}", payment_page)
        self.assertIn("const packageChanged=selected!==''&&selected!==item.id", payment_page)
        self.assertIn("if(attempt!==paymentAttempt)return", payment_page)

    def test_frontend_restores_only_current_tab_job(self) -> None:
        response = self.client.get("/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("window.sessionStorage", response.text)
        self.assertIn("restoreCurrentJob", response.text)
        self.assertIn("data.input_url", response.text)
        self.assertNotIn("clearClientState();\n  try {\n    await initializeSession", response.text)
        self.assertIn("initializePage();", response.text)

    def test_completed_result_refreshes_badge_without_opening_recharge(self) -> None:
        script = self.client.get("/app.js").text
        show_result = script.split("function showResult(data)", 1)[1].split("function isMobileSaveContext", 1)[0]
        self.assertIn("initializeSession().catch", show_result)
        self.assertNotIn("hasAvailableUse()", show_result)

    def test_invalid_upload_is_rejected(self) -> None:
        response = self.client.post("/api/jobs", files={"image": ("bad.txt", b"not-an-image", "text/plain")})
        self.assertEqual(response.status_code, 400)

    def test_cross_site_state_change_is_rejected_and_security_headers_are_set(self) -> None:
        rejected = self.client.post(
            "/api/auth/logout",
            headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.client.get("/api/session").status_code, 200)
        page = self.client.get("/app")
        self.assertEqual(page.headers["x-content-type-options"], "nosniff")
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertEqual(page.headers["referrer-policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])

    def test_dev_login_is_not_exposed_on_a_public_host(self) -> None:
        response = self.client.post(
            "/api/auth/wechat/dev-login",
            headers={"Host": "public.example"},
            data={"openid": "should-not-exist"},
        )
        self.assertEqual(response.status_code, 404)

    def test_uploaded_image_is_reencoded_without_trailing_payload(self) -> None:
        payload = jpeg_bytes() + b"<script>alert('polyglot')</script>"
        response = self.client.post(
            "/api/jobs", files={"image": ("polyglot.jpg", payload, "image/jpeg")},
        )
        self.assertEqual(response.status_code, 202)
        stored = self.client.get(response.json()["input_url"])
        self.assertEqual(stored.status_code, 200)
        self.assertNotIn(b"polyglot", stored.content)
        self.assertLess(len(stored.content), len(payload))

    def test_connection_error_is_safe_for_frontend(self) -> None:
        error = public_job_error(RuntimeError("traceback\nopenai.APIConnectionError: Connection error."))
        self.assertIn("图片生成服务连接失败", error)
        self.assertNotIn("traceback", error)

    def test_analysis_error_uses_durable_stage_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            input_path = root / "input.jpg"
            input_path.write_bytes(jpeg_bytes())
            job = JobState("f" * 32, 1, root, input_path)
            job.set_step("analysis", "failed", error="failed")
            (root / "logs/analysis.log").write_text(
                "openai.RateLimitError: Error code: 429", encoding="utf-8"
            )
            error = job_failure_error(job, RuntimeError("generic workflow exit"))
            self.assertIn("请求过于频繁", error)
            self.assertNotIn("generic workflow exit", error)

    def test_generic_shell_stage_error_does_not_hide_analysis_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            input_path = root / "input.jpg"
            input_path.write_bytes(jpeg_bytes())
            job = JobState("e" * 32, 1, root, input_path)
            job.set_step("analysis", "failed", error="failed")
            job.error = "失败阶段：商品信息分析。详细信息：stage=商品信息分析::status=1"
            (root / "logs/analysis.log").write_text(
                "openai.APIConnectionError: Connection error.", encoding="utf-8"
            )
            error = job_failure_error(job, RuntimeError("generic workflow exit"))
            self.assertIn("商品分析服务连接失败", error)
            self.assertNotIn("status=1", error)

    def test_permission_error_is_not_misreported_as_stale_analysis_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            input_path = root / "input.jpg"
            input_path.write_bytes(jpeg_bytes())
            job = JobState("p" * 32, 1, root, input_path)
            job.set_step("analysis", "failed", error="failed")
            (root / "logs/analysis.log").write_text(
                "APITimeoutError: Request timed out\nPermissionError: [Errno 13] Permission denied",
                encoding="utf-8",
            )
            error = job_failure_error(job, RuntimeError("generic workflow exit"))
            self.assertIn("输出目录不可写", error)
            self.assertNotIn("商品分析服务请求超时", error)

    def test_unsupported_apparel_error_is_user_facing(self) -> None:
        error = public_job_error(RuntimeError(
            "traceback\nValueError: UNSUPPORTED_PRODUCT: 暂不支持服装类商品\n::workflow::error::internal"
        ))
        self.assertEqual(error, "暂不支持服装类商品")

    def test_structured_product_and_panel_events_update_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.jpg"
            input_path.write_bytes(jpeg_bytes())
            job = JobState(job_id="event-test", user_id=1, output_dir=root, input_path=input_path)
            parse_workflow_line(job, '::workflow::product::{"category_group":"bags","subcategory":"手提包","product_name":"测试手提包"}')
            parse_workflow_line(job, "::workflow::panel_ready::01::5")
            self.assertEqual(job.subcategory, "手提包")
            self.assertEqual(job.public_data()["product"]["product_name"], "测试手提包")
            self.assertEqual(job.panels, {"01"})
            self.assertIn("1/5", job.stage)

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
        self.assertEqual(len(terminal["images"]), 6)

        account = self.client.get("/api/account")
        self.assertEqual(account.status_code, 200)
        self.assertEqual(account.json()["user"]["openid_masked"], "***id-a")
        self.assertTrue(any(item["job_id"] == job_id for item in account.json()["generation_records"]))
        history_item = next(item for item in account.json()["generation_records"] if item["job_id"] == job_id)
        self.assertEqual(history_item["download_url"], f"/api/jobs/{job_id}/download")
        self.assertTrue(history_item["download_transfer_url"].startswith("/api/download-transfer/"))
        self.assertGreater(history_item["download_transfer_expires_in"], 0)
        self.assertEqual(history_item["images"], terminal["images"])
        self.assertTrue(history_item["product"]["subcategory"])

        events = self.client.get(f"/api/jobs/{job_id}/events")
        self.assertEqual(events.status_code, 200)
        event_data = json.loads(events.text.split("data: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(event_data["status"], "completed")

        result = self.client.get(terminal["result_url"])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["content-type"], "image/png")
        self.assertNotIn("attachment", result.headers.get("content-disposition", ""))

        download = self.client.get(terminal["download_url"])
        self.assertEqual(download.status_code, 200)
        disposition = download.headers["content-disposition"]
        self.assertIn("attachment", disposition)
        self.assertIn(f'filename="product-images-{job_id[:8]}.zip"', disposition)
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertEqual(download.headers["content-type"], "application/zip")
        self.assertEqual(download.headers["content-transfer-encoding"], "binary")
        self.assertEqual(download.headers["x-content-type-options"], "nosniff")
        self.assertEqual(download.headers["accept-ranges"], "bytes")
        self.assertEqual(download.headers["cache-control"], "no-store, no-cache, must-revalidate, max-age=0")
        self.assertEqual(int(download.headers["content-length"]), len(download.content))
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            self.assertEqual(archive.namelist(), [
                "商品展示图_01.png", "商品展示图_02.png", "商品展示图_03.png",
                "商品展示图_04.png", "商品展示图_05.png", "商品信息长图.png",
            ])
            self.assertEqual(len(archive.infolist()), 6)

        immediate_cookie_free = httpx.get(
            f"{self.base_url}{terminal['download_transfer_url']}", timeout=5, trust_env=False,
        )
        self.assertEqual(immediate_cookie_free.status_code, 200)
        self.assertEqual(immediate_cookie_free.content, download.content)

        handoff_url = terminal["download_transfer_url"].replace(
            "/api/download-transfer/", "/download-open/",
        )
        wechat_handoff = httpx.get(
            f"{self.base_url}{handoff_url}",
            headers={"User-Agent": "Mozilla/5.0 MicroMessenger Android"},
            timeout=5, trust_env=False,
        )
        self.assertEqual(wechat_handoff.status_code, 200)
        self.assertIn("在浏览器打开", wechat_handoff.text)
        external_handoff = httpx.get(
            f"{self.base_url}{handoff_url}", follow_redirects=False, timeout=5, trust_env=False,
        )
        self.assertEqual(external_handoff.status_code, 302)
        self.assertEqual(external_handoff.headers["location"], terminal["download_transfer_url"])

        transfer = self.client.post(f"/api/jobs/{job_id}/download-transfer")
        self.assertEqual(transfer.status_code, 200)
        transfer_url = transfer.json()["transfer_url"]
        cookie_free = httpx.get(f"{self.base_url}{transfer_url}", timeout=5, trust_env=False)
        self.assertEqual(cookie_free.status_code, 200)
        self.assertEqual(cookie_free.headers["content-type"], "application/zip")
        self.assertEqual(cookie_free.content, download.content)
        with zipfile.ZipFile(io.BytesIO(cookie_free.content)) as archive:
            self.assertEqual(len(archive.infolist()), 6)
        tampered = httpx.get(f"{self.base_url}{transfer_url}x", timeout=5, trust_env=False)
        self.assertEqual(tampered.status_code, 403)
        tampered_handoff = httpx.get(
            f"{self.base_url}{handoff_url}x", follow_redirects=False, timeout=5, trust_env=False,
        )
        self.assertEqual(tampered_handoff.status_code, 403)

        for image in terminal["images"]:
            preview = self.client.get(image["url"])
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.headers["content-type"], "image/png")
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}/images/not-allowed").status_code, 404)

        matching_jobs = []
        for metadata_path in Path(self.temporary.name).glob("wechat-*/job-*/job-state.json"):
            if json.loads(metadata_path.read_text(encoding="utf-8"))["job_id"] == job_id:
                matching_jobs.append(metadata_path.parent)
        self.assertEqual(len(matching_jobs), 1)
        current_job_dir = matching_jobs[0]
        user_output_root = current_job_dir.parent
        self.assertTrue((current_job_dir / "input.jpg").is_file())

        self.assertTrue((current_job_dir / "product-long.png").is_file())
        self.assertTrue((current_job_dir / "job-state.json").is_file())
        self.assertFalse(list(Path(self.temporary.name).glob("job-*")))

        archive_path = current_job_dir / "product-images.zip"
        saved_archive_path = current_job_dir / "product-images.zip.saved"
        archive_path.replace(saved_archive_path)
        try:
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/download-transfer").status_code, 409)
            missing_original = httpx.get(f"{self.base_url}{transfer_url}", timeout=5, trust_env=False)
            self.assertEqual(missing_original.status_code, 409)
            self.assertFalse(archive_path.exists())
        finally:
            saved_archive_path.replace(archive_path)

        other = httpx.Client(base_url=self.base_url, timeout=5, trust_env=False)
        try:
            login = other.post("/api/auth/wechat/dev-login", data={"openid": "test-openid-b"})
            self.assertEqual(login.status_code, 200)
            self.assertNotEqual(login.json()["id"], 1)
            self.assertEqual(other.get(f"/api/jobs/{job_id}").status_code, 404)
            self.assertEqual(other.get(terminal["result_url"]).status_code, 404)
            self.assertEqual(other.get(terminal["download_url"]).status_code, 404)
            self.assertEqual(other.post(f"/api/jobs/{job_id}/download-transfer").status_code, 404)
            for image in terminal["images"]:
                self.assertEqual(other.get(image["url"]).status_code, 404)
            other_account = other.get("/api/account").json()
            self.assertFalse(any(item["job_id"] == job_id for item in other_account["generation_records"]))

            own = other.post("/api/jobs", files={"image": ("other.jpg", jpeg_bytes(), "image/jpeg")})
            self.assertEqual(own.status_code, 202)
            other_job_id = own.json()["job_id"]
            other_metadata = next(
                path for path in Path(self.temporary.name).glob("wechat-*/job-*/job-state.json")
                if json.loads(path.read_text(encoding="utf-8"))["job_id"] == other_job_id
            )
            self.assertNotEqual(other_metadata.parent.parent, user_output_root)

        finally:
            other.close()

    def test_multiple_users_upload_generate_poll_and_download_concurrently(self) -> None:
        clients = [httpx.Client(base_url=self.base_url, timeout=10, trust_env=False) for _ in range(4)]
        try:
            for index, client in enumerate(clients):
                login = client.post("/api/auth/wechat/dev-login", data={"openid": f"concurrent-user-{index}"})
                self.assertEqual(login.status_code, 200)

            def upload(item: tuple[int, httpx.Client]):
                index, client = item
                return client.post(
                    "/api/jobs",
                    files={"image": (f"concurrent-{index}.jpg", jpeg_bytes(), "image/jpeg")},
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(clients)) as executor:
                responses = list(executor.map(upload, enumerate(clients)))
            self.assertEqual([response.status_code for response in responses], [202] * len(clients))
            job_ids = [response.json()["job_id"] for response in responses]
            self.assertEqual(len(set(job_ids)), len(job_ids))

            # An authenticated user can see only its own task, even while all
            # jobs share the same worker queue and are changing state.
            for owner_index, client in enumerate(clients):
                for job_index, job_id in enumerate(job_ids):
                    expected = 200 if owner_index == job_index else 404
                    self.assertEqual(client.get(f"/api/jobs/{job_id}").status_code, expected)

            maximum_running = 0
            terminal_states: list[str] = []
            for _ in range(300):
                terminal_states = [
                    clients[index].get(f"/api/jobs/{job_id}").json()["status"]
                    for index, job_id in enumerate(job_ids)
                ]
                maximum_running = max(maximum_running, terminal_states.count("running"))
                if all(state in {"completed", "failed"} for state in terminal_states):
                    break
                time.sleep(0.01)
            self.assertGreaterEqual(maximum_running, 2)
            self.assertEqual(terminal_states, ["completed"] * len(clients))

            def download(item: tuple[int, httpx.Client]):
                index, client = item
                return client.get(f"/api/jobs/{job_ids[index]}/download")

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(clients)) as executor:
                downloads = list(executor.map(download, enumerate(clients)))
            self.assertEqual([response.status_code for response in downloads], [200] * len(clients))
            for response in downloads:
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    self.assertEqual(len(archive.infolist()), 6)
        finally:
            for client in clients:
                client.close()

    def test_z_two_uploads_are_accepted_by_fifo_queue(self) -> None:
        login = self.client.post("/api/auth/wechat/dev-login", data={"openid": "queue-test-openid"})
        self.assertEqual(login.status_code, 200)
        responses = [
            self.client.post("/api/jobs", files={"image": (f"item-{index}.jpg", jpeg_bytes(), "image/jpeg")})
            for index in range(3)
        ]
        self.assertEqual([response.status_code for response in responses], [202, 202, 202])
        job_ids = [response.json()["job_id"] for response in responses]
        cancelled = self.client.post(f"/api/jobs/{job_ids[2]}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "failed")
        states = []
        for _ in range(150):
            states = [self.client.get(f"/api/jobs/{job_id}").json()["status"] for job_id in job_ids[:2]]
            if states == ["completed", "completed"]:
                break
            time.sleep(0.02)
        self.assertEqual(states, ["completed", "completed"])


class PaymentModelTests(unittest.TestCase):
    def test_admin_session_is_invalidated_when_credentials_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AuthStore(DatabaseConfig(driver="sqlite", sqlite_path=Path(temporary) / "admin.db"))
            token = store.create_admin_session("credential-fingerprint-a")
            self.assertEqual(
                store.admin_for_token(token, "credential-fingerprint-a", "admin").username,
                "admin",
            )
            self.assertIsNone(store.admin_for_token(token, "credential-fingerprint-b", "admin"))

    def test_new_wechat_user_is_unpaid_and_schema_contains_order_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "payment.db"
            store = AuthStore(DatabaseConfig(driver="sqlite", sqlite_path=database_path))
            user = store.find_or_create_user("payment-openid")
            self.assertEqual(user.pay_status, "unpaid")
            connection = store.connect()
            try:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertIn("wx_user", tables)
                self.assertIn("pay_order", tables)
                columns = {row[1] for row in connection.execute("PRAGMA table_info(wx_user)")}
                self.assertIn("use_credits", columns)
                self.assertIn("balance_cent", columns)
                self.assertIn("generation_charge", tables)
                self.assertIn("credit_lot", tables)
                self.assertIn("admin_sessions", tables)
                order_columns = {row[1] for row in connection.execute("PRAGMA table_info(pay_order)")}
                self.assertIn("session_token_hash", order_columns)
                self.assertIn("order_status", order_columns)
                self.assertTrue({"package_id", "package_name", "credits"} <= order_columns)
            finally:
                connection.close()

    def test_account_credits_are_reserved_and_charged_once_per_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "payment.db"
            store = AuthStore(DatabaseConfig(driver="sqlite", sqlite_path=database_path))
            user = store.find_or_create_user("pay-per-use-openid")
            token = store.create_session(user.id)
            with store.connect() as connection:
                connection.execute("UPDATE wx_user SET balance_cent = 2, use_credits = 2, pay_status = 1 WHERE openid = ?", (user.openid,))
                connection.commit()

            session_user = store.user_for_token(token)
            self.assertIsNotNone(session_user)
            self.assertEqual(session_user.service_status, "paid")
            self.assertTrue(store.consume_service_order(session_user, "job-one"))
            self.assertTrue(store.consume_service_order(session_user, "job-two"))
            self.assertFalse(store.consume_service_order(session_user, "job-three"))
            self.assertTrue(store.mark_service_order_consumed("job-one"))
            self.assertTrue(store.finalize_generation("job-two", success=False))
            refreshed = store.user_for_token(token)
            self.assertEqual(refreshed.remaining_uses, 1)
            self.assertEqual(refreshed.balance_cent, 1)
            self.assertTrue(store.consume_service_order(refreshed, "job-three"))
            self.assertEqual([item["job_id"] for item in store.reserved_generations()], ["job-three"])
            self.assertTrue(store.finalize_generation("job-three", success=True))
            exhausted = store.user_for_token(token)
            self.assertEqual(exhausted.remaining_uses, 0)
            self.assertEqual(exhausted.balance_cent, 0)
            self.assertEqual(exhausted.service_status, "unpaid")
            with store.connect() as connection:
                charges = connection.execute("SELECT job_id, status FROM generation_charge ORDER BY job_id").fetchall()
            self.assertEqual([(row["job_id"], row["status"]) for row in charges], [("job-one", "completed"), ("job-three", "completed"), ("job-two", "released")])

    def test_resumed_job_reuses_charge_record_and_finalization_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AuthStore(DatabaseConfig(driver="sqlite", sqlite_path=Path(temporary) / "payment.db"))
            user = store.find_or_create_user("resume-payment-openid")
            token = store.create_session(user.id)
            with store.connect() as connection:
                connection.execute("UPDATE wx_user SET balance_cent = 1, use_credits = 1, pay_status = 1 WHERE openid = ?", (user.openid,))
                connection.commit()
            self.assertTrue(store.reserve_generation(user.openid, "same-resumed-job"))
            self.assertTrue(store.finalize_generation("same-resumed-job", success=False))
            self.assertTrue(store.resume_generation(user.openid, "same-resumed-job"))
            self.assertTrue(store.finalize_generation("same-resumed-job", success=True))
            self.assertTrue(store.finalize_generation("same-resumed-job", success=True))
            refreshed = store.user_for_token(token)
            self.assertEqual((refreshed.remaining_uses, refreshed.balance_cent), (0, 0))
            with store.connect() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM generation_charge WHERE job_id = 'same-resumed-job'"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_new_package_keeps_money_and_generation_credits_synchronized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AuthStore(DatabaseConfig(driver="sqlite", sqlite_path=Path(temporary) / "payment.db"))
            user = store.find_or_create_user("small-package-openid")
            token = store.create_session(user.id)
            with store.connect() as connection:
                connection.execute(
                    "UPDATE wx_user SET balance_cent = 2900, use_credits = 5, pay_status = 1 WHERE openid = ?",
                    (user.openid,),
                )
                connection.execute(
                    "INSERT INTO credit_lot(order_id, openid, package_id, total_uses, remaining_uses, "
                    "amount_cent, remaining_amount_cent, created_at) VALUES (?, ?, ?, 5, 5, 2900, 2900, CURRENT_TIMESTAMP)",
                    ("new-small-order", user.openid, "small_5"),
                )
                connection.commit()
            self.assertTrue(store.reserve_generation(user.openid, "small-package-job"))
            self.assertTrue(store.finalize_generation("small-package-job", success=True))
            refreshed = store.user_for_token(token)
            self.assertEqual(refreshed.remaining_uses, 4)
            self.assertEqual(refreshed.balance_cent, 2320)
            charge = store.account_summary(user.openid)["consumption_records"][0]
            self.assertEqual(charge["amount_cent"], 580)

    def test_package_configuration_matches_commercial_pricing(self) -> None:
        self.assertEqual(
            [(item["id"], item["uses"], item["amount_cent"]) for item in PAYMENT_PACKAGES],
            [("trial_1", 1, 1), ("small_5", 5, 2900), ("standard_10", 10, 5600),
             ("premium_20", 20, 10600), ("business_50", 50, 25000),
             ("enterprise_100", 100, 47500)],
        )

    def test_existing_account_credit_is_retired_and_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "legacy-payment.db"
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE wx_user (id INTEGER PRIMARY KEY, openid TEXT UNIQUE, "
                "pay_status INTEGER, create_time TEXT, update_time TEXT, status INTEGER)"
            )
            connection.execute(
                "INSERT INTO wx_user VALUES (1, 'legacy-paid', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)"
            )
            connection.commit()
            connection.close()

            store = AuthStore(DatabaseConfig(driver="sqlite", sqlite_path=database_path))
            with store.connect() as connection:
                credits = connection.execute(
                    "SELECT use_credits FROM wx_user WHERE openid = 'legacy-paid'"
                ).fetchone()[0]
            self.assertEqual(credits, 0)

    def test_account_credit_persists_across_login_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AuthStore(DatabaseConfig(driver="sqlite", sqlite_path=Path(temporary) / "payment.db"))
            user = store.find_or_create_user("session-isolated-openid")
            old_token = store.create_session(user.id)
            with store.connect() as connection:
                connection.execute("UPDATE wx_user SET balance_cent = 1, use_credits = 1, pay_status = 1 WHERE openid = ?", (user.openid,))
                connection.commit()
            self.assertEqual(store.user_for_token(old_token).service_status, "paid")

            new_token = store.create_session(user.id)
            refreshed = store.user_for_token(new_token)
            self.assertEqual(refreshed.service_status, "paid")
            self.assertIsNotNone(store.user_for_token(old_token))
            self.assertTrue(store.consume_service_order(refreshed, "cross-session-job"))

    def test_recharge_history_contains_only_paid_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AuthStore(DatabaseConfig(driver="sqlite", sqlite_path=Path(temporary) / "payment.db"))
            user = store.find_or_create_user("paid-history-openid")
            with store.connect() as connection:
                connection.execute(
                    "INSERT INTO pay_order(order_id, openid, total_fee, order_status, pay_status) VALUES (?, ?, 5, 'pending', 0)",
                    ("pending-order", user.openid),
                )
                connection.execute(
                    "INSERT INTO pay_order(order_id, openid, total_fee, order_status, pay_status, pay_time) VALUES (?, ?, 10, 'paid', 1, CURRENT_TIMESTAMP)",
                    ("paid-order", user.openid),
                )
                connection.commit()
            records = store.account_summary(user.openid)["recharge_records"]
            self.assertEqual([item["order_id"] for item in records], ["paid-order"])

    def test_paid_dependency_rejects_unpaid_user(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            paid_user(User(1, "openid-unpaid", "active", "unpaid"))
        self.assertEqual(raised.exception.status_code, 402)
        paid = User(1, "openid-paid", "active", "paid", balance_cent=1, remaining_uses=1)
        self.assertEqual(paid_user(paid), paid)


if __name__ == "__main__":
    unittest.main()
