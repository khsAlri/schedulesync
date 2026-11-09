"""슬랙 자동 연결 테스트. 임시 DB와 모의 조회만 사용한다.

실행: python -m unittest discover -s tests -v
"""

import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

with patch.dict(
    os.environ,
    {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "DATABASE_URL": "sqlite:///:memory:",
    },
):
    from app.database import Base, configure_sqlite_transactions
    from app.models import Role, User
    from app.slack_client import SlackError
    from app.slack_mapping import connect_slack_user


class FakeClient:
    def __init__(self, lookup):
        self.lookup = lookup

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    def lookup_user_id(self, email):
        return self.lookup(email)


class SlackMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "mapping.db"
        self.engine = create_engine(f"sqlite:///{path.as_posix()}")
        configure_sqlite_transactions(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        Base.metadata.create_all(self.engine)

        with self.sessions() as db:
            db.add_all([
                User(
                    id=1, email="admin@example.com", name="관리자",
                    password_hash="unused", role=Role.ADMIN,
                ),
                User(
                    id=2, email="staff@example.com", name="직원",
                    password_hash="unused", role=Role.EMPLOYEE,
                ),
                User(
                    id=3, email="other@example.com", name="다른 직원",
                    password_hash="unused", role=Role.EMPLOYEE,
                ),
            ])
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def run_mapping(self, lookup, token=None):
        connect_slack_user(
            user_id=2,
            expected_email="staff@example.com",
            admin_id=1,
            token=secrets.token_urlsafe(32) if token is None else token,
            session_factory=self.sessions,
            client_factory=lambda value: FakeClient(lookup),
        )

    def mapping(self):
        with self.sessions() as db:
            return db.get(User, 2).slack_user_id

    def fail_if_called(self, email):
        self.fail("조회가 실행되면 안 됩니다.")

    def test_successful_lookup_is_saved(self):
        calls = []

        def lookup(email):
            calls.append(email)
            return "U01ABCDEF"

        self.run_mapping(lookup)
        self.assertEqual(calls, ["staff@example.com"])
        self.assertEqual(self.mapping(), "U01ABCDEF")

    def test_no_token_skips_lookup(self):
        self.run_mapping(self.fail_if_called, token="")
        self.assertIsNone(self.mapping())

    def test_manual_mapping_is_not_overwritten(self):
        with self.sessions() as db:
            db.get(User, 2).slack_user_id = "U02MANUAL"
            db.commit()
        self.run_mapping(self.fail_if_called)
        self.assertEqual(self.mapping(), "U02MANUAL")

    def test_lookup_failure_does_not_remove_account(self):
        def lookup(email):
            raise SlackError("users_not_found")

        self.run_mapping(lookup)
        self.assertIsNone(self.mapping())
        with self.sessions() as db:
            self.assertEqual(db.get(User, 2).email, "staff@example.com")

    def test_database_is_unlocked_during_network_lookup(self):
        def lookup(email):
            with self.sessions() as db:
                db.get(User, 2).name = "조회 중 수정 성공"
                db.commit()
            return "U01ABCDEF"

        self.run_mapping(lookup)
        self.assertEqual(self.mapping(), "U01ABCDEF")

    def test_manual_change_during_lookup_wins(self):
        def lookup(email):
            with self.sessions() as db:
                db.get(User, 2).slack_user_id = "U02MANUAL"
                db.commit()
            return "U01ABCDEF"

        self.run_mapping(lookup)
        self.assertEqual(self.mapping(), "U02MANUAL")

    def test_email_change_during_lookup_discards_result(self):
        def lookup(email):
            with self.sessions() as db:
                db.get(User, 2).email = "changed@example.com"
                db.commit()
            return "U01ABCDEF"

        self.run_mapping(lookup)
        self.assertIsNone(self.mapping())

    def test_admin_permission_change_discards_result(self):
        def lookup(email):
            with self.sessions() as db:
                db.get(User, 1).role = Role.EMPLOYEE
                db.commit()
            return "U01ABCDEF"

        self.run_mapping(lookup)
        self.assertIsNone(self.mapping())

    def test_inactive_target_skips_lookup(self):
        with self.sessions() as db:
            db.get(User, 2).is_active = False
            db.commit()
        self.run_mapping(self.fail_if_called)
        self.assertIsNone(self.mapping())

    def test_duplicate_mapping_is_not_saved(self):
        with self.sessions() as db:
            db.get(User, 3).slack_user_id = "U01ABCDEF"
            db.commit()
        self.run_mapping(lambda email: "U01ABCDEF")
        self.assertIsNone(self.mapping())


if __name__ == "__main__":
    unittest.main()
