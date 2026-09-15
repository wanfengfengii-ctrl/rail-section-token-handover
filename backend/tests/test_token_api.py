"""验收测试：

1. 首次启动空库 → CONSTRUCTION / version 0，且重启不会重置；
2. 正常移交：版本恰好加一、持有人翻转；
3. 409：版本不匹配，且失败不改写状态；
4. 422：字段缺失 / 目标非法（非合法值、或与当前持有人相同），不改写状态；
5. 事务竞争：同一版本的多次并发移交恰好一次成功，绝无双持；
6. 重启持久化：移交后杀进程再启动，持有人与版本保留。

所有测试打真实 HTTP（uvicorn 子进程），不是进程内 TestClient。
"""

import concurrent.futures

import httpx

from conftest import ApiServer, _free_port


def get_state(client: httpx.Client, base_url: str) -> dict:
    resp = client.get(f"{base_url}/api/token")
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def test_first_boot_initializes_construction_v0(api_server):
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        state = get_state(client, api_server.base_url)
    assert state == {"holder": "CONSTRUCTION", "version": 0}


def test_init_row_is_unique_and_not_reinitialized(api_server):
    """再次 init_db / 重启都不能把已移交的状态重置回 (CONSTRUCTION, 0)。"""
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        resp = client.post(
            f"{api_server.base_url}/api/token/transfer",
            json={"expected_version": 0, "target_holder": "TRAFFIC"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"holder": "TRAFFIC", "version": 1}

        restarted = api_server.restart()
        state = get_state(client, restarted.base_url)
        assert state == {"holder": "TRAFFIC", "version": 1}


# ---------------------------------------------------------------------------
# 正常移交
# ---------------------------------------------------------------------------

def test_transfer_flips_holder_and_increments_by_one(api_server):
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        r1 = client.post(
            f"{api_server.base_url}/api/token/transfer",
            json={"expected_version": 0, "target_holder": "TRAFFIC"},
        )
        assert r1.status_code == 200
        assert r1.json() == {"holder": "TRAFFIC", "version": 1}

        r2 = client.post(
            f"{api_server.base_url}/api/token/transfer",
            json={"expected_version": 1, "target_holder": "CONSTRUCTION"},
        )
        assert r2.status_code == 200
        assert r2.json() == {"holder": "CONSTRUCTION", "version": 2}

        assert get_state(client, api_server.base_url) == {
            "holder": "CONSTRUCTION",
            "version": 2,
        }


# ---------------------------------------------------------------------------
# 409 版本冲突
# ---------------------------------------------------------------------------

def test_stale_version_returns_409_and_does_not_mutate(api_server):
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        # 先让版本前进到 1。
        ok = client.post(
            f"{api_server.base_url}/api/token/transfer",
            json={"expected_version": 0, "target_holder": "TRAFFIC"},
        )
        assert ok.status_code == 200

        # 旧页面拿着 version 0 继续提交。
        stale = client.post(
            f"{api_server.base_url}/api/token/transfer",
            json={"expected_version": 0, "target_holder": "CONSTRUCTION"},
        )
        assert stale.status_code == 409
        body = stale.json()
        # 冲突响应携带服务器最新状态，供页面放弃本次意图并刷新。
        assert body["current"] == {"holder": "TRAFFIC", "version": 1}

        # 状态没有被这次失败请求改写。
        assert get_state(client, api_server.base_url) == {
            "holder": "TRAFFIC",
            "version": 1,
        }


# ---------------------------------------------------------------------------
# 422 字段/目标非法
# ---------------------------------------------------------------------------

def test_missing_fields_return_422(api_server):
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        for payload in (
            {},
            {"target_holder": "TRAFFIC"},
            {"expected_version": 0},
            {"expected_version": 0, "target_holder": "TRAFFIC", "extra": 1},
        ):
            resp = client.post(
                f"{api_server.base_url}/api/token/transfer", json=payload
            )
            assert resp.status_code == 422, payload

        # Pydantic 类型/范围非法也 422。
        resp = client.post(
            f"{api_server.base_url}/api/token/transfer",
            json={"expected_version": -1, "target_holder": "TRAFFIC"},
        )
        assert resp.status_code == 422

        assert get_state(client, api_server.base_url) == {
            "holder": "CONSTRUCTION",
            "version": 0,
        }


def test_illegal_target_returns_422_without_mutation(api_server):
    with httpx.Client(base_url=api_server.base_url, timeout=10) as client:
        # 不得弱类型强转：bool/字符串/浮点版本号一律 422，状态不变。
        for raw_version in (False, True, "0", "1", 0.0, 1.0, 1.5):
            resp = client.post(
                f"{api_server.base_url}/api/token/transfer",
                json={"expected_version": raw_version, "target_holder": "TRAFFIC"},
            )
            assert resp.status_code == 422, raw_version

        # null 版本号同样拒绝。
        resp = client.post(
            f"{api_server.base_url}/api/token/transfer",
            json={"expected_version": None, "target_holder": "TRAFFIC"},
        )
        assert resp.status_code == 422

        # 不在两个合法持有人之内。
        bad_value = client.post(
            f"{api_server.base_url}/api/token/transfer",
            json={"expected_version": 0, "target_holder": "DISPATCH"},
        )
        assert bad_value.status_code == 422

        # 值合法，但与当前持有人相同（不是“相反的一方”）。
        same_side = client.post(
            f"{api_server.base_url}/api/token/transfer",
            json={"expected_version": 0, "target_holder": "CONSTRUCTION"},
        )
        assert same_side.status_code == 422

        assert get_state(client, api_server.base_url) == {
            "holder": "CONSTRUCTION",
            "version": 0,
        }


# ---------------------------------------------------------------------------
# 并发竞争：同一版本只有一次成功
# ---------------------------------------------------------------------------

def test_concurrent_same_version_transfers_exactly_one_wins(api_server):
    url = api_server.base_url

    def fire():
        # 每个线程独立连接，都拿着初始 version 0 请求移交给 TRAFFIC。
        with httpx.Client(base_url=url, timeout=30) as client:
            return client.post(
                f"{url}/api/token/transfer",
                json={"expected_version": 0, "target_holder": "TRAFFIC"},
            ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: fire(), range(8)))

    assert statuses.count(200) == 1
    assert statuses.count(409) == 7

    with httpx.Client(base_url=url, timeout=10) as client:
        # 版本只前进一格，持有人唯一，不存在“双方都持有”。
        assert get_state(client, url) == {"holder": "TRAFFIC", "version": 1}


