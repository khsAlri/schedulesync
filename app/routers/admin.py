"""시스템 관리자: 계정 발급·관리."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_admin, verify_csrf
from app.models import Assignment, Project, Role, User
from app.security import (
    hash_password,
    is_valid_email,
    is_valid_slack_user_id,
    normalize_email,
    validate_password,
)
from app.templating import flash, render

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

# 요구사항: 허가권자는 부서장 2명
EXPECTED_APPROVER_COUNT = 2


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


def _optional(value: str) -> str | None:
    value = value.strip()
    return value or None


def _parse_role(value: str) -> Role | None:
    try:
        return Role(value)
    except ValueError:
        return None


def _get_user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    return user


def _user_to_form(user: User) -> dict:
    return {
        "email": user.email,
        "name": user.name,
        "role": user.role.value,
        "department": user.department or "",
        "slack_user_id": user.slack_user_id or "",
        "is_active": user.is_active,
    }


def _validate_common(
    db: Session,
    *,
    email: str,
    name: str,
    role: Role | None,
    slack_user_id: str | None,
    exclude_user_id: int | None = None,
) -> list[str]:
    errors: list[str] = []
    if not is_valid_email(email):
        errors.append("이메일 형식이 올바르지 않습니다.")
    else:
        stmt = select(User.id).where(User.email == email)
        if exclude_user_id is not None:
            stmt = stmt.where(User.id != exclude_user_id)
        if db.scalar(stmt) is not None:
            errors.append("이미 사용 중인 이메일입니다.")
    if not name:
        errors.append("이름을 입력하세요.")
    if role is None:
        errors.append("역할을 선택하세요.")
    if slack_user_id and not is_valid_slack_user_id(slack_user_id):
        errors.append("슬랙 사용자 ID 형식이 올바르지 않습니다. (예: U01ABCDEF)")
    return errors


def _warn_approver_count(request: Request, db: Session) -> None:
    count = db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == Role.APPROVER, User.is_active.is_(True))
    )
    if count and count > EXPECTED_APPROVER_COUNT:
        flash(
            request,
            f"활성 허가권자가 {count}명입니다. 요구사항상 허가권자는 부서장 "
            f"{EXPECTED_APPROVER_COUNT}명입니다.",
            "warning",
        )


def _render_form(
    request: Request,
    *,
    mode: str,
    form: dict,
    errors: list[str],
    user: User | None = None,
    status_code: int = 200,
):
    return render(
        request,
        "admin/user_form.html",
        {
            "mode": mode,
            "form": form,
            "errors": errors,
            "roles": list(Role),
            "target_user": user,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# 계정 목록
# ---------------------------------------------------------------------------


@router.get("/users")
def user_list(request: Request, db: Session = Depends(get_db)):
    users = db.scalars(
        select(User).order_by(User.is_active.desc(), User.role, User.name)
    ).all()
    return render(request, "admin/users.html", {"users": users})


# ---------------------------------------------------------------------------
# 계정 발급
# ---------------------------------------------------------------------------


@router.get("/users/new")
def user_new_form(request: Request):
    form = {
        "email": "",
        "name": "",
        "role": Role.EMPLOYEE.value,
        "department": "",
        "slack_user_id": "",
        "is_active": True,
    }
    return _render_form(request, mode="new", form=form, errors=[])


@router.post("/users/new", dependencies=[Depends(verify_csrf)])
def user_create(
    request: Request,
    email: str = Form(""),
    name: str = Form(""),
    role: str = Form(""),
    department: str = Form(""),
    slack_user_id: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    form = {
        "email": normalize_email(email),
        "name": name.strip(),
        "role": role,
        "department": department.strip(),
        "slack_user_id": slack_user_id.strip(),
        "is_active": True,
    }
    role_value = _parse_role(role)
    errors = _validate_common(
        db,
        email=form["email"],
        name=form["name"],
        role=role_value,
        slack_user_id=_optional(slack_user_id),
    )
    password_error = validate_password(password)
    if password_error:
        errors.append(password_error)

    if errors:
        return _render_form(request, mode="new", form=form, errors=errors, status_code=400)

    user = User(
        email=form["email"],
        name=form["name"],
        role=role_value,
        department=_optional(department),
        slack_user_id=_optional(slack_user_id),
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()

    flash(
        request,
        f"{user.name}님의 계정을 발급했습니다. 초기 비밀번호를 본인에게 안전하게 전달하세요.",
        "success",
    )
    _warn_approver_count(request, db)
    return RedirectResponse("/admin/users", status_code=303)


# ---------------------------------------------------------------------------
# 계정 수정
# ---------------------------------------------------------------------------


@router.get("/users/{user_id}/edit")
def user_edit_form(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = _get_user_or_404(db, user_id)
    return _render_form(request, mode="edit", form=_user_to_form(user), errors=[], user=user)


@router.post("/users/{user_id}/edit", dependencies=[Depends(verify_csrf)])
def user_update(
    request: Request,
    user_id: int,
    email: str = Form(""),
    name: str = Form(""),
    role: str = Form(""),
    department: str = Form(""),
    slack_user_id: str = Form(""),
    is_active: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = _get_user_or_404(db, user_id)
    form = {
        "email": normalize_email(email),
        "name": name.strip(),
        "role": role,
        "department": department.strip(),
        "slack_user_id": slack_user_id.strip(),
        "is_active": is_active == "on",
    }
    role_value = _parse_role(role)
    errors = _validate_common(
        db,
        email=form["email"],
        name=form["name"],
        role=role_value,
        slack_user_id=_optional(slack_user_id),
        exclude_user_id=user.id,
    )

    # 관리자가 스스로를 잠그지 않도록 막는다.
    if user.id == admin.id:
        if role_value is not None and role_value != Role.ADMIN:
            errors.append("자기 자신의 시스템 관리자 역할은 바꿀 수 없습니다.")
        if not form["is_active"]:
            errors.append("자기 자신의 계정은 비활성화할 수 없습니다.")

    # 역할 변경이 기존 데이터와 충돌하지 않는지 확인한다.
    if role_value is not None and role_value != user.role:
        if user.role == Role.APPROVER:
            owned = db.scalar(
                select(func.count()).select_from(Project).where(Project.owner_id == user.id)
            )
            if owned:
                errors.append(
                    f"이 허가권자가 등록한 프로젝트가 {owned}개 있어 역할을 바꿀 수 없습니다."
                )
        if role_value == Role.ADMIN:
            assigned = db.scalar(
                select(func.count()).select_from(Assignment).where(Assignment.user_id == user.id)
            )
            if assigned:
                errors.append("프로젝트 배정 기록이 있는 사용자는 시스템 관리자로 바꿀 수 없습니다.")

    if errors:
        return _render_form(
            request, mode="edit", form=form, errors=errors, user=user, status_code=400
        )

    user.email = form["email"]
    user.name = form["name"]
    user.role = role_value
    user.department = _optional(department)
    user.slack_user_id = _optional(slack_user_id)
    user.is_active = form["is_active"]
    db.commit()

    flash(request, f"{user.name}님의 계정 정보를 저장했습니다.", "success")
    _warn_approver_count(request, db)
    return RedirectResponse("/admin/users", status_code=303)


# ---------------------------------------------------------------------------
# 비밀번호 재설정
# ---------------------------------------------------------------------------


@router.post("/users/{user_id}/password", dependencies=[Depends(verify_csrf)])
def user_reset_password(
    request: Request,
    user_id: int,
    new_password: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _get_user_or_404(db, user_id)
    password_error = validate_password(new_password)
    if password_error:
        flash(request, password_error, "error")
        return RedirectResponse(f"/admin/users/{user.id}/edit", status_code=303)

    user.password_hash = hash_password(new_password)
    db.commit()
    flash(
        request,
        f"{user.name}님의 비밀번호를 재설정했습니다. 새 비밀번호를 본인에게 안전하게 전달하세요.",
        "success",
    )
    return RedirectResponse(f"/admin/users/{user.id}/edit", status_code=303)
