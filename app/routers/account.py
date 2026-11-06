"""로그인한 사용자 본인의 계정 설정."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user, verify_csrf
from app.models import User
from app.security import hash_password, validate_password, verify_password
from app.templating import flash, render

router = APIRouter(prefix="/account")


@router.get("/password")
def password_form(request: Request, user: User = Depends(require_user)):
    return render(request, "account/password.html", {"errors": []})


@router.post("/password", dependencies=[Depends(verify_csrf)])
def password_change(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    errors: list[str] = []
    if not verify_password(current_password, user.password_hash):
        errors.append("현재 비밀번호가 올바르지 않습니다.")

    password_error = validate_password(new_password)
    if password_error:
        errors.append(password_error)
    elif new_password != new_password_confirm:
        errors.append("새 비밀번호 확인이 일치하지 않습니다.")
    elif new_password == current_password:
        errors.append("새 비밀번호가 현재 비밀번호와 같습니다.")

    if errors:
        return render(request, "account/password.html", {"errors": errors}, status_code=400)

    user.password_hash = hash_password(new_password)
    db.commit()
    flash(request, "비밀번호를 변경했습니다.", "success")
    return RedirectResponse("/", status_code=303)
