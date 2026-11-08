"""알림 저장과 커밋 후 슬랙 발송.

사용 순서:
1. 요청 전용 세션에서 업무 변경과 store_notifications를 호출한다.
2. 호출자가 commit한다. 실패하면 업무 변경과 알림을 함께 rollback한다.
3. 요청 세션의 트랜잭션을 종료한 뒤 dispatch_notifications를 호출한다.

발송 함수에는 알림 ID만 전달한다. 세션이나 ORM 객체는 전달하지 않는다.
실제 네트워크 통신은 선점 트랜잭션을 커밋하고 세션을 닫은 후 수행한다.

자동 재시도는 하지 않는다. 발송 직전 FAILED/delivery_unconfirmed로
선점해 다른 실행이 같은 알림을 다시 발송하지 못하게 한다.
정상 종료하면 SENT 또는 구체적인 실패 코드로 갱신한다.
선점 후 프로세스가 종료되면 delivery_unconfirmed가 남는다.
이 상태는 실제 발송 실패가 확정되었다는 뜻이 아니다.
"""

import logging
from collections.abc import Iterable
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Notification, SlackStatus, User
from app.slack_client import SlackClient, SlackError

logger = logging.getLogger(__name__)

_SAFE_SLACK_ERRORS = frozenset({
    "not_configured",
    "timeout",
    "network_error",
    "ratelimited",
    "service_unavailable",
    "http_error",
    "invalid_response",
    "api_error",
    "invalid_user_id",
    "invalid_message",
    "invalid_auth",
    "not_authed",
    "token_revoked",
    "account_inactive",
    "missing_scope",
    "users_not_found",
    "user_not_found",
    "user_not_visible",
    "channel_not_found",
    "cannot_dm_bot",
    "cannot_dm_self",
    "restricted_action",
    "not_in_channel",
    "is_archived",
    "internal_error",
})


def _check_sqlite(db: Session) -> None:
    if db.get_bind().dialect.name != "sqlite":
        raise ValueError("알림 저장·발송은 현재 SQLite에서만 지원합니다.")


def _validate_link(link: str | None) -> None:
    if link is None:
        return
    if (
        not isinstance(link, str)
        or len(link) > 500
        or not link.startswith("/")
        or link.startswith("//")
        or "\\" in link
        or any(ord(character) <= 32 for character in link)
    ):
        raise ValueError("알림 링크는 유효한 사이트 내부 경로여야 합니다.")


def store_notifications(
    db: Session,
    *,
    user_ids: Iterable[int],
    message: str,
    link: str | None = None,
) -> tuple[int, ...]:
    """활성 수신자별 알림을 저장하고 ID를 반환한다. commit하지 않는다.

    동일 호출 안의 수신자 중복만 제거한다.
    업무 이벤트 자체의 중복 처리는 업무 서비스의 상태 검사로 방지해야 한다.
    비활성·없는 계정은 건너뛴다.
    """
    _check_sqlite(db)
    if not isinstance(message, str) or not message.strip() or len(message) > 3000:
        raise ValueError("알림 메시지는 공백이 아닌 1~3,000자여야 합니다.")
    _validate_link(link)

    ids = sorted(set(user_ids))
    if not ids:
        return ()

    active_ids = db.scalars(
        select(User.id)
        .where(User.id.in_(ids), User.is_active.is_(True))
        .order_by(User.id)
    ).all()
    notifications = [
        Notification(
            user_id=user_id,
            message=message,
            link=link,
            slack_status=SlackStatus.PENDING,
        )
        for user_id in active_ids
    ]
    db.add_all(notifications)
    db.flush()
    return tuple(item.id for item in notifications)


def _message_text(message: str, link: str | None, app_base_url: str) -> str:
    if link is None:
        return message

    _validate_link(link)
    base = app_base_url.strip().rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in base
        or any(ord(character) <= 32 for character in base)
    ):
        raise ValueError("APP_BASE_URL 설정이 올바르지 않습니다.")

    text = f"{message}\n{base}{link}"
    if len(text) > 4000:
        raise ValueError("슬랙 메시지가 너무 깁니다.")
    return text


