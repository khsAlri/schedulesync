"""일정 변경 요청 당시의 비교 기준을 저장한다.

기존 요청 테이블의 일정 JSON 형식은 그대로 유지한다.
별도 테이블이므로 기존 테이블에 컬럼을 추가할 필요가 없다.
"""

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScheduleChangeBaseline(Base):
    __tablename__ = "schedule_change_baselines"

    change_request_id: Mapped[int] = mapped_column(
        ForeignKey("schedule_change_requests.id", ondelete="CASCADE"),
        primary_key=True,
    )
    snapshot_json: Mapped[str] = mapped_column(Text)
