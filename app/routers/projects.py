"""프로젝트 조회·등록·수정.

일정은 기존 모델에 맞춰 시간대 정보 없는 서버 현지 시각으로 저장한다.
배정 진행 중인 프로젝트의 일정 변경은 별도 승인 기능 구현 전까지 막는다.
"""

import re
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.database import get_db
from app.deps import PermissionDenied, require_user, verify_csrf
from app.models import (
    Assignment,
    AssignmentStatus,
    ChangeRequestStatus,
    Project,
    ProjectSchedule,
    ScheduleChangeRequest,
    User,
)
from app.templating import flash, render

router = APIRouter(prefix="/projects", dependencies=[Depends(require_user)])

SCHEDULE_LINE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\s*/\s*"
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})"
)

ACTIVE_ASSIGNMENT_STATUSES = (
    AssignmentStatus.PENDING_ACCEPT,
    AssignmentStatus.PENDING_APPROVAL,
    AssignmentStatus.CONFIRMED,
)


def parse_schedules(text: str) -> list[tuple[datetime, datetime]]:
    """各行を検証するのではなく、プロジェクトの全予定をまとめて検証する。"""
    if len(text) > 10_000:
        raise ValueError("일정 입력은 10,000자 이하여야 합니다.")

    lines = [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), start=1)
        if line.strip()
    ]
    if not lines:
        raise ValueError("일정을 한 개 이상 입력하세요.")
    if len(lines) > 100:
        raise ValueError("일정은 최대 100개까지 입력할 수 있습니다.")

    schedules: list[tuple[datetime, datetime]] = []
    for number, line in lines:
        match = SCHEDULE_LINE_RE.fullmatch(line)
        if match is None:
            raise ValueError(
                f"{number}번째 줄: "
                "YYYY-MM-DDTHH:MM / YYYY-MM-DDTHH:MM 형식으로 입력하세요."
            )
        try:
            start = datetime.fromisoformat(match.group(1))
            end = datetime.fromisoformat(match.group(2))
        except ValueError as exc:
            raise ValueError(
                f"{number}번째 줄: 실제 존재하는 날짜와 시각을 입력하세요."
            ) from exc

        if end <= start:
            raise ValueError(
                f"{number}번째 줄: 종료 시각은 시작 시각보다 늦어야 합니다."
            )
        schedules.append((start, end))

    schedules.sort()
    for previous, current in zip(schedules, schedules[1:]):
        if current[0] < previous[1]:
            raise ValueError(
                "같은 프로젝트 안의 일정이 겹칩니다. 중복 구간을 정리하세요."
            )

    return schedules


def require_approver(user: User = Depends(require_user)) -> User:
    if not user.is_approver:
        raise PermissionDenied(
            "허가권자만 프로젝트를 등록하거나 수정할 수 있습니다."
        )
    return user


def _project_query():
    return select(Project).options(
        joinedload(Project.owner),
        selectinload(Project.schedules),
        selectinload(Project.assignments).joinedload(Assignment.user),
    )


