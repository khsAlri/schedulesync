"""실행: python -m unittest discover -s tests -v

이 파일의 테스트는 표준 라이브러리만 사용한다.
DB, SECRET_KEY, 웹 서버는 필요하지 않다.
"""

import unittest
from datetime import datetime, timezone

from app.assignment_rules import Participation, RuleViolation
from app.schedule_change_rules import (
    APPROVER_APPROVAL,
    MEMBER_CONSENT,
    build_change_plan,
    change_response_result,
    normalize_schedules,
    require_warning_acknowledgement,
)


def period(start_hour, end_hour):
    return (
        datetime(2026, 11, 10, start_hour),
        datetime(2026, 11, 10, end_hour),
    )


class ChangePlanTests(unittest.TestCase):
    def plan(self, **overrides):
        values = {
            "actor_id": 1,
            "actor_role": "approver",
            "project_id": 100,
            "owner_id": 1,
            "new_schedules": [period(10, 12)],
            "confirmed_user_ids": [10],
            "participations": [
                Participation(200, 10, "confirmed", (period(11, 13),)),
            ],
            "project_owner_ids": {200: 2},
        }
        values.update(overrides)
        return build_change_plan(**values)

    def test_external_project_requires_owner_and_affected_member(self):
        plan = self.plan()
        self.assertEqual(plan.conflicts, ((10, 200, 2),))
        self.assertEqual(
            set(plan.required_responses),
            {(2, APPROVER_APPROVAL), (10, MEMBER_CONSENT)},
        )
        self.assertTrue(plan.needs_warning)
        self.assertTrue(plan.needs_responses)

    def test_no_overlap_needs_no_warning_or_responses(self):
        plan = self.plan(new_schedules=[period(9, 11)])
        self.assertEqual(plan.conflicts, ())
        self.assertEqual(plan.required_responses, ())
        require_warning_acknowledgement(plan, acknowledged=False)

    def test_same_owner_only_requires_warning_acknowledgement(self):
        plan = self.plan(project_owner_ids={200: 1})
        self.assertEqual(plan.required_responses, ())
        with self.assertRaises(RuleViolation):
            require_warning_acknowledgement(plan, acknowledged=False)
        require_warning_acknowledgement(plan, acknowledged=True)

    def test_mixed_ownership_keeps_external_requirements(self):
        plan = self.plan(
            participations=[
                Participation(200, 10, "confirmed", (period(11, 13),)),
                Participation(300, 10, "confirmed", (period(11, 13),)),
            ],
            project_owner_ids={200: 1, 300: 2},
        )
        self.assertEqual(len(plan.conflicts), 2)
        self.assertEqual(
            set(plan.required_responses),
            {(2, APPROVER_APPROVAL), (10, MEMBER_CONSENT)},
        )

    def test_multiple_members_and_projects_deduplicate_responses(self):
        plan = self.plan(
            confirmed_user_ids=[10, 11, 12, 10],
            participations=[
                Participation(200, 10, "confirmed", (period(11, 13),)),
                Participation(300, 10, "confirmed", (period(11, 13),)),
                Participation(200, 11, "confirmed", (period(11, 13),)),
                Participation(400, 12, "confirmed", (period(13, 14),)),
            ],
            project_owner_ids={200: 2, 300: 2, 400: 3},
        )
        self.assertEqual(len(plan.conflicts), 3)
        self.assertEqual(
            set(plan.required_responses),
            {
                (2, APPROVER_APPROVAL),
                (10, MEMBER_CONSENT),
                (11, MEMBER_CONSENT),
            },
        )

    def test_pending_and_unrelated_participations_are_ignored(self):
        plan = self.plan(
            participations=[
                Participation(200, 10, "pending_approval", (period(11, 13),)),
                Participation(300, 11, "confirmed", (period(11, 13),)),
                Participation(100, 10, "confirmed", (period(11, 13),)),
            ],
            project_owner_ids={},
        )
        self.assertEqual(plan.conflicts, ())

    def test_no_confirmed_members_needs_no_responses(self):
        self.assertEqual(
            self.plan(confirmed_user_ids=[]).required_responses,
            (),
        )

    def test_requester_as_member_still_needs_consent_for_external_overlap(self):
        plan = self.plan(
            confirmed_user_ids=[1],
            participations=[
                Participation(200, 1, "confirmed", (period(11, 13),)),
            ],
        )
        self.assertIn((1, MEMBER_CONSENT), plan.required_responses)

    def test_one_user_can_have_separate_approval_and_consent(self):
        plan = self.plan(
            confirmed_user_ids=[2],
            participations=[
                Participation(200, 2, "confirmed", (period(11, 13),)),
            ],
        )
        self.assertEqual(
            set(plan.required_responses),
            {(2, APPROVER_APPROVAL), (2, MEMBER_CONSENT)},
        )

    def test_invalid_permissions_or_missing_owner_are_blocked(self):
        for overrides in [
            {"actor_id": 2},
            {"actor_role": "employee"},
            {"actor_role": "admin"},
            {"project_owner_ids": {}},
        ]:
            with self.subTest(overrides=overrides):
                with self.assertRaises(RuleViolation):
                    self.plan(**overrides)


