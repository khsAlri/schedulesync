"""배정·승인의 순수 업무 규칙.

DB 저장이나 HTTP 요청을 처리하지 않는다.
역할·상태 문자열은 app.models의 Enum 값과 동일하게 사용한다.
DB에서 최신 데이터를 읽고 권한·활성 상태·동시성을 보장하는 작업은
이 모듈을 호출하는 서비스 계층에서 수행해야 한다.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

TimeRange = tuple[datetime, datetime]

ACTIVE_STATUSES = frozenset({
    "pending_accept",
    "pending_approval",
    "confirmed",
})


class RuleViolation(ValueError):
    """업무 규칙 위반. 메시지를 사용자에게 표시할 수 있다."""


@dataclass(frozen=True)
class Participation:
    """겹침 검사에 필요한 다른 배정의 최소 정보."""

    project_id: int
    user_id: int
    status: str
    schedules: tuple[TimeRange, ...]


def _checked_ranges(ranges: Iterable[TimeRange]) -> tuple[TimeRange, ...]:
    values = tuple(ranges)
    if not values:
        raise RuleViolation("프로젝트 일정을 한 개 이상 등록해야 합니다.")

    for start, end in values:
        if start.tzinfo is not None or end.tzinfo is not None:
            raise RuleViolation("일정은 시간대 정보 없는 서버 현지 시각이어야 합니다.")
        if end <= start:
            raise RuleViolation("종료 시각은 시작 시각보다 늦어야 합니다.")
    return values


def conflicting_project_ids(
    *,
    project_id: int,
    user_id: int,
    schedules: Iterable[TimeRange],
    participations: Iterable[Participation],
) -> tuple[int, ...]:
    """같은 직원의 다른 확정 프로젝트 중 실제 시간이 겹치는 ID를 반환한다.

    구간은 [시작, 종료)로 비교하므로 종료와 다음 시작이 같으면 겹치지 않는다.
    수락·승인 대기 배정과 다른 직원의 참여 기록은 비교하지 않는다.
    """
    proposed = _checked_ranges(schedules)
    conflicts: set[int] = set()

    for participation in participations:
        if (
            participation.user_id != user_id
            or participation.project_id == project_id
            or participation.status != "confirmed"
        ):
            continue

        existing = _checked_ranges(participation.schedules)
        if any(
            start < other_end and other_start < end
            for start, end in proposed
            for other_start, other_end in existing
        ):
            conflicts.add(participation.project_id)

    return tuple(sorted(conflicts))


def initial_assignment_status(
    *,
    actor_id: int,
    actor_role: str,
    owner_id: int,
    target_id: int,
    target_role: str,
    has_active_assignment: bool,
) -> str:
    """프로젝트 소유자만 배정할 수 있다. 본인 투입은 즉시 확정한다.

    시간 겹침이 있어도 본인 투입은 허용한다.
    호출자는 본인 투입 시에도 겹침을 검사해 사용자에게 경고해야 한다.
    """
    if actor_role != "approver" or actor_id != owner_id:
        raise RuleViolation("자신이 등록한 프로젝트에만 직원을 배정할 수 있습니다.")
    if target_role not in {"employee", "approver"}:
        raise RuleViolation("직원 또는 허가권자만 배정할 수 있습니다.")
    if actor_id == target_id and target_role != actor_role:
        raise RuleViolation("본인 계정의 역할 정보가 일치하지 않습니다.")
    if has_active_assignment:
        raise RuleViolation("이미 진행 중이거나 확정된 배정이 있습니다.")

    if target_id == owner_id:
        return "confirmed"
    return "pending_accept"


def acceptance_result(
    *,
    status: str,
    actor_id: int,
    target_id: int,
    target_role: str,
    accept: bool,
    has_conflict: bool,
    owner_id: int,
    approver_ids: Iterable[int],
) -> tuple[str, tuple[int, ...]]:
    """배정 대상자의 수락·거절 결과와 생성할 승인자 목록을 반환한다.

    일반 직원의 승인자 목록은 수락 시점의 허가권자 전원이다.
    이후 승인 요청은 이 목록을 저장하고 기준으로 사용해야 한다.
    허가권자 간 배정은 별도 승인 없이 수락으로 확정한다.
    """
    if actor_id != target_id:
        raise RuleViolation("본인에게 온 배정만 수락하거나 거절할 수 있습니다.")
    if status != "pending_accept":
        raise RuleViolation("수락 대기 상태의 배정만 응답할 수 있습니다.")
    if target_role not in {"employee", "approver"}:
        raise RuleViolation("배정 대상자의 역할을 확인하세요.")

    if not accept:
        return "declined", ()

    if target_role == "approver":
        if has_conflict:
            raise RuleViolation("다른 확정 프로젝트와 시간이 겹쳐 참여를 확정할 수 없습니다.")
        return "confirmed", ()

    required = tuple(sorted(set(approver_ids)))
    if not required or owner_id not in required:
        raise RuleViolation("배정한 허가권자를 포함한 승인자 전원을 지정해야 합니다.")

    # 일반 직원은 수락 가능하나, 시간이 겹치면 승인 단계에서 차단한다.
    return "pending_approval", required


def approval_result(
    *,
    status: str,
    actor_id: int,
    decisions: Mapping[int, str],
    approve: bool,
    has_conflict: bool,
) -> tuple[str, dict[int, str]]:
    """허가권자 한 명의 응답을 반영한 새 상태와 승인 내역을 반환한다.

    decisions는 수락 시 생성한 승인자 전원의 최신 DB 응답이어야 한다.
    인자로 받은 매핑은 변경하지 않는다.
    시간 겹침은 승인할 때마다 최신 확정 프로젝트를 대상으로 다시 검사한다.
    """
    if status != "pending_approval":
        raise RuleViolation("승인 대기 상태의 배정만 처리할 수 있습니다.")
    if not decisions:
        raise RuleViolation("승인자 목록이 비어 있습니다.")
    if any(value not in {"pending", "approved", "rejected"} for value in decisions.values()):
        raise RuleViolation("올바르지 않은 승인 내역입니다.")
    if actor_id not in decisions:
        raise RuleViolation("이 배정의 승인자로 지정되지 않았습니다.")
    if "rejected" in decisions.values():
        raise RuleViolation("이미 반려된 배정입니다.")
    if decisions[actor_id] != "pending":
        raise RuleViolation("이미 응답한 승인 요청입니다.")

    updated = dict(decisions)

    # 시간이 겹치더라도 반려는 가능하다.
    if not approve:
        updated[actor_id] = "rejected"
        return "rejected", updated

    if has_conflict:
        raise RuleViolation("다른 확정 프로젝트와 시간이 겹쳐 승인할 수 없습니다.")

    updated[actor_id] = "approved"
    if all(value == "approved" for value in updated.values()):
        return "confirmed", updated
    return "pending_approval", updated
