"""웹 애플리케이션 시작 지점.

실행: uvicorn app.main:app --reload
첫 관리자 계정: python -m app.cli create-admin
"""

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import init_db
from app.deps import LoginRequired, PermissionDenied, require_user
from app.models import User
from app.routers import (
    account,
    admin,
    assignments,
    auth,
    notifications,
    projects,
    schedule_changes,
)
from app.templating import render

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="schedulesync", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="schedulesync_session",
    same_site="lax",
    max_age=60 * 60 * 12,  # 12시간
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(projects.router)
app.include_router(assignments.router)
app.include_router(schedule_changes.router)
app.include_router(notifications.router)


# ---------------------------------------------------------------------------
# 오류 처리
# ---------------------------------------------------------------------------


@app.exception_handler(LoginRequired)
async def handle_login_required(request: Request, exc: LoginRequired):
    next_path = "/"
    if request.method == "GET":
        next_path = request.url.path
        if request.url.query:
            next_path += "?" + request.url.query
    return RedirectResponse(f"/login?next={quote(next_path, safe='')}", status_code=303)


@app.exception_handler(PermissionDenied)
async def handle_permission_denied(request: Request, exc: PermissionDenied):
    return render(
        request,
        "error.html",
        {"title": "접근할 수 없습니다", "message": exc.message},
        status_code=403,
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        title = "페이지를 찾을 수 없습니다"
        message = exc.detail if exc.detail and exc.detail != "Not Found" else "주소를 다시 확인하세요."
    else:
        title = "요청을 처리할 수 없습니다"
        message = exc.detail if isinstance(exc.detail, str) else ""
    return render(
        request,
        "error.html",
        {"title": title, "message": message},
        status_code=exc.status_code,
    )


# ---------------------------------------------------------------------------
# 기본 화면
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: User = Depends(require_user)):
    return render(request, "index.html", {})


@app.get("/health")
def health():
    return {"status": "ok"}
