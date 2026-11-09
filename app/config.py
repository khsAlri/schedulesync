"""환경 변수 기반 설정. 운영 환경의 안전하지 않은 설정은 시작 시 거부한다."""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name}은 true 또는 false로 설정하세요.")


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str
    slack_bot_token: str
    app_base_url: str
    environment: str = "development"
    session_https_only: bool = False
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")


def _load_settings() -> Settings:
    environment = os.getenv("APP_ENV", "development").strip().lower()
    if environment not in {"development", "production"}:
        raise RuntimeError("APP_ENV는 development 또는 production이어야 합니다.")

    secret_key = os.getenv("SECRET_KEY", "").strip()
    if not secret_key:
        raise RuntimeError(
            "SECRET_KEY 환경 변수가 설정되지 않았습니다. "
            ".env.example을 참고해 .env 파일을 만드세요."
        )

    production = environment == "production"
    https_only = _bool_env("SESSION_HTTPS_ONLY", production)
    hosts = tuple(
        host.strip()
        for host in os.getenv(
            "ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"
        ).split(",")
        if host.strip()
    )
    if not hosts:
        raise RuntimeError("ALLOWED_HOSTS를 한 개 이상 설정하세요.")
    if any(
        "/" in host or ":" in host or any(character.isspace() for character in host)
        for host in hosts
    ):
        raise RuntimeError("ALLOWED_HOSTS에는 포트와 프로토콜 없는 호스트명을 넣으세요.")

    base_url = os.getenv("APP_BASE_URL", "http://localhost:8000").strip().rstrip("/")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("APP_BASE_URL이 올바르지 않습니다.") from exc

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or "\\" in base_url
        or any(character.isspace() for character in base_url)
    ):
        raise RuntimeError(
            "APP_BASE_URL에는 경로·인증 정보 없는 http/https 기본 주소를 넣으세요."
        )
    if port is not None and not 1 <= port <= 65535:
        raise RuntimeError("APP_BASE_URL의 포트가 올바르지 않습니다.")

    if production:
        if len(secret_key) < 32:
            raise RuntimeError("운영 SECRET_KEY는 무작위 문자열 32자 이상이어야 합니다.")
        if not https_only or parsed.scheme != "https":
            raise RuntimeError(
                "운영 환경에는 HTTPS 주소와 SESSION_HTTPS_ONLY=true가 필요합니다."
            )
        if any("*" in host for host in hosts):
            raise RuntimeError("운영 ALLOWED_HOSTS에는 와일드카드를 사용할 수 없습니다.")
        if parsed.hostname not in hosts:
            raise RuntimeError("APP_BASE_URL의 호스트를 ALLOWED_HOSTS에 포함하세요.")

    return Settings(
        database_url=os.getenv(
            "DATABASE_URL", "sqlite:///./schedulesync.db"
        ).strip(),
        secret_key=secret_key,
        slack_bot_token=os.getenv("SLACK_BOT_TOKEN", "").strip(),
        app_base_url=base_url,
        environment=environment,
        session_https_only=https_only,
        allowed_hosts=hosts,
    )


settings = _load_settings()
