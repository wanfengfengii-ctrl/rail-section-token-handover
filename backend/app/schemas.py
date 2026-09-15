"""请求/响应模型。

字段缺失由 FastAPI/Pydantic 自动返回 422；
``target_holder`` 非法（不在两个合法持有人之内）同样 422。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Holder = Literal["CONSTRUCTION", "TRAFFIC"]


class TokenStateOut(BaseModel):
    holder: Holder
    version: int = Field(ge=0)


class TransferRequest(BaseModel):
    # 多余字段同样拒绝，避免调用方误以为它们生效。
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    target_holder: Holder


class ConflictOut(BaseModel):
    detail: str
    current: TokenStateOut
