"""移交记录（handover record）验收测试：

1. 原契约兼容：不带说明的请求体与响应体不变，带说明（≤200 字）同样成功；
2. 原子一致：记录与令牌更新在同一事务，记录数恒等于成功移交次数，
   记录版本与令牌提交后版本一一对应；
3. 失败不留痕：409 / 422 / 说明超长 / 并发失败者都不产生记录；
4. 记录按版本倒序、最多返回 10 条，且不可修改、不可删除；
5. 旧库（无记录表）启动时自动建表，令牌状态不被重置。

所有测试打真实 HTTP（uvicorn 子进程），不是进程内 TestClient。
"""

import concurrent.futures
import sqlite3
from datetime import datetime

import httpx
import pytest

from conftest import ApiServer, _free_port

OTHER = {"CONSTRUCTION": "TRAFFIC", "TRAFFIC": "CONSTRUCTION"}


def get_state(client: httpx.Client, base_url: str) -> dict:
    resp = client.get(f"{base_url}/api/token")
    assert resp.status_code == 200
    return resp.json()


def get_records(client: httpx.Client, base_url: str) -> list[dict]:
    resp = client.get(f"{base_url}/api/token/handovers")
    assert resp.status_code == 200
    return resp.json()


def transfer(client: httpx.Client, base_url: str, payload: dict) -> httpx.Response:
    return client.post(f"{base_url}/api/token/transfer", json=payload)


# ---------------------------------------------------------------------------
# 原契约兼容：带说明 / 不带说明
# ---------------------------------------------------------------------------

