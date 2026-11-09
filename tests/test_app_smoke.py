"""실제 app.main 앱의 시작·인증·주요 화면과 설정 검사.

임시 DB를 의존성으로 주입하고 lifespan의 DB 초기화 대상도 대체한다. 실제 Slack 통신과 운영 DB는 사용하지 않는다. """

import os import secrets import tempfile import unittest from datetime import datetime from html.parser import HTMLParser from pathlib import Path from unittest.mock import patch

from fastapi.testclient import TestClient from sqlalchemy import create_engine from sqlalchemy.orm import Session

with patch.dict(os.environ, { "SECRET_KEY": secrets.token_urlsafe(48), "DATABASE_URL": "sqlite:///:memory:", "APP_ENV": "development", "APP_BASE_URL": "http://localhost:8000", "SESSION_HTTPS_ONLY": "false", "ALLOWED_HOSTS": "localhost,127.0.0.1,testserver", }): from app import main from app.config import _load_settings from app.database import Base, configure_sqlite_transactions, get_db from app.models import ( Notification, Project, ProjectSchedule, Role, User, ) from app.security import hash_password

class Inputs(HTMLParser): def init(self): super().init() self.values = {}

def handle_starttag(self, tag, attrs):
    values = dict(attrs)
    if tag == "input" and values.get("type") == "hidden":
        self.values[values.get("name")] = values.get("value", "")
def csrf(response): parser = Inputs() parser.feed(response.text) return parser.values["csrf_token"]

class AppSmokeTests(unittest.TestCase): @classmethod def setUpClass(cls): cls.password = secrets.token_urlsafe(18) cls.password_hash = hash_password(cls.password)

def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    path = Path(self.temp.name) / "app.db"
    self.engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    configure_sqlite_transactions(self.engine)
    Base.metadata.create_all(self.engine)

    with Session(self.engine) as db:
        for user_id, role in [
            (1, Role.ADMIN), (2, Role.APPROVER), (3, Role.EMPLOYEE),
        ]:
            db.add(User(
                id=user_id,
                email=f"user{user_id}@example.com",
                name=f"사용자 {user_id}",
                role=role,
                password_hash=self.password_hash,
            ))
        db.flush()
        db.add(Project(
            id=1,
            owner_id=2,
            name="통합검사 프로젝트",
            schedules=[ProjectSchedule(
                start_at=datetime(2026, 11, 10, 9),
                end_at=datetime(2026, 11, 10, 12),
            )],
        ))
        db.add(Notification(
            user_id=3,
            message="통합검사 알림",
            link="/assignments",
        ))
        db.commit()

    def test_db():
        with Session(self.engine, autoflush=False) as db:
            yield db

    self.old_overrides = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[get_db] = test_db
    self.init_patch = patch.object(
        main,
        "init_db",
        side_effect=lambda: Base.metadata.create_all(self.engine),
    )
    self.init_mock = self.init_patch.start()
    self.client = TestClient(
        main.app,
        base_url="https://localhost",
        follow_redirects=False,
    )
    self.client.__enter__()

def tearDown(self):
    try:
        self.client.__exit__(None, None, None)
    finally:
        self.init_patch.stop()
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(self.old_overrides)
        self.engine.dispose()
        self.temp.cleanup()

def login(self, user_id):
    self.client.cookies.clear()
    page = self.client.get("/login")
    self.assertEqual(page.status_code, 200)
    result = self.client.post("/login", data={
        "csrf_token": csrf(page),
        "email": f"user{user_id}@example.com",
        "password": self.password,
    })
    self.assertEqual(result.status_code, 303)

def test_actual_lifespan_and_anonymous_health(self):
    self.init_mock.assert_called_once()
    self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
    response = self.client.get("/")
    self.assertEqual(response.status_code, 303)
    self.assertTrue(response.headers["location"].startswith("/login"))
    self.assertEqual(self.client.get("/static/style.css").status_code, 200)

def test_all_main_pages_render_for_employee(self):
    self.login(3)
    for path in [
        "/", "/projects", "/projects/1", "/assignments",
        "/schedule-changes", "/notifications", "/account/password",
    ]:
        with self.subTest(path=path):
            self.assertEqual(self.client.get(path).status_code, 200)
    self.assertEqual(self.client.get("/admin/users").status_code, 403)
    self.assertEqual(
        self.client.get("/projects/1/edit").status_code, 403
    )
    self.assertEqual(self.client.get("/not-a-page").status_code, 404)

def test_admin_and_project_owner_forms_render(self):
    self.login(1)
    for path in ["/admin/users", "/admin/users/new", "/admin/users/3/edit"]:
        with self.subTest(path=path):
            self.assertEqual(self.client.get(path).status_code, 200)

    self.login(2)
    for path in [
        "/projects/new",
        "/projects/1/edit",
        "/schedule-changes/projects/1/new",
    ]:
        with self.subTest(path=path):
            self.assertEqual(self.client.get(path).status_code, 200)

def test_csrf_logout_and_cookie_flags(self):
    page = self.client.get("/login")
    cookie = page.headers.get("set-cookie", "").lower()
    self.assertIn("httponly", cookie)
    self.assertIn("samesite=lax", cookie)
    if main.settings.session_https_only:
        self.assertIn("secure", cookie)

    self.login(3)
    self.assertEqual(self.client.post("/logout").status_code, 403)
    page = self.client.get("/")
    response = self.client.post("/logout", data={"csrf_token": csrf(page)})
    self.assertEqual(response.status_code, 303)
    self.assertEqual(self.client.get("/").status_code, 303)
class ProductionSettingsTests(unittest.TestCase): def load(self, **overrides): values = { "APP_ENV": "production", "SECRET_KEY": secrets.token_urlsafe(48), "APP_BASE_URL": "https://schedule.example.com", "ALLOWED_HOSTS": "schedule.example.com", "SESSION_HTTPS_ONLY": "true", } values.update(overrides) with patch.dict(os.environ, values): return _load_settings()

def test_valid_production_settings(self):
    settings = self.load()
    self.assertTrue(settings.session_https_only)
    self.assertEqual(settings.allowed_hosts, ("schedule.example.com",))

def test_unsafe_production_settings_are_rejected(self):
    for overrides in [
        {"SECRET_KEY": "short"},
        {"APP_BASE_URL": "http://schedule.example.com"},
        {"SESSION_HTTPS_ONLY": "false"},
        {"ALLOWED_HOSTS": "*"},
        {"ALLOWED_HOSTS": "other.example.com"},
        {"APP_BASE_URL": "https://user:pass@schedule.example.com"},
        {"APP_BASE_URL": "https://schedule.example.com/subpath"},
        {"SESSION_HTTPS_ONLY": "invalid"},
    ]:
        with self.subTest(overrides=overrides):
            with self.assertRaises(RuntimeError):
                self.load(**overrides)
if name == "main": unittest.main() 
