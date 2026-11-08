"""일정 변경 라우터 HTTP 테스트.

설치: pip install -r requirements-dev.txt
실행: python -m unittest discover -s tests -v

실제 로그인 라우터, 세션, CSRF, 일정 변경 라우터와 템플릿을 사용한다.
테스트 전용 FastAPI 앱과 임시 DB를 사용하며 운영 앱의 lifespan은 실행하지 않는다.
운영 앱 전체의 미들웨어·오류 처리 검증을 대신하는 테스트는 아니다.
"""

import os
import secrets
import tempfile
import unittest
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.testclient import TestClient
from itsdangerous import SignatureExpired
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

with patch.dict(
    os.environ,
    {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "DATABASE_URL": "sqlite:///:memory:",
    },
):
    from app.config import settings
    from app.database import Base, configure_sqlite_transactions, get_db
    from app.deps import LoginRequired, PermissionDenied
    from app.models import (
        Assignment,
        AssignmentStatus,
        ChangeRequestStatus,
        Project,
        ProjectSchedule,
        Role,
        ScheduleChangeRequest,
        User,
    )
    from app.routers import auth, schedule_changes
    from app.security import hash_password


class HiddenInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("type") == "hidden":
            name = attributes.get("name")
            if name:
                self.values[name] = attributes.get("value", "")


def hidden_value(response, name):
    parser = HiddenInputs()
    parser.feed(response.text)
    if name not in parser.values:
        raise AssertionError(f"숨겨진 입력값을 찾을 수 없습니다: {name}")
    return parser.values[name]


class ScheduleChangeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 테스트 중에만 생성하는 비밀번호이며 실제 계정 정보가 아니다.
        cls.password = secrets.token_urlsafe(18)
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "http.db"
        self.engine = create_engine(
            f"sqlite:///{path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        configure_sqlite_transactions(self.engine)
        Base.metadata.create_all(self.engine)

        with Session(self.engine) as db:
            for user_id, role in [
                (1, Role.APPROVER),
                (2, Role.APPROVER),
                (3, Role.EMPLOYEE),
                (4, Role.EMPLOYEE),
            ]:
                db.add(User(
                    id=user_id,
                    email=f"user{user_id}@example.com",
                    name=f"사용자 {user_id}",
                    password_hash=self.password_hash,
                    role=role,
                ))
            db.flush()
            for project_id, owner_id, start, end in [
                (1, 1, 9, 10),
                (2, 2, 11, 12),
            ]:
                db.add(Project(
                    id=project_id,
                    owner_id=owner_id,
                    name=f"프로젝트 {project_id}",
                    schedules=[ProjectSchedule(
                        start_at=datetime(2026, 11, 10, start),
                        end_at=datetime(2026, 11, 10, end),
                    )],
                ))
            db.flush()
            for project_id, owner_id in [(1, 1), (2, 2)]:
                db.add(Assignment(
                    project_id=project_id,
                    user_id=3,
                    assigned_by_id=owner_id,
                    status=AssignmentStatus.CONFIRMED,
                ))
            db.commit()

        app = FastAPI()
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.secret_key,
            same_site="lax",
        )

        def test_db():
            with Session(self.engine, autoflush=False) as db:
                yield db

        app.dependency_overrides[get_db] = test_db

        @app.exception_handler(LoginRequired)
        async def login_required(request, exc):
            return RedirectResponse("/login", status_code=303)

        @app.exception_handler(PermissionDenied)
        async def permission_denied(request, exc):
            return JSONResponse({"detail": exc.message}, status_code=403)

        app.include_router(auth.router)
        app.include_router(schedule_changes.router)
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def login(self, user_id):
        self.client.cookies.clear()
        page = self.client.get("/login")
        response = self.client.post("/login", data={
            "csrf_token": hidden_value(page, "csrf_token"),
            "email": f"user{user_id}@example.com",
            "password": self.password,
        })
        self.assertEqual(response.status_code, 303)

    def preview(self, text="2026-11-10T11:00 / 2026-11-10T12:00"):
        page = self.client.get("/schedule-changes/projects/1/new")
        self.assertEqual(page.status_code, 200)
        return self.client.post(
            "/schedule-changes/projects/1/preview",
            data={
                "csrf_token": hidden_value(page, "csrf_token"),
                "schedules_text": text,
            },
        )

    def submit(self, preview, **overrides):
        data = {
            "csrf_token": hidden_value(preview, "csrf_token"),
            "preview_token": hidden_value(preview, "preview_token"),
            "warning_acknowledged": "on",
        }
        data.update(overrides)
        return self.client.post(
            "/schedule-changes/projects/1/submit", data=data
        )

    def request_id(self):
        with Session(self.engine) as db:
            return db.scalar(select(ScheduleChangeRequest.id))

    def assert_original(self):
        with Session(self.engine) as db:
            self.assertEqual(
                db.get(Project, 1).schedules[0].start_at,
                datetime(2026, 11, 10, 9),
            )

    def respond(self, request_id, kind):
        page = self.client.get("/schedule-changes")
        self.assertEqual(page.status_code, 200)
        return self.client.post(
            f"/schedule-changes/{request_id}/respond",
            data={
                "csrf_token": hidden_value(page, "csrf_token"),
                "kind": kind,
                "decision": "approve",
            },
        )

    def test_login_is_required(self):
        response = self.client.get("/schedule-changes")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_non_owner_and_employee_cannot_open_form(self):
        for user_id in [2, 3]:
            with self.subTest(user_id=user_id):
                self.login(user_id)
                response = self.client.get(
                    "/schedule-changes/projects/1/new"
                )
                self.assertEqual(response.status_code, 403)

    def test_missing_csrf_is_blocked(self):
        self.login(1)
        response = self.client.post(
            "/schedule-changes/projects/1/preview",
            data={"schedules_text": "invalid"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.request_id())

    def test_invalid_schedule_is_shown_without_creating_request(self):
        self.login(1)
        response = self.preview("invalid")
        self.assertEqual(response.status_code, 400)
        self.assertIn("형식", response.text)
        self.assertIsNone(self.request_id())

    def test_warning_must_be_acknowledged_on_server(self):
        self.login(1)
        preview = self.preview()
        self.assertEqual(preview.status_code, 200)
        response = self.submit(preview, warning_acknowledged="")
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(self.request_id())
        self.assert_original()

    def test_tampered_preview_is_rejected(self):
        self.login(1)
        preview = self.preview()
        response = self.submit(preview, preview_token="invalid.signature")
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(self.request_id())
        self.assert_original()

    def test_expired_preview_is_rejected(self):
        self.login(1)
        preview = self.preview()
        with patch.object(
            schedule_changes.signer,
            "loads",
            side_effect=SignatureExpired("expired"),
        ):
            response = self.submit(preview)
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(self.request_id())
        self.assert_original()

    def test_full_approval_and_consent_flow(self):
        self.login(1)
        preview = self.preview()
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(self.submit(preview).status_code, 303)
        request_id = self.request_id()
        self.assertIsNotNone(request_id)
        self.assert_original()

        # 요청 목록 템플릿도 실제로 렌더링한다.
        page = self.client.get("/schedule-changes")
        self.assertEqual(page.status_code, 200)
        self.assertIn("요청 당시 기존 일정", page.text)

        self.login(2)
        self.assertEqual(
            self.respond(request_id, "approver_approval").status_code,
            303,
        )
        self.assert_original()

        self.login(3)
        self.assertEqual(
            self.respond(request_id, "member_consent").status_code,
            303,
        )
        with Session(self.engine) as db:
            self.assertEqual(
                db.get(ScheduleChangeRequest, request_id).status,
                ChangeRequestStatus.APPLIED,
            )
            self.assertEqual(
                db.get(Project, 1).schedules[0].start_at,
                datetime(2026, 11, 10, 11),
            )

    def test_unrelated_user_cannot_respond_or_cancel(self):
        self.login(1)
        self.submit(self.preview())
        request_id = self.request_id()

        self.login(4)
        page = self.client.get("/schedule-changes")
        csrf = hidden_value(page, "csrf_token")

        response = self.client.post(
            f"/schedule-changes/{request_id}/respond",
            data={
                "csrf_token": csrf,
                "kind": "member_consent",
                "decision": "approve",
            },
        )
        self.assertEqual(response.status_code, 303)

        response = self.client.post(
            f"/schedule-changes/{request_id}/cancel",
            data={"csrf_token": csrf},
        )
        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as db:
            self.assertEqual(
                db.get(ScheduleChangeRequest, request_id).status,
                ChangeRequestStatus.PENDING,
            )
        self.assert_original()

    def test_requester_can_cancel_via_http(self):
        self.login(1)
        self.submit(self.preview())
        request_id = self.request_id()
        page = self.client.get("/schedule-changes")
        response = self.client.post(
            f"/schedule-changes/{request_id}/cancel",
            data={"csrf_token": hidden_value(page, "csrf_token")},
        )
        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as db:
            self.assertEqual(
                db.get(ScheduleChangeRequest, request_id).status,
                ChangeRequestStatus.CANCELLED,
            )
        self.assert_original()


if __name__ == "__main__":
    unittest.main()