def test_transfer_without_note_keeps_original_contract(api_server):
    """不带 handover_note 的请求体、响应体与原契约完全一致。"""
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        resp = transfer(
            client,
            api_server.base_url,
            {"expected_version": 0, "target_holder": "TRAFFIC"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"holder": "TRAFFIC", "version": 1}

        # 成功移交仍写入一条记录，说明为空。
        records = get_records(client, api_server.base_url)
        assert len(records) == 1
        record = records[0]
        assert record["from_holder"] == "CONSTRUCTION"
        assert record["to_holder"] == "TRAFFIC"
        assert record["version"] == 1
        assert record["note"] is None
        # 服务端记录时间是合法时间戳。
        assert datetime.fromisoformat(record["created_at"]).tzinfo is not None


def test_transfer_with_note_roundtrip(api_server):
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        resp = transfer(
            client,
            api_server.base_url,
            {
                "expected_version": 0,
                "target_holder": "TRAFFIC",
                "handover_note": "施工结束，区间空闲",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"holder": "TRAFFIC", "version": 1}

        records = get_records(client, api_server.base_url)
        assert len(records) == 1
        assert records[0]["note"] == "施工结束，区间空闲"


def test_note_length_boundary(api_server):
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        ok = transfer(
            client,
            api_server.base_url,
            {
                "expected_version": 0,
                "target_holder": "TRAFFIC",
                "handover_note": "x" * 200,
            },
        )
        assert ok.status_code == 200

        too_long = transfer(
            client,
            api_server.base_url,
            {
                "expected_version": 1,
                "target_holder": "CONSTRUCTION",
                "handover_note": "x" * 201,
            },
        )
        assert too_long.status_code == 422

        # 超长说明整单失败：状态不变，也不新增记录。
        assert get_state(client, api_server.base_url) == {
            "holder": "TRAFFIC",
            "version": 1,
        }
        records = get_records(client, api_server.base_url)
        assert len(records) == 1
        assert records[0]["note"] == "x" * 200


# ---------------------------------------------------------------------------
# 原子一致：记录数 == 成功移交次数，版本一一对应
# ---------------------------------------------------------------------------

def test_records_match_successful_transfers_atomically(api_server):
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        url = api_server.base_url
        # 成功 v0→v1（带说明）。
        r1 = transfer(
            client,
            url,
            {
                "expected_version": 0,
                "target_holder": "TRAFFIC",
                "handover_note": "第一班交接",
            },
        )
        assert r1.status_code == 200

        # 失败：旧版本 409、交给自己 422，都不应产生记录。
        stale = transfer(
            client, url, {"expected_version": 0, "target_holder": "CONSTRUCTION"}
        )
        assert stale.status_code == 409
        same_side = transfer(
            client, url, {"expected_version": 1, "target_holder": "TRAFFIC"}
        )
        assert same_side.status_code == 422

        # 成功 v1→v2（不带说明）。
        r2 = transfer(
            client, url, {"expected_version": 1, "target_holder": "CONSTRUCTION"}
        )
        assert r2.status_code == 200

        state = get_state(client, url)
        assert state == {"holder": "CONSTRUCTION", "version": 2}

        records = get_records(client, url)
        # 恰好两条记录，与两次成功移交一一对应，按版本倒序。
        assert [r["version"] for r in records] == [2, 1]
        assert records[0]["from_holder"] == "TRAFFIC"
        assert records[0]["to_holder"] == "CONSTRUCTION"
        assert records[0]["note"] is None
        assert records[1]["from_holder"] == "CONSTRUCTION"
        assert records[1]["to_holder"] == "TRAFFIC"
        assert records[1]["note"] == "第一班交接"
        # 记录版本与令牌当前版本衔接：最新一条即当前版本。
        assert records[0]["version"] == state["version"]


def test_failed_requests_leave_no_record(api_server):
    """409 / 422 / 非法请求一律不产生记录。"""
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        url = api_server.base_url
        # 版本不匹配（当前版本 0，这里拿 1 提交）。
        assert (
            transfer(
                client,
                url,
                {
                    "expected_version": 1,
                    "target_holder": "TRAFFIC",
                    "handover_note": "不该留下",
                },
            ).status_code
            == 409
        )
        # 交给自己 / 非法持有人 / 缺字段 / 多余字段 / 说明超长。
        assert (
            transfer(
                client, url, {"expected_version": 0, "target_holder": "CONSTRUCTION"}
            ).status_code
            == 422
        )
        assert (
            transfer(
                client, url, {"expected_version": 0, "target_holder": "DISPATCH"}
            ).status_code
            == 422
        )
        assert transfer(client, url, {"target_holder": "TRAFFIC"}).status_code == 422
        assert (
            transfer(
                client,
                url,
                {
                    "expected_version": 0,
                    "target_holder": "TRAFFIC",
                    "handover_note": "y" * 201,
                },
            ).status_code
            == 422
        )

        assert get_state(client, url) == {"holder": "CONSTRUCTION", "version": 0}
        assert get_records(client, url) == []


def test_concurrent_losers_leave_no_record(api_server):
    """8 路同版本并发：恰好一次成功，记录也恰好一条。"""
    url = api_server.base_url

    def fire(note):
        with httpx.Client(base_url=url, timeout=30) as client:
            return client.post(
                f"{url}/api/token/transfer",
                json={
                    "expected_version": 0,
                    "target_holder": "TRAFFIC",
                    "handover_note": note,
                },
            ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(fire, [f"并发-{i}" for i in range(8)]))

    assert statuses.count(200) == 1
    assert statuses.count(409) == 7

    with httpx.Client(base_url=url, timeout=10) as client:
        assert get_state(client, url) == {"holder": "TRAFFIC", "version": 1}
        records = get_records(client, url)
        assert len(records) == 1
        assert records[0]["version"] == 1
        assert records[0]["note"] in {f"并发-{i}" for i in range(8)}


# ---------------------------------------------------------------------------
# 读取口径：版本倒序、最多 10 条
# ---------------------------------------------------------------------------

def test_records_descending_and_limited_to_10(api_server):
    url = api_server.base_url
    with httpx.Client(base_url=url, timeout=30) as client:
        holder = "CONSTRUCTION"
        for expected in range(12):
            resp = transfer(
                client,
                url,
                {
                    "expected_version": expected,
                    "target_holder": OTHER[holder],
                    "handover_note": f"第 {expected + 1} 次移交",
                },
            )
            assert resp.status_code == 200
            holder = OTHER[holder]

        records = get_records(client, url)
        assert len(records) == 10
        assert [r["version"] for r in records] == list(range(12, 2, -1))
        assert records[0]["note"] == "第 12 次移交"
        assert records[-1]["note"] == "第 3 次移交"


# ---------------------------------------------------------------------------
# 不可修改：数据库触发器兜底
# ---------------------------------------------------------------------------

def test_records_are_immutable(api_server):
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        resp = transfer(
            client,
            api_server.base_url,
            {
                "expected_version": 0,
                "target_holder": "TRAFFIC",
                "handover_note": "原始说明",
            },
        )
        assert resp.status_code == 200

    # 绕过应用直接连库：UPDATE / DELETE 都被触发器拒绝。
    conn = sqlite3.connect(api_server.db_path, timeout=10)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE handover_record SET note = '篡改'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM handover_record")
        # 记录原样保留。
        row = conn.execute(
            "SELECT from_holder, to_holder, version, note FROM handover_record"
        ).fetchone()
        assert row == ("CONSTRUCTION", "TRAFFIC", 1, "原始说明")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 旧库升级：自动建记录表，不重置令牌
# ---------------------------------------------------------------------------

def test_old_database_upgrades_without_resetting_token(fresh_db_path):
    """只有 token_state 表的旧库：启动后自动补建记录表，令牌原样保留。"""
    conn = sqlite3.connect(fresh_db_path)
    try:
        conn.execute(
            """
            CREATE TABLE token_state (
                id INTEGER NOT NULL PRIMARY KEY,
                holder VARCHAR(16) NOT NULL,
                version INTEGER NOT NULL,
                CONSTRAINT ck_token_state_holder
                    CHECK (holder IN ('CONSTRUCTION', 'TRAFFIC'))
            )
            """
        )
        conn.execute(
            "INSERT INTO token_state (id, holder, version) VALUES (1, 'TRAFFIC', 3)"
        )
        conn.commit()
    finally:
        conn.close()

    server = ApiServer(fresh_db_path, _free_port()).start()
    try:
        with httpx.Client(base_url=server.base_url, timeout=10) as client:
            # 令牌状态没有被重置。
            assert get_state(client, server.base_url) == {
                "holder": "TRAFFIC",
                "version": 3,
            }
            # 记录表已自动建好，初始为空。
            assert get_records(client, server.base_url) == []

            # 升级后的库上移交正常，记录从既有版本继续。
            resp = transfer(
                client,
                server.base_url,
                {
                    "expected_version": 3,
                    "target_holder": "CONSTRUCTION",
                    "handover_note": "升级后首次移交",
                },
            )
            assert resp.status_code == 200
            assert resp.json() == {"holder": "CONSTRUCTION", "version": 4}

            records = get_records(client, server.base_url)
            assert len(records) == 1
            assert records[0]["from_holder"] == "TRAFFIC"
            assert records[0]["to_holder"] == "CONSTRUCTION"
            assert records[0]["version"] == 4
            assert records[0]["note"] == "升级后首次移交"
    finally:
        server.stop()

    # 再次重启（模拟容器重启挂回同一卷）：状态与记录都保留。
    restarted = ApiServer(fresh_db_path, _free_port()).start()
    try:
        with httpx.Client(base_url=restarted.base_url, timeout=10) as client:
            assert get_state(client, restarted.base_url) == {
                "holder": "CONSTRUCTION",
                "version": 4,
            }
            assert len(get_records(client, restarted.base_url)) == 1
    finally:
        restarted.stop()
