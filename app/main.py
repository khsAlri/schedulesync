"""웹 애플리케이션 시작 지점.

개발: uvicorn app.main:app --reload
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
from starlette.middleware.trustedhost import TrustedHostMiddleware

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


app = FastAPI(
    title="schedulesync",
    version="0.7.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.environment == "development" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.environment == "development" else None,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="schedulesync_session",
    same_site="lax",
    https_only=settings.session_https_only,
    max_age=60 * 60 * 12,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=list(settings.allowed_hosts),
    www_redirect=False,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(projects.router)
app.include_router(assignments.router)
app.include_router(schedule_changes.router)
app.include_router(notifications.router)


@app.exception_handler(LoginRequired)
async def handle_login_required(request: Request, exc: LoginRequired):
    next_path = "/"
    if request.method == "GET":
        next_path = request.url.path
        if request.url.query:
            next_path += "?" + request.url.query
    return RedirectResponse(
        f"/login?next={quote(next_path, safe='')}", status_code=303
    )


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
        message = (
            exc.detail
            if isinstance(exc.detail, str) and exc.detail != "Not Found"
            else "주소를 다시 확인하세요."
        )
    else:
        title = "요청을 처리할 수 없습니다"
        message = exc.detail if isinstance(exc.detail, str) else ""
    return render(
        request,
        "error.html",
        {"title": title, "message": message},
        status_code=exc.status_code,
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: User = Depends(require_user)):
    return render(request, "index.html", {})


@app.get("/health")
def health():
    """프로세스 상태 확인용. DB·Slack 연결 성공을 의미하지 않는다."""
    return {"status": "ok"}
