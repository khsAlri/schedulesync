"""사이트 내 알림 목록과 읽음 처리."""

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user, verify_csrf
from app.models import User
from app.notification_queries import (
    list_notifications,
    mark_all_read,
    mark_read,
    unread_count,
)
from app.templating import flash, render

router = APIRouter(
    prefix="/notifications",
    dependencies=[Depends(require_user)],
)


def _safe_link(value: str | None) -> str | None:
    """과거 DB 데이터도 외부 주소·실행 가능한 주소로 연결하지 않는다."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or any(ord(character) <= 32 for character in value):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    return value


@router.get("")
def notification_list(
    request: Request,
    page: int = Query(1, ge=1, le=1_000_000),
    unread_only: bool = False,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    rows, has_next = list_notifications(
        db,
        user_id=user.id,
        page=page,
        unread_only=unread_only,
    )
    return render(
        request,
        "notifications.html",
        {
            "notifications": rows,
            "safe_links": {row.id: _safe_link(row.link) for row in rows},
            "page": page,
            "has_next": has_next,
            "unread_only": unread_only,
            "unread_total": unread_count(db, user.id),
        },
    )


# 고정 경로를 동적 ID 경로보다 먼저 등록한다.
@router.post("/read-all", dependencies=[Depends(verify_csrf)])
def read_all(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        count = mark_all_read(db, user_id=user.id)
        db.commit()
    except OperationalError:
        db.rollback()
        flash(request, "읽음 처리에 실패했습니다. 다시 시도하세요.", "error")
    else:
        flash(request, f"알림 {count}개를 읽음 처리했습니다.", "success")
    return RedirectResponse("/notifications", status_code=303)


@router.post("/{notification_id}/read", dependencies=[Depends(verify_csrf)])
def read_one(
    request: Request,
    notification_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        found = mark_read(
            db, user_id=user.id, notification_id=notification_id
        )
        if not found:
            db.rollback()
            raise HTTPException(status_code=404, detail="알림을 찾을 수 없습니다.")
        db.commit()
    except OperationalError:
        db.rollback()
        flash(request, "읽음 처리에 실패했습니다. 다시 시도하세요.", "error")
    else:
        flash(request, "알림을 읽음 처리했습니다.", "success")
    return RedirectResponse("/notifications", status_code=303)
