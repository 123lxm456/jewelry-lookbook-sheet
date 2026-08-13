from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import signal
import time
import uuid
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from auth import (
    ADMIN_SESSION_COOKIE,
    SESSION_COOKIE,
    Admin,
    AuthStore,
    DatabaseConfig,
    User,
    clear_admin_session_cookie,
    clear_oauth_state_cookie,
    clear_session_cookie,
    current_user,
    set_admin_session_cookie,
    set_oauth_state_cookie,
    set_session_cookie,
    valid_oauth_state,
)


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
WEB_ROOT = ROOT / "web"
JOBS_ROOT = Path(os.environ.get("APP_OUTPUT_ROOT", ROOT / "outputs")).resolve()
DATABASE_CONFIG = DatabaseConfig.from_environment(ROOT)
WORKFLOW_SCRIPT = Path(os.environ.get("WEB_WORKFLOW_SCRIPT", ROOT / "run_workflow.sh")).resolve()
MAX_UPLOAD_BYTES = int(os.environ.get("WEB_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
MAX_IMAGE_PIXELS = int(os.environ.get("WEB_MAX_IMAGE_PIXELS", "40000000"))
MAX_ACTIVE_JOBS = int(os.environ.get("WEB_MAX_ACTIVE_JOBS", "2"))
MAX_QUEUED_JOBS = int(os.environ.get("WEB_MAX_QUEUED_JOBS", "100"))
JOB_TIMEOUT_SECONDS = int(os.environ.get("WEB_JOB_TIMEOUT_SECONDS", "3600"))
QUEUE_TIMEOUT_SECONDS = int(os.environ.get("WEB_QUEUE_TIMEOUT_SECONDS", "7200"))
ORPHAN_RESERVATION_TIMEOUT_SECONDS = int(os.environ.get("WEB_ORPHAN_RESERVATION_TIMEOUT_SECONDS", "86400"))
MAINTENANCE_INTERVAL_SECONDS = int(os.environ.get("WEB_MAINTENANCE_INTERVAL_SECONDS", "30"))
if MAX_ACTIVE_JOBS < 1:
    raise RuntimeError("WEB_MAX_ACTIVE_JOBS must be at least 1")
if MAX_QUEUED_JOBS < MAX_ACTIVE_JOBS:
    raise RuntimeError("WEB_MAX_QUEUED_JOBS must be at least WEB_MAX_ACTIVE_JOBS")
if min(JOB_TIMEOUT_SECONDS, QUEUE_TIMEOUT_SECONDS, ORPHAN_RESERVATION_TIMEOUT_SECONDS, MAINTENANCE_INTERVAL_SECONDS) < 1:
    raise RuntimeError("Web task timeout and maintenance settings must be positive")
ALLOWED_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
WEB_SESSION_ID = uuid.uuid4().hex
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}
WECHAT_APP_ID = os.environ.get("WECHAT_APP_ID", "")
WECHAT_APP_SECRET = os.environ.get("WECHAT_APP_SECRET", "")
WECHAT_REDIRECT_URI = os.environ.get("WECHAT_REDIRECT_URI", "")
WECHAT_DEV_LOGIN = os.environ.get("WECHAT_DEV_LOGIN", "false").lower() in {"1", "true", "yes", "on"}
PAYMENT_REQUIRED = os.environ.get("PAYMENT_REQUIRED", "true").lower() in {"1", "true", "yes", "on"}
PAY_CREATE_URL = os.environ.get("PAY_CREATE_URL", "/jewelry-lookbook-sheet/pay.php")
LOGIN_NEXT_COOKIE = "lookbook_login_next"
LOGIN_TARGETS = {"/app", "/profile", "/pay"}
PAY_BRIDGE_SECRET = os.environ.get("PAY_BRIDGE_SECRET", "") or WECHAT_APP_SECRET
DOWNLOAD_TRANSFER_SECRET = os.environ.get("DOWNLOAD_TRANSFER_SECRET", "") or PAY_BRIDGE_SECRET
DOWNLOAD_TRANSFER_TTL_SECONDS = int(os.environ.get("DOWNLOAD_TRANSFER_TTL_SECONDS", "600"))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "ltd")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ltd123456")
ADMIN_PAGE_SIZE_MAX = 100


def load_payment_packages() -> list[dict[str, object]]:
    path = ROOT / "config/payment_packages.json"
    packages = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(packages, list) or not packages:
        raise RuntimeError("充值套餐配置不能为空")
    seen: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            raise RuntimeError("充值套餐配置格式无效")
        package_id = str(package.get("id", ""))
        uses = int(package.get("uses", 0))
        amount_cent = int(package.get("amount_cent", 0))
        if not package_id or package_id in seen or uses < 1 or amount_cent < 1:
            raise RuntimeError("充值套餐 ID、次数或金额无效")
        if amount_cent % uses != 0:
            raise RuntimeError(f"充值套餐 {package_id} 的金额必须能按次数精确扣减")
        seen.add(package_id)
    return packages


PAYMENT_PACKAGES = load_payment_packages()


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_download_transfer_token(job_id: str, user_id: int) -> str:
    if not DOWNLOAD_TRANSFER_SECRET:
        raise HTTPException(status_code=503, detail="跨浏览器下载密钥尚未配置")
    payload = _urlsafe_encode(json.dumps({
        "v": 1,
        "job": job_id,
        "uid": user_id,
        "exp": int(time.time()) + DOWNLOAD_TRANSFER_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(8),
    }, separators=(",", ":")).encode("utf-8"))
    signature = _urlsafe_encode(hmac.new(
        DOWNLOAD_TRANSFER_SECRET.encode("utf-8"), payload.encode("ascii"), hashlib.sha256,
    ).digest())
    return f"{payload}.{signature}"


def verify_download_transfer_token(token: str) -> tuple[str, int]:
    if not DOWNLOAD_TRANSFER_SECRET:
        raise HTTPException(status_code=503, detail="跨浏览器下载密钥尚未配置")
    try:
        payload, supplied_signature = token.split(".", 1)
        expected_signature = _urlsafe_encode(hmac.new(
            DOWNLOAD_TRANSFER_SECRET.encode("utf-8"), payload.encode("ascii"), hashlib.sha256,
        ).digest())
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("signature mismatch")
        data = json.loads(_urlsafe_decode(payload))
        job_id = str(data["job"])
        user_id = int(data["uid"])
        expires_at = int(data["exp"])
        if data.get("v") != 1 or not re_full_job_id(job_id) or expires_at < int(time.time()):
            raise ValueError("invalid or expired transfer")
        if expires_at > int(time.time()) + DOWNLOAD_TRANSFER_TTL_SECONDS + 60:
            raise ValueError("invalid transfer lifetime")
        return job_id, user_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError, base64.binascii.Error):
        raise HTTPException(status_code=403, detail="下载链接无效或已过期") from None


def re_full_job_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


