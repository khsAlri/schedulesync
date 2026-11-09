"""사이트 내 알림 HTTP 테스트.

설치: pip install -r requirements-dev.txt
실행: python -m unittest discover -s tests -v

실제 인증·알림 라우터와 템플릿을 테스트 전용 앱에 연결한다.
임시 SQLite DB만 사용하며 실제 슬랙에 접속하지 않는다.
운영 앱의 lifespan과 전체 오류 처리 구성은 테스트 범위가 아니다.
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
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
    from app.models import Notification, Role, SlackStatus, User
    from app.routers import auth, notifications
    from app.security import hash_password


STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "static"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = {}
        self.links = []
        self.nav_text = []
        self.nav_depth = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("type") == "hidden":
            self.hidden[values.get("name")] = values.get("value", "")
        if tag == "a":
            self.links.append(values.get("href", ""))
        if tag == "nav":
            self.nav_depth += 1

    def handle_endtag(self, tag):
        if tag == "nav":
            self.nav_depth = max(0, self.nav_depth - 1)

    def handle_data(self, data):
        if self.nav_depth:
            self.nav_text.append(data)


def parse_page(response):
    parser = PageParser()
    parser.feed(response.text)
    return parser


class NotificationHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = secrets.token_urlsafe(18)
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "notification-http.db"
        self.engine = create_engine(
            f"sqlite:///{path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        configure_sqlite_transactions(self.engine)
        Base.metadata.create_all(self.engine)

        with Session(self.engine) as db:
            for user_id in [1, 2]:
                db.add(User(
                    id=user_id,
                    email=f"user{user_id}@example.com",
                    name=f"사용자 {user_id}",
                    password_hash=self.password_hash,
                    role=Role.EMPLOYEE,
                ))
            db.flush()

            for notification_id in range(1, 23):
                db.add(Notification(
                    id=notification_id,
                    user_id=1,
                    message=f"알림본문-{notification_id:03d}",
                    link="/assignments",
                    is_read=notification_id == 1,
                    created_at=datetime(2026, 11, 9, 9),
                    slack_status=SlackStatus.SENT,
                ))
            db.add(Notification(
                id=100,
                user_id=2,
                message="다른 계정 전용 비공개 알림",
                is_read=False,
                slack_status=SlackStatus.PENDING,
            ))
            db.commit()

        app = FastAPI()
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.secret_key,
            same_site="lax",
        )
        app.mount(
            "/static",
            StaticFiles(directory=str(STATIC_DIR)),
            name="static",
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
        app.include_router(notifications.router)
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def login(self, user_id=1):
        self.client.cookies.clear()
        page = self.client.get("/login")
        self.assertEqual(page.status_code, 200)
        csrf = parse_page(page).hidden["csrf_token"]
        response = self.client.post("/login", data={
            "csrf_token": csrf,
            "email": f"user{user_id}@example.com",
            "password": self.password,
        })
        self.assertEqual(response.status_code, 303)

    def csrf(self):
        page = self.client.get("/notifications")
        self.assertEqual(page.status_code, 200)
        return parse_page(page).hidden["csrf_token"]

    def post_read(self, notification_id, csrf=None):
        return self.client.post(
            f"/notifications/{notification_id}/read",
            data={"csrf_token": self.csrf() if csrf is None else csrf},
        )

    def notification_state(self, notification_id):
        with Session(self.engine) as db:
            row = db.get(Notification, notification_id)
            return row.is_read, row.slack_status

    def test_anonymous_user_is_redirected(self):
        response = self.client.get("/notifications")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_list_is_private_paginated_and_does_not_mark_read(self):
        self.login()
        page = self.client.get("/notifications")
        self.assertEqual(page.status_code, 200)
        self.assertIn("알림본문-022", page.text)
        self.assertNotIn("알림본문-002", page.text)
        self.assertNotIn("다른 계정 전용 비공개 알림", page.text)

        second = self.client.get("/notifications?page=2")
        self.assertIn("알림본문-002", second.text)
        self.assertIn("알림본문-001", second.text)
        self.assertNotIn("알림본문-022", second.text)
        self.assertFalse(self.notification_state(22)[0])

    def test_unread_filter_excludes_read_notification(self):
        self.login()
        page = self.client.get(
            "/notifications?page=2&unread_only=true"
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("알림본문-002", page.text)
        self.assertNotIn("알림본문-001", page.text)

    def test_menu_count_updates_after_read(self):
        self.login()
        page = self.client.get("/notifications")
        nav_text = "".join(parse_page(page).nav_text)
        self.assertIn("(21)", nav_text)

        response = self.post_read(22)
        self.assertEqual(response.status_code, 303)
        page = self.client.get("/notifications")
        nav_text = "".join(parse_page(page).nav_text)
        self.assertIn("(20)", nav_text)
        self.assertEqual(
            self.notification_state(22), (True, SlackStatus.SENT)
        )

    def test_missing_or_invalid_csrf_cannot_change_state(self):
        self.login()
        for path in [
            "/notifications/22/read",
            "/notifications/read-all",
        ]:
            for data in [{}, {"csrf_token": "invalid"}]:
                with self.subTest(path=path, data=data):
                    response = self.client.post(path, data=data)
                    self.assertEqual(response.status_code, 403)
        self.assertFalse(self.notification_state(22)[0])

    def test_other_users_and_missing_notifications_have_same_404(self):
        self.login()
        csrf = self.csrf()
        for notification_id in [100, 999]:
            with self.subTest(notification_id=notification_id):
                response = self.post_read(notification_id, csrf=csrf)
                self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.notification_state(100), (False, SlackStatus.PENDING)
        )

    def test_get_cannot_mark_notification_read(self):
        self.login()
        response = self.client.get("/notifications/22/read")
        self.assertEqual(response.status_code, 405)
        self.assertFalse(self.notification_state(22)[0])

    def test_read_all_only_affects_current_user_and_is_idempotent(self):
        self.login()
        csrf = self.csrf()
        for _ in range(2):
            response = self.client.post(
                "/notifications/read-all",
                data={"csrf_token": csrf},
            )
            self.assertEqual(response.status_code, 303)

        with Session(self.engine) as db:
            for notification_id in range(1, 23):
                row = db.get(Notification, notification_id)
                self.assertTrue(row.is_read)
                self.assertEqual(row.slack_status, SlackStatus.SENT)

        self.assertFalse(self.notification_state(100)[0])
        page = self.client.get("/notifications?unread_only=true")
        self.assertIn("표시할 미확인 알림이 없습니다.", page.text)
        self.assertNotIn("(21)", "".join(parse_page(page).nav_text))

    def test_html_is_escaped_and_external_links_are_not_rendered(self):
        self.login()
        with Session(self.engine) as db:
            row = db.get(Notification, 22)
            row.message = "<script>alert(1)</script>"
            row.link = "https://unsafe.example.com"
            db.commit()

        page = self.client.get("/notifications")
        self.assertNotIn("<script>alert(1)</script>", page.text)
        self.assertIn("&lt;script&gt;", page.text)
        self.assertNotIn(
            "https://unsafe.example.com", parse_page(page).links
        )
        self.assertIn("/assignments", parse_page(page).links)

    def test_deactivated_user_loses_access(self):
        self.login()
        with Session(self.engine) as db:
            db.get(User, 1).is_active = False
            db.commit()
        response = self.client.get("/notifications")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_invalid_page_is_rejected(self):
        self.login()
        for page in [0, -1, 1_000_001]:
            with self.subTest(page=page):
                response = self.client.get(
                    "/notifications", params={"page": page}
                )
                self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
