"""请求/响应模型。

字段缺失由 FastAPI/Pydantic 自动返回 422；
``target_holder`` 非法（不在两个合法持有人之内）同样 422。

``expected_version`` 使用严格整数校验：JSON 布尔（false/true）、
字符串（"0"）、浮点数（0.0/1.5）不会被弱类型强转成版本号蒙混过关，
一律 422，状态保持不变。

``handover_note`` 为可选说明，不超过 200 字；省略时按原契约处理。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

Holder = Literal["CONSTRUCTION", "TRAFFIC"]

HANDOVER_NOTE_MAX_LENGTH = 200


class TokenStateOut(BaseModel):
    holder: Holder
    version: int = Field(ge=0)


class TransferRequest(BaseModel):
    # 多余字段同样拒绝，避免调用方误以为它们生效。
    model_config = ConfigDict(extra="forbid")

    # StrictInt：只接受 JSON 整数；bool/str/float 一律 422。
    expected_version: StrictInt = Field(ge=0)
    target_holder: Holder
    # 可选移交说明；超过 200 字整单 422，不写入任何记录。
    handover_note: str | None = Field(
        default=None, max_length=HANDOVER_NOTE_MAX_LENGTH
    )


class HandoverRecordOut(BaseModel):
    """一条不可修改的移交记录。"""

    from_holder: Holder
    to_holder: Holder
    # 本次移交提交后的令牌版本号。
    version: int = Field(ge=1)
    note: str | None
    # 服务端记录时间（UTC，ISO 8601）。
    created_at: datetime


class ConflictOut(BaseModel):
    detail: str
    current: TokenStateOut
