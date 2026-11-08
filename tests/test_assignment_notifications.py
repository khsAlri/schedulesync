"""배정 이벤트와 알림 저장 통합 테스트.

실행: python -m unittest discover -s tests -v
임시 SQLite DB만 사용한다. 실제 Slack 발송은 하지 않는다.
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
    from app.assignment_notifications import (
        create_assignment,
        decide_assignment,
        respond_assignment,
    )
    from app.assignment_rules import RuleViolation
    from app.database import Base, configure_sqlite_transactions
    from app.models import (
        Assignment,
        AssignmentStatus,
        Notification,
        Project,
        ProjectSchedule,
        Role,
        SlackStatus,
        User,
    )


class AssignmentNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "events.db"
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
                    name=f"사용자 {user_id}",
                    email=f"user{user_id}@example.com",
                    role=role,
                    password_hash="unused",
                ))
            db.flush()
            db.add(Project(
                id=1,
                owner_id=1,
                name="알림 검증 프로젝트",
                schedules=[ProjectSchedule(
                    start_at=datetime(2026, 11, 10, 9),
                    end_at=datetime(2026, 11, 10, 12),
                )],
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

    def assign(self, target=3):
        return self.call(
            create_assignment,
            actor_id=1,
            project_id=1,
            target_id=target,
        )

    def accept(self, assignment_id, actor=3, accept=True):
        return self.call(
            respond_assignment,
            actor_id=actor,
            assignment_id=assignment_id,
            accept=accept,
        )

    def approve(self, assignment_id, actor, approve=True):
        return self.call(
            decide_assignment,
            actor_id=actor,
            assignment_id=assignment_id,
            approve=approve,
        )

    def recipients(self, result):
        if not result.notification_ids:
            return []
        with Session(self.engine) as db:
            rows = db.scalars(
                select(Notification)
                .where(Notification.id.in_(result.notification_ids))
                .order_by(Notification.user_id)
            ).all()
            self.assertEqual(len(rows), len(result.notification_ids))
            for row in rows:
                self.assertEqual(row.slack_status, SlackStatus.PENDING)
                self.assertEqual(row.link, "/assignments")
            return [row.user_id for row in rows]

    def count_notifications(self):
        with Session(self.engine) as db:
            return len(db.scalars(select(Notification.id)).all())

    def test_employee_flow_notifies_each_event_only(self):
        assigned = self.assign()
        self.assertEqual(self.recipients(assigned), [3])

        accepted = self.accept(assigned.assignment_id)
        self.assertEqual(self.recipients(accepted), [1, 2])

        first = self.approve(assigned.assignment_id, 2)
        self.assertEqual(first.status, "pending_approval")
        self.assertEqual(first.notification_ids, ())

        final = self.approve(assigned.assignment_id, 1)
        self.assertEqual(final.status, "confirmed")
        self.assertEqual(self.recipients(final), [1, 2, 3])
        self.assertEqual(self.count_notifications(), 6)

    def test_self_assignment_notifies_self_once(self):
        result = self.assign(target=1)
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(self.recipients(result), [1])

    def test_approver_acceptance_notifies_assigning_owner(self):
        assigned = self.assign(target=2)
        self.assertEqual(self.recipients(assigned), [2])
        accepted = self.accept(assigned.assignment_id, actor=2)
        self.assertEqual(accepted.status, "confirmed")
        self.assertEqual(self.recipients(accepted), [1])

    def test_decline_notifies_assigning_owner(self):
        assigned = self.assign()
        declined = self.accept(assigned.assignment_id, accept=False)
        self.assertEqual(declined.status, "declined")
        self.assertEqual(self.recipients(declined), [1])

    def test_rejection_notifies_employee_and_all_approvers(self):
        assigned = self.assign()
        self.accept(assigned.assignment_id)
        rejected = self.approve(
            assigned.assignment_id, actor=2, approve=False
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(self.recipients(rejected), [1, 2, 3])

    def test_duplicate_request_does_not_create_more_notifications(self):
        assigned = self.assign()
        before = self.count_notifications()
        with self.assertRaises(RuleViolation):
            self.assign()
        self.assertEqual(self.count_notifications(), before)

        self.accept(assigned.assignment_id)
        self.approve(assigned.assignment_id, actor=1)
        before = self.count_notifications()
        with self.assertRaises(RuleViolation):
            self.approve(assigned.assignment_id, actor=1)
        self.assertEqual(self.count_notifications(), before)

    def test_rollback_removes_assignment_and_notifications(self):
        with Session(self.engine, autoflush=False) as db:
            result = create_assignment(
                db, actor_id=1, project_id=1, target_id=3
            )
            self.assertTrue(result.notification_ids)
            db.rollback()

        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(Assignment.id)))
            self.assertIsNone(db.scalar(select(Notification.id)))

    def test_notification_storage_failure_rolls_back_business(self):
        assigned = self.assign()
        with self.assertRaises(RuntimeError):
            with Session(self.engine, autoflush=False) as db:
                with patch(
                    "app.assignment_notifications.store_notifications",
                    side_effect=RuntimeError("모의 저장 실패"),
                ):
                    respond_assignment(
                        db,
                        actor_id=3,
                        assignment_id=assigned.assignment_id,
                        accept=True,
                    )
                db.commit()

        with Session(self.engine) as db:
            assignment = db.get(Assignment, assigned.assignment_id)
            self.assertEqual(
                assignment.status, AssignmentStatus.PENDING_ACCEPT
            )
        self.assertEqual(self.count_notifications(), 1)


if __name__ == "__main__":
    unittest.main()
