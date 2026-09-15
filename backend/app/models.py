"""令牌状态表与移交记录表。

``token_state`` 整张表只有一行（id 固定为 1），``holder`` 是当前持有人，
``version`` 是单调递增版本号，每次成功移交恰好加一。

``handover_record`` 是只增不改的移交台账：每次成功移交在**同一数据库事务**
内追加一行，记录从哪一台交给哪一台、提交后的版本号、说明与服务端记录时间。
表上的两个触发器拒绝任何 UPDATE / DELETE，保证记录不可修改。
"""

from datetime import datetime, timezone

from sqlalchemy import DDL, CheckConstraint, DateTime, Integer, String, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ALLOWED_HOLDERS = ("CONSTRUCTION", "TRAFFIC")


def _utc_now() -> datetime:
    """当前 UTC 时间（naive 存储，读出时由接口层补上时区）。"""

    return datetime.now(timezone.utc).replace(tzinfo=None)


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


class HandoverRecord(Base):
    """一次成功移交的不可修改记录；version 与令牌提交后的版本号一一对应。"""

    __tablename__ = "handover_record"
    __table_args__ = (
        CheckConstraint(
            "from_holder IN ('CONSTRUCTION', 'TRAFFIC')",
            name="ck_handover_record_from",
        ),
        CheckConstraint(
            "to_holder IN ('CONSTRUCTION', 'TRAFFIC')",
            name="ck_handover_record_to",
        ),
        CheckConstraint("from_holder <> to_holder", name="ck_handover_record_flip"),
        CheckConstraint(
            "note IS NULL OR length(note) <= 200", name="ck_handover_record_note"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_holder: Mapped[str] = mapped_column(String(16), nullable=False)
    to_holder: Mapped[str] = mapped_column(String(16), nullable=False)
    # 本次移交提交后的令牌版本号。
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now
    )


# 记录只增不改：数据库触发器拒绝 UPDATE / DELETE，应用层之外也改不动。
for _ddl in (
    DDL(
        """
        CREATE TRIGGER handover_record_no_update
        BEFORE UPDATE ON handover_record
        BEGIN
            SELECT RAISE(ABORT, 'handover_record is immutable');
        END
        """
    ),
    DDL(
        """
        CREATE TRIGGER handover_record_no_delete
        BEFORE DELETE ON handover_record
        BEGIN
            SELECT RAISE(ABORT, 'handover_record is immutable');
        END
        """
    ),
):
    event.listen(HandoverRecord.__table__, "after_create", _ddl)
