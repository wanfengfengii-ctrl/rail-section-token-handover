# 区间占用令牌移交（施工台 ⇄ 行车台）

山区铁路施工结束后，调度员需要把区间占用令牌从**施工台（CONSTRUCTION）**
移交回**行车台（TRAFFIC）**。系统保证：任何时刻令牌只有一个持有人；
两个浏览器同时操作、或旧页面在服务重启后继续提交，都不会出现双方同时持有。

- 后端：FastAPI + SQLAlchemy 2 + SQLite（单文件持久化）
- 前端：React 18 + Vite
- 测试：pytest（初始化 / 事务竞争 / 重启持久化，打真实 HTTP）、Vitest（页面冲突反馈）
- 部署：Docker Compose，仅 `web` + `api` 两个常驻服务，另附 `verify` 一次性验收服务

---

## 快速开始（Docker Compose）

```bash
# 构建并启动 web + api（仅这两个常驻服务）
docker compose up --build -d

# 打开页面
#   http://localhost:8080
# 直接访问 API
#   http://localhost:8000/api/token
```

覆盖宿主端口（`WEB_PORT` / `API_PORT`）：

```bash
WEB_PORT=9090 API_PORT=9000 docker compose up --build
#   页面 http://localhost:9090   API http://localhost:9000
```

一次性验收（健康、初始化、422、409、并发唯一成功、数据卷落盘核对）：

```bash
docker compose --profile verify run --rm verify

# 对全新数据卷做验收（初始化断言要求空库）：
docker compose down -v
docker compose --profile verify run --rm verify
```

`verify` 服务带 compose profile，普通 `docker compose up` **不会**启动它；
它运行结束即退出，退出码 0 表示全部通过。

停止与清理：

```bash
docker compose down        # 保留数据卷 token-data
docker compose down -v     # 连数据卷一起删除（下次启动重新初始化为 CONSTRUCTION/0）
```

---

## 接口说明

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/token` | 读取当前持有人与版本号 |
| `POST` | `/api/token/transfer` | 移交令牌 |

### `GET /api/token`

```json
{ "holder": "CONSTRUCTION", "version": 0 }
```

`holder` 只可能是 `CONSTRUCTION`（施工台）或 `TRAFFIC`（行车台）。

### `POST /api/token/transfer`

请求体：

```json
{ "expected_version": 0, "target_holder": "TRAFFIC" }
```

- `expected_version`（必填，非负整数）：发起方看到的版本号；
- `target_holder`（必填）：目标持有人，只能是 `CONSTRUCTION` / `TRAFFIC`，
  且必须与当前持有人**相反**（施工台 ⇄ 行车台，不能交给自己）。

成功（200）—— 持有人翻转，版本号**恰好加一**：

```json
{ "holder": "TRAFFIC", "version": 1 }
```

版本不匹配（409）—— 响应体携带服务器最新状态：

```json
{
  "detail": "状态已变化，请重新确认",
  "current": { "holder": "TRAFFIC", "version": 1 }
}
```

请求不合法（422）：字段缺失、字段多余、类型/范围非法、`target_holder`
不是两个合法值之一，或目标与当前持有人相同。**所有失败都不会改写状态。**

### 前端在 409 时的行为

1. 取消本次移交意图（不自动重试）；
2. 重新 `GET /api/token` 读取服务器最新状态；
3. 提示 **“状态已变化，请重新确认”**，由调度员重新确认后再操作。

页面每 5 秒静默刷新一次，让另一浏览器的移交结果可见；移交进行中暂停刷新。

---

## 并发口径（为什么不可能双方都持有）

令牌在 SQLite 中只有一行（固定主键 `id = 1`），移交在**单个数据库事务**内完成：

1. 事务以 **`BEGIN IMMEDIATE`** 开启，进入即拿 SQLite 的 RESERVED 写锁，
   并发移交请求被数据库强制串行化（第二个写者在锁上等待，
   `busy_timeout=5000`），而不是各自读到旧快照后一起写；
2. 事务内读取当前唯一状态行，校验 `expected_version` 与当前版本一致；
3. 校验目标持有人必须是当前持有人的对立方；
4. 执行条件更新：

   ```sql
   UPDATE token_state
      SET holder = :target, version = version + 1
    WHERE id = 1 AND version = :expected_version;
   ```

   以条件更新命中行数（必须为 1）作为第二道保险；
5. 提交。任一步失败即回滚，状态保持原样。

因此，**同一版本的两次移交（两个浏览器、或重启后旧页面继续提交）只有一次
能成功**：先拿到写锁的事务把版本推进一格，后者的版本校验/条件更新必然
落空并返回 409。版本号单调递增，持有人只在两个合法值之间翻转，
数据库层另有 `CHECK (holder IN ('CONSTRUCTION','TRAFFIC'))` 约束兜底。

### 初始化与重启持久化

- 服务启动时建表；表为空才插入 `(CONSTRUCTION, 0)`，重复启动幂等，
  不会把已移交的状态重置；
- SQLite 文件位于独立数据卷（容器内 `/data/token.db`，WAL 模式），
  容器重启/重建后重新挂载同一卷，唯一持有人与已提交版本原样保留。

按需求边界，系统**不**提供审批、撤销、超时回收或其他处置流程。

---

## 本地开发与测试

### 后端

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

TOKEN_DATABASE_URL="sqlite:///./token.db" \
  uvicorn app.asgi:app --reload --port 8000

pytest            # 9 项：初始化 / 422 / 409 / 8 路并发 / 杀进程重启
```

pytest 用真实 `uvicorn` 子进程 + HTTP 请求验证；“重启”测试会终止子进程、
用**同一个数据库文件**启动新进程，等价于容器重启挂回同一数据卷。

### 前端

```bash
cd frontend
npm install
npm run dev       # http://localhost:5173，/api 自动代理到 :8000
npm test          # Vitest：渲染、成功移交、409 冲突反馈、双浏览器场景
npm run build
```

### 一次性验收服务

`verify/verify.py` 仅用 Python 标准库，对运行中的 compose 栈执行
端到端检查（含 8 路并发同版本移交、直接读取共享卷上的 SQLite 文件
核对唯一行），通过后以退出码 0 结束。

## 目录结构

```
.
├── backend/            FastAPI + SQLAlchemy + SQLite
│   ├── app/            config / database / models / crud / schemas / main / asgi
│   ├── tests/          pytest（真实 HTTP、子进程重启）
│   └── Dockerfile
├── frontend/           React + Vite + Vitest，nginx 托管并反代 /api
│   ├── src/            App.jsx / api.js / App.test.jsx
│   └── Dockerfile
├── verify/             一次性验收容器（compose profile: verify）
└── docker-compose.yml  web + api（+ verify profile），token-data 命名卷
```
