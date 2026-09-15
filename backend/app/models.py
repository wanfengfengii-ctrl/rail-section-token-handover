"""令牌状态表。

整张表只有一行（id 固定为 1），``holder`` 是当前持有人，
``version`` 是单调递增版本号，每次成功移交恰好加一。
"""

from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ALLOWED_HOLDERS = ("CONSTRUCTION", "TRAFFIC")


class Base(DeclarativeBase):
    pass


class TokenState(Base):
    __tablename__ = "token_state"
    __table_args__ = (
        # 数据库层面再兜底一次：持有人只能是两个合法值。
        CheckConstraint(
            "holder IN ('CONSTRUCTION', 'TRAFFIC')",
            name="ck_token_state_holder",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    holder: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
