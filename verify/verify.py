"""一次性验收脚本（仅使用 Python 标准库）。

对已启动的 API 容器做端到端验收：

  1. 健康检查与初始状态（全新卷上必须是 CONSTRUCTION / 0）；
  2. 422：缺字段、非法持有人、交给自己，且均不改写状态；
  3. 409：版本不匹配返回最新状态；
  4. 正常移交：持有人翻转、版本恰好加一；
  5. 并发：8 个同版本请求恰好一个 200、其余 409，最终唯一持有人；
  6. 持久化：直接读取共享卷上的 SQLite 文件，核对库里只有一行且与 API 一致
     —— 该行落在独立数据卷上，API 容器重启后仍会读到它。

用法：
    docker compose --profile verify run --rm verify
"""

import concurrent.futures
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

API_URL = os.getenv("API_URL", "http://api:8000").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "/data/token.db")
OTHER = {"CONSTRUCTION": "TRAFFIC", "TRAFFIC": "CONSTRUCTION"}

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def request(method: str, path: str, payload=None) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{API_URL}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def raw_transfer(raw_body: str) -> tuple[int, dict]:
    """发送原始 JSON 文本，精确控制 expected_version 的 JSON 类型。"""
    req = urllib.request.Request(
        f"{API_URL}/api/token/transfer",
        data=raw_body.encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def wait_for_api() -> None:
    for _ in range(60):
        try:
            status, _ = request("GET", "/health")
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("API 未在 60 秒内就绪")


def transfer(payload):
    return request("POST", "/api/token/transfer", payload)


def main() -> int:
    print(f"验收目标: {API_URL}")
    wait_for_api()

    # 1. 初始/当前状态 -----------------------------------------------------
    status, state = request("GET", "/api/token")
    check("GET /api/token 返回 200", status == 200, str(state))
    check(
        "持有人合法",
        state.get("holder") in OTHER,
        str(state),
    )
    check("版本号为非负整数", isinstance(state.get("version"), int) and state["version"] >= 0)

    if state == {"holder": "CONSTRUCTION", "version": 0}:
        check("全新数据卷初始化为 CONSTRUCTION / 0", True)
    else:
        print(f"[INFO] 数据卷已有历史状态: {state}（跳过初始值断言）")

    holder, version = state["holder"], state["version"]

    # 2. 422 ---------------------------------------------------------------
    for label, payload in (
        ("空请求体", {}),
        ("缺少 expected_version", {"target_holder": OTHER[holder]}),
        ("缺少 target_holder", {"expected_version": version}),
        ("持有人非法值", {"expected_version": version, "target_holder": "DISPATCH"}),
        ("交给自己", {"expected_version": version, "target_holder": holder}),
        ("版本号为负", {"expected_version": -1, "target_holder": OTHER[holder]}),
    ):
        code, _ = transfer(payload)
        check(f"422: {label}", code == 422, f"got {code}")

    # 弱类型版本号：false / 字符串 / 浮点数不得被强转为版本号而成功转移。
    for label, raw_version in (
        ("false", "false"),
        ("true", "true"),
        ("字符串 \"0\"", '"0"'),
        ("浮点 0.0", "0.0"),
        ("浮点 1.5", "1.5"),
        ("null", "null"),
    ):
        body = (
            f'{{"expected_version": {raw_version}, '
            f'"target_holder": "{OTHER[holder]}"}}'
        )
        code, _ = raw_transfer(body)
        check(f"422: 弱类型版本号 {label}", code == 422, f"got {code}")

    _, unchanged = request("GET", "/api/token")
    check("422 失败请求均未改写状态", unchanged == {"holder": holder, "version": version})

    # 3. 409 版本不匹配 -----------------------------------------------------
    code, body = transfer(
        {"expected_version": version + 1, "target_holder": OTHER[holder]}
    )
    check("未来版本号移交返回 409", code == 409, str(body))
    check("409 响应携带服务器最新状态", body.get("current") == {
        "holder": holder, "version": version
    })

    # 4. 正常移交 -----------------------------------------------------------
    target = OTHER[holder]
    code, moved = transfer(
        {"expected_version": version, "target_holder": target}
    )
    check("正常移交返回 200", code == 200, str(moved))
    check("移交后持有人翻转", moved.get("holder") == target)
    check("移交后版本恰好加一", moved.get("version") == version + 1)

    code, body = transfer(
        {"expected_version": version, "target_holder": target}
    )
    check("旧版本再次移交返回 409", code == 409)

    # 5. 同版本并发移交 -----------------------------------------------------
    stale_version = version + 1  # 当前版本
    concurrent_target = OTHER[target]

    def fire(_):
        return transfer(
            {"expected_version": stale_version, "target_holder": concurrent_target}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fire, range(8)))
    codes = [code for code, _ in results]
    check("8 个并发同版本请求恰好一次成功", codes.count(200) == 1, str(codes))
    check("其余 7 个请求全部 409", codes.count(409) == 7, str(codes))

    _, final_state = request("GET", "/api/token")
    expected_final = {"holder": concurrent_target, "version": version + 2}
    check("并发后唯一持有人且版本只前进一格", final_state == expected_final, str(final_state))

    # 6. 数据卷持久化核对 ---------------------------------------------------
    # 以普通连接打开（WAL 库需要能访问 -shm/-wal 才能读到最新已提交数据）。
    # 此处只做 SELECT；真正的“杀进程后重启”持久化由后端 pytest
    # test_state_persists_across_restart 覆盖。
    if os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            rows = conn.execute(
                "SELECT id, holder, version FROM token_state"
            ).fetchall()
        finally:
            conn.close()
        check("数据库中令牌行唯一", len(rows) == 1, str(rows))
        check(
            "数据库落盘状态与 API 一致（重启后保留）",
            rows == [(1, final_state["holder"], final_state["version"])],
            str(rows),
        )
    else:
        check(f"数据卷上存在数据库文件 {DB_PATH}", False)

    print("-" * 60)
    if failures:
        print(f"验收失败：{len(failures)} 项 -> {failures}")
        return 1
    print("全部验收通过：初始化 / 422 / 409 / 并发唯一成功 / 卷持久化")
    return 0


if __name__ == "__main__":
    sys.exit(main())
