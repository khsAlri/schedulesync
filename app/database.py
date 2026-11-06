"""DB 연결과 세션 관리."""

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        # SQLite 는 기본적으로 외래 키 제약을 검사하지 않으므로 켠다.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """요청마다 DB 세션을 열고 끝나면 닫는다 (FastAPI 의존성)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """모든 테이블을 만든다. 이미 있는 테이블은 건드리지 않는다."""
    from app import models  # noqa: F401  (모델을 등록하기 위해 import)

    Base.metadata.create_all(bind=engine)
