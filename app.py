from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from auth import AuthStore, User, clear_session_cookie, current_user, set_session_cookie


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
JOBS_ROOT = Path(os.environ.get("APP_OUTPUT_ROOT", ROOT / "outputs")).resolve()
DATABASE_PATH = Path(os.environ.get("APP_DB_PATH", ROOT / "var/app.db")).resolve()
WORKFLOW_SCRIPT = Path(os.environ.get("WEB_WORKFLOW_SCRIPT", ROOT / "run_workflow.sh")).resolve()
MAX_UPLOAD_BYTES = int(os.environ.get("WEB_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
MAX_IMAGE_PIXELS = int(os.environ.get("WEB_MAX_IMAGE_PIXELS", "40000000"))
MAX_ACTIVE_JOBS = int(os.environ.get("WEB_MAX_ACTIVE_JOBS", "2"))
ALLOWED_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
WEB_SESSION_ID = uuid.uuid4().hex
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}


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
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    panels: set[str] = field(default_factory=set)
    log_tail: deque[str] = field(default_factory=lambda: deque(maxlen=30))
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    @property
    def metadata_path(self) -> Path:
        return self.output_dir / "job-state.json"

    @property
    def result_path(self) -> Path:
        return self.output_dir / "jewelry-long.png"

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
        payload = {
            "job_id": self.job_id, "user_id": self.user_id,
            "status": self.status, "progress": self.progress,
            "stage": self.stage, "error": self.error,
            "created_at": self.created_at,
            "log_tail": list(self.log_tail), "panels": sorted(self.panels),
            "input_name": self.input_path.name,
        }
        temporary = self.metadata_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.metadata_path)

    def public_data(self) -> dict[str, object]:
        complete = self.status == "completed"
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "error": self.error,
            "created_at": self.created_at,
            "input_name": self.input_path.name,
            "input_url": f"/api/jobs/{self.job_id}/input",
            "result_url": f"/api/jobs/{self.job_id}/result" if complete else None,
            "download_url": f"/api/jobs/{self.job_id}/download" if complete else None,
        }


app = FastAPI(title="Jewelry Lookbook Sheet")
jobs: dict[str, JobState] = {}
auth_store = AuthStore(DATABASE_PATH)
if os.environ.get("AUTH_RESET_ON_START", "false").lower() in {"1", "true", "yes", "on"}:
    auth_store.clear_sessions()


def authenticated_user(request: Request) -> User:
    return current_user(request, auth_store)


def user_jobs_root(user: User) -> Path:
    """Return the private output root for one authenticated user.

    Usernames are validated by AuthStore and therefore contain only path-safe
    characters.  The username is used for operator-facing organization while
    authorization still relies on the immutable database user ID.
    """
    return JOBS_ROOT / user.username


def no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/", include_in_schema=False)
async def home(request: Request) -> Response:
    # The public entry point is deliberately stateless.  Do not let a browser
    # reopen the previous workspace just because an old cookie still exists.
    response = FileResponse(WEB_ROOT / "login.html")
    auth_store.delete_session(request.cookies.get("jewelry_session"))
    clear_session_cookie(response)
    return no_store(response)


@app.get("/app", include_in_schema=False)
async def workspace(user: User = Depends(authenticated_user)) -> Response:
    return no_store(FileResponse(WEB_ROOT / "index.html"))


@app.get("/index.html", include_in_schema=False)
async def direct_workspace_file() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=303)
    return no_store(response)


@app.post("/api/auth/register")
async def register(response: Response, username: str = Form(...), password: str = Form(...)) -> dict[str, str]:
    user = auth_store.register(username, password)
    set_session_cookie(response, auth_store.create_session(user.id), COOKIE_SECURE)
    no_store(response)
    return {"username": user.username}


@app.post("/api/auth/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)) -> dict[str, str]:
    user = auth_store.authenticate(username, password)
    set_session_cookie(response, auth_store.create_session(user.id), COOKIE_SECURE)
    no_store(response)
    return {"username": user.username}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    auth_store.delete_session(request.cookies.get("jewelry_session"))
    clear_session_cookie(response)
    no_store(response)
    return {"ok": True}


@app.get("/logout", include_in_schema=False)
async def logout_page(request: Request) -> RedirectResponse:
    auth_store.delete_session(request.cookies.get("jewelry_session"))
    response = RedirectResponse(url="/?logged-out=1", status_code=303)
    clear_session_cookie(response)
    no_store(response)
    return response


@app.get("/api/auth/me")
async def me(user: User = Depends(authenticated_user)) -> dict[str, object]:
    return {"id": user.id, "username": user.username}


@app.get("/api/session")
async def web_session(response: Response, user: User = Depends(authenticated_user)) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    return {"session_id": WEB_SESSION_ID, "user_id": user.id, "username": user.username}


def public_job_error(exc: Exception) -> str:
    message = str(exc)
    if any(marker in message for marker in ("APIConnectionError", "RemoteProtocolError", "Connection error")):
        return "图片生成服务连接失败，请检查 IMAGE2_BASE_URL、代理设置和服务状态后重试。详细信息已写入任务 logs 目录。"
    if "Missing" in message or "not found" in message.lower():
        return "生成所需文件或依赖缺失，请检查服务日志。"
    return message[-600:]


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
            "长图排版与合成（动态避让人物和珠宝）": 90,
            "文件保存": 97,
        }
        if stage.startswith("商品展示图片生成：第 "):
            number = stage.rsplit("第 ", 1)[-1].split(" 张", 1)[0]
            job.update(progress=25 + max(0, int(number) - 1) * 11, stage=stage)
        elif stage.startswith("第 5 张模特镜面展示图生成"):
            job.update(progress=69, stage=stage)
        else:
            job.update(progress=stage_progress.get(stage, job.progress), stage=stage)
    elif line.startswith("::workflow::error::"):
        details = line.split("::workflow::error::", 1)[1]
        failed_stage = details.split("::status=", 1)[0].removeprefix("stage=")
        job.update(stage=f"生成失败：{failed_stage}", status="failed")
        job.error = f"失败阶段：{failed_stage}。详细信息：{details}"
    elif "Analyzing jewelry with Qwen model" in line:
        job.update(progress=8, stage="Qwen 正在识别珠宝信息")
    elif "Reusing Qwen analysis" in line:
        job.update(progress=18, stage="已读取珠宝商品信息")
    elif line == "::workflow::spec_ready":
        job.update(progress=20, stage="珠宝商品信息分析完成")
    elif line == "::workflow::assets_ready":
        job.update(progress=25, stage="五张展示图提示词已生成")
    elif line.startswith("::workflow::panel_ready::"):
        number = line.rsplit("::", 1)[-1]
        job.panels.add(number)
        count = len(job.panels)
        job.update(progress=25 + count * 11, stage=f"展示图已完成 {count}/5")
    elif "Saved jewelry details:" in line:
        job.update(progress=97, stage="正在整理最终长图")
    elif line == "::workflow::complete":
        job.update(progress=100, stage="商品信息长图生成完成")