@dataclass
class JobState:
    job_id: str
    user_id: int
    output_dir: Path
    input_path: Path
    status: str = "queued"
    progress: int = 2
    stage: str = "任务已创建"
    error: str | None = None
    category_group: str | None = None
    subcategory: str | None = None
    product_name: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    queued_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    panels: set[str] = field(default_factory=set)
    steps: dict[str, dict[str, object]] = field(default_factory=dict)
    recoverable: bool = True
    resume_count: int = 0
    log_tail: deque[str] = field(default_factory=lambda: deque(maxlen=30))
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    @property
    def metadata_path(self) -> Path:
        return self.output_dir / "job-state.json"

    @property
    def result_path(self) -> Path:
        canonical = self.output_dir / "product-long.png"
        return canonical if canonical.is_file() else self.output_dir / "jewelry-long.png"

    @property
    def archive_path(self) -> Path:
        return self.output_dir / "product-images.zip"

    def ensure_steps(self) -> None:
        defaults = {
            "analysis": ("商品信息分析", "product-spec.json"),
            "assets": ("展示方案与文案", "display-plan.json"),
            **{
                f"display_{number:02d}": (f"商品展示图 {number}/5", f"panel-{number:02d}.png")
                for number in range(1, 6)
            },
            "long_image": ("商品信息长图", "product-long.png"),
            "zip": ("ZIP 文件", "product-images.zip"),
        }
        for key, (label, path) in defaults.items():
            current = self.steps.setdefault(key, {})
            current.setdefault("label", label)
            current.setdefault("status", "waiting")
            current.setdefault("path", path)

    def set_step(self, key: str, status: str, *, error: str | None = None) -> None:
        if status not in {"waiting", "running", "success", "failed"}:
            raise ValueError(f"Unsupported step status: {status}")
        self.ensure_steps()
        step = self.steps[key]
        step["status"] = status
        step["updated_at"] = datetime.now(timezone.utc).isoformat()
        if error:
            step["error"] = error
        elif status != "failed":
            step.pop("error", None)

    @staticmethod
    def valid_image(path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            with Image.open(path) as image:
                image.verify()
            return True
        except (UnidentifiedImageError, OSError):
            return False

    def valid_archive(self) -> bool:
        if not self.archive_path.is_file():
            return False
        try:
            with zipfile.ZipFile(self.archive_path) as archive:
                return archive.testzip() is None and archive.namelist() == [filename for _, filename, _ in self.image_paths]
        except (OSError, zipfile.BadZipFile):
            return False

    def reconcile_steps(self) -> None:
        """Rebuild durable checkpoints from verified artifacts after a restart."""
        self.ensure_steps()
        artifacts = {
            "analysis": self.output_dir / "product-spec.json",
            "assets": self.output_dir / "display-plan.json",
            **{f"display_{number:02d}": self.output_dir / f"panel-{number:02d}.png" for number in range(1, 6)},
            "long_image": self.result_path,
        }
        for key, path in artifacts.items():
            valid = self.valid_image(path) if key.startswith("display_") or key == "long_image" else path.is_file() and path.stat().st_size > 0
            if valid:
                self.set_step(key, "success")
            elif self.steps[key].get("status") in {"running", "success"}:
                self.set_step(key, "waiting")
        if self.valid_archive():
            self.set_step("zip", "success")
        elif self.steps["zip"].get("status") in {"running", "success"}:
            self.set_step("zip", "waiting")
        self.panels = {
            f"{number:02d}" for number in range(1, 6)
            if self.steps[f"display_{number:02d}"]["status"] == "success"
        }

    def fail_running_steps(self, error: str) -> None:
        self.ensure_steps()
        for key, step in self.steps.items():
            if step.get("status") == "running":
                self.set_step(key, "failed", error=error)

    def reset_failed_steps(self) -> None:
        quarantine = self.output_dir / "work" / "recovery-invalid"
        candidates = [self.output_dir / f"panel-{number:02d}.png" for number in range(1, 6)]
        candidates.append(self.output_dir / "product-long.png")
        for path in candidates:
            if path.exists() and not self.valid_image(path):
                quarantine.mkdir(parents=True, exist_ok=True)
                path.replace(quarantine / f"{path.name}.{uuid.uuid4().hex[:8]}")
        if self.archive_path.exists() and not self.valid_archive():
            quarantine.mkdir(parents=True, exist_ok=True)
            self.archive_path.replace(quarantine / f"{self.archive_path.name}.{uuid.uuid4().hex[:8]}")
        self.reconcile_steps()
        for key, step in self.steps.items():
            if step.get("status") == "failed":
                self.set_step(key, "waiting")

    @property
    def image_paths(self) -> list[tuple[str, str, Path]]:
        """The only six publishable artifacts for a completed task."""
        images = [
            (f"display-{number:02d}", f"商品展示图_{number:02d}.png", self.output_dir / f"panel-{number:02d}.png")
            for number in range(1, 6)
        ]
        images.append(("final", "商品信息长图.png", self.result_path))
        return images

    def published_images(self) -> list[dict[str, str]]:
        return [
            {
                "id": image_id,
                "type": "final" if image_id == "final" else "display",
                "label": "最终商品信息长图" if image_id == "final" else filename.removesuffix(".png"),
                "url": f"/api/jobs/{self.job_id}/images/{image_id}",
            }
            for image_id, filename, _ in self.image_paths
        ]

    def has_complete_image_set(self) -> bool:
        return all(self.valid_image(path) for _, _, path in self.image_paths)

    def update(self, *, progress: int | None = None, stage: str | None = None, status: str | None = None) -> None:
        if progress is not None:
            self.progress = max(self.progress, min(progress, 100))
        if stage is not None:
            self.stage = stage
        if status is not None:
            self.status = status
        self.persist()
        self.changed.set()

    def persist(self) -> None:
        self.ensure_steps()
        self.updated_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "job_id": self.job_id, "user_id": self.user_id,
            "status": self.status, "progress": self.progress,
            "stage": self.stage, "error": self.error,
            "created_at": self.created_at, "updated_at": self.updated_at, "queued_at": self.queued_at,
            "category_group": self.category_group, "subcategory": self.subcategory,
            "product_name": self.product_name,
            "log_tail": list(self.log_tail), "panels": sorted(self.panels),
            "steps": self.steps, "recoverable": self.recoverable,
            "resume_count": self.resume_count,
            "input_name": self.input_path.name,
            "product": {
                "category_group": self.category_group,
                "subcategory": self.subcategory,
                "product_name": self.product_name,
            } if self.category_group else None,
        }
        temporary = self.metadata_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.metadata_path)

    def public_data(self) -> dict[str, object]:
        complete = self.status == "completed"
        transfer_url = (
            f"/api/download-transfer/{create_download_transfer_token(self.job_id, self.user_id)}"
            if complete and DOWNLOAD_TRANSFER_SECRET else None
        )
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "input_name": self.input_path.name,
            "product": {
                "category_group": self.category_group,
                "subcategory": self.subcategory,
                "product_name": self.product_name,
            } if self.category_group else None,
            "input_url": f"/api/jobs/{self.job_id}/input",
            "result_url": f"/api/jobs/{self.job_id}/result" if complete else None,
            "download_url": f"/api/jobs/{self.job_id}/download" if complete else None,
            "download_transfer_url": transfer_url,
            "download_transfer_expires_in": DOWNLOAD_TRANSFER_TTL_SECONDS if transfer_url else None,
            "images": self.published_images() if complete else [],
            "steps": self.steps,
            "can_resume": self.status == "failed" and self.recoverable and self.input_path.is_file(),
            "resume_url": f"/api/jobs/{self.job_id}/resume" if self.status == "failed" and self.recoverable else None,
        }


jobs: dict[str, JobState] = {}
job_queue: asyncio.Queue[str] = asyncio.Queue()
queue_workers: list[asyncio.Task[None]] = []
maintenance_task: asyncio.Task[None] | None = None
auth_store = AuthStore(DATABASE_CONFIG)
if os.environ.get("AUTH_RESET_ON_START", "false").lower() in {"1", "true", "yes", "on"}:
    auth_store.clear_sessions()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global maintenance_task
    recover_persisted_jobs()
    for index in range(MAX_ACTIVE_JOBS):
        queue_workers.append(asyncio.create_task(job_worker(index), name=f"job-queue-worker-{index}"))
    maintenance_task = asyncio.create_task(maintenance_worker(), name="job-maintenance-worker")
    try:
        yield
    finally:
        for worker in queue_workers:
            worker.cancel()
        if maintenance_task is not None:
            maintenance_task.cancel()
        await asyncio.gather(*queue_workers, return_exceptions=True)
        if maintenance_task is not None:
            await asyncio.gather(maintenance_task, return_exceptions=True)
            maintenance_task = None
        queue_workers.clear()


app = FastAPI(title="General Product Visual Generation System", lifespan=lifespan)


