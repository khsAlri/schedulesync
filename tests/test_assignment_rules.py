"""실행: python -m unittest discover -s tests -v

표준 라이브러리만 사용한다. DB, SECRET_KEY, 웹 서버가 필요하지 않다.
"""

import unittest
from datetime import datetime, timezone

from app.assignment_rules import (
    Participation,
    RuleViolation,
    acceptance_result,
    approval_result,
    conflicting_project_ids,
    initial_assignment_status,
)


def period(start_hour: int, end_hour: int):
    return (
        datetime(2026, 11, 10, start_hour),
        datetime(2026, 11, 10, end_hour),
    )


class ConflictTests(unittest.TestCase):
    def test_only_same_user_other_confirmed_projects_are_compared(self):
        participations = [
            Participation(2, 10, "confirmed", (period(10, 12),)),
            Participation(3, 10, "pending_approval", (period(10, 12),)),
            Participation(4, 11, "confirmed", (period(10, 12),)),
            Participation(1, 10, "confirmed", (period(10, 12),)),
            Participation(5, 10, "pending_accept", (period(10, 12),)),
            Participation(6, 10, "cancelled", (period(10, 12),)),
        ]
        self.assertEqual(
            conflicting_project_ids(
                project_id=1,
                user_id=10,
                schedules=[period(9, 11)],
                participations=participations,
            ),
            (2,),
        )

    def test_adjacent_times_and_gaps_are_allowed(self):
        self.assertEqual(
            conflicting_project_ids(
                project_id=1,
                user_id=10,
                schedules=[period(9, 10), period(12, 13)],
                participations=[
                    Participation(2, 10, "confirmed", (period(10, 12),)),
                ],
            ),
            (),
        )

    def test_containment_and_duplicate_project_ids(self):
        self.assertEqual(
            conflicting_project_ids(
                project_id=1,
                user_id=10,
                schedules=[period(10, 11)],
                participations=[
                    Participation(3, 10, "confirmed", (period(9, 12),)),
                    Participation(2, 10, "confirmed", (period(10, 11),)),
                    Participation(3, 10, "confirmed", (period(10, 12),)),
                ],
            ),
            (2, 3),
        )

    def test_invalid_schedules_fail_closed(self):
        start = datetime(2026, 11, 10, 9)
        for schedules in [
            [],
            [(start, start)],
            [(start, datetime(2026, 11, 10, 8))],
            [(start.replace(tzinfo=timezone.utc), start)],
        ]:
            with self.subTest(schedules=schedules):
                with self.assertRaises(RuleViolation):
                    conflicting_project_ids(
                        project_id=1,
                        user_id=10,
                        schedules=schedules,
                        participations=[],
                    )


class AssignmentTests(unittest.TestCase):
    def assign(self, **overrides):
        values = {
            "actor_id": 1,
            "actor_role": "approver",
            "owner_id": 1,
            "target_id": 10,
            "target_role": "employee",
            "has_active_assignment": False,
        }
        values.update(overrides)
        return initial_assignment_status(**values)

    def test_self_assignment_is_immediately_confirmed(self):
        self.assertEqual(
            self.assign(target_id=1, target_role="approver"),
            "confirmed",
        )

    def test_other_users_need_acceptance(self):
        for role in ["employee", "approver"]:
            self.assertEqual(self.assign(target_role=role), "pending_accept")

    def test_invalid_assignment_is_blocked(self):
        for overrides in [
            {"actor_id": 2},
            {"actor_role": "employee"},
            {"actor_role": "admin"},
            {"target_role": "admin"},
            {"has_active_assignment": True},
            {"target_id": 1, "target_role": "employee"},
        ]:
            with self.subTest(overrides=overrides):
                with self.assertRaises(RuleViolation):
                    self.assign(**overrides)


class AcceptanceTests(unittest.TestCase):
    def respond(self, **overrides):
        values = {
            "status": "pending_accept",
            "actor_id": 10,
            "target_id": 10,
            "target_role": "employee",
            "accept": True,
            "has_conflict": False,
            "owner_id": 1,
            "approver_ids": [2, 1, 2],
        }
        values.update(overrides)
        return acceptance_result(**values)

    def test_employee_requires_all_approvers_including_owner(self):
        self.assertEqual(self.respond(), ("pending_approval", (1, 2)))

    def test_employee_can_accept_but_cannot_yet_be_confirmed_with_conflict(self):
        self.assertEqual(
            self.respond(has_conflict=True),
            ("pending_approval", (1, 2)),
        )

    def test_approver_acceptance_needs_no_approval(self):
        self.assertEqual(
            self.respond(target_role="approver", approver_ids=[]),
            ("confirmed", ()),
        )

    def test_approver_acceptance_with_conflict_is_blocked(self):
        with self.assertRaises(RuleViolation):
            self.respond(target_role="approver", has_conflict=True)

    def test_decline_is_allowed_despite_conflict(self):
        self.assertEqual(
            self.respond(accept=False, has_conflict=True),
            ("declined", ()),
        )

    def test_invalid_actor_state_and_approver_list_are_blocked(self):
        for overrides in [
            {"actor_id": 11},
            {"status": "confirmed"},
            {"target_role": "admin"},
            {"approver_ids": []},
            {"approver_ids": [2]},
        ]:
            with self.subTest(overrides=overrides):
                with self.assertRaises(RuleViolation):
                    self.respond(**overrides)


class ApprovalTests(unittest.TestCase):
    def decide(self, **overrides):
        values = {
            "status": "pending_approval",
            "actor_id": 1,
            "decisions": {1: "pending", 2: "pending"},
            "approve": True,
            "has_conflict": False,
        }
        values.update(overrides)
        return approval_result(**values)

    def test_all_approvers_must_approve_in_any_order(self):
        for first, second in [(1, 2), (2, 1)]:
            with self.subTest(first=first):
                original = {1: "pending", 2: "pending"}
                status, decisions = self.decide(
                    actor_id=first, decisions=original
                )
                self.assertEqual(status, "pending_approval")
                self.assertEqual(original, {1: "pending", 2: "pending"})
                status, decisions = self.decide(
                    status=status, actor_id=second, decisions=decisions
                )
                self.assertEqual(status, "confirmed")
                self.assertTrue(
                    all(value == "approved" for value in decisions.values())
                )

    def test_one_rejection_rejects_entire_assignment(self):
        status, decisions = self.decide(
            actor_id=2,
            decisions={1: "approved", 2: "pending"},
            approve=False,
            has_conflict=True,
        )
        self.assertEqual(status, "rejected")
        self.assertEqual(decisions[2], "rejected")

    def test_conflict_blocks_first_and_final_approval(self):
        for decisions in [
            {1: "pending", 2: "pending"},
            {1: "pending", 2: "approved"},
        ]:
            with self.subTest(decisions=decisions):
                with self.assertRaises(RuleViolation):
                    self.decide(decisions=decisions, has_conflict=True)
                self.assertEqual(decisions[1], "pending")

    def test_invalid_state_actor_and_repeat_response_are_blocked(self):
        for overrides in [
            {"status": "confirmed"},
            {"status": "rejected"},
            {"actor_id": 3},
            {"decisions": {}},
            {"decisions": {1: "approved", 2: "pending"}},
            {"decisions": {1: "pending", 2: "rejected"}},
            {"decisions": {1: "pending", 2: "unknown"}},
        ]:
            with self.subTest(overrides=overrides):
                with self.assertRaises(RuleViolation):
                    self.decide(**overrides)


if __name__ == "__main__":
    unittest.main()
