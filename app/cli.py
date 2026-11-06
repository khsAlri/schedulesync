"""명령줄 관리 도구.

사용법:
    python -m app.cli create-admin   # 시스템 관리자 계정 만들기 (첫 실행 시 필요)
"""

import argparse
import getpass
import sys

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import Role, User
from app.security import hash_password, is_valid_email, normalize_email, validate_password


def _fail(message: str) -> int:
    print(f"오류: {message}", file=sys.stderr)
    return 1


def create_admin() -> int:
    init_db()

    email = normalize_email(input("관리자 이메일: "))
    if not is_valid_email(email):
        return _fail("이메일 형식이 올바르지 않습니다.")

    name = input("이름: ").strip()
    if not name:
        return _fail("이름을 입력하세요.")

    password = getpass.getpass("비밀번호: ")
    password_error = validate_password(password)
    if password_error:
        return _fail(password_error)
    if password != getpass.getpass("비밀번호 확인: "):
        return _fail("비밀번호 확인이 일치하지 않습니다.")

    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.email == email)) is not None:
            return _fail("이미 등록된 이메일입니다.")
        db.add(
            User(
                email=email,
                name=name,
                password_hash=hash_password(password),
                role=Role.ADMIN,
            )
        )
        db.commit()

    print(f"시스템 관리자 계정을 만들었습니다: {email}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="schedulesync 관리 도구")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create-admin", help="시스템 관리자 계정을 만든다")

    args = parser.parse_args(argv)
    if args.command == "create-admin":
        return create_admin()
    return 1


if __name__ == "__main__":
    sys.exit(main())