def authenticated_user(request: Request) -> User:
    return current_user(request, auth_store)


def paid_user(user: User = Depends(authenticated_user)) -> User:
    if PAYMENT_REQUIRED and user.remaining_uses < 1 and user.pay_status != "paid":
        raise HTTPException(status_code=402, detail="账户余额不足，请先充值")
    return user


def user_jobs_root(user: User) -> Path:
    """Return a private path derived from the authenticated WeChat identity."""
    return JOBS_ROOT / user.storage_key


def no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def login_target(value: str | None) -> str:
    return value if value in LOGIN_TARGETS else "/app"


def protected_page(request: Request, target: str, filename: str) -> Response:
    if auth_store.user_for_token(request.cookies.get(SESSION_COOKIE)) is None:
        response = RedirectResponse(url=f"/?{urlencode({'next': target})}", status_code=303)
        clear_session_cookie(response)
        return no_store(response)
    return no_store(FileResponse(WEB_ROOT / filename))


@app.get("/", include_in_schema=False)
async def home(request: Request, next: str = "/app") -> Response:
    if auth_store.user_for_token(request.cookies.get(SESSION_COOKIE)) is not None:
        return no_store(RedirectResponse(url=login_target(next), status_code=303))
    return no_store(FileResponse(WEB_ROOT / "login.html"))


@app.get("/app", include_in_schema=False)
async def workspace(request: Request) -> Response:
    return protected_page(request, "/app", "index.html")


@app.get("/pay", include_in_schema=False)
async def payment_page(request: Request) -> Response:
    return protected_page(request, "/pay", "pay.html")


@app.get("/profile", include_in_schema=False)
async def profile_page(request: Request) -> Response:
    return protected_page(request, "/profile", "profile.html")


@app.get("/index.html", include_in_schema=False)
async def direct_workspace_file() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=303)
    return no_store(response)


@app.get("/api/auth/wechat/start")
async def wechat_login_start(request: Request, next: str = "/app") -> Response:
    if not WECHAT_APP_ID or not WECHAT_APP_SECRET:
        raise HTTPException(status_code=503, detail="微信登录尚未配置")
    state = secrets.token_urlsafe(32)
    callback = WECHAT_REDIRECT_URI or str(request.url_for("wechat_login_callback"))
    query = urlencode({
        "appid": WECHAT_APP_ID,
        "redirect_uri": callback,
        "response_type": "code",
        "scope": "snsapi_base",
        "state": state,
    })
    response = RedirectResponse(f"https://open.weixin.qq.com/connect/oauth2/authorize?{query}#wechat_redirect", status_code=302)
    set_oauth_state_cookie(response, state, COOKIE_SECURE)
    response.set_cookie(
        LOGIN_NEXT_COOKIE, login_target(next), max_age=600, httponly=True,
        secure=COOKIE_SECURE, samesite="lax", path="/api/auth/wechat/callback",
    )
    return no_store(response)


@app.get("/api/auth/wechat/callback", name="wechat_login_callback")
async def wechat_login_callback(request: Request, code: str = "", state: str = "") -> Response:
    if not valid_oauth_state(request, state):
        raise HTTPException(status_code=400, detail="微信登录状态无效或已过期")
    if not code:
        raise HTTPException(status_code=400, detail="微信未返回授权码")
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            wechat_response = await client.get(
                "https://api.weixin.qq.com/sns/oauth2/access_token",
                params={
                    "appid": WECHAT_APP_ID,
                    "secret": WECHAT_APP_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                },
            )
            wechat_response.raise_for_status()
            payload = wechat_response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="微信身份验证服务暂时不可用") from exc
    openid = payload.get("openid")
    if not openid or payload.get("errcode"):
        raise HTTPException(status_code=401, detail="微信授权验证失败")
    user = auth_store.find_or_create_user(str(openid))
    destination = login_target(request.cookies.get(LOGIN_NEXT_COOKIE))
    separator = "&" if "?" in destination else "?"
    response = RedirectResponse(url=f"{destination}{separator}wechat-login=success", status_code=303)
    clear_oauth_state_cookie(response)
    response.delete_cookie(LOGIN_NEXT_COOKIE, path="/api/auth/wechat/callback")
    set_session_cookie(response, auth_store.create_session(user.id), COOKIE_SECURE)
    return no_store(response)


@app.post("/api/auth/wechat/dev-login", include_in_schema=False)
async def wechat_dev_login(response: Response, openid: str = Form(...)) -> dict[str, object]:
    """Explicit local/test login; never enabled in a production environment."""
    if not WECHAT_DEV_LOGIN:
        raise HTTPException(status_code=404, detail="接口不存在")
    user = auth_store.find_or_create_user(openid)
    set_session_cookie(response, auth_store.create_session(user.id), COOKIE_SECURE)
    no_store(response)
    return {"id": user.id, "openid_masked": f"***{user.openid[-4:]}"}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    auth_store.delete_session(request.cookies.get(SESSION_COOKIE))
    clear_session_cookie(response)
    no_store(response)
    return {"ok": True}


@app.get("/logout", include_in_schema=False)
async def logout_page(request: Request) -> RedirectResponse:
    auth_store.delete_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse(url="/?logged-out=1", status_code=303)
    clear_session_cookie(response)
    no_store(response)
    return response


@app.get("/api/auth/me")
async def me(user: User = Depends(authenticated_user)) -> dict[str, object]:
    return {
        "id": user.id, "openid_masked": f"***{user.openid[-4:]}",
        "status": user.status, "pay_status": user.pay_status,
        "service_status": user.service_status,
        "balance_cent": user.balance_cent, "remaining_uses": user.remaining_uses,
    }


@app.get("/api/session")
async def web_session(response: Response, user: User = Depends(authenticated_user)) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    return {
        "session_id": WEB_SESSION_ID, "user_id": user.id,
        "openid_masked": f"***{user.openid[-4:]}", "pay_status": user.pay_status,
        "service_status": user.service_status, "service_job_id": user.service_job_id,
        "balance_cent": user.balance_cent, "remaining_uses": user.remaining_uses,
        "payment_required": PAYMENT_REQUIRED,
    }


@app.get("/api/payment/status")
async def payment_status(request: Request, response: Response, user: User = Depends(authenticated_user)) -> dict[str, object]:
    refreshed = auth_store.user_for_token(request.cookies.get(SESSION_COOKIE))
    if refreshed is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    response.headers["Cache-Control"] = "no-store"
    return {
        "paid": not PAYMENT_REQUIRED or refreshed.remaining_uses > 0,
        "pay_status": refreshed.pay_status,
        "service_status": refreshed.service_status,
        "create_url": PAY_CREATE_URL,
        "balance_cent": refreshed.balance_cent,
        "remaining_uses": refreshed.remaining_uses,
        "packages": PAYMENT_PACKAGES,
    }


