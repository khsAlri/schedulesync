"""임시 SQLite DB와 모의 클라이언트를 사용하는 알림 테스트.

설치: pip install -r requirements-dev.txt
실행: python -m unittest discover -s tests -v

실제 슬랙 토큰과 네트워크 연결을 사용하지 않는다.
"""

import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

with patch.dict(
    os.environ,
    {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "DATABASE_URL": "sqlite:///:memory:",
    },
):
    from app.database import Base, configure_sqlite_transactions
    from app.models import Notification, Role, SlackStatus, User
    from app.notification_service import (
        dispatch_notifications,
        store_notifications,
    )
    from app.slack_client import SlackError


class FakeClient:
    def __init__(self, send):
        self.send = send

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    def send_dm(self, user_id, text):
        return self.send(user_id, text)


class NotificationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "notifications.db"
        self.engine = create_engine(f"sqlite:///{path.as_posix()}")
        configure_sqlite_transactions(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        Base.metadata.create_all(self.engine)
        self.token = secrets.token_urlsafe(32)

        with self.sessions() as db:
            db.add_all([
                User(
                    id=1, email="one@example.com", name="직원 1",
                    role=Role.EMPLOYEE, password_hash="unused",
                    slack_user_id="U01ABCDEF",
                ),
                User(
                    id=2, email="two@example.com", name="직원 2",
                    role=Role.EMPLOYEE, password_hash="unused",
                ),
                User(
                    id=3, email="three@example.com", name="비활성 직원",
                    role=Role.EMPLOYEE, password_hash="unused",
                    slack_user_id="U03ABCDEF", is_active=False,
                ),
            ])
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def store(self, user_ids=(1,), link="/assignments"):
        with self.sessions() as db:
            ids = store_notifications(
                db,
                user_ids=user_ids,
                message="프로젝트 배정 안내",
                link=link,
            )
            db.commit()
            return ids

    def dispatch(self, ids, send, token=None, base="https://schedule.example.com"):
        dispatch_notifications(
            ids,
            token=self.token if token is None else token,
            app_base_url=base,
            session_factory=self.sessions,
            client_factory=lambda value: FakeClient(send),
        )

    def state(self, notification_id):
        with self.sessions() as db:
            row = db.get(Notification, notification_id)
            return row.slack_status, row.slack_error

    def fail_if_called(self, user_id, text):
        self.fail("슬랙 발송이 호출되면 안 됩니다.")

    def test_deduplicate_recipients_and_skip_inactive_or_missing_users(self):
        ids = self.store((1, 1, 2, 3, 999))
        self.assertEqual(len(ids), 2)
        with self.sessions() as db:
            users = db.scalars(
                select(Notification.user_id).order_by(Notification.user_id)
            ).all()
            self.assertEqual(users, [1, 2])

    def test_business_and_notifications_roll_back_together(self):
        with self.sessions() as db:
            db.get(User, 1).name = "임시 변경"
            store_notifications(db, user_ids=[1], message="임시 알림")
            db.rollback()

        with self.sessions() as db:
            self.assertEqual(db.get(User, 1).name, "직원 1")
            self.assertIsNone(db.scalar(select(Notification.id)))

    def test_success_and_duplicate_dispatch_are_sent_once(self):
        ids = self.store()
        calls = []

        def send(user_id, text):
            calls.append((user_id, text))
            return "1234567890.123456"

        self.dispatch([*ids, *ids], send)
        self.dispatch(ids, send)
        self.assertEqual(
            calls,
            [(
                "U01ABCDEF",
                "프로젝트 배정 안내\nhttps://schedule.example.com/assignments",
            )],
        )
        self.assertEqual(self.state(ids[0]), (SlackStatus.SENT, None))

    def test_network_call_runs_without_database_write_lock(self):
        ids = self.store()

        def send(user_id, text):
            # 다른 DBAPI 연결에서 즉시 쓰기 잠금을 확보할 수 있어야 한다.
            with Session(self.engine) as db:
                db.get(User, 1).name = "통신 중 DB 접근 성공"
                db.commit()
            return "1234567890.123456"

        self.dispatch(ids, send)
        self.assertEqual(self.state(ids[0]), (SlackStatus.SENT, None))
        with self.sessions() as db:
            self.assertEqual(db.get(User, 1).name, "통신 중 DB 접근 성공")

    def test_no_token_or_missing_mapping_are_skipped(self):
        first = self.store()
        self.dispatch(first, self.fail_if_called, token="")
        self.assertEqual(
            self.state(first[0]),
            (SlackStatus.SKIPPED, "not_configured"),
        )

        second = self.store((2,))
        self.dispatch(second, self.fail_if_called)
        self.assertEqual(
            self.state(second[0]),
            (SlackStatus.SKIPPED, "missing_slack_user_id"),
        )

    def test_deactivated_recipient_is_not_sent(self):
        ids = self.store()
        with self.sessions() as db:
            db.get(User, 1).is_active = False
            db.commit()
        self.dispatch(ids, self.fail_if_called)
        self.assertEqual(
            self.state(ids[0]), (SlackStatus.SKIPPED, "inactive_user")
        )

    def test_slack_failure_preserves_notification_and_is_not_retried(self):
        ids = self.store()
        calls = []

        def send(user_id, text):
            calls.append(user_id)
            raise SlackError("timeout")

        self.dispatch(ids, send)
        self.dispatch(ids, send)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            self.state(ids[0]), (SlackStatus.FAILED, "timeout")
        )
        with self.sessions() as db:
            self.assertEqual(
                db.get(Notification, ids[0]).message, "프로젝트 배정 안내"
            )

    def test_unknown_error_does_not_store_sensitive_text(self):
        ids = self.store()

        def send(user_id, text):
            raise RuntimeError(self.token)

        self.dispatch(ids, send)
        self.assertEqual(
            self.state(ids[0]),
            (SlackStatus.FAILED, "unexpected_delivery_error"),
        )

    def test_invalid_base_url_fails_without_network(self):
        ids = self.store()
        self.dispatch(
            ids,
            self.fail_if_called,
            base="https://user:password@example.com",
        )
        self.assertEqual(
            self.state(ids[0]),
            (SlackStatus.FAILED, "invalid_delivery_configuration"),
        )

    def test_interrupted_attempt_is_not_automatically_resent(self):
        ids = self.store()
        with self.sessions() as db:
            notification = db.get(Notification, ids[0])
            notification.slack_status = SlackStatus.FAILED
            notification.slack_error = "delivery_unconfirmed"
            db.commit()

        self.dispatch(ids, self.fail_if_called)
        self.assertEqual(
            self.state(ids[0]),
            (SlackStatus.FAILED, "delivery_unconfirmed"),
        )

    def test_invalid_message_or_external_link_is_rejected(self):
        for message, link in [
            ("", None),
            (" " * 3, None),
            ("a" * 3001, None),
            ("안내", "https://example.com"),
            ("안내", "//example.com"),
            ("안내", "/unsafe\npath"),
            ("안내", "/unsafe\\path"),
        ]:
            with self.subTest(message_length=len(message), link=link):
                with self.sessions() as db:
                    with self.assertRaises(ValueError):
                        store_notifications(
                            db, user_ids=[1], message=message, link=link
                        )
                    db.rollback()

    def test_missing_notification_is_ignored(self):
        self.dispatch([999], self.fail_if_called)


if __name__ == "__main__":
    unittest.main()
