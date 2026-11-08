"""SQLite DB를 사용하는 배정·승인 테스트.

실행: python -m unittest discover -s tests -v
requirements.txt 패키지 설치가 필요하다.
각 테스트는 별도 임시 DB를 만들며 운영 DB는 사용하지 않는다.
"""

import os
import secrets
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

# 모듈 로딩에 필요한 설정만 제공한다. 실제 테스트는 별도 엔진을 사용한다.
with patch.dict(
    os.environ,
    {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "DATABASE_URL": "sqlite:///:memory:",
    },
):
    from app.assignment_rules import RuleViolation
    from app.assignment_service import (
        create_assignment,
        decide_assignment,
        respond_assignment,
    )
    from app.database import Base, configure_sqlite_transactions
    from app.models import (
        Approval,
        Assignment,
        AssignmentStatus,
        Decision,
        Project,
        ProjectSchedule,
        Role,
        User,
    )


class AssignmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database_path = Path(self.temp.name) / "test.db"
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}"
        )
        configure_sqlite_transactions(self.engine)
        Base.metadata.create_all(self.engine)

        with Session(self.engine) as db:
            db.add_all([
                User(
                    id=1, name="허가권자 A", email="a@example.com",
                    password_hash="unused", role=Role.APPROVER,
                ),
                User(
                    id=2, name="허가권자 B", email="b@example.com",
                    password_hash="unused", role=Role.APPROVER,
                ),
                User(
                    id=3, name="직원", email="e@example.com",
                    password_hash="unused", role=Role.EMPLOYEE,
                ),
                User(
                    id=4, name="관리자", email="admin@example.com",
                    password_hash="unused", role=Role.ADMIN,
                ),
            ])
            db.flush()
            for project_id, owner_id in [(1, 1), (2, 2)]:
                db.add(
                    Project(
                        id=project_id,
                        name=f"프로젝트 {project_id}",
                        owner_id=owner_id,
                        schedules=[
                            ProjectSchedule(
                                start_at=datetime(2026, 11, 10, 9),
                                end_at=datetime(2026, 11, 10, 12),
                            )
                        ],
                    )
                )
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def call(self, operation, **kwargs):
        with Session(self.engine, autoflush=False) as db:
            result = operation(db, **kwargs)
            db.commit()
            return result

    def assign(self, actor=1, project=1, target=3):
        return self.call(
            create_assignment,
            actor_id=actor,
            project_id=project,
            target_id=target,
        )

    def accept(self, assignment_id, actor=3):
        return self.call(
            respond_assignment,
            actor_id=actor,
            assignment_id=assignment_id,
            accept=True,
        )

    def approve(self, assignment_id, actor, approve=True):
        return self.call(
            decide_assignment,
            actor_id=actor,
            assignment_id=assignment_id,
            approve=approve,
        )

    def test_employee_requires_both_approvals(self):
        assignment = self.assign()
        self.assertEqual(assignment.status, "pending_accept")
        self.assertEqual(
            self.accept(assignment.assignment_id).status,
            "pending_approval",
        )

        with Session(self.engine) as db:
            approvals = db.scalars(
                select(Approval).where(
                    Approval.assignment_id == assignment.assignment_id
                )
            ).all()
            self.assertEqual(
                {item.approver_id for item in approvals},
                {1, 2},
            )

        self.assertEqual(
            self.approve(assignment.assignment_id, 2).status,
            "pending_approval",
        )
        self.assertEqual(
            self.approve(assignment.assignment_id, 1).status,
            "confirmed",
        )

    def test_duplicate_assignment_and_repeat_responses_are_blocked(self):
        assignment = self.assign()
        with self.assertRaises(RuleViolation):
            self.assign()
        self.accept(assignment.assignment_id)
        with self.assertRaises(RuleViolation):
            self.accept(assignment.assignment_id)
        self.approve(assignment.assignment_id, 1)
        with self.assertRaises(RuleViolation):
            self.approve(assignment.assignment_id, 1)

    def test_owner_and_target_permissions(self):
        with self.assertRaises(RuleViolation):
            self.assign(actor=2, project=1)
        with self.assertRaises(RuleViolation):
            self.assign(target=4)

        assignment = self.assign()
        with self.assertRaises(RuleViolation):
            self.accept(assignment.assignment_id, actor=2)

    def test_one_rejection_rejects_assignment(self):
        assignment = self.assign()
        self.accept(assignment.assignment_id)
        self.approve(assignment.assignment_id, 1)
        result = self.approve(
            assignment.assignment_id, 2, approve=False
        )
        self.assertEqual(result.status, "rejected")
        with self.assertRaises(RuleViolation):
            self.approve(assignment.assignment_id, 2)

    def test_decline_allows_new_assignment(self):
        assignment = self.assign()
        result = self.call(
            respond_assignment,
            actor_id=3,
            assignment_id=assignment.assignment_id,
            accept=False,
        )
        self.assertEqual(result.status, "declined")
        replacement = self.assign()
        self.assertNotEqual(
            replacement.assignment_id, assignment.assignment_id
        )

    def test_approver_acceptance_needs_no_approvals(self):
        assignment = self.assign(target=2)
        result = self.accept(assignment.assignment_id, actor=2)
        self.assertEqual(result.status, "confirmed")
        with Session(self.engine) as db:
            self.assertIsNone(
                db.scalar(
                    select(Approval.id).where(
                        Approval.assignment_id == assignment.assignment_id
                    )
                )
            )

    def test_approver_conflict_blocks_acceptance_but_self_is_allowed(self):
        self.assign(actor=2, project=2, target=2)
        assignment = self.assign(target=2)
        with self.assertRaises(RuleViolation):
            self.accept(assignment.assignment_id, actor=2)

        # A가 B의 프로젝트에 먼저 확정된 뒤 자신의 프로젝트에 투입한다.
        other = self.assign(actor=2, project=2, target=1)
        self.accept(other.assignment_id, actor=1)
        own = self.assign(actor=1, project=1, target=1)
        self.assertEqual(own.status, "confirmed")
        self.assertEqual(own.conflicts, (2,))

    def test_final_approval_rechecks_new_conflict_and_rolls_back(self):
        first = self.assign()
        self.accept(first.assignment_id)
        self.approve(first.assignment_id, 1)

        second = self.assign(actor=2, project=2)
        self.accept(second.assignment_id)
        self.approve(second.assignment_id, 1)
        self.approve(second.assignment_id, 2)

        with self.assertRaises(RuleViolation):
            self.approve(first.assignment_id, 2)

        with Session(self.engine) as db:
            assignment = db.get(Assignment, first.assignment_id)
            approval = db.scalar(
                select(Approval).where(
                    Approval.assignment_id == first.assignment_id,
                    Approval.approver_id == 2,
                )
            )
            self.assertEqual(
                assignment.status, AssignmentStatus.PENDING_APPROVAL
            )
            self.assertEqual(approval.decision, Decision.PENDING)

        # 겹침이 있어도 반려는 가능하다.
        self.assertEqual(
            self.approve(
                first.assignment_id, 2, approve=False
            ).status,
            "rejected",
        )

    def test_adjacent_projects_can_both_be_confirmed(self):
        with Session(self.engine) as db:
            project = db.get(Project, 2)
            project.schedules[0].start_at = datetime(2026, 11, 10, 12)
            project.schedules[0].end_at = datetime(2026, 11, 10, 15)
            db.commit()

        first = self.assign()
        self.accept(first.assignment_id)
        self.approve(first.assignment_id, 1)
        self.approve(first.assignment_id, 2)

        second = self.assign(actor=2, project=2)
        self.assertEqual(second.conflicts, ())
        self.accept(second.assignment_id)
        self.approve(second.assignment_id, 1)
        result = self.approve(second.assignment_id, 2)
        self.assertEqual(result.status, "confirmed")

    def test_inactive_target_is_blocked(self):
        with Session(self.engine) as db:
            db.get(User, 3).is_active = False
            db.commit()
        with self.assertRaises(RuleViolation):
            self.assign()

    def test_changed_approver_role_blocks_approval(self):
        assignment = self.assign()
        self.accept(assignment.assignment_id)

        with Session(self.engine) as db:
            db.get(User, 2).role = Role.EMPLOYEE
            db.commit()

        with self.assertRaises(RuleViolation):
            self.approve(assignment.assignment_id, 1)

    def test_inactive_approver_blocks_approval(self):
        assignment = self.assign()
        self.accept(assignment.assignment_id)

        with Session(self.engine) as db:
            db.get(User, 2).is_active = False
            db.commit()

        with self.assertRaises(RuleViolation):
            self.approve(assignment.assignment_id, 1)

        with Session(self.engine) as db:
            self.assertEqual(
                db.get(Assignment, assignment.assignment_id).status,
                AssignmentStatus.PENDING_APPROVAL,
            )
            approvals = db.scalars(
                select(Approval).where(
                    Approval.assignment_id == assignment.assignment_id
                )
            ).all()
            self.assertTrue(
                all(item.decision == Decision.PENDING for item in approvals)
            )

    def test_second_sqlite_writer_is_blocked_then_can_retry(self):
        with self.engine.connect() as first:
            with self.engine.connect() as second:
                # exec_driver_sql을 쓰면 autobegin 이벤트가 실행된다.
                # 잠금을 잡기 전에 DBAPI 연결에 직접 제한 시간만 설정한다.
                raw_connection = second.connection.driver_connection
                cursor = raw_connection.cursor()
                try:
                    cursor.execute("PRAGMA busy_timeout=1")
                finally:
                    cursor.close()

                transaction = first.begin()
                try:
                    with self.assertRaises(OperationalError):
                        second.begin()
                    second.rollback()
                finally:
                    transaction.rollback()

                # 첫 트랜잭션 종료 후에는 정상적으로 시작할 수 있어야 한다.
                with second.begin():
                    self.assertEqual(
                        second.exec_driver_sql("SELECT 1").scalar_one(),
                        1,
                    )


if __name__ == "__main__":
    unittest.main()
