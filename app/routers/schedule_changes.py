"""일정 변경 미리보기·요청·응답 화면."""

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.assignment_rules import RuleViolation
from app.config import settings
from app.database import get_db
from app.deps import PermissionDenied, require_user, verify_csrf
from app.models import (
    Project,
    ScheduleChangeRequest,
    ScheduleChangeResponse,
    User,
)
from app.routers.projects import parse_schedules
from app.schedule_change_models import ScheduleChangeBaseline
from app.schedule_change_service import (
    cancel_change,
    preview_change,
    requested_schedules,
    respond_change,
    submit_change,
)
from app.templating import flash, render

router = APIRouter(
    prefix="/schedule-changes",
    dependencies=[Depends(require_user)],
)

signer = URLSafeTimedSerializer(
    settings.secret_key,
    salt="schedulesync-schedule-change-preview-v1",
)

STATUS_LABELS = {
    "pending": "承認・同意待ち",
    "applied": "반영 완료",
    "rejected": "거부됨",
    "cancelled": "취소됨",
}
KIND_LABELS = {
    "approver_approval": "상대 프로젝트 등록자 승인",
    "member_consent": "참여자 동의",
}
DECISION_LABELS = {
    "pending": "대기",
    "approved": "승인·동의",
    "rejected": "거부",
}


