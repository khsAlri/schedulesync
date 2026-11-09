"""계정 발급 후 슬랙 사용자 자동 연결.

업무 커밋 후 백그라운드에서 실행한다.
조회 전·후 각각 별도 세션으로 검사하며 외부 통신 중에는 세션을 닫는다.
자동 재시도하지 않는다. 실패하더라도 발급된 계정은 유지한다.
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models import Role, User
from app.slack_client import SlackClient, SlackError

logger = logging.getLogger(__name__)


def _allowed(db, *, admin_id: int, user_id: int, expected_email: str) -> bool:
    if db.get_bind().dialect.name != "sqlite":
        return False

    admin = db.get(User, admin_id)
    user = db.get(User, user_id)
    return bool(
        admin is not None
        and admin.is_active
        and admin.role == Role.ADMIN
        and user is not None
        and user.is_active
        and user.email == expected_email
        and not user.slack_user_id
    )


def connect_slack_user(
    *,
    user_id: int,
    expected_email: str,
    admin_id: int,
    token: str,
    session_factory,
    client_factory=SlackClient,
) -> None:
    """空欄で発行したアカウントだけを自動接続する。

    expected_email은 발급 시점 이메일이다. 조회 도중 이메일이 바뀌었거나
    관리자가 슬랙 ID를 수동 입력했으면 결과를 저장하지 않는다.
    세션 팩토리는 호출자가 요청과 같은 DB 엔진으로 구성해 전달한다.
    """
    if not token.strip():
        return

    try:
        with session_factory() as db:
            allowed = _allowed(
                db,
                admin_id=admin_id,
                user_id=user_id,
                expected_email=expected_email,
            )
        if not allowed:
            return

        # 앞의 세션과 SQLite 트랜잭션이 종료된 상태에서만 조회한다.
        with client_factory(token) as client:
            slack_user_id = client.lookup_user_id(expected_email)

        with session_factory() as db:
            if not _allowed(
                db,
                admin_id=admin_id,
                user_id=user_id,
                expected_email=expected_email,
            ):
                return

            # 다른 계정에 이미 사용 중인 ID는 자동으로 중복 연결하지 않는다.
            duplicate = db.scalar(
                select(User.id)
                .where(
                    User.id != user_id,
                    User.slack_user_id == slack_user_id,
                )
                .limit(1)
            )
            if duplicate is not None:
                logger.warning(
                    "user_id=%s slack_mapping_code=duplicate_mapping",
                    user_id,
                )
                return

            user = db.get(User, user_id)
            user.slack_user_id = slack_user_id
            db.commit()

    except SlackError:
        # 외부 오류 코드도 이 작업에서는 저장하지 않는다.
        # 토큰·이메일·외부 응답·예외 본문은 로그에 포함하지 않는다.
        logger.warning(
            "user_id=%s slack_mapping_code=lookup_failed", user_id
        )
    except SQLAlchemyError:
        logger.warning(
            "user_id=%s slack_mapping_code=database_failed", user_id
        )
    except Exception:
        logger.warning(
            "user_id=%s slack_mapping_code=unexpected_failure", user_id
        )
