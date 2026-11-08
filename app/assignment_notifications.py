"""배정 업무 서비스와 알림 저장을 연결한다.

기존 assignment_service는 변경하지 않는다.
웹 경로에서는 이 모듈을 사용하고, 호출자가 commit/rollback한다.
외부 네트워크 호출은 하지 않는다.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import assignment_service
from app.assignment_service import AssignmentResult
from app.models import Approval, Assignment, Project
from app.notification_service import store_notifications


@dataclass(frozen=True)
class NotifiedAssignmentResult:
    assignment_id: int
    status: str
    conflicts: tuple[int, ...]
    notification_ids: tuple[int, ...]


def _result(
    result: AssignmentResult,
    notification_ids: tuple[int, ...],
) -> NotifiedAssignmentResult:
    return NotifiedAssignmentResult(
        assignment_id=result.assignment_id,
        status=result.status,
        conflicts=result.conflicts,
        notification_ids=notification_ids,
    )


def _context(db: Session, assignment_id: int):
    assignment = db.get(Assignment, assignment_id)
    project = db.get(Project, assignment.project_id)
    # 既存データでもメッセージ長が制限を超えないよう表示名を制限する。
    project_name = project.name[:200]
    return assignment, project_name


def _approver_ids(db: Session, assignment_id: int) -> list[int]:
    return list(db.scalars(
        select(Approval.approver_id).where(
            Approval.assignment_id == assignment_id
        )
    ).all())


def create_assignment(
    db: Session, *, actor_id: int, project_id: int, target_id: int
) -> NotifiedAssignmentResult:
    result = assignment_service.create_assignment(
        db,
        actor_id=actor_id,
        project_id=project_id,
        target_id=target_id,
    )
    assignment, name = _context(db, result.assignment_id)
    if result.status == "confirmed":
        message = f"프로젝트 '{name}'에 본인 참여가 확정되었습니다."
    else:
        message = f"프로젝트 '{name}'에 배정되었습니다. 수락 또는 거절해 주세요."

    ids = store_notifications(
        db,
        user_ids=[assignment.user_id],
        message=message,
        link="/assignments",
    )
    return _result(result, ids)


def respond_assignment(
    db: Session, *, actor_id: int, assignment_id: int, accept: bool
) -> NotifiedAssignmentResult:
    result = assignment_service.respond_assignment(
        db,
        actor_id=actor_id,
        assignment_id=assignment_id,
        accept=accept,
    )
    assignment, name = _context(db, result.assignment_id)
    if result.status == "pending_approval":
        recipients = _approver_ids(db, assignment.id)
        message = (
            f"프로젝트 '{name}'의 배정 #{assignment.id}을 직원이 수락했습니다. "
            "허가권자 전원의 승인이 필요합니다."
        )
    elif result.status == "declined":
        recipients = [assignment.assigned_by_id]
        message = (
            f"프로젝트 '{name}'의 배정 #{assignment.id}을 대상자가 거절했습니다."
        )
    else:
        # 허가권자 간 배정은 수락만으로 확정된다.
        recipients = [assignment.assigned_by_id]
        message = (
            f"프로젝트 '{name}'의 배정 #{assignment.id}을 허가권자가 수락해 "
            "참여가 확정되었습니다."
        )

    ids = store_notifications(
        db,
        user_ids=recipients,
        message=message,
        link="/assignments",
    )
    return _result(result, ids)


def decide_assignment(
    db: Session, *, actor_id: int, assignment_id: int, approve: bool
) -> NotifiedAssignmentResult:
    result = assignment_service.decide_assignment(
        db,
        actor_id=actor_id,
        assignment_id=assignment_id,
        approve=approve,
    )
    if result.status == "pending_approval":
        # 일부 승인은 업무 상태만 저장한다. 리마인드도 생성하지 않는다.
        return _result(result, ())

    assignment, name = _context(db, result.assignment_id)
    recipients = [
        assignment.user_id,
        *_approver_ids(db, assignment.id),
    ]
    if result.status == "confirmed":
        message = (
            f"프로젝트 '{name}'의 배정 #{assignment.id}이 "
            "허가권자 전원 승인으로 확정되었습니다."
        )
    else:
        message = (
            f"프로젝트 '{name}'의 배정 #{assignment.id}이 반려되었습니다. "
            "한 명의 반려로 전체 배정이 반려 처리됩니다."
        )

    ids = store_notifications(
        db,
        user_ids=recipients,
        message=message,
        link="/assignments",
    )
    return _result(result, ids)
