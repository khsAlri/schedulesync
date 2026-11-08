"""배정·승인 DB 처리.

호출자는 get_db로 얻은 요청 전용 세션을 전달한다.
각 함수는 flush까지만 수행하며 commit/rollback은 호출자가 담당한다.

현재 동시성 보호가 구현된 SQLite에서만 상태 변경을 허용한다.
다른 DB로 이전할 때는 잠금 전략을 먼저 구현해야 한다.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.assignment_rules import (
    ACTIVE_STATUSES,
    Participation,
    RuleViolation,
    acceptance_result,
    approval_result,
    conflicting_project_ids,
    initial_assignment_status,
)
from app.models import (
    Approval,
    Assignment,
    AssignmentStatus,
    ChangeRequestStatus,
    Decision,
    Project,
    Role,
    ScheduleChangeRequest,
    User,
)


@dataclass(frozen=True)
class AssignmentResult:
    assignment_id: int
    status: str
    conflicts: tuple[int, ...]


def _check_database(db: Session) -> None:
    if db.get_bind().dialect.name != "sqlite":
        raise RuleViolation(
            "배정·승인 상태 변경은 현재 SQLite에서만 지원합니다. "
            "다른 DB는 동시성 보호 구현 후 사용할 수 있습니다."
        )


def _active_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise RuleViolation("사용자를 찾을 수 없거나 비활성 계정입니다.")
    return user


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise RuleViolation("프로젝트를 찾을 수 없습니다.")
    return project


def _assignment(db: Session, assignment_id: int) -> Assignment:
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        raise RuleViolation("배정을 찾을 수 없습니다.")
    return assignment


def _validate_owner(db: Session, project: Project) -> None:
    owner = _active_user(db, project.owner_id)
    if owner.role != Role.APPROVER:
        raise RuleViolation("프로젝트 등록자의 허가권자 역할을 확인하세요.")


def _ensure_no_pending_change(db: Session, project_id: int) -> None:
    pending = db.scalar(
        select(ScheduleChangeRequest.id)
        .where(
            ScheduleChangeRequest.project_id == project_id,
            ScheduleChangeRequest.status == ChangeRequestStatus.PENDING,
        )
        .limit(1)
    )
    if pending is not None:
        raise RuleViolation("일정 변경 요청 처리 중에는 배정·수락·승인을 할 수 없습니다.")


def assignment_conflicts(
    db: Session, project: Project, user_id: int
) -> tuple[int, ...]:
    """한 직원의 다른 확정 프로젝트와 최신 일정을 비교한다."""
    assignments = db.scalars(
        select(Assignment)
        .where(
            Assignment.user_id == user_id,
            Assignment.project_id != project.id,
            Assignment.status == AssignmentStatus.CONFIRMED,
        )
        .options(
            selectinload(Assignment.project).selectinload(Project.schedules)
        )
    ).all()

    participations = [
        Participation(
            project_id=item.project_id,
            user_id=item.user_id,
            status=item.status.value,
            schedules=tuple(
                (schedule.start_at, schedule.end_at)
                for schedule in item.project.schedules
            ),
        )
        for item in assignments
    ]
    return conflicting_project_ids(
        project_id=project.id,
        user_id=user_id,
        schedules=[
            (schedule.start_at, schedule.end_at)
            for schedule in project.schedules
        ],
        participations=participations,
    )


def create_assignment(
    db: Session, *, actor_id: int, project_id: int, target_id: int
) -> AssignmentResult:
    _check_database(db)
    actor = _active_user(db, actor_id)
    project = _project(db, project_id)

    # 소유권을 먼저 검사해 다른 프로젝트의 배정 정보를 노출하지 않는다.
    if not actor.is_approver or project.owner_id != actor.id:
        raise RuleViolation("자신이 등록한 프로젝트에만 배정할 수 있습니다.")

    target = _active_user(db, target_id)
    _ensure_no_pending_change(db, project.id)
    existing = db.scalar(
        select(Assignment.id)
        .where(
            Assignment.project_id == project.id,
            Assignment.user_id == target.id,
            Assignment.status.in_(
                [AssignmentStatus(value) for value in ACTIVE_STATUSES]
            ),
        )
        .limit(1)
    )
    status = initial_assignment_status(
        actor_id=actor.id,
        actor_role=actor.role.value,
        owner_id=project.owner_id,
        target_id=target.id,
        target_role=target.role.value,
        has_active_assignment=existing is not None,
    )
    conflicts = assignment_conflicts(db, project, target.id)

    assignment = Assignment(
        project_id=project.id,
        user_id=target.id,
        assigned_by_id=actor.id,
        status=AssignmentStatus(status),
    )
    db.add(assignment)
    db.flush()
    return AssignmentResult(assignment.id, status, conflicts)


def respond_assignment(
    db: Session, *, actor_id: int, assignment_id: int, accept: bool
) -> AssignmentResult:
    _check_database(db)
    actor = _active_user(db, actor_id)
    assignment = _assignment(db, assignment_id)
    if assignment.user_id != actor.id:
        raise RuleViolation("본인에게 온 배정만 응답할 수 있습니다.")

    project = _project(db, assignment.project_id)
    conflicts: tuple[int, ...] = ()
    approver_ids: list[int] = []

    # 거절은 일정 겹침이나 소유자 비활성 상태와 무관하게 허용한다.
    if accept:
        _validate_owner(db, project)
        _ensure_no_pending_change(db, project.id)
        conflicts = assignment_conflicts(db, project, actor.id)
        approver_ids = list(
            db.scalars(
                select(User.id)
                .where(User.role == Role.APPROVER, User.is_active.is_(True))
                .order_by(User.id)
            ).all()
        )

    status, required = acceptance_result(
        status=assignment.status.value,
        actor_id=actor.id,
        target_id=assignment.user_id,
        target_role=actor.role.value,
        accept=accept,
        has_conflict=bool(conflicts),
        owner_id=project.owner_id,
        approver_ids=approver_ids,
    )

    assignment.status = AssignmentStatus(status)
    for approver_id in required:
        db.add(
            Approval(
                assignment_id=assignment.id,
                approver_id=approver_id,
                decision=Decision.PENDING,
            )
        )
    db.flush()
    return AssignmentResult(assignment.id, status, conflicts)


def decide_assignment(
    db: Session, *, actor_id: int, assignment_id: int, approve: bool
) -> AssignmentResult:
    _check_database(db)
    actor = _active_user(db, actor_id)
    if not actor.is_approver:
        raise RuleViolation("허가권자만 승인·반려할 수 있습니다.")

    assignment = _assignment(db, assignment_id)
    approvals = db.scalars(
        select(Approval).where(Approval.assignment_id == assignment.id)
    ).all()
    decisions = {
        item.approver_id: item.decision.value
        for item in approvals
    }
    if actor.id not in decisions:
        raise RuleViolation("이 배정의 승인자로 지정되지 않았습니다.")

    conflicts: tuple[int, ...] = ()
    if approve:
        project = _project(db, assignment.project_id)
        _validate_owner(db, project)
        _ensure_no_pending_change(db, project.id)

        target = _active_user(db, assignment.user_id)
        if target.role != Role.EMPLOYEE:
            raise RuleViolation(
                "승인 대기 중 대상자의 역할이 변경되었습니다. 계정 설정을 확인하세요."
            )

        # 승인 대상 목록은 수락 시점에 고정한다.
        # 중간에 역할·활성 상태가 바뀌면 자동으로 승인자를 빼지 않고 차단한다.
        for approver_id in decisions:
            approver = _active_user(db, approver_id)
            if not approver.is_approver:
                raise RuleViolation(
                    "승인자의 역할이 변경되었습니다. 계정 설정을 확인하세요."
                )
        if project.owner_id not in decisions:
            raise RuleViolation("배정한 허가권자의 승인 요청이 누락되었습니다.")

        conflicts = assignment_conflicts(db, project, target.id)

    status, updated = approval_result(
        status=assignment.status.value,
        actor_id=actor.id,
        decisions=decisions,
        approve=approve,
        has_conflict=bool(conflicts),
    )

    own_approval = next(
        item for item in approvals if item.approver_id == actor.id
    )
    own_approval.decision = Decision(updated[actor.id])
    own_approval.decided_at = datetime.now()
    assignment.status = AssignmentStatus(status)
    db.flush()
    return AssignmentResult(assignment.id, status, conflicts)