@app.post("/api/payment/authorization")
async def payment_authorization(user: User = Depends(authenticated_user)) -> dict[str, object]:
    """Issue a short-lived signed identity for the separate PHP payment runtime.

    PHP no longer has to choose between duplicate/path-scoped browser cookies;
    it verifies this server-signed OpenID and session hash instead.
    """
    if not PAY_BRIDGE_SECRET:
        raise HTTPException(status_code=503, detail="支付授权密钥尚未配置")
    expires_at = int(time.time()) + 300
    payload = {
        "v": 1, "sub": user.openid, "sid": user.session_hash,
        "exp": expires_at,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(PAY_BRIDGE_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return {"authorization": f"{encoded}.{encoded_signature}", "expires_at": expires_at}


def generation_history(user: User) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    root = user_jobs_root(user)
    if not root.is_dir():
        return result
    for metadata_path in sorted(root.glob("job-*/job-state.json"), reverse=True):
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            if int(data.get("user_id")) != user.id:
                continue
            job_id = str(data["job_id"])
            product = data.get("product") or {
                "category_group": data.get("category_group") or "unknown",
                "subcategory": data.get("subcategory") or "未分类商品",
                "product_name": data.get("product_name") or "商品视觉任务",
            }
            complete = data.get("status") == "completed"
            transfer_url = (
                f"/api/download-transfer/{create_download_transfer_token(job_id, user.id)}"
                if complete and DOWNLOAD_TRANSFER_SECRET else None
            )
            images = ([
                {
                    "id": f"display-{number:02d}", "type": "display",
                    "label": f"商品展示图_{number:02d}",
                    "url": f"/api/jobs/{job_id}/images/display-{number:02d}",
                }
                for number in range(1, 6)
            ] + [{
                "id": "final", "type": "final", "label": "最终商品信息长图",
                "url": f"/api/jobs/{job_id}/images/final",
            }]) if complete else []
            result.append({
                "job_id": job_id, "status": data.get("status"), "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at", data.get("created_at")),
                "progress": int(data.get("progress", 0)), "stage": data.get("stage"), "error": data.get("error"),
                "product": product,
                "result_url": f"/api/jobs/{job_id}/result" if complete else None,
                "download_url": f"/api/jobs/{job_id}/download" if complete else None,
                "download_transfer_url": transfer_url,
                "download_transfer_expires_in": DOWNLOAD_TRANSFER_TTL_SECONDS if transfer_url else None,
                "images": images,
                "steps": data.get("steps", {}),
                "can_resume": data.get("status") == "failed" and bool(data.get("recoverable", True)),
                "resume_url": f"/api/jobs/{job_id}/resume" if data.get("status") == "failed" and bool(data.get("recoverable", True)) else None,
            })
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return result[:100]


@app.get("/api/account")
async def account(user: User = Depends(authenticated_user)) -> dict[str, object]:
    summary = auth_store.account_summary(user.openid)
    return {
        "user": {"id": user.id, "openid_masked": f"***{user.openid[-4:]}", "status": user.status},
        **summary,
        "generation_records": generation_history(user),
        "generation_credit_cost": 1,
        "packages": PAYMENT_PACKAGES,
    }


def public_job_error(exc: Exception) -> str:
    message = str(exc)
    if "UNSUPPORTED_PRODUCT:" in message:
        detail = message.split("UNSUPPORTED_PRODUCT:", 1)[1].splitlines()[0].strip()
        return detail or "暂不支持服装类商品"
    if any(marker in message for marker in ("APIConnectionError", "RemoteProtocolError", "Connection error")):
        return "图片生成服务连接失败，请检查 IMAGE2_BASE_URL、代理设置和服务状态后重试。详细信息已写入任务 logs 目录。"
    lowered = message.lower()
    if "authenticationerror" in lowered or "error code: 401" in lowered or "status_code=401" in lowered:
        return "商品分析服务认证失败，请检查 QWEN_API_KEY 后恢复任务。"
    if "permissiondenied" in lowered or "error code: 403" in lowered or "status_code=403" in lowered:
        return "商品分析服务拒绝访问，请检查 Qwen 服务权限后恢复任务。"
    if "ratelimiterror" in lowered or "error code: 429" in lowered or "status_code=429" in lowered:
        return "商品分析服务请求过于频繁，请稍后恢复任务。"
    if "apiconnectionerror" in lowered or "connecterror" in lowered:
        return "商品分析服务连接失败，请检查 QWEN_BASE_URL 和服务状态后恢复任务。"
    if "timeout" in lowered or "timed out" in lowered:
        return "商品分析服务请求超时，请检查服务状态后恢复任务。"
    if "validation error" in lowered or "jsondecodeerror" in lowered or "failed validation" in lowered:
        return "商品分析模型返回的数据格式不完整，多次自动修复仍未通过校验；请恢复任务重试。"
    if "qwen api exposes multiple models" in lowered or "qwen api returned no models" in lowered:
        return "商品分析模型配置无效，请检查 QWEN_MODEL 后恢复任务。"
    if "Missing" in message or "not found" in message.lower():
        return "生成所需文件或依赖缺失，请检查服务日志。"
    return message[-600:]


def panel_failure_error(job: JobState, number: str) -> str:
    """Turn a private provider log into a safe, actionable browser message."""
    logs = sorted((job.output_dir / "logs").glob(f"panel-{number}-attempt-*.log"))
    try:
        diagnostic = logs[-1].read_text(encoding="utf-8", errors="replace")[-12000:] if logs else ""
    except OSError:
        diagnostic = ""
    lowered = diagnostic.lower()
    prefix = f"第 {int(number):02d} 张商品展示图生成失败："
    if any(marker in lowered for marker in ("error code: 429", "status_code=429", "too many requests", "ratelimit", "rate limit")):
        return prefix + "图片服务并发或频率受限，系统已自动重试；请稍后从断点恢复任务。"
    if any(marker in lowered for marker in ("error code: 401", "status_code=401", "authenticationerror", "invalid api key")):
        return prefix + "图片服务认证失败，请管理员检查 IMAGE2_API_KEY。"
    if any(marker in lowered for marker in ("error code: 403", "status_code=403", "permissiondenied")):
        return prefix + "图片服务拒绝访问，请管理员检查模型和接口权限。"
    if any(marker in lowered for marker in ("content policy", "content_policy", "safety", "moderation", "blocked prompt")):
        return prefix + "请求被图片服务的内容安全策略拦截；请更换商品图片后恢复任务。"
    if any(marker in lowered for marker in ("error code: 500", "error code: 502", "error code: 503", "error code: 504", "bad gateway", "upstream")):
        return prefix + "图片服务上游暂时不可用，系统已自动重试；请稍后从断点恢复任务。"
    if any(marker in lowered for marker in ("apiconnectionerror", "remoteprotocolerror", "connection error", "connecterror")):
        return prefix + "无法连接图片服务，请管理员检查 IMAGE2_BASE_URL、代理和服务状态。"
    if any(marker in lowered for marker in ("timeout", "timed out", "readtimeout", "writetimeout")):
        return prefix + "图片服务响应超时，系统已自动重试；请稍后从断点恢复任务。"
    if "returned no base64 image data" in lowered:
        return prefix + "图片服务返回了空结果，请稍后从断点恢复任务。"
    return prefix + f"详细原因已保存到任务 logs/panel-{number}-attempt-*.log，请管理员检查后恢复任务。"


def job_failure_error(job: JobState, exc: Exception) -> str:
    """Prefer the durable stage log, while returning only a safe diagnosis."""
    if job.error:
        return job.error
    if job.steps.get("analysis", {}).get("status") in {"running", "failed"}:
        analysis_log = job.output_dir / "logs" / "analysis.log"
        try:
            diagnostic = analysis_log.read_text(encoding="utf-8", errors="replace")[-12000:]
        except OSError:
            diagnostic = ""
        if diagnostic.strip():
            lowered = diagnostic.lower()
            if any(marker in lowered for marker in ("apiconnectionerror", "remoteprotocolerror", "connection error", "connecterror")):
                return "商品分析服务连接失败，请检查 QWEN_BASE_URL 和服务状态后恢复任务。"
            safe = public_job_error(RuntimeError(diagnostic))
            # Unknown raw provider output may include request data. Keep it in
            # the private log instead of exposing it through the browser API.
            if safe == diagnostic[-600:]:
                return "商品信息分析失败，详细原因已保存到任务 logs/analysis.log；请管理员检查后恢复任务。"
            return safe
    return public_job_error(exc)


def inspect_image(content: bytes) -> tuple[str, tuple[int, int]]:
    try:
        with Image.open(BytesIO(content)) as image:
            image_format = image.format or ""
            size = image.size
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=400, detail="上传文件不是有效图片") from exc
    if image_format not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail="仅支持 JPEG、PNG 或 WebP 图片")
    if size[0] < 64 or size[1] < 64:
        raise HTTPException(status_code=400, detail="图片尺寸过小")
    if size[0] * size[1] > MAX_IMAGE_PIXELS:
        raise HTTPException(status_code=400, detail="图片像素尺寸超过限制")
    return ALLOWED_FORMATS[image_format], size


