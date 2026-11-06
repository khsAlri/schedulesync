"""환경 변수에서 설정을 읽는다. 비밀 값은 코드에 넣지 않는다."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str
    slack_bot_token: str
    app_base_url: str


def _load_settings() -> Settings:
    secret_key = os.getenv("SECRET_KEY", "").strip()
    if not secret_key:
        raise RuntimeError(
            "SECRET_KEY 환경 변수가 설정되지 않았습니다. "
            ".env.example 을 참고해 .env 파일을 만드세요."
        )
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./schedulesync.db").strip(),
        secret_key=secret_key,
        slack_bot_token=os.getenv("SLACK_BOT_TOKEN", "").strip(),
        app_base_url=os.getenv("APP_BASE_URL", "http://localhost:8000").strip().rstrip("/"),
    )


settings = _load_settings()
