"""数据库引擎与访问对象。

核心并发口径：
* SQLite 驱动被置为不自开事务，事务起点由 SQLAlchemy 的 ``begin`` 事件
  统一发出 ``BEGIN IMMEDIATE``，写事务在第一时间拿到 RESERVED 锁，
  两个并发移交会被数据库强制串行化；
* 移交的“读当前状态 → 校验版本/方向 → 条件更新”全部发生在这一个
  IMMEDIATE 事务内，配合 ``WHERE version = :expected`` 的条件更新，
  同一版本的两次并发移交最多一次成功；
* 纯读取走 AUTOCOMMIT 单语句，不持写锁。
"""

import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .models import Base, TokenState


def _ensure_sqlite_parent(url: str) -> None:
    """sqlite:////data/token.db → 确保 /data 目录存在。"""

    prefix = "sqlite:////"
    if url.startswith(prefix):
        path = url[len(prefix) - 1 :]
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    elif url.startswith("sqlite:///"):
        path = url[len("sqlite:///") :]
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)


class Database:
    """持有引擎与会话工厂；测试可对临时文件新建独立实例（模拟重启）。"""

    def __init__(self, url: str) -> None:
        self.url = url
        is_sqlite = url.startswith("sqlite")
        if is_sqlite:
            _ensure_sqlite_parent(url)

        self.engine: Engine = create_engine(
            url,
            connect_args={"check_same_thread": False} if is_sqlite else {},
            future=True,
        )

        if is_sqlite:
            self._install_sqlite_guards(self.engine)

        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, future=True
        )

    @staticmethod
    def _install_sqlite_guards(engine: Engine) -> None:
        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_connection, _connection_record):
            # 关闭 pysqlite 自动 BEGIN，完全交给 SQLAlchemy 控制事务边界。
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            # 锁竞争时等待而不是立刻报 database is locked。
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _on_begin(conn):
            # 每个事务一启动就申请写锁，把并发写者挡在事务入口。
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    def init_db(self) -> None:
        """建表；表为空时插入唯一初始行 (CONSTRUCTION, 0)。

        重复调用（含两个进程同时首启）是幂等的：已有行不会被重置。
        """

        Base.metadata.create_all(bind=self.engine)
        session = self.SessionLocal()
        try:
            if session.get(TokenState, 1) is None:
                session.add(
                    TokenState(id=1, holder="CONSTRUCTION", version=0)
                )
                try:
                    session.commit()
                except Exception:
                    # 并发首启时另一个进程可能已插入（主键冲突），放弃即可。
                    session.rollback()
        finally:
            session.close()