def test_two_browsers_handshake_never_both_hold(api_server):
    """模拟两个浏览器反复互交：每次成功都严格基于最新版本。"""
    url = api_server.base_url
    with httpx.Client(base_url=url, timeout=10) as client:
        current_version = 0
        current_holder = "CONSTRUCTION"
        other = {"CONSTRUCTION": "TRAFFIC", "TRAFFIC": "CONSTRUCTION"}

        for _ in range(5):
            resp = client.post(
                f"{url}/api/token/transfer",
                json={
                    "expected_version": current_version,
                    "target_holder": other[current_holder],
                },
            )
            assert resp.status_code == 200
            current_version += 1
            current_holder = other[current_holder]
            assert get_state(client, url) == {
                "holder": current_holder,
                "version": current_version,
            }


# ---------------------------------------------------------------------------
# 重启持久化
# ---------------------------------------------------------------------------

def test_state_persists_across_restart(fresh_db_path):
    server = ApiServer(fresh_db_path, _free_port()).start()
    try:
        with httpx.Client(base_url=server.base_url, timeout=10) as client:
            assert get_state(client, server.base_url) == {
                "holder": "CONSTRUCTION",
                "version": 0,
            }
            for target, expected_v in (
                ("TRAFFIC", 1),
                ("CONSTRUCTION", 2),
                ("TRAFFIC", 3),
            ):
                r = client.post(
                    f"{server.base_url}/api/token/transfer",
                    json={
                        "expected_version": expected_v - 1,
                        "target_holder": target,
                    },
                )
                assert r.status_code == 200
    finally:
        server.stop()

    # 容器重启：新进程、同一数据库文件。
    restarted = ApiServer(fresh_db_path, _free_port()).start()
    try:
        with httpx.Client(base_url=restarted.base_url, timeout=10) as client:
            assert get_state(client, restarted.base_url) == {
                "holder": "TRAFFIC",
                "version": 3,
            }
            # 重启后旧版本号提交仍然被判定为冲突。
            stale = client.post(
                f"{restarted.base_url}/api/token/transfer",
                json={"expected_version": 0, "target_holder": "CONSTRUCTION"},
            )
            assert stale.status_code == 409
            assert stale.json()["current"] == {
                "holder": "TRAFFIC",
                "version": 3,
            }
            # 用持久化下来的最新版本仍可正常移交。
            ok = client.post(
                f"{restarted.base_url}/api/token/transfer",
                json={"expected_version": 3, "target_holder": "CONSTRUCTION"},
            )
            assert ok.status_code == 200
            assert ok.json() == {"holder": "CONSTRUCTION", "version": 4}
    finally:
        restarted.stop()
