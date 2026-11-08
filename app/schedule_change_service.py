"""SQLite 기반 일정 변경 처리.

요청마다 새 세션을 사용한다. 기존 database 모듈의 BEGIN IMMEDIATE 설정이
적용된 엔진을 사용해야 하며, commit/rollback은 호출자가 담당한다.

승인 도중 기준 상태가 달라지면 RuleViolation을 발생시킨다.
이 경우 호출자는 반드시 rollback하고 취소·재요청을 안내해야 한다.
실패 시 기존 요청을 자동 취소하거나 기존 일정을 덮어쓰지 않는다.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.assignment_rules import Participation, RuleViolation
from app.models import (
    Assignment,
    AssignmentStatus,
    ChangeRequestStatus,
    ChangeResponseKind,
    Decision,
    Project,
    ProjectSchedule,
    Role,
    ScheduleChangeRequest,
    ScheduleChangeResponse,
    User,
)
from app.schedule_change_models import ScheduleChangeBaseline
from app.schedule_change_rules import (
    APPROVER_APPROVAL,
    ChangePlan,
    build_change_plan,
    change_response_result,
    normalize_schedules,
    require_warning_acknowledgement,
)

STALE_MESSAGE = (
    "요청 기준 일정·참여자·계정 상태가 변경되었습니다. "
    "기존 요청을 취소하고 최신 내용을 확인해 다시 요청하세요."
)


@dataclass(frozen=True)
class ChangePreview:
    project_id: int
    schedules: tuple
    plan: ChangePlan
    fingerprint: str


@dataclass(frozen=True)
class ChangeResult:
    request_id: int
    status: str


def _check_database(db: Session) -> None:
    if db.get_bind().dialect.name != "sqlite":
        raise RuleViolation("일정 변경은 현재 SQLite에서만 지원합니다.")


def _active_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise RuleViolation("사용자를 찾을 수 없거나 비활성 계정입니다.")
    return user


def _owned_project(db: Session, actor_id: int, project_id: int) -> Project:
    actor = _active_user(db, actor_id)
    project = db.get(Project, project_id)
    if project is None:
        raise RuleViolation("프로젝트를 찾을 수 없습니다.")
    if actor.role != Role.APPROVER or project.owner_id != actor.id:
        raise RuleViolation("자신이 등록한 프로젝트의 일정만 변경할 수 있습니다.")
    return project


def _encode(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _schedule_data(schedules) -> list[dict]:
    return [
        {"start_at": start.isoformat(), "end_at": end.isoformat()}
        for start, end in sorted(schedules)
    ]


def requested_schedules(request: ScheduleChangeRequest) -> tuple:
    """기존 모델의 일정 목록 JSON을 읽는다. 화면에서도 재사용할 수 있다."""
    try:
        data = json.loads(request.new_schedules_json)
        if not isinstance(data, list):
            raise ValueError("일정 목록이 아닙니다.")
        schedules = [
            (
                datetime.fromisoformat(item["start_at"]),
                datetime.fromisoformat(item["end_at"]),
            )
            for item in data
        ]
    except (ValueError, TypeError, KeyError) as exc:
        raise RuleViolation("저장된 변경 일정을 읽을 수 없습니다.") from exc
    return normalize_schedules(schedules)


def _snapshot(db: Session, project: Project, schedules):
    """최신 관련 상태와 승인 계획을 함께 만든다.

    비교 범위는 변경 프로젝트의 확정 배정, 그 참여자들의 다른 확정 배정,
    관련 프로젝트 일정·등록자, 관련 계정의 역할·활성 상태다.
    겹치지 않는 다른 확정 프로젝트의 변화도 보수적으로 감지한다.
    """
    pending_assignment = db.scalar(
        select(Assignment.id)
        .where(
            Assignment.project_id == project.id,
            Assignment.status.in_([
                AssignmentStatus.PENDING_ACCEPT,
                AssignmentStatus.PENDING_APPROVAL,
            ]),
        )
        .limit(1)
    )
    if pending_assignment is not None:
        raise RuleViolation(
            "수락·승인 대기 배정을 먼저 처리한 뒤 일정을 변경하세요."
        )

    assignments = db.scalars(
        select(Assignment)
        .where(
            Assignment.project_id == project.id,
            Assignment.status == AssignmentStatus.CONFIRMED,
        )
        .order_by(Assignment.id)
    ).all()
    member_ids = sorted({item.user_id for item in assignments})

    others = db.scalars(
        select(Assignment)
        .where(
            Assignment.user_id.in_(member_ids),
            Assignment.project_id != project.id,
            Assignment.status == AssignmentStatus.CONFIRMED,
        )
        .options(
            selectinload(Assignment.project).selectinload(Project.schedules)
        )
        .order_by(Assignment.id)
    ).all()

    projects = {project.id: project}
    for item in others:
        projects[item.project_id] = item.project

    user_ids = set(member_ids)
    user_ids.update(item.owner_id for item in projects.values())
    users = []
    for user_id in sorted(user_ids):
        user = db.get(User, user_id)
        if user is None:
            raise RuleViolation("관련 계정 정보를 확인할 수 없습니다.")
        users.append({
            "id": user.id,
            "role": user.role.value,
            "active": user.is_active,
        })

    plan = build_change_plan(
        actor_id=project.owner_id,
        actor_role="approver",
        project_id=project.id,
        owner_id=project.owner_id,
        new_schedules=schedules,
        confirmed_user_ids=member_ids,
        participations=[
            Participation(
                project_id=item.project_id,
                user_id=item.user_id,
                status=item.status.value,
                schedules=tuple(
                    (entry.start_at, entry.end_at)
                    for entry in item.project.schedules
                ),
            )
            for item in others
        ],
        project_owner_ids={
            item.id: item.owner_id for item in projects.values()
        },
    )

    snapshot = {
        "version": 1,
        "proposed": _schedule_data(schedules),
        "projects": [
            {
                "id": item.id,
                "owner_id": item.owner_id,
                "schedules": _schedule_data(
                    (entry.start_at, entry.end_at)
                    for entry in item.schedules
                ),
            }
            for item in sorted(projects.values(), key=lambda value: value.id)
        ],
        "assignments": [
            {
                "id": item.id,
                "project_id": item.project_id,
                "user_id": item.user_id,
                "status": item.status.value,
            }
            for item in sorted(
                [*assignments, *others], key=lambda value: value.id
            )
        ],
        "users": users,
        "conflicts": plan.conflicts,
        "responses": plan.required_responses,
    }
    return _encode(snapshot), plan


def _fingerprint(snapshot: str) -> str:
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


def _validate_responders(db: Session, plan: ChangePlan) -> None:
    for user_id, kind in plan.required_responses:
        user = _active_user(db, user_id)
        if kind == APPROVER_APPROVAL:
            if user.role != Role.APPROVER:
                raise RuleViolation("승인 대상자의 허가권자 역할을 확인하세요.")
        elif user.role not in {Role.APPROVER, Role.EMPLOYEE}:
            raise RuleViolation("동의 대상자의 직원 역할을 확인하세요.")


def _ensure_no_pending_request(db: Session, project_id: int) -> None:
    existing = db.scalar(
        select(ScheduleChangeRequest.id)
        .where(
            ScheduleChangeRequest.project_id == project_id,
            ScheduleChangeRequest.status == ChangeRequestStatus.PENDING,
        )
        .limit(1)
    )
    if existing is not None:
        raise RuleViolation("이미 대기 중인 일정 변경 요청이 있습니다.")


def preview_change(
    db: Session, *, actor_id: int, project_id: int, new_schedules
) -> ChangePreview:
    _check_database(db)
    project = _owned_project(db, actor_id, project_id)
    _ensure_no_pending_request(db, project.id)
    schedules = normalize_schedules(new_schedules)
    current = normalize_schedules(
        (item.start_at, item.end_at) for item in project.schedules
    )
    if schedules == current:
        raise RuleViolation("기존 일정과 같습니다.")

    snapshot, plan = _snapshot(db, project, schedules)
    _validate_responders(db, plan)
    return ChangePreview(project.id, schedules, plan, _fingerprint(snapshot))


def _apply(project: Project, schedules) -> None:
    project.schedules = [
        ProjectSchedule(start_at=start, end_at=end)
        for start, end in schedules
    ]
    project.updated_at = datetime.now()


def submit_change(
    db: Session,
    *,
    actor_id: int,
    project_id: int,
    new_schedules,
    expected_fingerprint: str,
    warning_acknowledged: bool,
) -> ChangeResult:
    """미리보기를 다시 계산하고 요청 생성 또는 즉시 반영한다.

    fingerprint는 미리보기와 저장 사이의 변경 감지용이며 인증 토큰이 아니다.
    HTTP 계층은 별도로 로그인과 CSRF를 검사해야 한다.
    """
    preview = preview_change(
        db,
        actor_id=actor_id,
        project_id=project_id,
        new_schedules=new_schedules,
    )
    if (
        not isinstance(expected_fingerprint, str)
        or not hmac.compare_digest(preview.fingerprint, expected_fingerprint)
    ):
        raise RuleViolation("미리보기 이후 내용이 바뀌었습니다. 다시 확인하세요.")

    require_warning_acknowledgement(
        preview.plan, acknowledged=warning_acknowledged
    )
    project = _owned_project(db, actor_id, project_id)
    snapshot, plan = _snapshot(db, project, preview.schedules)

    request = ScheduleChangeRequest(
        project_id=project.id,
        requested_by_id=actor_id,
        new_schedules_json=_encode(_schedule_data(preview.schedules)),
        status=ChangeRequestStatus.PENDING,
    )
    db.add(request)
    db.flush()
    db.add(
        ScheduleChangeBaseline(
            change_request_id=request.id,
            snapshot_json=snapshot,
        )
    )
    for user_id, kind in plan.required_responses:
        db.add(
            ScheduleChangeResponse(
                change_request_id=request.id,
                user_id=user_id,
                kind=ChangeResponseKind(kind),
                decision=Decision.PENDING,
            )
        )

    if not plan.needs_responses:
        _apply(project, preview.schedules)
        request.status = ChangeRequestStatus.APPLIED
        request.resolved_at = datetime.now()

    db.flush()
    return ChangeResult(request.id, request.status.value)


def _pending_request(db: Session, request_id: int) -> ScheduleChangeRequest:
    request = db.get(ScheduleChangeRequest, request_id)
    if request is None:
        raise RuleViolation("일정 변경 요청을 찾을 수 없습니다.")
    if request.status != ChangeRequestStatus.PENDING:
        raise RuleViolation("이미 종료된 일정 변경 요청입니다.")
    return request


def respond_change(
    db: Session,
    *,
    actor_id: int,
    request_id: int,
    kind: str,
    approve: bool,
) -> ChangeResult:
    _check_database(db)
    actor = _active_user(db, actor_id)
    request = _pending_request(db, request_id)
    responses = db.scalars(
        select(ScheduleChangeResponse)
        .where(ScheduleChangeResponse.change_request_id == request.id)
    ).all()
    decisions = {
        (item.user_id, item.kind.value): item.decision.value
        for item in responses
    }
    status, updated = change_response_result(
        status=request.status.value,
        actor_id=actor.id,
        kind=kind,
        decisions=decisions,
        approve=approve,
    )

    project = None
    schedules = None
    if approve:
        project = _owned_project(
            db, request.requested_by_id, request.project_id
        )
        schedules = requested_schedules(request)
        baseline = db.get(ScheduleChangeBaseline, request.id)
        if baseline is None:
            raise RuleViolation("기준 기록이 없습니다. 요청을 취소하고 다시 요청하세요.")

        snapshot, plan = _snapshot(db, project, schedules)
        if snapshot != baseline.snapshot_json:
            raise RuleViolation(STALE_MESSAGE)
        if set(decisions) != set(plan.required_responses):
            raise RuleViolation("응답자 목록이 변경되었습니다. 취소 후 다시 요청하세요.")
        _validate_responders(db, plan)

    # 검사를 모두 마친 후에만 응답과 일정을 함께 변경한다.
    own_response = next(
        item for item in responses
        if item.user_id == actor.id and item.kind.value == kind
    )
    own_response.decision = Decision(updated[(actor.id, kind)])
    own_response.decided_at = datetime.now()

    if status == "ready":
        _apply(project, schedules)
        request.status = ChangeRequestStatus.APPLIED
        request.resolved_at = datetime.now()
    elif status == "rejected":
        request.status = ChangeRequestStatus.REJECTED
        request.resolved_at = datetime.now()

    db.flush()
    return ChangeResult(request.id, request.status.value)


def cancel_change(
    db: Session, *, actor_id: int, request_id: int
) -> ChangeResult:
    """원래 요청자만 취소한다. 역할이 바뀌어도 본인 요청 정리는 허용한다."""
    _check_database(db)
    actor = _active_user(db, actor_id)
    request = _pending_request(db, request_id)
    if request.requested_by_id != actor.id:
        raise RuleViolation("본인이 생성한 일정 변경 요청만 취소할 수 있습니다.")

    request.status = ChangeRequestStatus.CANCELLED
    request.resolved_at = datetime.now()
    db.flush()
    return ChangeResult(request.id, request.status.value)
