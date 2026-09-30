# 双人见证资料封存服务

本地账号、密钥操作的资料封存/开封服务。封存后正文不可修改、不可（在开封前）被读取；
开封必须由封存人发起请求（理由 + 截止时刻，可选**最早开封时刻**），并在截止前取得
**两名不同指定见证人**分别同意；同意必须绑定本次请求与封存时的摘要。
技术栈：Python 3.11 + FastAPI + SQLite。

## 安全与业务规则

| 规则 | 实现方式 |
| --- | --- |
| 本地账号，凭密钥操作 | `POST /register` 返回一次性 API Key，请求头 `X-API-Key` 携带；服务端只存 SHA-256 哈希 |
| 封存需三名不同见证人 | 封存时校验三人互异、均已注册、且不含封存人本人 |
| 保存正文及摘要 | 正文原文与 SHA-256 摘要同表保存；封存后无任何更新接口，内容不可改 |
| 开封前不泄露正文 | 封存/待开封状态下查询/列表/错误响应均不含 `body`；非参与方一律 `403`，不区分资料是否存在 |
| 开封请求 | 封存人提交 `reason` 与带时区的 `deadline`（必须晚于当前时间） |
| 最早开封时刻（可选） | `earliest_open_at`：带时区，不早于提交时刻且严格早于 `deadline`；不填时行为不变（两票集齐即开封） |
| 两票同意 | 截止前两名**不同**见证人各同意一次才开封；`UNIQUE(request_id, witness_id)` 保证同一人重试只计一次 |
| 同意绑定摘要 | 同意时须提交封存摘要，服务端比对一致才记录，防止对被篡改内容背书 |
| 定时开封 | 填了 `earliest_open_at` 时，两票在该时刻前集齐只进入 `waiting`（待开封）；到点自动开封。到点时不足两票的，截止前补齐第二票时立即开封 |
| 到点必开封 | 到点前未撤销且已集齐两票的请求，自到点起只能开封：后台扫描器与每个写事务都先做确定性到点判定，即使后台处理延迟，之后的撤销或到期扫描也不能使其失效 |
| 撤销/到期作废 | 到点**之前**封存人可撤销；过截止时刻始终不足两票则置为 `revoked`/`expired`，其同意永久失效；新请求从 0 票开始 |
| 拒绝且状态不变 | 失效凭据(401)、非见证人(403)、无效时限(400/422)、重复开封(409) 等任何拒绝路径都不写数据（到点自动开封的提交除外：开封先落库，拒绝在提交后返回） |
| 并发只产生一个结果 | 全部“检查 + 写入”在 SQLite `BEGIN IMMEDIATE` 短事务内完成；并发同意/撤销/定时开封/到期只有一个已提交终态 |
| 操作留痕、重启一致 | 每次成功操作按自增序号追加 `audit_log`；WAL 持久化，重启时先跑一轮恢复扫描，状态、票数、审计顺序完全一致 |

### 请求状态机

```
pending  ──(两票集齐，未设最早开封时刻 或 已到最早开封时刻)──▶ opened
pending  ──(两票集齐但未到 earliest_open_at)───────────────▶ waiting
waiting  ──(到达 earliest_open_at；扫描器或任意写事务触发)──▶ opened（不可再撤销/过期）
pending  ──(封存人在截止前撤销)─────────────────────────────▶ revoked
pending  ──(过 deadline 仍不足两票)─────────────────────────▶ expired
```