def parse_workflow_line(job: JobState, line: str) -> None:
    if not line:
        return
    job.log_tail.append(line)
    print(f"[job {job.job_id[:8]}] {line}", flush=True)
    if line.startswith("::workflow::stage::"):
        stage = line.split("::workflow::stage::", 1)[1]
        stage_progress = {
            "图片输入与预处理": 6,
            "商品信息分析": 10,
            "商品文案与展示提示词生成": 22,
            "图片输入与风格参考预处理": 25,
            "长图排版与合成（动态避让商品与人物）": 90,
            "文件保存": 97,
        }
        if stage.startswith("商品展示图片生成：第 "):
            number = stage.rsplit("第 ", 1)[-1].split(" 张", 1)[0]
            job.set_step(f"display_{int(number):02d}", "running")
            job.update(progress=25 + max(0, int(number) - 1) * 11, stage=stage)
        elif stage.startswith("商品展示图片后处理："):
            number = stage.split("第 ", 1)[1].split(" 张", 1)[0]
            job.set_step(f"display_{int(number):02d}", "running")
            job.update(progress=84, stage=stage)
        else:
            if stage == "商品信息分析":
                job.set_step("analysis", "running")
            elif stage == "商品文案与展示提示词生成":
                job.set_step("assets", "running")
            elif stage == "长图排版与合成（动态避让商品与人物）":
                job.set_step("long_image", "running")
            job.update(progress=stage_progress.get(stage, job.progress), stage=stage)
    elif line.startswith("::workflow::panel_error::"):
        payload = line.split("::workflow::panel_error::", 1)[1]
        number = payload.split("::", 1)[0]
        if number.isdigit():
            job.error = panel_failure_error(job, f"{int(number):02d}")
    elif line.startswith("::workflow::error::"):
        details = line.split("::workflow::error::", 1)[1]
        failed_stage = details.split("::status=", 1)[0].removeprefix("stage=")
        if job.status != "failed":
            job.fail_running_steps(f"失败阶段：{failed_stage}")
            job.update(stage=f"生成失败：{failed_stage}", status="failed")
        if not job.error:
            job.error = f"失败阶段：{failed_stage}。详细信息：{details}"
    elif "Analyzing product with Qwen model" in line:
        job.update(progress=8, stage="视觉模型正在识别商品类别与特征")
    elif "Reusing Qwen analysis" in line:
        job.update(progress=18, stage="已读取商品规格信息")
    elif line == "::workflow::spec_ready":
        job.set_step("analysis", "success")
        job.update(progress=20, stage="商品类别与特征分析完成")
    elif line.startswith("::workflow::product::"):
        try:
            product = json.loads(line.split("::workflow::product::", 1)[1])
            job.category_group = str(product["category_group"])
            job.subcategory = str(product["subcategory"])
            job.product_name = str(product["product_name"])
            job.update(stage=f"已识别：{job.subcategory} · {job.product_name}")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
    elif line == "::workflow::assets_ready":
        job.set_step("assets", "success")
        job.update(progress=25, stage="动态展示策略与提示词已生成")
    elif line.startswith("::workflow::panel_ready::"):
        payload = line.split("::workflow::panel_ready::", 1)[1].split("::")
        number = payload[0]
        total = int(payload[1]) if len(payload) > 1 and payload[1].isdigit() else 5
        job.panels.add(number)
        job.set_step(f"display_{int(number):02d}", "success")
        count = len(job.panels)
        job.update(progress=25 + round(count / max(1, total) * 55), stage=f"展示图已完成 {count}/{total}")
    elif line.startswith("::workflow::postprocess_ready::") or line.startswith("::workflow::postprocess_skipped::"):
        number = line.split("::")[3]
        job.set_step(f"display_{int(number):02d}", "success")
        job.persist()
    elif "Saved product details:" in line or "Saved jewelry details:" in line:
        job.set_step("long_image", "success")
        job.update(progress=97, stage="正在整理最终长图")
    elif line == "::workflow::complete":
        job.update(progress=100, stage="商品信息长图生成完成")


async def stop_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def execute_job(job: JobState) -> None:
    job.reconcile_steps()
    job.update(status="running", progress=4, stage="正在启动生成流程")
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    command = [
        str(WORKFLOW_SCRIPT),
        "--output-dir",
        str(job.output_dir),
        str(job.input_path),
    ]
    process: asyncio.subprocess.Process | None = None
    try:
        if not job.has_complete_image_set():
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=ROOT,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            assert process.stdout is not None
            async with asyncio.timeout(JOB_TIMEOUT_SECONDS):
                while True:
                    try:
                        raw_line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
                    except asyncio.TimeoutError:
                        job.persist()
                        job.changed.set()
                        continue
                    if not raw_line:
                        break
                    parse_workflow_line(job, raw_line.decode("utf-8", errors="replace").strip())
                return_code = await process.wait()
            if return_code != 0:
                tail = "\n".join(job.log_tail)
                raise RuntimeError(f"生成流程退出，状态码 {return_code}\n{tail}")
        job.reconcile_steps()
        if not job.has_complete_image_set():
            missing = [filename for _, filename, path in job.image_paths if not job.valid_image(path)]
            raise RuntimeError(f"生成流程结束，但六张最终图片不完整：{', '.join(missing)}")
        if not job.valid_archive():
            write_job_archive(job)
        # Commit the charge before publishing the completed state so SSE and
        # personal-center refreshes can never observe a successful image with
        # a stale balance.
        if PAYMENT_REQUIRED and not auth_store.finalize_generation(job.job_id, success=True):
            raise RuntimeError("生成已完成，但账户扣费确认失败，请联系管理员")
        job.recoverable = False
        job.update(status="completed", progress=100, stage="商品信息长图生成完成")
    except TimeoutError:
        await stop_process(process)
        job.error = f"任务运行超过 {JOB_TIMEOUT_SECONDS // 60} 分钟，已自动停止；可从当前断点继续"
        job.fail_running_steps(job.error)
        job.recoverable = True
        job.update(status="failed", stage="生成超时，任务已停止")
    except asyncio.CancelledError:
        await stop_process(process)
        # A worker cancellation means the service is shutting down. Keep the
        # durable job queued so the next process can resume it.
        job.error = None
        job.status = "queued"
        job.stage = "服务重启，任务等待恢复"
        job.persist()
        job.changed.set()
        raise
    except Exception as exc:
        job.error = job_failure_error(job, exc)
        job.fail_running_steps(job.error)
        job.recoverable = "UNSUPPORTED_PRODUCT:" not in str(exc)
        if not job.stage.startswith("生成失败"):
            job.update(status="failed", stage=f"生成失败：{job.stage}")
        else:
            job.update(status="failed")
        if PAYMENT_REQUIRED and not job.recoverable:
            auth_store.finalize_generation(job.job_id, success=False)


def write_job_archive(job: JobState) -> None:
    """Persist the ZIP atomically so packaging is itself a durable checkpoint."""
    job.set_step("zip", "running")
    job.update(progress=98, stage="正在打包 ZIP 文件")
    temporary = job.archive_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for _, filename, image_path in job.image_paths:
                archive.write(image_path, arcname=filename)
        temporary.replace(job.archive_path)
        job.set_step("zip", "success")
        job.persist()
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        job.set_step("zip", "failed", error=str(exc))
        job.persist()
        raise


async def job_worker(index: int) -> None:
    """Consume the durable FIFO queue with a fixed global workflow limit."""
    while True:
        job_id = await job_queue.get()
        try:
            job = jobs.get(job_id)
            if job is not None and job.status == "queued":
                await execute_job(job)
        finally:
            job_queue.task_done()


def parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def persisted_job_statuses() -> dict[str, tuple[str, bool]]:
    statuses: dict[str, tuple[str, bool]] = {}
    if not JOBS_ROOT.is_dir():
        return statuses
    for metadata_path in JOBS_ROOT.glob("wechat-*/job-*/job-state.json"):
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            statuses[str(data["job_id"])] = (
                str(data.get("status", "failed")), bool(data.get("recoverable", True))
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return statuses


def maintain_jobs() -> None:
    """Expire stale queued work and reconcile stranded payment reservations."""
    now = datetime.now(timezone.utc)
    for job in list(jobs.values()):
        queued_at = parse_timestamp(job.queued_at)
        if job.status == "queued" and queued_at and (now - queued_at).total_seconds() > QUEUE_TIMEOUT_SECONDS:
            job.error = f"任务排队超过 {QUEUE_TIMEOUT_SECONDS // 60} 分钟，已自动取消并释放额度"
            job.recoverable = False
            job.update(status="failed", stage="排队超时，任务已取消")
            if PAYMENT_REQUIRED:
                auth_store.finalize_generation(job.job_id, success=False)

    if not PAYMENT_REQUIRED:
        return
    statuses = persisted_job_statuses()
    for reservation in auth_store.reserved_generations():
        job_id = str(reservation["job_id"])
        record = statuses.get(job_id)
        status, recoverable = record if record is not None else (None, False)
        if status == "completed":
            auth_store.finalize_generation(job_id, success=True)
        elif status == "failed" and not recoverable:
            auth_store.finalize_generation(job_id, success=False)
        elif status is None:
            created = parse_timestamp(reservation.get("created_at"))
            if created and (now - created).total_seconds() > ORPHAN_RESERVATION_TIMEOUT_SECONDS:
                auth_store.finalize_generation(job_id, success=False)


async def maintenance_worker() -> None:
    while True:
        try:
            maintain_jobs()
        except Exception as exc:
            print(f"[job maintenance] {exc}", flush=True)
        await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)


def recover_persisted_jobs() -> None:
    """Requeue jobs left queued or running by a previous server process."""
    recovered: list[JobState] = []
    if not JOBS_ROOT.is_dir():
        return
    for metadata_path in JOBS_ROOT.glob("wechat-*/job-*/job-state.json"):
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            if data.get("status") not in {"queued", "running"}:
                continue
            output_dir = metadata_path.parent.resolve()
            if JOBS_ROOT not in output_dir.parents:
                continue
            input_path = output_dir / str(data.get("input_name", "input.jpg"))
            if not input_path.is_file():
                continue
            job = JobState(
                job_id=str(data["job_id"]), user_id=int(data["user_id"]), output_dir=output_dir,
                input_path=input_path, status="queued", progress=int(data.get("progress", 2)),
                stage="服务已恢复，任务重新排队", error=None,
                category_group=data.get("category_group"), subcategory=data.get("subcategory"),
                product_name=data.get("product_name"), created_at=str(data.get("created_at", "")),
                updated_at=str(data.get("updated_at", data.get("created_at", ""))),
                queued_at=(datetime.now(timezone.utc).isoformat() if data.get("status") == "running"
                           else str(data.get("queued_at", data.get("created_at", "")))),
                panels=set(map(str, data.get("panels", []))),
                steps=dict(data.get("steps", {})), recoverable=bool(data.get("recoverable", True)),
                resume_count=int(data.get("resume_count", 0)),
                log_tail=deque(map(str, data.get("log_tail", [])), maxlen=30),
            )
            job.reset_failed_steps()
            job.persist()
            jobs[job.job_id] = job
            recovered.append(job)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    for job in sorted(recovered, key=lambda item: item.created_at):
        job_queue.put_nowait(job.job_id)


def get_job(job_id: str, user_id: int) -> JobState:
    if len(job_id) != 32 or any(character not in "0123456789abcdef" for character in job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    job = jobs.get(job_id)
    if job is None:
        job = load_job(job_id, user_id)
    if job is None or job.user_id != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


def load_job(job_id: str, user_id: int) -> JobState | None:
    """Rehydrate only a job stored below this authenticated user's directory."""
    user = auth_store.get_user(user_id)
    if user is None or user.status != "active":
        return None
    root = user_jobs_root(user)
    for output_dir in root.glob(f"job-*-{job_id[:8]}"):
        metadata_path = output_dir / "job-state.json"
        if not metadata_path.is_file():
            continue
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            if data.get("job_id") != job_id or int(data.get("user_id")) != user_id:
                continue
            input_path = output_dir / str(data.get("input_name", "input.jpg"))
            job = JobState(job_id=job_id, user_id=user_id, output_dir=output_dir, input_path=input_path,
                           status=str(data.get("status", "failed")), progress=int(data.get("progress", 0)),
                           stage=str(data.get("stage", "任务状态已恢复")), error=data.get("error"),
                           category_group=data.get("category_group"), subcategory=data.get("subcategory"),
                           product_name=data.get("product_name"),
                           created_at=str(data.get("created_at", "")),
                           updated_at=str(data.get("updated_at", data.get("created_at", ""))),
                           queued_at=str(data.get("queued_at", data.get("created_at", ""))),
                           panels=set(map(str, data.get("panels", []))),
                           steps=dict(data.get("steps", {})), recoverable=bool(data.get("recoverable", True)),
                           resume_count=int(data.get("resume_count", 0)),
                           log_tail=deque(map(str, data.get("log_tail", [])), maxlen=30))
            job.reconcile_steps()
            jobs[job_id] = job
            return job
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
    return None


@app.post("/api/jobs", status_code=202)
async def create_job(image: UploadFile = File(...), user: User = Depends(authenticated_user)) -> dict[str, object]:
    if PAYMENT_REQUIRED and user.remaining_uses < 1:
        raise HTTPException(status_code=402, detail="账户余额不足，请前往个人中心充值后继续")
    content = await image.read(MAX_UPLOAD_BYTES + 1)
    await image.close()
    if not content:
        raise HTTPException(status_code=400, detail="请选择商品图片")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="图片文件不能超过 20 MB")
    extension, _ = inspect_image(content)
    # Check capacity after the last await so concurrent uploads cannot all pass
    # a stale queue-size check before any one of them is persisted.
    active_jobs = sum(job.status in {"queued", "running"} for job in jobs.values())
    if active_jobs >= MAX_QUEUED_JOBS:
        raise HTTPException(status_code=429, detail="当前任务队列已满，请稍后重试")

    # Freeze one account credit after validation. It is charged only when the
    # workflow succeeds; failed tasks release it. The reservation is atomic so
    # concurrent requests cannot reuse the same balance.
    job_id = uuid.uuid4().hex
    if PAYMENT_REQUIRED and not auth_store.reserve_generation(user.openid, job_id):
        raise HTTPException(status_code=402, detail="账户可用次数不足，请充值后继续")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    # Every upload and generated artifact is stored below a directory derived
    # from the authenticated OpenID; no client-supplied identity enters paths.
    output_dir = user_jobs_root(user) / f"job-{timestamp}-{job_id[:8]}"
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
        input_path = output_dir / f"input{extension}"
        input_path.write_bytes(content)
    except Exception:
        if PAYMENT_REQUIRED:
            auth_store.finalize_generation(job_id, success=False)
        raise

    job = JobState(job_id=job_id, user_id=user.id, output_dir=output_dir, input_path=input_path)
    job.persist()
    jobs[job_id] = job
    job_queue.put_nowait(job_id)
    return job.public_data()


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, user: User = Depends(authenticated_user)) -> dict[str, object]:
    return get_job(job_id, user.id).public_data()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_queued_job(job_id: str, user: User = Depends(authenticated_user)) -> dict[str, object]:
    job = get_job(job_id, user.id)
    if job.status != "queued":
        raise HTTPException(status_code=409, detail="只能取消仍在排队中的任务")
    job.error = "任务已由用户取消，预留额度已释放"
    job.recoverable = False
    job.update(status="failed", stage="任务已取消")
    if PAYMENT_REQUIRED:
        auth_store.finalize_generation(job.job_id, success=False)
    return job.public_data()


