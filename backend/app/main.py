"""FastAPI 入口（应用工厂）。

接口：
* ``GET  /api/token``           查询当前持有人与版本号
* ``POST /api/token/transfer``  移交令牌（请求体见 schemas.TransferRequest，
                                可附带不超过 200 字的 handover_note）
* ``GET  /api/token/handovers`` 最近 10 条移交记录，按版本倒序

状态码约定：
* 200 移交成功，返回最新状态；
* 409 版本不匹配，响应体携带服务器最新状态，页面据此放弃本次意图；
* 422 字段缺失、expected_version 非法、说明超长，或目标持有人不合法
      （不是两个合法值之一，或与当前持有人相同）。

每次成功移交在更新唯一令牌的同一数据库事务中写入一条不可修改的移交记录；
任何 409、422 或事务失败都会整体回滚，不留下记录。
"""

from contextlib import asynccontextmanager
from datetime import timezone

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crud
from .config import CORS_ORIGINS
from .database import Database
from .models import HandoverRecord, TokenState
from .schemas import ConflictOut, HandoverRecordOut, TokenStateOut, TransferRequest

# 记录接口固定返回的最近条数。
RECENT_HANDOVERS_LIMIT = 10


def get_db_factory(database: Database):
    def get_db() -> Session:
        db = database.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    return get_db


def _normalize_note(raw: str | None) -> str | None:
    """说明去首尾空白；空白说明等价于省略（记录为 NULL）。"""

    if raw is None:
        return None
    return raw.strip() or None


def create_app(database: Database) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动即建表（含旧库自动补建移交记录表）并在空库时初始化
        # (CONSTRUCTION, 0)；已有令牌状态不会被重置。
        database.init_db()
        yield

    app = FastAPI(title="区间占用令牌移交服务", version="1.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    get_db = get_db_factory(database)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/token", response_model=TokenStateOut)
    def read_token():
        # 纯读取走 AUTOCOMMIT 单条 SELECT，不申请写锁，
        # 避免在 WAL 下读者与移交事务相互阻塞。
        with database.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            row = conn.execute(
                select(TokenState.holder, TokenState.version).where(
                    TokenState.id == 1
                )
            ).first()
        if row is None:
            return JSONResponse(
                status_code=500, content={"detail": "令牌未初始化"}
            )
        return TokenStateOut(holder=row.holder, version=row.version)

    @app.get("/api/token/handovers", response_model=list[HandoverRecordOut])
    def read_handovers():
        # 与令牌读取同口径：AUTOCOMMIT 单条 SELECT，不申请写锁；
        # 该接口故障只影响页面记录区，不阻断令牌主流程。
        with database.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            rows = conn.execute(
                select(
                    HandoverRecord.from_holder,
                    HandoverRecord.to_holder,
                    HandoverRecord.version,
                    HandoverRecord.note,
                    HandoverRecord.created_at,
                )
                .order_by(HandoverRecord.version.desc())
                .limit(RECENT_HANDOVERS_LIMIT)
            ).all()
        return [
            HandoverRecordOut(
                from_holder=row.from_holder,
                to_holder=row.to_holder,
                version=row.version,
                note=row.note,
                # 库里存的是 naive UTC，响应里显式标注时区。
                created_at=row.created_at.replace(tzinfo=timezone.utc),
            )
            for row in rows
        ]

    @app.post(
        "/api/token/transfer",
        response_model=TokenStateOut,
        responses={409: {"model": ConflictOut}},
    )
    def transfer(body: TransferRequest, db: Session = Depends(get_db)):
        # 第一个 SQL 触发 begin 事件 → BEGIN IMMEDIATE，整个函数体在一个
        # 数据库事务中；commit 正常返回，任何异常回滚，状态与记录都不留痕。
        try:
            snapshot = crud.transfer_token(
                db,
                expected_version=body.expected_version,
                target_holder=body.target_holder,
                handover_note=_normalize_note(body.handover_note),
            )
        except crud.ConflictError as exc:
            db.rollback()
            current = exc.args[0]
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "状态已变化，请重新确认",
                    "current": {
                        "holder": current.holder,
                        "version": current.version,
                    },
                },
            )
        except crud.IllegalTargetError:
            db.rollback()
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "目标持有人非法：必须是与当前持有人相反的一方"
                },
            )
        except crud.MissingStateError:
            db.rollback()
            return JSONResponse(status_code=500, content={"detail": "令牌未初始化"})

        try:
            db.commit()
        except Exception:
            # 提交失败：令牌更新与移交记录在同一事务里一起回滚。
            db.rollback()
            raise
        return TokenStateOut(holder=snapshot.holder, version=snapshot.version)

    return app