后台扫描器（默认每秒一次）在一轮内先把到点的 `waiting` 请求确定性开封，
再把已到截止时刻仍 `pending` 的请求置为 `expired`；
所有涉时刻判定在写事务内再做一次，杜绝临界时间竞态。

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
curl http://127.0.0.1:8000/health
```

镜像构建时安装依赖；容器入口 `entrypoint.sh` 会幂等初始化库表
（`/data/seal.db`，位于 Docker 卷 `seal-data`，重启数据不丢）。

停止：`docker compose down`（加 `-v` 同时删除数据卷）。

## 本地启动（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SEAL_DB_PATH=./data/seal.db   # SQLite 文件路径（默认 /data/seal.db）
export PORT=8000                      # 监听端口（默认 8000）
export SWEEP_INTERVAL_SECONDS=1       # 到期扫描间隔（默认 1 秒）
python init_db.py                     # 可选：单独初始化库表（应用启动也会初始化）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- 服务地址：`http://127.0.0.1:8000`
- 交互式文档：`http://127.0.0.1:8000/docs`
- 健康检查：`GET /health`

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SEAL_DB_PATH` | `/data/seal.db` | SQLite 数据库文件路径（自动创建，WAL 模式） |
| `PORT` | `8000` | 监听端口（容器入口使用） |
| `SWEEP_INTERVAL_SECONDS` | `1` | 到期请求后台扫描间隔秒数 |
| `HOST_PORT` | `8000` | 仅 Compose 使用：宿主机映射端口 |

## HTTP API

所有接口除 `/register`、`/health` 外均需请求头 `X-API-Key: <注册时返回的密钥>`。
时间字段使用带时区 ISO-8601，例如 `2026-10-01T12:00:00Z` 或 `2026-10-01T20:00:00+08:00`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/register` | 注册，返回一次性 `api_key` |
| GET | `/me` | 查询当前密钥对应的账号 |
| POST | `/documents` | 封存资料：`{body, witnesses:[3人]}`，返回 `digest`（无正文） |
| GET | `/documents` | 列出本人封存/见证的资料（仅元数据） |
| GET | `/documents/{id}` | 详情；开封后才含 `body`、`reason`、`witness_approvals` |
| POST | `/documents/{id}/open-requests` | 封存人提交开封请求：`{reason, deadline, earliest_open_at?}` |
| POST | `/open-requests/{id}/approve` | 见证人同意：`{digest}`（须等于封存摘要）；第二票可能返回 `waiting` 或 `opened` |
| POST | `/open-requests/{id}/revoke` | 封存人撤销：`{reason?}` |
| GET | `/documents/{id}/audit` | 按顺序返回该资料的全部操作记录 |

状态码：成功 `200/201`；参数校验失败 `422`（如时间不带时区、`earliest_open_at`
不早于 `deadline`）；业务校验失败 `400`（如三人不互异、截止时刻已过、最早开封时刻
早于当前）；未认证 `401`；无权/不可见 `403`；冲突（重复同意、重复开封、
请求已撤销/到期、waiting 下再同意、已有进行中请求等）`409`。错误体统一为
`{"detail": "..."}`，不含任何正文内容。

### 最早开封时刻（earliest_open_at）

- 字段可选，带时区 ISO-8601；必须**不早于提交时刻**且**严格早于** `deadline`。
- 不填写时一切照旧：两票集齐立即开封，响应里 `earliest_open_at` 为 `null`。
- 填写后，两票在该时刻前集齐只把请求置为 `waiting`（待开封）：同意响应与
  `GET /documents/{id}` 都会返回 `status: "waiting"` 与 `earliest_open_at`，
  但仍不返回 `body` 与见证明细。
- 到达 `earliest_open_at` 后由后台扫描器（或任意并发写事务，二者等价且互斥）
  自动开封；若到点时只有一票，则保持 `pending`，截止前第二票到达的**同一请求**
  立即开封。
- 到点前可以撤销；一旦到点且两票已齐，撤销与到期扫描都不再生效。

```bash
# 提交带最早开封时刻的请求：最早 1 小时后、截止 24 小时后
EARLIEST=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat())")
DEADLINE=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=24)).isoformat())")
curl -sS -X POST $BASE/documents/1/open-requests \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"reason\":\"家庭会议\",\"deadline\":\"$DEADLINE\",\"earliest_open_at\":\"$EARLIEST\"}"
# -> {"request_id":1,"status":"pending",...,"earliest_open_at":"…"}

# 两票在时刻前集齐 -> waiting（而非 opened）
curl -sS -X POST $BASE/open-requests/1/approve -H "X-API-Key: $W1_KEY" \
  -H 'Content-Type: application/json' -d '{"digest":"ab12…"}'
# -> {"approval_count":1,"status":"pending",...}
curl -sS -X POST $BASE/open-requests/1/approve -H "X-API-Key: $W2_KEY" \
  -H 'Content-Type: application/json' -d '{"digest":"ab12…"}'
# -> {"approval_count":2,"status":"waiting","earliest_open_at":"…"}

# 待开封期间查询：有 earliest_open_at 与 waiting，仍无 body
curl -sS $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"
# 到达最早开封时刻后（扫描间隔内）再查 -> status=opened，可见 body
```

