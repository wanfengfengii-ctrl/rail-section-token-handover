"""请求/响应模型。

字段缺失由 FastAPI/Pydantic 自动返回 422；
``target_holder`` 非法（不在两个合法持有人之内）同样 422。

``expected_version`` 使用严格整数校验：JSON 布尔（false/true）、
字符串（"0"）、浮点数（0.0/1.5）不会被弱类型强转成版本号蒙混过关，
一律 422，状态保持不变。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

Holder = Literal["CONSTRUCTION", "TRAFFIC"]


class TokenStateOut(BaseModel):
    holder: Holder
    version: int = Field(ge=0)


class TransferRequest(BaseModel):
    # 多余字段同样拒绝，避免调用方误以为它们生效。
    model_config = ConfigDict(extra="forbid")

    # StrictInt：只接受 JSON 整数；bool/str/float 一律 422。
    expected_version: StrictInt = Field(ge=0)
    target_holder: Holder


class ConflictOut(BaseModel):
    detail: str
    current: TokenStateOut