async def execute_job(job: JobState) -> None:
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
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=ROOT,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        async for raw_line in process.stdout:
            parse_workflow_line(job, raw_line.decode("utf-8", errors="replace").strip())
        return_code = await process.wait()
        if return_code != 0:
            tail = "\n".join(job.log_tail)
            raise RuntimeError(f"生成流程退出，状态码 {return_code}\n{tail}")
        if not job.result_path.is_file():
            raise RuntimeError("生成流程结束，但未找到最终长图")
        job.update(status="completed", progress=100, stage="商品信息长图生成完成")
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        job.error = "任务已取消"
        job.update(status="failed", stage="任务已取消")
        raise
    except Exception as exc:
        job.error = public_job_error(exc)
        if not job.stage.startswith("生成失败"):
            job.update(status="failed", stage=f"生成失败：{job.stage}")
        else:
            job.update(status="failed")


def get_job(job_id: str, user_id: int) -> JobState:
    job = jobs.get(job_id)
    if job is None:
        job = load_job(job_id, user_id)
    if job is None or job.user_id != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


def load_job(job_id: str, user_id: int) -> JobState | None:
    """Rehydrate only a job stored below this authenticated user's directory."""
    # The username is not available here; resolve the private root from the DB
    # rather than accepting a path supplied by the client.
    with auth_store.connect() as connection:
        row = connection.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    root = JOBS_ROOT / str(row["username"])
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
                           created_at=str(data.get("created_at", "")),
                           panels=set(map(str, data.get("panels", []))),
                           log_tail=deque(map(str, data.get("log_tail", [])), maxlen=30))
            if job.status in {"queued", "running"}:
                job.error = "服务刷新后，原生成进程已停止，请重新生成"
                job.status = "failed"
                job.stage = "任务中断"
                job.persist()
            jobs[job_id] = job
            return job
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
    return None


@app.post("/api/jobs", status_code=202)
async def create_job(image: UploadFile = File(...), user: User = Depends(authenticated_user)) -> dict[str, object]:
    active_jobs = sum(job.status in {"queued", "running"} for job in jobs.values())
    if active_jobs >= MAX_ACTIVE_JOBS:
        raise HTTPException(status_code=429, detail="当前生成任务已满，请稍后重试")
    content = await image.read(MAX_UPLOAD_BYTES + 1)
    await image.close()
    if not content:
        raise HTTPException(status_code=400, detail="请选择珠宝图片")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="图片文件不能超过 20 MB")
    extension, _ = inspect_image(content)

    job_id = uuid.uuid4().hex
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    # Keep every uploaded input, workflow artifact and generated result below
    # the authenticated user's private root. The numeric database ID is used
    # instead of the username so renaming/display rules can never affect paths.
    output_dir = user_jobs_root(user) / f"job-{timestamp}-{job_id[:8]}"
    output_dir.mkdir(parents=True, exist_ok=False)
    input_path = output_dir / f"input{extension}"
    input_path.write_bytes(content)

    job = JobState(job_id=job_id, user_id=user.id, output_dir=output_dir, input_path=input_path)
    job.persist()
    jobs[job_id] = job
    job.task = asyncio.create_task(execute_job(job), name=f"jewelry-job-{job_id}")
    return job.public_data()


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, user: User = Depends(authenticated_user)) -> dict[str, object]:
    return get_job(job_id, user.id).public_data()


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


@app.get("/api/jobs/{job_id}/download")
async def job_download(job_id: str, user: User = Depends(authenticated_user)) -> FileResponse:
    job = get_job(job_id, user.id)
    if job.status != "completed" or not job.result_path.is_file():
        raise HTTPException(status_code=409, detail="结果尚未生成")
    return no_store(FileResponse(
        job.result_path,
        media_type="image/png",
        filename=f"jewelry-product-{job.job_id[:8]}.png",
    ))


app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
