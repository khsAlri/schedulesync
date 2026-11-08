"""일정 변경 이벤트 알림 테스트. 실제 슬랙에 접속하지 않는다.

실행: python -m unittest discover -s tests -v
"""

import os
import secrets
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

with patch.dict(
    os.environ,
    {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "DATABASE_URL": "sqlite:///:memory:",
    },
):
    from app.assignment_rules import RuleViolation
    from app.database import Base, configure_sqlite_transactions
    from app.models import (
        Assignment,
        AssignmentStatus,
        ChangeRequestStatus,
        Notification,
        Project,
        ProjectSchedule,
        Role,
        ScheduleChangeRequest,
        User,
    )
    from app.schedule_change_notifications import (
        cancel_change,
        respond_change,
        submit_change,
    )
    from app.schedule_change_service import preview_change


def period(start, end):
    return (
        datetime(2026, 11, 10, start),
        datetime(2026, 11, 10, end),
    )


class ScheduleChangeNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "change-events.db"
        self.engine = create_engine(f"sqlite:///{path.as_posix()}")
        configure_sqlite_transactions(self.engine)
        Base.metadata.create_all(self.engine)

        with Session(self.engine) as db:
            for user_id, role in [
                (1, Role.APPROVER),
                (2, Role.APPROVER),
                (3, Role.EMPLOYEE),
            ]:
                db.add(User(
                    id=user_id,
                    email=f"user{user_id}@example.com",
                    name=f"사용자 {user_id}",
                    password_hash="unused",
                    role=role,
                ))
            db.flush()
            for project_id, owner, hours in [
                (1, 1, (9, 10)),
                (2, 2, (11, 12)),
            ]:
                start, end = period(*hours)
                db.add(Project(
                    id=project_id,
                    owner_id=owner,
                    name=f"프로젝트 {project_id}",
                    schedules=[ProjectSchedule(start_at=start, end_at=end)],
                ))
            db.flush()
            for project_id, owner in [(1, 1), (2, 2)]:
                db.add(Assignment(
                    project_id=project_id,
                    user_id=3,
                    assigned_by_id=owner,
                    status=AssignmentStatus.CONFIRMED,
                ))
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def call(self, operation, **kwargs):
        with Session(self.engine, autoflush=False) as db:
            result = operation(db, **kwargs)
            db.commit()
            return result

    def submit(self, hours=(11, 12)):
        schedules = [period(*hours)]
        preview = self.call(
            preview_change,
            actor_id=1,
            project_id=1,
            new_schedules=schedules,
        )
        return self.call(
            submit_change,
            actor_id=1,
            project_id=1,
            new_schedules=schedules,
            expected_fingerprint=preview.fingerprint,
            warning_acknowledged=True,
        )

    def respond(self, request_id, actor, kind, approve=True):
        return self.call(
            respond_change,
            actor_id=actor,
            request_id=request_id,
            kind=kind,
            approve=approve,
        )

    def recipients(self, result):
        with Session(self.engine) as db:
            return list(db.scalars(
                select(Notification.user_id)
                .where(Notification.id.in_(result.notification_ids))
                .order_by(Notification.user_id)
            ).all())

    def test_request_partial_response_and_final_result(self):
        result = self.submit()
        self.assertEqual(self.recipients(result), [2, 3])

        partial = self.respond(result.request_id, 2, "approver_approval")
        self.assertEqual(partial.notification_ids, ())
        final = self.respond(result.request_id, 3, "member_consent")
        self.assertEqual(final.status, "applied")
        self.assertEqual(self.recipients(final), [1, 2, 3])

    def test_immediate_apply_notifies_requester_and_members(self):
        result = self.submit(hours=(10, 11))
        self.assertEqual(result.status, "applied")
        self.assertEqual(self.recipients(result), [1, 3])

    def test_rejection_notifies_related_users_and_keeps_schedule(self):
        result = self.submit()
        rejected = self.respond(
            result.request_id, 3, "member_consent", approve=False
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(self.recipients(rejected), [1, 2, 3])
        with Session(self.engine) as db:
            self.assertEqual(
                db.get(Project, 1).schedules[0].start_at, period(9, 10)[0]
            )

    def test_cancel_notifies_requester_members_and_responders(self):
        result = self.submit()
        cancelled = self.call(
            cancel_change, actor_id=1, request_id=result.request_id
        )
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.recipients(cancelled), [1, 2, 3])
        with self.assertRaises(RuleViolation):
            self.call(
                cancel_change, actor_id=1, request_id=result.request_id
            )

    def test_storage_failure_rolls_back_final_consent_and_schedule(self):
        result = self.submit()
        self.respond(result.request_id, 2, "approver_approval")

        with self.assertRaises(RuntimeError):
            with Session(self.engine, autoflush=False) as db:
                with patch(
                    "app.schedule_change_notifications.store_notifications",
                    side_effect=RuntimeError("모의 저장 실패"),
                ):
                    respond_change(
                        db,
                        actor_id=3,
                        request_id=result.request_id,
                        kind="member_consent",
                        approve=True,
                    )
                db.commit()

        with Session(self.engine) as db:
            self.assertEqual(
                db.get(ScheduleChangeRequest, result.request_id).status,
                ChangeRequestStatus.PENDING,
            )
            self.assertEqual(
                db.get(Project, 1).schedules[0].start_at, period(9, 10)[0]
            )
            self.assertEqual(
                len(db.scalars(select(Notification.id)).all()), 2
            )

    def test_rollback_removes_new_request_and_notifications(self):
        preview = self.call(
            preview_change,
            actor_id=1,
            project_id=1,
            new_schedules=[period(11, 12)],
        )
        with Session(self.engine, autoflush=False) as db:
            submit_change(
                db,
                actor_id=1,
                project_id=1,
                new_schedules=preview.schedules,
                expected_fingerprint=preview.fingerprint,
                warning_acknowledged=True,
            )
            db.rollback()

        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(ScheduleChangeRequest.id)))
            self.assertIsNone(db.scalar(select(Notification.id)))


if __name__ == "__main__":
    unittest.main()
