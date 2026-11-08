"""확정 후 일정 변경의 순수 업무 규칙.

DB와 HTTP 처리는 하지 않는다. 일정 반영 여부의 최종 판단과 저장은
서비스 계층에서 최신 DB 상태를 읽고 같은 트랜잭션 안에서 수행해야 한다.

응답 키는 기존 ScheduleChangeResponse의 (user_id, kind)에 대응한다.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from app.assignment_rules import (
    Participation,
    RuleViolation,
    conflicting_project_ids,
)

TimeRange = tuple[datetime, datetime]
ResponseKey = tuple[int, str]
ConflictKey = tuple[int, int, int]

APPROVER_APPROVAL = "approver_approval"
MEMBER_CONSENT = "member_consent"
RESPONSE_KINDS = frozenset({APPROVER_APPROVAL, MEMBER_CONSENT})


@dataclass(frozen=True)
class ChangePlan:
    # (겹치는 참여자 ID, 상대 프로젝트 ID, 상대 등록자 ID)
    conflicts: tuple[ConflictKey, ...]
    # (응답할 사용자 ID, 승인 또는 동의 종류)
    required_responses: tuple[ResponseKey, ...]

    @property
    def needs_warning(self) -> bool:
        return bool(self.conflicts)

    @property
    def needs_responses(self) -> bool:
        return bool(self.required_responses)


def normalize_schedules(
    schedules: Iterable[TimeRange],
) -> tuple[TimeRange, ...]:
    """시각과 같은 프로젝트 내부 중복을 검사하고 정렬한다."""
    values = tuple(schedules)
    if not values:
        raise RuleViolation("일정을 한 개 이상 입력하세요.")
    if len(values) > 100:
        raise RuleViolation("일정은 최대 100개까지 입력할 수 있습니다.")

    for start, end in values:
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise RuleViolation("올바른 날짜와 시각을 입력하세요.")
        if start.tzinfo is not None or end.tzinfo is not None:
            raise RuleViolation("서버 현지 시각으로 입력하세요.")
        if end <= start:
            raise RuleViolation("종료 시각은 시작 시각보다 늦어야 합니다.")

    ordered = tuple(sorted(values))
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise RuleViolation("같은 프로젝트 안의 일정이 겹칩니다.")
    return ordered


def build_change_plan(
    *,
    actor_id: int,
    actor_role: str,
    project_id: int,
    owner_id: int,
    new_schedules: Iterable[TimeRange],
    confirmed_user_ids: Iterable[int],
    participations: Iterable[Participation],
    project_owner_ids: Mapping[int, int],
) -> ChangePlan:
    """변경 후 일정에 필요한 승인·동의를 계산한다.

    confirmed_user_ids는 변경하는 프로젝트의 확정 참여자 목록이다.
    participations는 이들의 다른 프로젝트 배정 기록이다.
    project_owner_ids에는 비교 대상 프로젝트의 최신 등록자가 필요하다.

    다른 등록자의 프로젝트와 겹치는 당사자에게만 동의를 요청한다.
    상대 프로젝트도 변경 요청자 소유이면 승인·동의는 필요 없다.
    기존에 겹쳤던 구간도 변경 후 여전히 겹치면 검사 대상에 포함한다.
    """
    if actor_role != "approver" or actor_id != owner_id:
        raise RuleViolation("자신이 등록한 프로젝트의 일정만 변경할 수 있습니다.")

    schedules = normalize_schedules(new_schedules)
    records = tuple(participations)
    conflicts: set[ConflictKey] = set()
    responses: set[ResponseKey] = set()

    for user_id in sorted(set(confirmed_user_ids)):
        other_ids = conflicting_project_ids(
            project_id=project_id,
            user_id=user_id,
            schedules=schedules,
            participations=records,
        )
        for other_id in other_ids:
            other_owner = project_owner_ids.get(other_id)
            if other_owner is None:
                raise RuleViolation("상대 프로젝트의 등록자 정보를 확인할 수 없습니다.")

            conflicts.add((user_id, other_id, other_owner))
            if other_owner != owner_id:
                responses.add((other_owner, APPROVER_APPROVAL))
                responses.add((user_id, MEMBER_CONSENT))

    return ChangePlan(
        conflicts=tuple(sorted(conflicts)),
        required_responses=tuple(sorted(responses)),
    )


def require_warning_acknowledgement(
    plan: ChangePlan, *, acknowledged: bool
) -> None:
    """겹침이 있으면 요청 생성 또는 즉시 저장 전에 경고 확인을 요구한다."""
    if plan.needs_warning and not acknowledged:
        raise RuleViolation("다른 확정 프로젝트와의 시간 겹침 경고를 확인하세요.")


def change_response_result(
    *,
    status: str,
    actor_id: int,
    kind: str,
    decisions: Mapping[ResponseKey, str],
    approve: bool,
) -> tuple[str, dict[ResponseKey, str]]:
    """응답 후 상태와 복사한 응답 내역을 반환한다.

    모든 응답이 승인일 때 ready를 반환한다.
    ready는 DB에 저장할 상태가 아니라 서비스 계층에 대한 재검사 신호다.
    서비스는 최신 겹침·참여자·등록자를 다시 검사한 뒤에만 applied로 저장한다.
    한 명이라도 거부하면 rejected이며 원래 일정은 유지해야 한다.
    """
    if status != "pending":
        raise RuleViolation("대기 중인 일정 변경 요청만 응답할 수 있습니다.")
    if not decisions:
        raise RuleViolation("응답 요청 목록이 비어 있습니다.")
    if kind not in RESPONSE_KINDS:
        raise RuleViolation("올바른 응답 종류를 선택하세요.")
    if any(key[1] not in RESPONSE_KINDS for key in decisions):
        raise RuleViolation("응답 요청 종류가 올바르지 않습니다.")
    if any(
        value not in {"pending", "approved", "rejected"}
        for value in decisions.values()
    ):
        raise RuleViolation("응답 내역이 올바르지 않습니다.")

    key = (actor_id, kind)
    if key not in decisions:
        raise RuleViolation("본인에게 지정된 승인 또는 동의만 처리할 수 있습니다.")
    if "rejected" in decisions.values():
        raise RuleViolation("이미 거부된 일정 변경 요청입니다.")
    if decisions[key] != "pending":
        raise RuleViolation("이미 응답한 요청입니다.")

    updated = dict(decisions)
    updated[key] = "approved" if approve else "rejected"

    if not approve:
        return "rejected", updated
    if all(value == "approved" for value in updated.values()):
        return "ready", updated
    return "pending", updated
