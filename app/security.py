"""비밀번호 암호화와 입력값 검사, 폼 위조 방지(CSRF) 토큰."""

import hashlib
import hmac
import re
import secrets

from fastapi import Request

_HASH_ALGORITHM = "pbkdf2_sha256"
_HASH_ITERATIONS = 600_000

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")

_CSRF_SESSION_KEY = "csrf_token"


# ---------------------------------------------------------------------------
# 비밀번호
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """비밀번호를 'pbkdf2_sha256$반복횟수$salt$해시' 형식으로 암호화한다."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _HASH_ITERATIONS
    )
    return f"{_HASH_ALGORITHM}${_HASH_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """입력한 비밀번호가 저장된 해시와 맞는지 확인한다."""
    try:
        algorithm, iterations_text, salt, expected = stored_hash.split("$")
        iterations = int(iterations_text)
    except ValueError:
        return False
    if algorithm != _HASH_ALGORITHM:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    return hmac.compare_digest(digest.hex(), expected)


def validate_password(password: str) -> str | None:
    """비밀번호 규칙을 검사한다. 문제가 있으면 오류 문구, 없으면 None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"비밀번호는 {MAX_PASSWORD_LENGTH}자 이하여야 합니다."
    if password.strip() != password:
        return "비밀번호 앞뒤에 공백을 넣을 수 없습니다."
    return None


# ---------------------------------------------------------------------------
# 입력값 형식
# ---------------------------------------------------------------------------


def normalize_email(email: str) -> str:
    return email.strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email))


def is_valid_slack_user_id(slack_user_id: str) -> bool:
    return bool(_SLACK_USER_ID_RE.match(slack_user_id))


# ---------------------------------------------------------------------------
# CSRF 토큰
# ---------------------------------------------------------------------------


def get_csrf_token(request: Request) -> str:
    """세션에 저장된 CSRF 토큰을 돌려준다. 없으면 새로 만든다."""
    token = request.session.get(_CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_CSRF_SESSION_KEY] = token
    return token


def is_valid_csrf_token(request: Request, sent_token: object) -> bool:
    expected = request.session.get(_CSRF_SESSION_KEY)
    if not expected or not isinstance(sent_token, str):
        return False
    return hmac.compare_digest(sent_token, expected)
