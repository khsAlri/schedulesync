"""일정 변경 서비스 테스트. 운영 DB 대신 임시 SQLite DB를 사용한다.

실행: python -m unittest discover -s tests -v
requirements.txt 패키지 설치가 필요하다.
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
        Decision,
        Project,
        ProjectSchedule,
        Role,
        ScheduleChangeRequest,
        ScheduleChangeResponse,
        User,
    )
    from app.schedule_change_models import ScheduleChangeBaseline
    from app.schedule_change_service import (
        cancel_change,
        preview_change,
        respond_change,
        submit_change,
    )


def period(start, end):
    return (
        datetime(2026, 11, 10, start),
        datetime(2026, 11, 10, end),
    )


class ScheduleChangeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "changes.db"
        self.engine = create_engine(f"sqlite:///{path.as_posix()}")
        configure_sqlite_transactions(self.engine)
        Base.metadata.create_all(self.engine)

        with Session(self.engine) as db:
            for user_id, role in [
                (1, Role.APPROVER),
                (2, Role.APPROVER),
                (3, Role.EMPLOYEE),
                (4, Role.EMPLOYEE),
            ]:
                db.add(User(
                    id=user_id,
                    email=f"user{user_id}@example.com",
                    name=f"사용자 {user_id}",
                    password_hash="unused",
                    role=role,
                ))
            db.flush()
            for project_id, owner_id, start, end in [
                (1, 1, 9, 10),
                (2, 2, 11, 12),
                (3, 1, 13, 14),
            ]:
                db.add(Project(
                    id=project_id,
                    owner_id=owner_id,
                    name=f"프로젝트 {project_id}",
                    schedules=[ProjectSchedule(
                        start_at=period(start, end)[0],
                        end_at=period(start, end)[1],
                    )],
                ))
            db.flush()
            for project_id in [1, 2]:
                db.add(Assignment(
                    project_id=project_id,
                    user_id=3,
                    assigned_by_id=1 if project_id == 1 else 2,
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

    def preview(self, schedules=None, actor_id=1):
        return self.call(
            preview_change,
            actor_id=actor_id,
            project_id=1,
            new_schedules=schedules or [period(11, 12)],
        )

    def submit(self, schedules=None, acknowledged=True):
        preview = self.preview(schedules)
        return self.call(
            submit_change,
            actor_id=1,
            project_id=1,
            new_schedules=preview.schedules,
            expected_fingerprint=preview.fingerprint,
            warning_acknowledged=acknowledged,
        )

    def respond(self, request_id, actor_id, kind, approve=True):
        return self.call(
            respond_change,
            request_id=request_id,
            actor_id=actor_id,
            kind=kind,
            approve=approve,
        )

    def assert_original(self):
        with Session(self.engine) as db:
            project = db.get(Project, 1)
            self.assertEqual(
                [(item.start_at, item.end_at) for item in project.schedules],
                [period(9, 10)],
            )

    def test_external_conflict_waits_for_approval_and_consent(self):
        result = self.submit()
        self.assertEqual(result.status, "pending")
        self.assert_original()

        result = self.respond(
            result.request_id, 3, "member_consent"
        )
        self.assertEqual(result.status, "pending")
        self.assert_original()

        result = self.respond(
            result.request_id, 2, "approver_approval"
        )
        self.assertEqual(result.status, "applied")
        with Session(self.engine) as db:
            project = db.get(Project, 1)
            self.assertEqual(
                [(item.start_at, item.end_at) for item in project.schedules],
                [period(11, 12)],
            )
            request = db.get(ScheduleChangeRequest, result.request_id)
            self.assertIsNotNone(request.resolved_at)
            self.assertIsNotNone(
                db.get(ScheduleChangeBaseline, result.request_id)
            )

    def test_no_conflict_is_applied_immediately(self):
        result = self.submit([period(10, 11)], acknowledged=False)
        self.assertEqual(result.status, "applied")

    def test_same_owner_requires_warning_but_no_responses(self):
        with Session(self.engine) as db:
            db.get(Project, 2).owner_id = 1
            db.commit()

        with self.assertRaises(RuleViolation):
            self.submit(acknowledged=False)
        self.assert_original()
        result = self.submit()
        self.assertEqual(result.status, "applied")
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(
                select(ScheduleChangeResponse.id).where(
                    ScheduleChangeResponse.change_request_id == result.request_id
                )
            ))

    def test_rejection_preserves_original_and_allows_new_request(self):
        result = self.submit()
        rejected = self.respond(
            result.request_id, 3, "member_consent", approve=False
        )
        self.assertEqual(rejected.status, "rejected")
        self.assert_original()
        replacement = self.submit()
        self.assertNotEqual(replacement.request_id, result.request_id)

    def test_only_requester_can_cancel(self):
        result = self.submit()
        with self.assertRaises(RuleViolation):
            self.call(
                cancel_change, actor_id=2, request_id=result.request_id
            )
        cancelled = self.call(
            cancel_change, actor_id=1, request_id=result.request_id
        )
        self.assertEqual(cancelled.status, "cancelled")
        self.assert_original()
        with self.assertRaises(RuleViolation):
            self.respond(result.request_id, 3, "member_consent")

    def test_duplicate_request_and_repeat_response_are_blocked(self):
        result = self.submit()
        with self.assertRaises(RuleViolation):
            self.submit()
        self.respond(result.request_id, 2, "approver_approval")
        with self.assertRaises(RuleViolation):
            self.respond(result.request_id, 2, "approver_approval")
        with self.assertRaises(RuleViolation):
            self.respond(result.request_id, 4, "member_consent")
        self.assert_original()

    def test_pending_assignment_blocks_request(self):
        with Session(self.engine) as db:
            db.add(Assignment(
                project_id=1,
                user_id=4,
                assigned_by_id=1,
                status=AssignmentStatus.PENDING_ACCEPT,
            ))
            db.commit()
        with self.assertRaises(RuleViolation):
            self.preview()

    def test_non_owner_and_employee_cannot_request(self):
        for actor_id in [2, 3]:
            with self.subTest(actor_id=actor_id):
                with self.assertRaises(RuleViolation):
                    self.preview(actor_id=actor_id)

    def test_preview_fingerprint_detects_intervening_change(self):
        preview = self.preview()
        with Session(self.engine) as db:
            db.get(Project, 2).schedules[0].end_at = period(11, 13)[1]
            db.commit()

        with self.assertRaises(RuleViolation):
            self.call(
                submit_change,
                actor_id=1,
                project_id=1,
                new_schedules=preview.schedules,
                expected_fingerprint=preview.fingerprint,
                warning_acknowledged=True,
            )
        self.assert_original()

    def test_changed_other_schedule_blocks_final_response_without_saving_it(self):
        result = self.submit()
        self.respond(result.request_id, 2, "approver_approval")
        with Session(self.engine) as db:
            db.get(Project, 2).schedules[0].end_at = period(11, 13)[1]
            db.commit()

        with self.assertRaises(RuleViolation):
            self.respond(result.request_id, 3, "member_consent")
        self.assert_original()
        with Session(self.engine) as db:
            consent = db.scalar(
                select(ScheduleChangeResponse).where(
                    ScheduleChangeResponse.change_request_id == result.request_id,
                    ScheduleChangeResponse.user_id == 3,
                )
            )
            self.assertEqual(consent.decision, Decision.PENDING)
            self.assertEqual(
                db.get(ScheduleChangeRequest, result.request_id).status,
                ChangeRequestStatus.PENDING,
            )

        # 기준이 바뀌었더라도 취소하거나 거부하는 것은 가능하다.
        self.assertEqual(
            self.respond(
                result.request_id, 3, "member_consent", approve=False
            ).status,
            "rejected",
        )

    def test_new_other_confirmed_assignment_invalidates_request(self):
        result = self.submit()
        with Session(self.engine) as db:
            db.add(Assignment(
                project_id=3,
                user_id=3,
                assigned_by_id=1,
                status=AssignmentStatus.CONFIRMED,
            ))
            db.commit()
        with self.assertRaises(RuleViolation):
            self.respond(result.request_id, 2, "approver_approval")
        self.assert_original()

    def test_inactive_responder_blocks_approval(self):
        result = self.submit()
        with Session(self.engine) as db:
            db.get(User, 3).is_active = False
            db.commit()
        with self.assertRaises(RuleViolation):
            self.respond(result.request_id, 2, "approver_approval")
        self.assert_original()

    def test_missing_baseline_fails_closed_but_can_be_cancelled(self):
        result = self.submit()
        with Session(self.engine) as db:
            db.delete(db.get(ScheduleChangeBaseline, result.request_id))
            db.commit()
        with self.assertRaises(RuleViolation):
            self.respond(result.request_id, 2, "approver_approval")
        self.assertEqual(
            self.call(
                cancel_change, actor_id=1, request_id=result.request_id
            ).status,
            "cancelled",
        )

    def test_same_schedule_is_not_a_change(self):
        with self.assertRaises(RuleViolation):
            self.preview([period(9, 10)])


if __name__ == "__main__":
    unittest.main()
