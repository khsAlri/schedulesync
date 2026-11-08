"""일정 변경 업무 처리와 알림 저장 연결.

호출자가 업무와 알림을 함께 commit/rollback한다.
외부 네트워크 통신은 하지 않는다.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schedule_change_service
from app.models import (
    Assignment,
    AssignmentStatus,
    Project,
    ScheduleChangeRequest,
    ScheduleChangeResponse,
)
from app.notification_service import store_notifications


@dataclass(frozen=True)
class NotifiedChangeResult:
    request_id: int
    status: str
    notification_ids: tuple[int, ...]


def _response_user_ids(db: Session, request_id: int) -> set[int]:
    return set(db.scalars(
        select(ScheduleChangeResponse.user_id).where(
            ScheduleChangeResponse.change_request_id == request_id
        )
    ).all())


def _notify_result(db: Session, result) -> NotifiedChangeResult:
    if result.status == "pending":
        return NotifiedChangeResult(result.request_id, result.status, ())

    request = db.get(ScheduleChangeRequest, result.request_id)
    project = db.get(Project, request.project_id)
    members = set(db.scalars(
        select(Assignment.user_id).where(
            Assignment.project_id == project.id,
            Assignment.status == AssignmentStatus.CONFIRMED,
        )
    ).all())
    responders = _response_user_ids(db, request.id)

    # 応答者にも終了を知らせ、不要になった依頼を追い続けないようにする。
    recipients = {request.requested_by_id, *members, *responders}
    labels = {
        "applied": "반영되었습니다",
        "rejected": "거부되어 원래 일정을 유지합니다",
        "cancelled": "취소되어 원래 일정을 유지합니다",
    }
    ids = store_notifications(
        db,
        user_ids=recipients,
        message=(
            f"프로젝트 '{project.name[:200]}'의 "
            f"일정 변경 요청 #{request.id}이 {labels[result.status]}."
        ),
        link="/schedule-changes",
    )
    return NotifiedChangeResult(result.request_id, result.status, ids)


def submit_change(
    db: Session,
    *,
    actor_id: int,
    project_id: int,
    new_schedules,
    expected_fingerprint: str,
    warning_acknowledged: bool,
) -> NotifiedChangeResult:
    result = schedule_change_service.submit_change(
        db,
        actor_id=actor_id,
        project_id=project_id,
        new_schedules=new_schedules,
        expected_fingerprint=expected_fingerprint,
        warning_acknowledged=warning_acknowledged,
    )
    if result.status != "pending":
        # 겹침 없음 또는 본인 등록 프로젝트 예외로 즉시 반영된 경우.
        return _notify_result(db, result)

    request = db.get(ScheduleChangeRequest, result.request_id)
    project = db.get(Project, request.project_id)
    ids = store_notifications(
        db,
        user_ids=_response_user_ids(db, request.id),
        message=(
            f"프로젝트 '{project.name[:200]}'의 일정 변경 요청 #{request.id}이 "
            "생성되었습니다. 본인에게 지정된 승인 또는 동의를 처리해 주세요."
        ),
        link="/schedule-changes",
    )
    return NotifiedChangeResult(result.request_id, result.status, ids)


def respond_change(
    db: Session,
    *,
    actor_id: int,
    request_id: int,
    kind: str,
    approve: bool,
) -> NotifiedChangeResult:
    result = schedule_change_service.respond_change(
        db,
        actor_id=actor_id,
        request_id=request_id,
        kind=kind,
        approve=approve,
    )
    return _notify_result(db, result)


def cancel_change(
    db: Session, *, actor_id: int, request_id: int
) -> NotifiedChangeResult:
    result = schedule_change_service.cancel_change(
        db, actor_id=actor_id, request_id=request_id
    )
    return _notify_result(db, result)
