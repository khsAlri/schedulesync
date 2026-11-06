"""Jinja2 템플릿 설정. 여러 라우터에서 같은 객체를 쓴다."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