@app.post("/api/jobs/{job_id}/resume", status_code=202)
async def resume_job(job_id: str, user: User = Depends(authenticated_user)) -> dict[str, object]:
    """Requeue the same durable task and retain every verified checkpoint."""
    job = get_job(job_id, user.id)
    if job.status != "failed" or not job.recoverable:
        raise HTTPException(status_code=409, detail="当前任务不可恢复")
    if not job.input_path.is_file():
        raise HTTPException(status_code=409, detail="原始商品图片已丢失，无法恢复任务")
    active_jobs = sum(item.status in {"queued", "running"} for item in jobs.values())
    if active_jobs >= MAX_QUEUED_JOBS:
        raise HTTPException(status_code=429, detail="当前任务队列已满，请稍后重试")
    if PAYMENT_REQUIRED and not auth_store.resume_generation(user.openid, job.job_id):
        raise HTTPException(status_code=409, detail="原任务的计费预留已失效，请联系管理员")
    job.reset_failed_steps()
    job.status = "queued"
    job.error = None
    job.stage = "任务已恢复，等待从断点继续"
    job.queued_at = datetime.now(timezone.utc).isoformat()
    job.resume_count += 1
    job.persist()
    job.changed.set()
    job_queue.put_nowait(job.job_id)
    return job.public_data()


@app.get("/api/jobs/{job_id}/input")
async def job_input(job_id: str, user: User = Depends(authenticated_user)) -> FileResponse:
    job = get_job(job_id, user.id)
    if not job.input_path.is_file():
        raise HTTPException(status_code=404, detail="上传图片不存在")
    return no_store(FileResponse(job.input_path))


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, user: User = Depends(authenticated_user)) -> StreamingResponse:
    job = get_job(job_id, user.id)

    async def stream():
        while True:
            job.changed.clear()
            yield f"data: {json.dumps(job.public_data(), ensure_ascii=False)}\n\n"
            if job.status in {"completed", "failed"}:
                return
            try:
                await asyncio.wait_for(job.changed.wait(), timeout=15)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/result")
async def job_result(job_id: str, user: User = Depends(authenticated_user)) -> FileResponse:
    job = get_job(job_id, user.id)
    if job.status != "completed" or not job.result_path.is_file():
        raise HTTPException(status_code=409, detail="结果尚未生成")
    return no_store(FileResponse(job.result_path, media_type="image/png"))


@app.get("/api/jobs/{job_id}/images/{image_id}")
async def job_image(job_id: str, image_id: str, user: User = Depends(authenticated_user)) -> FileResponse:
    job = get_job(job_id, user.id)
    allowed = {key: path for key, _, path in job.image_paths}
    image_path = allowed.get(image_id)
    if job.status != "completed" or image_path is None or not image_path.is_file():
        raise HTTPException(status_code=404 if image_path is None else 409, detail="图片不存在或尚未生成")
    return no_store(FileResponse(image_path, media_type="image/png"))


@app.get("/api/jobs/{job_id}/download")
async def job_download(job_id: str, user: User = Depends(authenticated_user)) -> FileResponse:
    job = get_job(job_id, user.id)
    return job_archive_response(job)


def job_archive_response(job: JobState, *, allow_rebuild: bool = True) -> FileResponse:
    if job.status != "completed" or not job.has_complete_image_set():
        raise HTTPException(status_code=409, detail="结果尚未生成")
    if not job.valid_archive() and not allow_rebuild:
        raise HTTPException(status_code=409, detail="当前任务 ZIP 文件不可用")
    if not job.valid_archive():
        write_job_archive(job)
    ascii_filename = f"product-images-{job.job_id[:8]}.zip"
    display_filename = f"商品视觉图片-{job.job_id[:8]}.zip"
    return no_store(FileResponse(
        job.archive_path,
        media_type="application/zip",
        headers={
            # Older Android WebViews require filename=, while modern browsers
            # use filename*= for the human-readable UTF-8 download name.
            "Content-Disposition": (
                f'attachment; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{quote(display_filename)}"
            ),
            "Content-Transfer-Encoding": "binary",
            "X-Content-Type-Options": "nosniff",
            "X-Accel-Buffering": "no",
        },
    ))


@app.post("/api/jobs/{job_id}/download-transfer")
async def create_job_download_transfer(
    job_id: str, user: User = Depends(authenticated_user),
) -> dict[str, object]:
    """Create a short-lived cookie-free URL for handoff to another browser."""
    job = get_job(job_id, user.id)
    if job.status != "completed" or not job.has_complete_image_set():
        raise HTTPException(status_code=409, detail="结果尚未生成")
    if not job.valid_archive():
        raise HTTPException(status_code=409, detail="当前任务 ZIP 文件不可用")
    token = create_download_transfer_token(job.job_id, user.id)
    return {
        "transfer_url": f"/api/download-transfer/{token}",
        "expires_in": DOWNLOAD_TRANSFER_TTL_SECONDS,
    }


@app.get("/api/download-transfer/{token}")
async def transferred_job_download(token: str) -> FileResponse:
    """Serve the same six-image ZIP after a WebView-to-browser handoff.

    The signed token binds the job to its owning user and expires quickly, so
    the target browser does not need the WeChat WebView's session cookie.
    """
    job_id, user_id = verify_download_transfer_token(token)
    job = get_job(job_id, user_id)
    return job_archive_response(job, allow_rebuild=False)