def _owned_project(db: Session, project_id: int, user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    if not user.is_approver or project.owner_id != user.id:
        raise PermissionDenied("자신이 등록한 프로젝트의 일정만 변경할 수 있습니다.")
    return project


def _names(db: Session) -> dict[int, str]:
    return dict(db.execute(select(User.id, User.name)).all())


def _form(
    request: Request,
    project: Project,
    schedules_text: str,
    *,
    error: str | None = None,
    preview=None,
    preview_token: str = "",
    names=None,
    status_code: int = 200,
):
    return render(
        request,
        "schedule_changes/form.html",
        {
            "project": project,
            "schedules_text": schedules_text,
            "error": error,
            "preview": preview,
            "preview_token": preview_token,
            "names": names or {},
            "kind_labels": KIND_LABELS,
        },
        status_code=status_code,
    )


@router.get("")
def change_list(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    requests = db.scalars(
        select(ScheduleChangeRequest)
        .join(Project, ScheduleChangeRequest.project_id == Project.id)
        .where(
            or_(
                ScheduleChangeRequest.requested_by_id == user.id,
                Project.owner_id == user.id,
                ScheduleChangeRequest.responses.any(
                    ScheduleChangeResponse.user_id == user.id
                ),
            )
        )
        .options(
            joinedload(ScheduleChangeRequest.project),
            joinedload(ScheduleChangeRequest.requested_by),
            selectinload(ScheduleChangeRequest.responses)
            .joinedload(ScheduleChangeResponse.user),
        )
        .order_by(ScheduleChangeRequest.id.desc())
    ).all()

    baselines = {}
    if requests:
        records = db.scalars(
            select(ScheduleChangeBaseline).where(
                ScheduleChangeBaseline.change_request_id.in_(
                    [item.id for item in requests]
                )
            )
        ).all()
        baselines = {
            item.change_request_id: item.snapshot_json for item in records
        }

    entries = []
    for item in requests:
        error = None
        schedules = ()
        snapshot = None
        try:
            schedules = requested_schedules(item)
        except RuleViolation as exc:
            error = str(exc)
        try:
            raw = baselines.get(item.id)
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    snapshot = parsed
        except (ValueError, TypeError):
            snapshot = None

        entries.append({
            "record": item,
            "schedules": schedules,
            "snapshot": snapshot,
            "error": error,
        })

    return render(
        request,
        "schedule_changes/list.html",
        {
            "entries": entries,
            "names": _names(db),
            "status_labels": STATUS_LABELS,
            "kind_labels": KIND_LABELS,
            "decision_labels": DECISION_LABELS,
        },
    )


@router.get("/projects/{project_id}/new")
def new_form(
    request: Request,
    project_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    project = _owned_project(db, project_id, user)
    text = "\n".join(
        f"{item.start_at.isoformat(timespec='minutes')} / "
        f"{item.end_at.isoformat(timespec='minutes')}"
        for item in project.schedules
    )
    return _form(request, project, text)


@router.post(
    "/projects/{project_id}/preview",
    dependencies=[Depends(verify_csrf)],
)
def preview_form(
    request: Request,
    project_id: int,
    schedules_text: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    project = _owned_project(db, project_id, user)
    try:
        schedules = parse_schedules(schedules_text)
        preview = preview_change(
            db,
            actor_id=user.id,
            project_id=project.id,
            new_schedules=schedules,
        )
    except ValueError as exc:
        return _form(
            request,
            project,
            schedules_text,
            error=str(exc),
            status_code=400,
        )

    token = signer.dumps({
        "actor_id": user.id,
        "project_id": project.id,
        "schedules_text": schedules_text,
        "fingerprint": preview.fingerprint,
    })
    return _form(
        request,
        project,
        schedules_text,
        preview=preview,
        preview_token=token,
        names=_names(db),
    )


@router.post(
    "/projects/{project_id}/submit",
    dependencies=[Depends(verify_csrf)],
)
def submit_form(
    request: Request,
    project_id: int,
    preview_token: str = Form(""),
    warning_acknowledged: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _owned_project(db, project_id, user)
    try:
        payload = signer.loads(preview_token, max_age=1800)
        if (
            not isinstance(payload, dict)
            or payload.get("actor_id") != user.id
            or payload.get("project_id") != project_id
        ):
            raise RuleViolation("미리보기 사용자 또는 프로젝트가 일치하지 않습니다.")

        text = payload.get("schedules_text")
        fingerprint = payload.get("fingerprint")
        if not isinstance(text, str) or not isinstance(fingerprint, str):
            raise RuleViolation("미리보기 정보가 올바르지 않습니다.")

        result = submit_change(
            db,
            actor_id=user.id,
            project_id=project_id,
            new_schedules=parse_schedules(text),
            expected_fingerprint=fingerprint,
            warning_acknowledged=warning_acknowledged == "on",
        )
        db.commit()
    except BadData:
        db.rollback()
        flash(request, "미리보기가 만료되었거나 유효하지 않습니다. 다시 확인하세요.", "error")
        return RedirectResponse(
            f"/schedule-changes/projects/{project_id}/new", status_code=303
        )
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(
            f"/schedule-changes/projects/{project_id}/new", status_code=303
        )
    except OperationalError:
        db.rollback()
        flash(request, "DB 처리에 실패했습니다. 상태를 확인한 뒤 다시 시도하세요.", "error")
        return RedirectResponse("/schedule-changes", status_code=303)

    flash(request, f"일정 변경 처리: {STATUS_LABELS[result.status]}", "success")
    return RedirectResponse("/schedule-changes", status_code=303)


@router.post("/{request_id}/respond", dependencies=[Depends(verify_csrf)])
def respond_form(
    request: Request,
    request_id: int,
    kind: str = Form(""),
    decision: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if decision not in {"approve", "reject"}:
        flash(request, "승인·동의 또는 거부를 선택하세요.", "error")
        return RedirectResponse("/schedule-changes", status_code=303)
    return _action(
        request,
        db,
        respond_change,
        actor_id=user.id,
        request_id=request_id,
        kind=kind,
        approve=decision == "approve",
    )


@router.post("/{request_id}/cancel", dependencies=[Depends(verify_csrf)])
def cancel_form(
    request: Request,
    request_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return _action(
        request,
        db,
        cancel_change,
        actor_id=user.id,
        request_id=request_id,
    )


def _action(request: Request, db: Session, operation, **kwargs):
    try:
        result = operation(db, **kwargs)
        db.commit()
    except RuleViolation as exc:
        db.rollback()
        flash(request, str(exc), "error")
    except OperationalError:
        db.rollback()
        flash(request, "DB 처리에 실패했습니다. 상태를 확인한 뒤 다시 시도하세요.", "error")
    else:
        flash(request, f"일정 변경 처리: {STATUS_LABELS[result.status]}", "success")
    return RedirectResponse("/schedule-changes", status_code=303)
