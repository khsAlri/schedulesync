"""로그인·로그아웃."""

import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, verify_csrf
from app.models import User
from app.security import hash_password, normalize_email, verify_password
from app.templating import flash, render

router = APIRouter()

# 존재하지 않는 이메일로 로그인할 때도 비밀번호 확인 시간을 똑같이 들여,
# 응답 시간으로 계정 존재 여부를 알 수 없게 한다.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_hex(16))


def _safe_next(next_path: str) -> str:
    """로그인 후 이동할 주소. 외부 사이트로 보내는 주소는 막는다."""
    if not next_path.startswith("/") or next_path.startswith("//") or "\\" in next_path:
        return "/"
    return next_path


@router.get("/login")
def login_form(
    request: Request,
    next: str = "/",
    user: User | None = Depends(get_current_user),
):
    if user is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", {"next": _safe_next(next), "email": ""})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    normalized = normalize_email(email)
    user = db.scalar(select(User).where(User.email == normalized)) if normalized else None
    password_ok = verify_password(password, user.password_hash if user else _DUMMY_PASSWORD_HASH)

    if user is None or not password_ok or not user.is_active:
        return render(
            request,
            "login.html",
            {
                "next": _safe_next(next),
                "email": email,
                "error": "이메일 또는 비밀번호가 올바르지 않습니다.",
            },
            status_code=400,
        )

    # 로그인 전 세션 값을 버리고 새로 시작한다.
    request.session.clear()
    request.session["user_id"] = user.id
    flash(request, f"{user.name}님, 환영합니다.", "success")
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    flash(request, "로그아웃했습니다.", "info")
    return RedirectResponse("/login", status_code=303)