def _get_project(db: Session, project_id: int) -> Project:
    project = db.scalar(_project_query().where(Project.id == project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    return project


def _require_owner(project: Project, user: User) -> None:
    if not user.is_approver or project.owner_id != user.id:
        raise PermissionDenied("자신이 등록한 프로젝트만 수정할 수 있습니다.")


def _schedule_pairs(project: Project) -> list[tuple[datetime, datetime]]:
    return sorted(
        (schedule.start_at, schedule.end_at)
        for schedule in project.schedules
    )


def _schedule_text(project: Project) -> str:
    return "\n".join(
        f"{start.isoformat(timespec='minutes')} / "
        f"{end.isoformat(timespec='minutes')}"
        for start, end in _schedule_pairs(project)
    )


def _schedule_locked(db: Session, project_id: int) -> bool:
    assignment_id = db.scalar(
        select(Assignment.id)
        .where(
            Assignment.project_id == project_id,
            Assignment.status.in_(ACTIVE_ASSIGNMENT_STATUSES),
        )
        .limit(1)
    )
    change_id = db.scalar(
        select(ScheduleChangeRequest.id)
        .where(
            ScheduleChangeRequest.project_id == project_id,
            ScheduleChangeRequest.status == ChangeRequestStatus.PENDING,
        )
        .limit(1)
    )
    return assignment_id is not None or change_id is not None


def _validate_form(name: str, description: str, schedules_text: str):
    errors: list[str] = []
    if not name:
        errors.append("프로젝트 이름을 입력하세요.")
    elif len(name) > 200:
        errors.append("프로젝트 이름은 200자 이하여야 합니다.")

    if len(description) > 10_000:
        errors.append("설명은 10,000자 이하여야 합니다.")

    schedules = []
    try:
        schedules = parse_schedules(schedules_text)
    except ValueError as exc:
        errors.append(str(exc))
    return errors, schedules


def _render_form(
    request: Request,
    *,
    project: Project | None,
    form: dict,
    errors: list[str],
    locked: bool = False,
    status_code: int = 200,
):
    return render(
        request,
        "projects/form.html",
        {
            "project": project,
            "form": form,
            "errors": errors,
            "schedule_locked": locked,
        },
        status_code=status_code,
    )


@router.get("")
def project_list(request: Request, db: Session = Depends(get_db)):
    projects = db.scalars(
        _project_query().order_by(Project.id.desc())
    ).all()
    return render(
        request,
        "projects/list.html",
        {
            "projects": projects,
            "confirmed_status": AssignmentStatus.CONFIRMED,
        },
    )


# /new는 /{project_id}보다 먼저 등록한다.
@router.get("/new")
def project_new_form(
    request: Request,
    user: User = Depends(require_approver),
):
    return _render_form(
        request,
        project=None,
        form={"name": "", "description": "", "schedules_text": ""},
        errors=[],
    )


@router.post("/new", dependencies=[Depends(verify_csrf)])
def project_create(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    schedules_text: str = Form(""),
    user: User = Depends(require_approver),
    db: Session = Depends(get_db),
):
    form = {
        "name": name.strip(),
        "description": description.strip(),
        "schedules_text": schedules_text,
    }
    errors, schedules = _validate_form(
        form["name"], form["description"], schedules_text
    )
    if errors:
        return _render_form(
            request,
            project=None,
            form=form,
            errors=errors,
            status_code=400,
        )

    project = Project(
        name=form["name"],
        description=form["description"],
        owner_id=user.id,
        schedules=[
            ProjectSchedule(start_at=start, end_at=end)
            for start, end in schedules
        ],
    )
    db.add(project)
    db.commit()
    flash(request, "프로젝트를 등록했습니다.", "success")
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


@router.get("/{project_id}")
def project_detail(
    request: Request,
    project_id: int,
    db: Session = Depends(get_db),
):
    project = _get_project(db, project_id)
    return render(
        request,
        "projects/detail.html",
        {
            "project": project,
            "confirmed_status": AssignmentStatus.CONFIRMED,
        },
    )


@router.get("/{project_id}/edit")
def project_edit_form(
    request: Request,
    project_id: int,
    user: User = Depends(require_approver),
    db: Session = Depends(get_db),
):
    project = _get_project(db, project_id)
    _require_owner(project, user)
    return _render_form(
        request,
        project=project,
        form={
            "name": project.name,
            "description": project.description,
            "schedules_text": _schedule_text(project),
        },
        errors=[],
        locked=_schedule_locked(db, project.id),
    )


@router.post("/{project_id}/edit", dependencies=[Depends(verify_csrf)])
def project_update(
    request: Request,
    project_id: int,
    name: str = Form(""),
    description: str = Form(""),
    schedules_text: str = Form(""),
    user: User = Depends(require_approver),
    db: Session = Depends(get_db),
):
    project = _get_project(db, project_id)
    _require_owner(project, user)

    form = {
        "name": name.strip(),
        "description": description.strip(),
        "schedules_text": schedules_text,
    }
    errors, schedules = _validate_form(
        form["name"], form["description"], schedules_text
    )
    locked = _schedule_locked(db, project.id)
    schedule_changed = schedules != _schedule_pairs(project)

    # readonly 속성은 우회할 수 있으므로 서버에서도 반드시 검사한다.
    if not errors and locked and schedule_changed:
        errors.append(
            "진행 중인 배정 또는 일정 변경 요청이 있어 일정을 직접 바꿀 수 없습니다. "
            "이름과 설명은 수정할 수 있습니다. "
            "일정 변경 승인 기능은 5단계에서 추가됩니다."
        )
        return _render_form(
            request,
            project=project,
            form=form,
            errors=errors,
            locked=locked,
            status_code=409,
        )

    if errors:
        return _render_form(
            request,
            project=project,
            form=form,
            errors=errors,
            locked=locked,
            status_code=400,
        )

    project.name = form["name"]
    project.description = form["description"]
    if schedule_changed:
        project.schedules = [
            ProjectSchedule(start_at=start, end_at=end)
            for start, end in schedules
        ]

    db.commit()
    flash(request, "프로젝트를 수정했습니다.", "success")
    return RedirectResponse(f"/projects/{project.id}", status_code=303)
