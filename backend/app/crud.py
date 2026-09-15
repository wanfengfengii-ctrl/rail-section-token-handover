"""令牌状态的读写逻辑。

移交在单个数据库事务中完成：

1. 事务由 ``BEGIN IMMEDIATE`` 开启（见 database.py），进入即持有写锁；
2. 读取唯一一行当前状态；
3. 校验 ``expected_version`` 与当前版本一致、目标与当前持有人相反；
4. 用带 ``WHERE id = 1 AND version = :expected`` 的条件 UPDATE 落库，
   成功时版本号恰好加一。

任一步失败都回滚，状态不被改写。并发事务被 SQLite 写锁串行化，
因此同一版本的两次移交只会有一次条件更新命中。
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import TokenState

Holder = Literal["CONSTRUCTION", "TRAFFIC"]


class ConflictError(Exception):
    """expected_version 与当前版本不一致（HTTP 409）。"""


class IllegalTargetError(Exception):
    """目标持有人不是当前持有人的对立方（HTTP 422）。"""


class MissingStateError(Exception):
    """唯一状态行缺失，说明未执行初始化（HTTP 500）。"""


@dataclass(frozen=True)
class TokenSnapshot:
    holder: str
    version: int


def get_token(db: Session) -> TokenSnapshot:
    state = db.get(TokenState, 1)
    if state is None:
        raise MissingStateError
    return TokenSnapshot(holder=state.holder, version=state.version)


def transfer_token(
    db: Session,
    expected_version: int,
    target_holder: str,
) -> TokenSnapshot:
    """在调用方提供的会话/事务中完成校验与移交。"""

    state = db.get(TokenState, 1)
    if state is None:
        raise MissingStateError

    # 乐观锁：版本先行校验。持锁状态下读到的就是已提交的最新版本。
    if state.version != expected_version:
        raise ConflictError(
            TokenSnapshot(holder=state.holder, version=state.version)
        )

    # 只能交给对方：CONSTRUCTION <-> TRAFFIC。
    if target_holder == state.holder:
        raise IllegalTargetError

    # 条件更新作为第二道保险：即使锁语义被绕过，版本不匹配也命中 0 行。
    result = db.execute(
        update(TokenState)
        .where(TokenState.id == 1, TokenState.version == expected_version)
        .values(holder=target_holder, version=TokenState.version + 1)
    )
    if result.rowcount != 1:
        raise ConflictError(
            TokenSnapshot(holder=state.holder, version=state.version)
        )

    return TokenSnapshot(holder=target_holder, version=expected_version + 1)