def _claim(session_factory, notification_id: int, token: str, app_base_url: str):
    """발송 데이터를 복사하고 시도를 선점한다. 반환 전에 세션을 닫는다."""
    with session_factory() as db:
        _check_sqlite(db)
        notification = db.get(Notification, notification_id)
        if notification is None or notification.slack_status != SlackStatus.PENDING:
            return None

        user = db.get(User, notification.user_id)
        skip_reason = None
        if user is None or not user.is_active:
            skip_reason = "inactive_user"
        elif not token.strip():
            skip_reason = "not_configured"
        elif not user.slack_user_id:
            skip_reason = "missing_slack_user_id"

        if skip_reason:
            notification.slack_status = SlackStatus.SKIPPED
            notification.slack_error = skip_reason
            db.commit()
            return None

        try:
            text = _message_text(
                notification.message, notification.link, app_base_url
            )
        except ValueError:
            notification.slack_status = SlackStatus.FAILED
            notification.slack_error = "invalid_delivery_configuration"
            db.commit()
            return None

        user_id = user.slack_user_id
        notification.slack_status = SlackStatus.FAILED
        notification.slack_error = "delivery_unconfirmed"
        db.commit()
        return user_id, text


def _record_result(session_factory, notification_id: int, status, error) -> None:
    with session_factory() as db:
        _check_sqlite(db)
        db.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.slack_status == SlackStatus.FAILED,
                Notification.slack_error == "delivery_unconfirmed",
            )
            .values(slack_status=status, slack_error=error)
        )
        db.commit()


def dispatch_notifications(
    notification_ids: Iterable[int],
    *,
    token: str,
    app_base_url: str,
    session_factory=SessionLocal,
    client_factory=SlackClient,
) -> None:
    """커밋된 알림만 발송한다. HTTP 응답 이후 백그라운드 작업에서 사용 가능하다.

    외부 통신·발송 결과 저장 오류는 업무 성공을 되돌리지 않는다.
    로그에는 알림 ID와 정해진 코드만 기록한다.
    토큰·사용자 정보·메시지·외부 예외 본문은 기록하지 않는다.

    이 함수는 내구성 있는 작업 큐가 아니다. 업무 커밋 후 이 함수 호출 전
    프로세스가 종료되면 PENDING이 남고 자동 복구 발송은 하지 않는다.
    """
    for notification_id in sorted(set(notification_ids)):
        try:
            payload = _claim(
                session_factory, notification_id, token, app_base_url
            )
        except (SQLAlchemyError, ValueError):
            logger.warning(
                "notification_id=%s delivery_code=claim_failed",
                notification_id,
            )
            continue

        if payload is None:
            continue

        user_id, text = payload
        status = SlackStatus.SENT
        error = None
        try:
            # 이 지점에는 열려 있는 DB 세션·트랜잭션이 없다.
            with client_factory(token) as client:
                client.send_dm(user_id, text)
        except SlackError as exc:
            status = SlackStatus.FAILED
            error = exc.code if exc.code in _SAFE_SLACK_ERRORS else "api_error"
        except Exception:
            # 외부 예외의 본문이나 traceback에는 민감 정보가 있을 수 있다.
            status = SlackStatus.FAILED
            error = "unexpected_delivery_error"

        if error is not None:
            logger.warning(
                "notification_id=%s delivery_code=%s",
                notification_id,
                error,
            )

        try:
            _record_result(
                session_factory, notification_id, status, error
            )
        except (SQLAlchemyError, ValueError):
            # 선점 상태가 남으므로 자동 재발송하지 않는다.
            logger.warning(
                "notification_id=%s delivery_code=result_save_failed",
                notification_id,
            )
