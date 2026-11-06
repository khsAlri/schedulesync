"""Jinja2 템플릿 설정과 화면 렌더링 도우미. 여러 라우터에서 같은 객체를 쓴다."""

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.models import Role
from app.security import get_csrf_token

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

ROLE_LABELS = {
    Role.ADMIN: "시스템 관리자",
    Role.APPROVER: "허가권자",
    Role.EMPLOYEE: "직원",
}


def role_label(role: Any) -> str:
    try:
        return ROLE_LABELS[Role(role)]
    except ValueError:
        return str(role)


templates.env.globals["role_label"] = role_label

_FLASH_SESSION_KEY = "_flashes"


def flash(request: Request, text: str, level: str = "info") -> None:
    """다음 화면에 한 번 보여 줄 안내 문구를 저장한다. level: info/success/warning/error"""
    request.session.setdefault(_FLASH_SESSION_KEY, []).append({"text": text, "level": level})


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
):
    """로그인 사용자, 안내 문구, CSRF 토큰을 함께 넘겨 템플릿을 렌더링한다."""
    ctx = dict(context or {})
    ctx.setdefault("current_user", getattr(request.state, "user", None))
    ctx["flash_messages"] = request.session.pop(_FLASH_SESSION_KEY, [])
    ctx["csrf_token"] = get_csrf_token(request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