### 调用示例（curl）

```bash
BASE=http://127.0.0.1:8000

# 注册（api_key 只显示这一次，请保存）
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' \
  -d '{"username":"alice"}'
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' -d '{"username":"bob"}'
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' -d '{"username":"carol"}'
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' -d '{"username":"dave"}'

# 封存
curl -sS -X POST $BASE/documents \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d '{"body":"遗嘱正文……","witnesses":["bob","carol","dave"]}'
# -> {"document_id":1,"status":"sealed","digest":"ab12…","witnesses":[...]}

# 开封前查询（无 body）
curl -sS $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"

# 提交开封请求（截止时刻取 24 小时后）
DEADLINE=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=24)).isoformat())")
curl -sS -X POST $BASE/documents/1/open-requests \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"reason\":\"家庭会议\",\"deadline\":\"$DEADLINE\"}"
# -> {"request_id":1,"status":"pending",...}

# 两名见证人分别同意，digest 必须是封存响应里的摘要
curl -sS -X POST $BASE/open-requests/1/approve \
  -H "X-API-Key: $W1_KEY" -H 'Content-Type: application/json' \
  -d '{"digest":"ab12…"}'
# -> {"approval_count":1,"status":"pending"}
curl -sS -X POST $BASE/open-requests/1/approve \
  -H "X-API-Key: $W2_KEY" -H 'Content-Type: application/json' \
  -d '{"digest":"ab12…"}'
# -> {"approval_count":2,"status":"opened"}

# 开封后查询：body / digest / reason / witness_approvals 全部可见
curl -sS $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"

# 操作记录（按时间顺序）
curl -sS $BASE/documents/1/audit -H "X-API-Key: $OWNER_KEY"
```

可直接运行的端到端示例（服务启动后）：

```bash
bash examples/demo.sh                    # curl 版（未设最早开封时刻的基础流程）
python3 examples/demo.py                 # 纯标准库 Python 版
python3 examples/demo_scheduled.py       # 最早开封时刻：waiting -> 自动开封；补票立即开封
```

### 典型拒绝场景

```bash
# 错误密钥 -> 401，不产生任何记录
curl -sS -o /dev/null -w '%{http_code}\n' $BASE/me -H 'X-API-Key: wrong'

# 非见证人同意 -> 403；摘要不一致 -> 409；截止后同意 -> 409；
# 同一见证人再次同意 -> 409（只计一次）；开封后再提请求 -> 409（重复开封）
```

## 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试覆盖：注册与密钥、封存校验、两票开封全流程、重复同意、撤销后旧同意失效、
到期失效、最早开封时刻校验、`waiting` 待开封与到点自动开封、到点时票不足补齐
立即开封、迟到撤销/迟到扫描不能作废已到点开封、停机期间到点的重启恢复、
开封前正文零泄露、审计顺序，以及基于真实 uvicorn + 多线程的
**并发同意/同意撤销赛跑**（验证只产生一个已提交终态）和**重启一致性**。

## 项目结构

```
app/
  config.py     # 环境变量配置
  db.py         # SQLite 连接、建表与旧库幂等迁移、BEGIN IMMEDIATE 写事务、时间规范
  schemas.py    # 请求模型（含带时区 deadline / earliest_open_at 校验）
  main.py       # FastAPI 路由、认证、定时开封+到期扫描器、全部业务规则
init_db.py      # 幂等初始化入口
entrypoint.sh   # 容器入口（初始化 + uvicorn）
examples/       # demo.sh / demo.py / demo_scheduled.py 端到端调用示例
tests/          # pytest 测试（功能、并发、重启）
Dockerfile
docker-compose.yml
```