class ScheduleValidationTests(unittest.TestCase):
    def test_sorting_and_adjacent_intervals(self):
        self.assertEqual(
            normalize_schedules([period(11, 12), period(9, 11)]),
            (period(9, 11), period(11, 12)),
        )

    def test_invalid_ranges(self):
        aware = datetime(2026, 11, 10, 9, tzinfo=timezone.utc)
        examples = [
            [],
            [period(12, 10)],
            [period(10, 10)],
            [period(9, 12), period(11, 13)],
            [period(9, 12), period(9, 12)],
            [(aware, aware)],
            [("invalid", "invalid")],
            [period(9, 10)] * 101,
        ]
        for schedules in examples:
            with self.subTest(schedules=schedules):
                with self.assertRaises(RuleViolation):
                    normalize_schedules(schedules)


class ChangeResponseTests(unittest.TestCase):
    def respond(self, **overrides):
        values = {
            "status": "pending",
            "actor_id": 2,
            "kind": APPROVER_APPROVAL,
            "decisions": {
                (2, APPROVER_APPROVAL): "pending",
                (10, MEMBER_CONSENT): "pending",
            },
            "approve": True,
        }
        values.update(overrides)
        return change_response_result(**values)

    def test_all_responses_are_needed_in_any_order(self):
        keys = [(2, APPROVER_APPROVAL), (10, MEMBER_CONSENT)]
        for order in [keys, list(reversed(keys))]:
            with self.subTest(order=order):
                original = {key: "pending" for key in keys}
                status, updated = self.respond(
                    actor_id=order[0][0],
                    kind=order[0][1],
                    decisions=original,
                )
                self.assertEqual(status, "pending")
                self.assertTrue(all(value == "pending" for value in original.values()))
                status, updated = self.respond(
                    status=status,
                    actor_id=order[1][0],
                    kind=order[1][1],
                    decisions=updated,
                )
                # ready는 재검사 신호이며 DB의 applied 상태가 아니다.
                self.assertEqual(status, "ready")

    def test_rejection_does_not_require_remaining_responses(self):
        status, updated = self.respond(approve=False)
        self.assertEqual(status, "rejected")
        self.assertEqual(updated[(10, MEMBER_CONSENT)], "pending")

    def test_same_user_must_respond_to_each_kind(self):
        decisions = {
            (2, APPROVER_APPROVAL): "pending",
            (2, MEMBER_CONSENT): "pending",
        }
        status, updated = self.respond(decisions=decisions)
        self.assertEqual(status, "pending")
        status, updated = self.respond(
            decisions=updated, kind=MEMBER_CONSENT
        )
        self.assertEqual(status, "ready")

    def test_invalid_states_actors_and_repeat_responses(self):
        examples = [
            {"status": "applied"},
            {"status": "cancelled"},
            {"status": "rejected"},
            {"actor_id": 99},
            {"kind": "unknown"},
            {"decisions": {}},
            {"decisions": {(2, APPROVER_APPROVAL): "approved"}},
            {
                "decisions": {
                    (2, APPROVER_APPROVAL): "pending",
                    (10, MEMBER_CONSENT): "rejected",
                }
            },
            {"decisions": {(2, APPROVER_APPROVAL): "unknown"}},
            {"decisions": {(2, "unknown"): "pending"}},
        ]
        for overrides in examples:
            with self.subTest(overrides=overrides):
                with self.assertRaises(RuleViolation):
                    self.respond(**overrides)


if __name__ == "__main__":
    unittest.main()
