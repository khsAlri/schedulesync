"""사이트 내 알림 조회·읽음 처리 DB 테스트.

실행: python -m unittest discover -s tests -v
임시 SQLite DB만 사용한다. HTTP·CSRF 검증은 이 테스트 범위가 아니다.
"""

import os
import secrets
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

with patch.dict(
    os.environ,
    {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "DATABASE_URL": "sqlite:///:memory:",
    },
):
    from app.database import Base, configure_sqlite_transactions
    from app.models import Notification, Role, SlackStatus, User
    from app.notification_queries import (
        list_notifications,
        mark_all_read,
        mark_read,
        unread_count,
    )


class NotificationQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "queries.db"
        self.engine = create_engine(f"sqlite:///{path.as_posix()}")
        configure_sqlite_transactions(self.engine)
        Base.metadata.create_all(self.engine)

        with Session(self.engine) as db:
            for user_id in [1, 2]:
                db.add(User(
                    id=user_id,
                    email=f"user{user_id}@example.com",
                    name=f"사용자 {user_id}",
                    role=Role.EMPLOYEE,
                    password_hash="unused",
                ))
            db.flush()
            for notification_id in range(1, 23):
                db.add(Notification(
                    id=notification_id,
                    user_id=1,
                    message=f"알림 {notification_id}",
                    is_read=notification_id == 1,
                    created_at=datetime(2026, 11, 8, 9),
                    slack_status=SlackStatus.SENT,
                ))
            db.add(Notification(
                id=100,
                user_id=2,
                message="다른 사용자 전용",
                is_read=False,
                slack_status=SlackStatus.PENDING,
            ))
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def test_list_is_scoped_and_paginated_with_stable_order(self):
        with Session(self.engine) as db:
            rows, has_next = list_notifications(db, user_id=1)
            self.assertEqual(
                [row.id for row in rows], list(range(22, 2, -1))
            )
            self.assertTrue(has_next)
            rows, has_next = list_notifications(db, user_id=1, page=2)
            self.assertEqual([row.id for row in rows], [2, 1])
            self.assertFalse(has_next)

    def test_unread_filter_and_count_are_user_scoped(self):
        with Session(self.engine) as db:
            self.assertEqual(unread_count(db, 1), 21)
            self.assertEqual(unread_count(db, 2), 1)
            rows, _ = list_notifications(
                db, user_id=1, page=2, unread_only=True
            )
            self.assertEqual([row.id for row in rows], [2])

    def test_other_users_notification_cannot_be_marked(self):
        with Session(self.engine) as db:
            self.assertFalse(mark_read(db, user_id=1, notification_id=100))
            self.assertFalse(mark_read(db, user_id=1, notification_id=999))
            db.commit()
        with Session(self.engine) as db:
            self.assertFalse(db.get(Notification, 100).is_read)

    def test_mark_read_is_idempotent_and_does_not_change_slack_status(self):
        with Session(self.engine) as db:
            self.assertTrue(mark_read(db, user_id=1, notification_id=2))
            self.assertTrue(mark_read(db, user_id=1, notification_id=2))
            db.commit()
        with Session(self.engine) as db:
            row = db.get(Notification, 2)
            self.assertTrue(row.is_read)
            self.assertEqual(row.slack_status, SlackStatus.SENT)
            self.assertEqual(unread_count(db, 1), 20)

    def test_mark_all_only_changes_current_users_unread_notifications(self):
        with Session(self.engine) as db:
            self.assertEqual(mark_all_read(db, user_id=1), 21)
            db.commit()
        with Session(self.engine) as db:
            self.assertEqual(unread_count(db, 1), 0)
            self.assertEqual(unread_count(db, 2), 1)
            self.assertEqual(mark_all_read(db, user_id=1), 0)
            db.commit()

    def test_rollback_preserves_unread_state(self):
        with Session(self.engine) as db:
            mark_all_read(db, user_id=1)
            db.rollback()
        with Session(self.engine) as db:
            self.assertEqual(unread_count(db, 1), 21)

    def test_empty_page_and_invalid_page(self):
        with Session(self.engine) as db:
            rows, has_next = list_notifications(db, user_id=1, page=99)
            self.assertEqual(rows, [])
            self.assertFalse(has_next)
            with self.assertRaises(ValueError):
                list_notifications(db, user_id=1, page=0)


if __name__ == "__main__":
    unittest.main()
