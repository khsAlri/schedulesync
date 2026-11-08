"""배정·수락·승인 화면과 POST 처리."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.assignment_rules import RuleViolation
from app.assignment_service import (
    create_assignment,
    decide_assignment,
    respond_assignment,
)
from app.database import get_db
from app.deps import require_user, verify_csrf
from app.models import Approval, Assignment, Project, Role, User
from app.templating import flash, render

router = APIRouter(
    prefix="/assignments",
    dependencies=[Depends(require_user)],
)

STATUS_LABELS = {
    "pending_accept": "수락 대기",
    "pending_approval": "승인 대기",
    "confirmed": "참여 확정",
    "declined": "배정 거절",
    "rejected": "전체 반려",
    "cancelled": "배정 취소",
}

DECISION_LABELS = {
    "pending": "대기",
    "approved": "승인",
    "rejected": "반려",
}


@router.get("")
def assignment_page(
    request: Request,
    project_id: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    owned_projects = []
    targets = []
    if user.is_approver:
        owned_projects = db.scalars(
            select(Project)
            .where(Project.owner_id == user.id)
            .order_by(Project.id.desc())
        ).all()
        targets = db.scalars(
            select(User)
            .where(
                User.is_active.is_(True),
                User.role.in_([Role.APPROVER, Role.EMPLOYEE]),
            )
            .order_by(User.name, User.id)
        ).all()

    related = db.scalars(
        select(Assignment)
        .join(Project, Assignment.project_id == Project.id)
        .where(
            or_(
                Assignment.user_id == user.id,
                Project.owner_id == user.id,
                Assignment.approvals.any(Approval.approver_id == user.id),
            )
        )
        .options(
            joinedload(Assignment.project),
            joinedload(Assignment.user),
            selectinload(Assignment.approvals).joinedload(Approval.approver),
        )
        .order_by(Assignment.id.desc())
    ).all()

    return render(
        request,
        "assignments.html",
        {
            "owned_projects": owned_projects,
            "targets": targets,
            "related": related,
            "selected_project_id": project_id,
            "status_labels": STATUS_LABELS,
            "decision_labels": DECISION_LABELS,
        },
    )


def _save_action(request: Request, db: Session, operation, **kwargs):
    """서비스 변경과 승인 내역을 하나의 트랜잭션으로 저장한다."""
    try:
        result = operation(db, **kwargs)
        db.commit()
    except RuleViolation as exc:
        db.rollback()
        flash(request, str(exc), "error")
    except OperationalError:
        db.rollback()
        flash(
            request,
            "DB 처리에 실패했습니다. 다른 요청이 처리 중일 수 있습니다. "
            "화면을 새로고침해 상태를 확인한 뒤 다시 시도하세요.",
            "error",
        )
    else:
        flash(
            request,
            f"처리했습니다: {STATUS_LABELS[result.status]}",
            "success",
        )
        if result.conflicts:
            project_ids = ", ".join(str(value) for value in result.conflicts)
            if result.status == "confirmed":
                message = (
                    f"다른 확정 프로젝트(ID: {project_ids})와 시간이 겹칩니다. "
                    "본인 프로젝트에 본인을 투입한 경우이므로 예외적으로 확정했습니다."
                )
            else:
                message = (
                    f"다른 확정 프로젝트(ID: {project_ids})와 시간이 겹칩니다. "
                    "일정 겹침이 해소되기 전에는 참여를 확정할 수 없습니다."
                )
            flash(request, message, "warning")

    return RedirectResponse("/assignments", status_code=303)


@router.post("/new", dependencies=[Depends(verify_csrf)])
def assign(
    request: Request,
    project_id: int = Form(...),
    target_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return _save_action(
        request,
        db,
        create_assignment,
        actor_id=user.id,
        project_id=project_id,
        target_id=target_id,
    )


@router.post("/{assignment_id}/respond", dependencies=[Depends(verify_csrf)])
def respond(
    request: Request,
    assignment_id: int,
    response: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if response not in {"accept", "decline"}:
        flash(request, "수락 또는 거절을 선택하세요.", "error")
        return RedirectResponse("/assignments", status_code=303)
    return _save_action(
        request,
        db,
        respond_assignment,
        actor_id=user.id,
        assignment_id=assignment_id,
        accept=response == "accept",
    )


@router.post("/{assignment_id}/decide", dependencies=[Depends(verify_csrf)])
def decide(
    request: Request,
    assignment_id: int,
    decision: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if decision not in {"approve", "reject"}:
        flash(request, "승인 또는 반려를 선택하세요.", "error")
        return RedirectResponse("/assignments", status_code=303)
    return _save_action(
        request,
        db,
        decide_assignment,
        actor_id=user.id,
        assignment_id=assignment_id,
        approve=decision == "approve",
    )
