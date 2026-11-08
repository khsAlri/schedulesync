"""라우터에서 쓰는 공통 의존성: 로그인 사용자 확인, 권한 검사, CSRF 검사."""

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.notification_queries import unread_count
from app.security import is_valid_csrf_token


class LoginRequired(Exception):
    """로그인이 필요한 화면에 로그인하지 않고 접근했을 때."""


class PermissionDenied(Exception):
    """권한이 없거나 요청이 유효하지 않을 때."""

    def __init__(self, message: str = "이 작업을 할 권한이 없습니다."):
        super().__init__(message)
        self.message = message


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """세션의 로그인 사용자를 확인하고 메뉴용 미확인 알림 수를 계산한다."""
    user = None
    user_id = request.session.get("user_id")
    if user_id is not None:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            request.session.pop("user_id", None)
            user = None

    request.state.user = user
    # 기존 요청 DB 세션을 재사용한다. 별도 세션으로 SQLite 잠금을 잡지 않는다.
    request.state.unread_notification_count = (
        unread_count(db, user.id) if user is not None else 0
    )
    return user


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise LoginRequired()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise PermissionDenied("시스템 관리자만 접근할 수 있습니다.")
    return user


async def verify_csrf(request: Request) -> None:
    """POST 폼에 담긴 CSRF 토큰이 세션의 토큰과 같은지 확인한다."""
    form = await request.form()
    if not is_valid_csrf_token(request, form.get("csrf_token")):
        raise PermissionDenied("요청이 만료되었습니다. 페이지를 새로고침한 뒤 다시 시도하세요.")
