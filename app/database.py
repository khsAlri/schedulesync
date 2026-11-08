"""DB 연결과 세션 관리."""

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


def configure_sqlite_transactions(target_engine: Engine) -> None:
    """SQLite 트랜잭션을 명시적으로 시작하고 쓰기 작업을 직렬화한다.

    SELECT 이전부터 BEGIN IMMEDIATE로 예약 잠금을 확보한다.
    따라서 권한·겹침 검사와 실제 저장 사이에 다른 쓰기가 끼어들지 않는다.
    모든 세션에 적용되므로 프로젝트 수정과 계정 변경도 같은 보호를 받는다.

    조회도 예약 잠금을 사용하므로 동시 요청 처리량은 낮다.
    사용자 약 10명 규모의 단일 SQLite DB 운영을 위한 선택이다.
    """
    if target_engine.dialect.name != "sqlite":
        return

    @event.listens_for(target_engine, "connect")
    def _configure_connection(dbapi_connection, connection_record):
        # sqlite3의 암묵적 트랜잭션 대신 아래 begin 이벤트를 사용한다.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    @event.listens_for(target_engine, "begin")
    def _begin_transaction(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")


_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)
configure_sqlite_transactions(engine)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """요청마다 세션을 열고 끝나면 미완료 트랜잭션을 정리한다."""
    with SessionLocal() as db:
        yield db


def init_db() -> None:
    """없는 테이블만 만든다. 기존 테이블 구조를 변경하지 않는다."""
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
