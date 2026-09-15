"""FastAPI 入口（应用工厂）。

接口：
* ``GET  /api/token``         查询当前持有人与版本号
* ``POST /api/token/transfer`` 移交令牌（请求体见 schemas.TransferRequest）

状态码约定：
* 200 移交成功，返回最新状态；
* 409 版本不匹配，响应体携带服务器最新状态，页面据此放弃本次意图；
* 422 字段缺失、expected_version 非法，或目标持有人不合法
      （不是两个合法值之一，或与当前持有人相同）。
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crud
from .config import CORS_ORIGINS
from .database import Database
from .models import TokenState
from .schemas import ConflictOut, TokenStateOut, TransferRequest


def get_db_factory(database: Database):
    def get_db() -> Session:
        db = database.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    return get_db


def create_app(database: Database) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动即建表并在空库时初始化 (CONSTRUCTION, 0)。
        database.init_db()
        yield

    app = FastAPI(title="区间占用令牌移交服务", version="1.0.0", lifespan=lifespan)

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

    @app.post(
        "/api/token/transfer",
        response_model=TokenStateOut,
        responses={409: {"model": ConflictOut}},
    )
    def transfer(body: TransferRequest, db: Session = Depends(get_db)):
        # 第一个 SQL 触发 begin 事件 → BEGIN IMMEDIATE，整个函数体在一个
        # 数据库事务中；commit 正常返回，任何异常回滚，状态不被改写。
        try:
            snapshot = crud.transfer_token(
                db,
                expected_version=body.expected_version,
                target_holder=body.target_holder,
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

        db.commit()
        return TokenStateOut(holder=snapshot.holder, version=snapshot.version)

    return app
