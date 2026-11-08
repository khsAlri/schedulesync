"""사용자별 알림 조회와 읽음 처리.

호출자가 인증된 사용자 ID를 전달하고 commit/rollback한다.
관리자도 이 모듈에서는 다른 사용자의 알림을 조회하거나 수정하지 않는다.
"""

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Notification

PAGE_SIZE = 20


def unread_count(db: Session, user_id: int) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.user_id == user_id,
            Notification.is_read.is_(False),
        )
    ) or 0


def list_notifications(
    db: Session,
    *,
    user_id: int,
    page: int = 1,
    unread_only: bool = False,
):
    if page < 1:
        raise ValueError("페이지는 1 이상이어야 합니다.")

    conditions = [Notification.user_id == user_id]
    if unread_only:
        conditions.append(Notification.is_read.is_(False))

    rows = list(db.scalars(
        select(Notification)
        .where(*conditions)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE + 1)
    ).all())
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def mark_read(
    db: Session, *, user_id: int, notification_id: int
) -> bool:
    """본인 알림이면 읽음 처리한다. 다른 사람 알림과 없는 ID는 모두 False."""
    notification = db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
    )
    if notification is None:
        return False
    notification.is_read = True
    db.flush()
    return True


def mark_all_read(db: Session, *, user_id: int) -> int:
    result = db.execute(
        update(Notification)
        .where(
            Notification.user_id == user_id,
            Notification.is_read.is_(False),
        )
        .values(is_read=True)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount
