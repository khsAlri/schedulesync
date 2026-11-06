"""DB 모델. README 의 시스템 요구사항과 슬랙 DM 알림 설계를 기준으로 한다.

시각은 모두 서버 기준 현지 시각(naive datetime)으로 저장한다.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _enum_column(enum_cls: type[enum.Enum]) -> Enum:
    """Enum 을 DB 에 값 문자열로 저장한다 (DB 종류와 무관하게 동작)."""
    return Enum(
        enum_cls,
        native_enum=False,
        values_callable=lambda members: [m.value for m in members],
        length=30,
    )


# ---------------------------------------------------------------------------
# Enum 정의
# ---------------------------------------------------------------------------


class Role(str, enum.Enum):
    ADMIN = "admin"  # 시스템 관리자: 계정 발급·관리 (별도 역할)
    APPROVER = "approver"  # 허가권자(부서장): 프로젝트 등록·배정·승인
    EMPLOYEE = "employee"  # 일반 직원: 배정 수락·거절


class AssignmentStatus(str, enum.Enum):
    PENDING_ACCEPT = "pending_accept"  # 직원 수락 대기
    PENDING_APPROVAL = "pending_approval"  # 직원 수락 후 허가권자 승인 대기
    CONFIRMED = "confirmed"  # 참여 확정
    DECLINED = "declined"  # 직원이 거절
    REJECTED = "rejected"  # 허가권자 반려 (1명이라도 반려하면 전체 반려)
    CANCELLED = "cancelled"  # 허가권자가 배정을 취소


class Decision(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ChangeRequestStatus(str, enum.Enum):
    PENDING = "pending"  # 승인·동의 대기
    APPLIED = "applied"  # 변경 반영됨
    REJECTED = "rejected"  # 승인 또는 동의 거부로 반영 안 됨
    CANCELLED = "cancelled"  # 요청자가 취소


class ChangeResponseKind(str, enum.Enum):
    APPROVER_APPROVAL = "approver_approval"  # 상대 프로젝트를 등록한 허가권자의 승인
    MEMBER_CONSENT = "member_consent"  # 참여 직원의 동의


class SlackStatus(str, enum.Enum):
    PENDING = "pending"  # 아직 발송 전
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"  # 토큰 미설정 또는 slack_user_id 없음


# ---------------------------------------------------------------------------
# 사용자
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(_enum_column(Role), default=Role.EMPLOYEE)
    department: Mapped[str | None] = mapped_column(String(100), default=None)
    slack_user_id: Mapped[str | None] = mapped_column(String(50), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_approver(self) -> bool:
        return self.role == Role.APPROVER


# ---------------------------------------------------------------------------
# 프로젝트와 일정
# ---------------------------------------------------------------------------


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # 등록한 허가권자. 이 사람만 수정·배정할 수 있다.
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped[User] = relationship()
    schedules: Mapped[list["ProjectSchedule"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectSchedule.start_at",
    )
    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )


class ProjectSchedule(Base):
    """프로젝트의 실제 진행 시간대. 한 프로젝트에 여러 개를 둘 수 있다.

    겹침 판단은 이 시간대끼리만 비교한다 (개인 일정은 관리하지 않음).
    """

    __tablename__ = "project_schedules"
    __table_args__ = (CheckConstraint("end_at > start_at", name="ck_schedule_time_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)

    project: Mapped[Project] = relationship(back_populates="schedules")


# ---------------------------------------------------------------------------
# 배정과 승인
# ---------------------------------------------------------------------------


class Assignment(Base):
    """허가권자가 직원을 프로젝트에 배정한 기록."""

    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[AssignmentStatus] = mapped_column(
        _enum_column(AssignmentStatus), default=AssignmentStatus.PENDING_ACCEPT
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="assignments")
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    assigned_by: Mapped[User] = relationship(foreign_keys=[assigned_by_id])
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
    )


class Approval(Base):
    """일반 직원 배정에 대한 허가권자 한 명의 승인·반려."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("assignment_id", "approver_id", name="uq_approval_per_approver"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), index=True
    )
    approver_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[Decision] = mapped_column(_enum_column(Decision), default=Decision.PENDING)
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    assignment: Mapped[Assignment] = relationship(back_populates="approvals")
    approver: Mapped[User] = relationship()


# ---------------------------------------------------------------------------
# 확정 후 일정 변경
# ---------------------------------------------------------------------------


class ScheduleChangeRequest(Base):
    """확정 참여자가 있는 프로젝트의 일정 변경이 다른 확정 프로젝트와 겹칠 때 만드는 요청."""

    __tablename__ = "schedule_change_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # 변경 후 일정 목록. 예: [{"start_at": "2026-11-10T09:00", "end_at": "2026-11-10T12:00"}]
    new_schedules_json: Mapped[str] = mapped_column(Text)
    status: Mapped[ChangeRequestStatus] = mapped_column(
        _enum_column(ChangeRequestStatus), default=ChangeRequestStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    project: Mapped[Project] = relationship()
    requested_by: Mapped[User] = relationship()
    responses: Mapped[list["ScheduleChangeResponse"]] = relationship(
        back_populates="change_request",
        cascade="all, delete-orphan",
    )


class ScheduleChangeResponse(Base):
    """일정 변경 요청에 대한 허가권자 승인 또는 참여 직원 동의."""

    __tablename__ = "schedule_change_responses"
    __table_args__ = (
        UniqueConstraint(
            "change_request_id", "user_id", "kind", name="uq_change_response_per_user_kind"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    change_request_id: Mapped[int] = mapped_column(
        ForeignKey("schedule_change_requests.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[ChangeResponseKind] = mapped_column(_enum_column(ChangeResponseKind))
    decision: Mapped[Decision] = mapped_column(_enum_column(Decision), default=Decision.PENDING)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    change_request: Mapped[ScheduleChangeRequest] = relationship(back_populates="responses")
    user: Mapped[User] = relationship()


# ---------------------------------------------------------------------------
# 알림
# ---------------------------------------------------------------------------


class Notification(Base):
    """웹 알림. 저장한 뒤 슬랙 DM 을 보내고 그 결과를 함께 기록한다."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    message: Mapped[str] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500), default=None)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    slack_status: Mapped[SlackStatus] = mapped_column(
        _enum_column(SlackStatus), default=SlackStatus.PENDING
    )
    slack_error: Mapped[str | None] = mapped_column(Text, default=None)

    user: Mapped[User] = relationship()