@app.get("/download-open/{token}", include_in_schema=False)
async def mobile_download_handoff(token: str, request: Request) -> Response:
    """Open externally, then immediately download the already-created ZIP."""
    job_id, user_id = verify_download_transfer_token(token)
    job = get_job(job_id, user_id)
    if job.status != "completed" or not job.valid_archive():
        raise HTTPException(status_code=409, detail="当前任务 ZIP 文件不可用")
    download_url = f"/api/download-transfer/{quote(token, safe='')}"
    if "MicroMessenger" not in request.headers.get("user-agent", ""):
        return no_store(RedirectResponse(download_url, status_code=302))
    return no_store(HTMLResponse(f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>在浏览器中下载 ZIP</title><style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;color:#fff;background:rgba(0,0,0,.86);font-family:-apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif}}
.arrow{{position:fixed;top:calc(12px + env(safe-area-inset-top));right:24px;font-size:70px;line-height:1;transform:rotate(-20deg)}}
.card{{padding:calc(112px + env(safe-area-inset-top)) 30px 30px;text-align:center}}h1{{margin:40px 0 24px;font-size:26px}}p{{margin:0 auto;max-width:420px;font-size:18px;line-height:1.9}}strong{{color:#76e3b8}}small{{display:block;margin-top:30px;color:#b9c4bf;font-size:14px;line-height:1.7}}
</style></head><body><div class="arrow">↗</div><main class="card">
<h1>请在浏览器中打开</h1><p>点击右上角 <strong>•••</strong><br>选择<strong>“在浏览器打开”</strong></p>
<small>选择浏览器后将自动下载当前任务原 ZIP<br>无需重新登录或再次点击下载</small>
</main></body></html>"""))


def authenticated_admin(request: Request) -> Admin:
    admin = auth_store.admin_for_token(request.cookies.get(ADMIN_SESSION_COOKIE), ADMIN_USERNAME)
    if admin is None:
        raise HTTPException(status_code=401, detail="管理员登录已失效")
    return admin


def admin_page_response(request: Request) -> Response:
    if auth_store.admin_for_token(request.cookies.get(ADMIN_SESSION_COOKIE), ADMIN_USERNAME) is None:
        response = RedirectResponse(url="/admin/login", status_code=303)
        clear_admin_session_cookie(response)
        return no_store(response)
    return no_store(FileResponse(WEB_ROOT / "admin.html"))


def checked_pagination(page: int, page_size: int) -> tuple[int, int]:
    if page < 1 or page_size < 1 or page_size > ADMIN_PAGE_SIZE_MAX:
        raise HTTPException(status_code=422, detail=f"分页参数无效，page_size 最大为 {ADMIN_PAGE_SIZE_MAX}")
    return page, page_size


def admin_job_records(user_id: int | None = None) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    pattern = "wechat-*/job-*/job-state.json"
    for metadata_path in JOBS_ROOT.glob(pattern) if JOBS_ROOT.is_dir() else []:
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            record_user_id = int(data["user_id"])
            job_id = str(data["job_id"])
            if user_id is not None and record_user_id != user_id:
                continue
            if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
                continue
            user = auth_store.admin_user(record_user_id)
            if user is None:
                continue
            product = data.get("product") or {
                "category_group": data.get("category_group") or "unknown",
                "subcategory": data.get("subcategory") or "未分类商品",
                "product_name": data.get("product_name") or "商品视觉任务",
            }
            complete = data.get("status") == "completed"
            prefix = f"/admin/api/jobs/{record_user_id}/{job_id}"
            images = ([{
                "id": f"display-{number:02d}",
                "label": f"商品展示图_{number:02d}",
                "url": f"{prefix}/images/display-{number:02d}",
            } for number in range(1, 6)] + [{
                "id": "final", "label": "商品信息长图", "url": f"{prefix}/images/final",
            }]) if complete else []
            records.append({
                "job_id": job_id,
                "user_id": record_user_id,
                "openid": user["openid"],
                "product": product,
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at", data.get("created_at")),
                "status": data.get("status", "unknown"),
                "progress": int(data.get("progress", 0)),
                "stage": data.get("stage"),
                "error": data.get("error"),
                "images": images,
                "download_url": f"{prefix}/download" if complete else None,
            })
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return records


def admin_job_files(user_id: int, job_id: str) -> tuple[dict[str, object], Path]:
    if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    user_data = auth_store.admin_user(user_id)
    if user_data is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    storage_user = User(user_id, str(user_data["openid"]), str(user_data["status"]))
    root = user_jobs_root(storage_user)
    for output_dir in root.glob(f"job-*-{job_id[:8]}") if root.is_dir() else []:
        metadata_path = output_dir / "job-state.json"
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            if str(data.get("job_id")) == job_id and int(data.get("user_id")) == user_id:
                return data, output_dir.resolve()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    raise HTTPException(status_code=404, detail="任务不存在")


@app.get("/admin/login", include_in_schema=False)
async def admin_login_page(request: Request) -> Response:
    if auth_store.admin_for_token(request.cookies.get(ADMIN_SESSION_COOKIE), ADMIN_USERNAME) is not None:
        return no_store(RedirectResponse(url="/admin", status_code=303))
    return no_store(FileResponse(WEB_ROOT / "admin-login.html"))


@app.get("/admin-login.html", include_in_schema=False)
async def direct_admin_login_file() -> RedirectResponse:
    return no_store(RedirectResponse(url="/admin/login", status_code=303))


@app.get("/admin.html", include_in_schema=False)
async def direct_admin_console_file(request: Request) -> Response:
    return admin_page_response(request)


@app.get("/admin", include_in_schema=False)
@app.get("/admin/users", include_in_schema=False)
@app.get("/admin/jobs", include_in_schema=False)
@app.get("/admin/payments", include_in_schema=False)
async def admin_console_page(request: Request) -> Response:
    return admin_page_response(request)


@app.post("/admin/api/login")
async def admin_login(response: Response, username: str = Form(...), password: str = Form(...)) -> dict[str, object]:
    username_ok = hmac.compare_digest(username.encode("utf-8"), ADMIN_USERNAME.encode("utf-8"))
    password_ok = hmac.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, detail="管理员账号或密码错误")
    token = auth_store.create_admin_session(ADMIN_USERNAME)
    set_admin_session_cookie(response, token, COOKIE_SECURE)
    no_store(response)
    return {"ok": True, "username": ADMIN_USERNAME}


@app.post("/admin/api/logout")
async def admin_logout(request: Request, response: Response) -> dict[str, bool]:
    auth_store.delete_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE))
    clear_admin_session_cookie(response)
    no_store(response)
    return {"ok": True}


@app.get("/admin/api/session")
async def admin_session(admin: Admin = Depends(authenticated_admin)) -> dict[str, object]:
    return {"authenticated": True, "username": admin.username}


@app.get("/admin/api/users")
async def admin_users(
    page: int = 1, page_size: int = 20, search: str = "", _: Admin = Depends(authenticated_admin)
) -> dict[str, object]:
    page, page_size = checked_pagination(page, page_size)
    items, total = auth_store.admin_users(page, page_size, search.strip()[:128])
    return {"items": items, "page": page, "page_size": page_size, "total": total}


@app.get("/admin/api/users/{user_id}")
async def admin_user_detail(user_id: int, _: Admin = Depends(authenticated_admin)) -> dict[str, object]:
    user = auth_store.admin_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    jobs = admin_job_records(user_id)
    payments, payment_total = auth_store.admin_payments(1, 20, user_id)
    return {"user": user, "recent_jobs": jobs[:20], "recent_payments": payments, "payment_total": payment_total}


@app.get("/admin/api/jobs")
async def admin_jobs(
    page: int = 1, page_size: int = 20, user_id: int | None = None,
    _: Admin = Depends(authenticated_admin),
) -> dict[str, object]:
    page, page_size = checked_pagination(page, page_size)
    records = admin_job_records(user_id)
    start = (page - 1) * page_size
    return {"items": records[start:start + page_size], "page": page, "page_size": page_size, "total": len(records)}


@app.get("/admin/api/payments")
async def admin_payments(
    page: int = 1, page_size: int = 20, user_id: int | None = None,
    _: Admin = Depends(authenticated_admin),
) -> dict[str, object]:
    page, page_size = checked_pagination(page, page_size)
    items, total = auth_store.admin_payments(page, page_size, user_id)
    return {"items": items, "page": page, "page_size": page_size, "total": total}


@app.get("/admin/api/jobs/{user_id}/{job_id}/images/{image_id}")
async def admin_job_image(
    user_id: int, job_id: str, image_id: str, _: Admin = Depends(authenticated_admin)
) -> FileResponse:
    data, output_dir = admin_job_files(user_id, job_id)
    allowed = {f"display-{number:02d}": output_dir / f"panel-{number:02d}.png" for number in range(1, 6)}
    result = output_dir / "product-long.png"
    if not result.is_file():
        result = output_dir / "jewelry-long.png"
    allowed["final"] = result
    image_path = allowed.get(image_id)
    if data.get("status") != "completed" or image_path is None or not image_path.is_file():
        raise HTTPException(status_code=404 if image_path is None else 409, detail="图片不存在或尚未生成")
    return no_store(FileResponse(image_path, media_type="image/png"))


@app.get("/admin/api/jobs/{user_id}/{job_id}/download")
async def admin_job_download(
    user_id: int, job_id: str, _: Admin = Depends(authenticated_admin)
) -> FileResponse:
    data, output_dir = admin_job_files(user_id, job_id)
    input_name = str(data.get("input_name", "input.jpg"))
    job = JobState(job_id, user_id, output_dir, output_dir / input_name, status=str(data.get("status", "unknown")))
    if job.status != "completed" or not job.has_complete_image_set():
        raise HTTPException(status_code=409, detail="结果尚未生成")
    if not job.valid_archive():
        write_job_archive(job)
    return no_store(FileResponse(
        job.archive_path, media_type="application/zip",
        filename=f"product-images-{job.job_id[:8]}.zip",
        headers={"X-Content-Type-Options": "nosniff"},
    ))


app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
