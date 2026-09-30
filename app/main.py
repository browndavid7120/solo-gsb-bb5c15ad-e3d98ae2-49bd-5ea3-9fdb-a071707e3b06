"""双人见证资料封存服务。

关键规则：
- 本地账号注册后凭 API Key（X-API-Key）操作，服务端只存密钥哈希；
- 封存后正文不可改；开封前任何查询与错误响应都不返回正文；
- 开封请求需两名不同指定见证人在截止时刻前分别同意，同一人重试只计一次；
- 可指定带时区的最早开封时刻：不填则两票即开封；填写后两票在该时刻前集齐只进入
  scheduled（待开封），到点定时开封；到点时不足两票则保持 pending，截止前补齐第二票立即开封；
- 到点前未撤销且已集齐两票的请求自到点起无条件开封，后台延迟、事后撤销/到期扫描均不得使其失效；
- 同意必须绑定本次请求与封存时的摘要（客户端提交摘要，服务端比对）；
- 撤销或到期后旧同意永久失效，不能用于后续请求；
- 所有“检查 + 写入”都在 BEGIN IMMEDIATE 短事务内完成，并发同意/撤销只有一个已提交结果；
- 每次操作按顺序追加到 audit_log，SQLite 持久化，重启状态一致。
"""
import asyncio
import hashlib
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import settings
from .db import connect, init_db, now_utc, utc_str, write_tx
from .schemas import ApproveIn, OpenRequestIn, RegisterIn, RevokeIn, SealIn

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def add_audit(
    conn: sqlite3.Connection,
    *,
    actor_id: int | None,
    action: str,
    detail: str,
    document_id: int | None = None,
    request_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO audit_log (at, actor_id, action, document_id, request_id, detail)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (utc_str(now_utc()), actor_id, action, document_id, request_id, detail),
    )


# ---------------------------------------------------------------------------
# 到期/定时开封状态机
#
# 请求状态：
# - pending   ：进行中（未填写最早开封时刻，或已填写但尚未集齐两票）
# - scheduled ：待开封（已填最早开封时刻，两票已在该时刻前集齐，只等到点）
# - opened    ：已开封（终态）
# - revoked   ：已撤销（终态）
# - expired   ：已过期（终态）
#
# 确定性推进规则（now 时刻）：
# 1. scheduled 且 earliest_open_at <= now：无条件开封。即使已过截止时刻、
#    即使后台处理延迟，也必须开封——撤销与到期扫描都不得使其失效。
# 2. pending 且 deadline <= now：始终不足两票，置为 expired。
# 所有推进均发生在调用方持有的 BEGIN IMMEDIATE 事务内，保证并发只产生一个终态。
# ---------------------------------------------------------------------------


def _mark_opened(tx: sqlite3.Connection, req: sqlite3.Row, now: str, *, actor_id: int | None,
                 detail: str) -> None:
    tx.execute(
        "UPDATE documents SET status='opened', opened_at=? WHERE id=?",
        (now, req["document_id"]),
    )
    tx.execute(
        "UPDATE open_requests SET status='opened', resolved_at=? WHERE id=?",
        (now, req["id"]),
    )
    add_audit(
        tx,
        actor_id=actor_id,
        action="opened",
        document_id=req["document_id"],
        request_id=req["id"],
        detail=detail,
    )


def settle_request(tx: sqlite3.Connection, request_id: int, now: str) -> sqlite3.Row:
    """在当前写事务内确定性推进一个请求的状态，返回推进后的请求行。

    幂等：已是终态（opened/revoked/expired）或无需推进时直接返回。
    """
    req = tx.execute("SELECT * FROM open_requests WHERE id = ?", (request_id,)).fetchone()
    if req is None:
        return req

    if req["status"] == "scheduled":
        if req["earliest_open_at"] is not None and req["earliest_open_at"] <= now:
            _mark_opened(
                tx, req, now, actor_id=None,
                detail="到达最早开封时刻，两名不同指定见证人的同意此前已集齐，资料定时开封"
                       "（后台处理延迟不影响开封生效）",
            )
            req = tx.execute("SELECT * FROM open_requests WHERE id = ?", (request_id,)).fetchone()
        return req

    if req["status"] == "pending" and req["deadline"] <= now:
        tx.execute(
            "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
            (now, request_id),
        )
        add_audit(
            tx,
            actor_id=None,
            action="request_expired",
            document_id=req["document_id"],
            request_id=request_id,
            detail="开封请求到期前未取得两名不同见证人同意，请求失效，已有同意作废",
        )
        req = tx.execute("SELECT * FROM open_requests WHERE id = ?", (request_id,)).fetchone()
    return req


def settle_due(tx: sqlite3.Connection, now: str) -> int:
    """找出所有“理论上已到点”的非终态请求并在本事务内逐一推进，返回推进条数。"""
    rows = tx.execute(
        "SELECT id FROM open_requests"
        " WHERE status = 'scheduled' OR (status = 'pending' AND deadline <= ?)",
        (now,),
    ).fetchall()
    changed = 0
    for row in rows:
        before = tx.execute(
            "SELECT status FROM open_requests WHERE id=?", (row["id"],)
        ).fetchone()["status"]
        after = settle_request(tx, row["id"], now)
        if after is not None and after["status"] != before:
            changed += 1
    return changed


class ExpirySweeper:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await asyncio.to_thread(init_db)
        await asyncio.to_thread(self.sweep_once)  # 重启后先恢复一次
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(settings.expiry_interval_seconds)
                await asyncio.to_thread(self.sweep_once)
            except asyncio.CancelledError:
                break

    @staticmethod
    def sweep_once() -> int:
        """推进所有到点请求：定时开封优先于到期失效；返回状态发生变化的条数。"""
        conn = connect()
        try:
            with write_tx(conn) as tx:
                return settle_due(tx, utc_str(now_utc()))
        finally:
            conn.close()


sweeper = ExpirySweeper()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await sweeper.start()
    try:
        yield
    finally:
        await sweeper.stop()


app = FastAPI(
    title="双人见证资料封存服务",
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 错误体只返回字段路径与类型信息，绝不回显提交内容（否则会泄露封存正文）。"""
    safe_errors = [
        {
            "loc": list(e.get("loc", [])),
            "msg": e.get("msg", "参数错误"),
            "type": e.get("type", "value_error"),
        }
        for e in exc.errors()
    ]
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        content={"error": "请求参数校验失败", "details": safe_errors})


# ---------------------------------------------------------------------------
# 依赖：数据库连接 + API Key 认证（无效凭据一律 401，不写任何记录）
# ---------------------------------------------------------------------------


def get_db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def current_user(
    x_api_key: str | None = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
) -> sqlite3.Row:
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 API Key")
    row = db.execute(
        "SELECT * FROM users WHERE key_hash = ?", (sha256_hex(x_api_key),)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="凭据无效")
    return row


def err(status_code: int, message: str) -> HTTPException:
    """统一错误体，只含一句不含正文/摘要的提示。"""
    return HTTPException(status_code=status_code, detail=message)


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------


@app.post("/register", status_code=201)
def register(payload: RegisterIn, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """本地注册：用户名唯一；只在本次响应中返回 API Key，之后无法找回。"""
    api_key = secrets.token_hex(32)
    with write_tx(db) as tx:
        exists = tx.execute("SELECT 1 FROM users WHERE username = ?", (payload.username,)).fetchone()
        if exists:
            raise err(409, "用户名已存在")
        cur = tx.execute(
            "INSERT INTO users (username, key_hash, created_at) VALUES (?, ?, ?)",
            (payload.username, sha256_hex(api_key), utc_str(now_utc())),
        )
        add_audit(tx, actor_id=cur.lastrowid, action="register", detail=f"用户 {payload.username} 注册")
        user_id = cur.lastrowid
    return {"user_id": user_id, "username": payload.username, "api_key": api_key}


@app.get("/me")
def me(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    return {"user_id": user["id"], "username": user["username"]}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 资料封存
# ---------------------------------------------------------------------------


@app.post("/documents", status_code=201)
def seal_document(
    payload: SealIn,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    if len(set(payload.witnesses)) != 3:
        raise err(400, "必须指定三名互不相同的见证人")
    if user["username"] in payload.witnesses:
        raise err(400, "封存人不能同时担任见证人")

    with write_tx(db) as tx:
        placeholders = ",".join("?" for _ in payload.witnesses)
        witness_rows = tx.execute(
            f"SELECT id, username FROM users WHERE username IN ({placeholders})",
            payload.witnesses,
        ).fetchall()
        found = {row["username"] for row in witness_rows}
        missing = [w for w in payload.witnesses if w not in found]
        if missing:
            raise err(400, f"见证人尚未注册: {', '.join(missing)}")
        id_by_name = {row["username"]: row["id"] for row in witness_rows}

        digest = sha256_hex(payload.body)
        now = utc_str(now_utc())
        cur = tx.execute(
            "INSERT INTO documents (owner_id, body, digest, status, created_at)"
            " VALUES (?, ?, ?, 'sealed', ?)",
            (user["id"], payload.body, digest, now),
        )
        doc_id = cur.lastrowid
        for position, witness_name in enumerate(payload.witnesses):
            tx.execute(
                "INSERT INTO document_witnesses (document_id, user_id, position) VALUES (?, ?, ?)",
                (doc_id, id_by_name[witness_name], position),
            )
        add_audit(
            tx,
            actor_id=user["id"],
            action="seal",
            document_id=doc_id,
            detail=f"封存资料，摘要 {digest[:12]}…；见证人: {', '.join(payload.witnesses)}",
        )

    # 注意：响应不含正文
    return {"document_id": doc_id, "status": "sealed", "digest": digest, "witnesses": payload.witnesses}


def load_doc_for_participant(
    db: sqlite3.Connection, doc_id: int, user: sqlite3.Row
) -> sqlite3.Row:
    """取资料并校验参与方（封存人或指定见证人）；否则统一 403，不区分是否存在。"""
    doc = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if doc is None:
        raise err(403, "无权访问该资料或资料不存在")
    if doc["owner_id"] == user["id"]:
        return doc
    is_witness = db.execute(
        "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
        (doc_id, user["id"]),
    ).fetchone()
    if is_witness is None:
        raise err(403, "无权访问该资料或资料不存在")
    return doc


@app.get("/documents")
def list_documents(
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    """仅列出本人封存或本人见证的资料（只有元数据，永不返回正文）。"""
    rows = db.execute(
        """
        SELECT DISTINCT d.id, d.status, d.created_at, d.opened_at
        FROM documents d
        LEFT JOIN document_witnesses w ON w.document_id = d.id
        WHERE d.owner_id = ? OR w.user_id = ?
        ORDER BY d.id
        """,
        (user["id"], user["id"]),
    ).fetchall()
    return {"documents": [dict(row) for row in rows]}


@app.get("/documents/{doc_id}")
def get_document(
    doc_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    """查询资料。开封前不含正文；开封后返回正文、摘要、理由与见证记录。只读，不改状态。"""
    doc = load_doc_for_participant(db, doc_id, user)

    witnesses = [
        row["username"]
        for row in db.execute(
            """SELECT u.username FROM document_witnesses w
               JOIN users u ON u.id = w.user_id
               WHERE w.document_id = ? ORDER BY w.position""",
            (doc_id,),
        ).fetchall()
    ]

    result: dict[str, Any] = {
        "document_id": doc_id,
        "status": doc["status"],
        "created_at": doc["created_at"],
        "witnesses": witnesses,
    }

    latest = db.execute(
        "SELECT * FROM open_requests WHERE document_id = ? ORDER BY id DESC LIMIT 1",
        (doc_id,),
    ).fetchone()

    if doc["status"] == "opened":
        result["body"] = doc["body"]            # 开封后才返回正文
        result["digest"] = doc["digest"]
        result["opened_at"] = doc["opened_at"]
        if latest is not None:
            result["open_request"] = {
                "request_id": latest["id"],
                "reason": latest["reason"],
                "deadline": latest["deadline"],
                "earliest_open_at": latest["earliest_open_at"],
                "status": latest["status"],
                "created_at": latest["created_at"],
                "resolved_at": latest["resolved_at"],
            }
            witness_records = db.execute(
                """SELECT u.username, a.bound_digest, a.created_at AS approved_at
                   FROM approvals a JOIN users u ON u.id = a.witness_id
                   WHERE a.request_id = ? ORDER BY a.id""",
                (latest["id"],),
            ).fetchall()
            result["witness_approvals"] = [dict(row) for row in witness_records]
    else:
        # 开封前：只给当前开封请求的元数据，不给正文，也不给谁已同意
        if latest is not None:
            result["latest_open_request"] = {
                "request_id": latest["id"],
                "reason": latest["reason"],
                "deadline": latest["deadline"],
                "earliest_open_at": latest["earliest_open_at"],
                "status": latest["status"],
                "created_at": latest["created_at"],
                "approval_count": db.execute(
                    "SELECT COUNT(*) AS c FROM approvals WHERE request_id = ?",
                    (latest["id"],),
                ).fetchone()["c"],
            }
    return result


# ---------------------------------------------------------------------------
# 开封请求
# ---------------------------------------------------------------------------


@app.post("/documents/{doc_id}/open-requests", status_code=201)
def create_open_request(
    doc_id: int,
    payload: OpenRequestIn,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以提交开封请求")

    deadline = utc_str(payload.deadline)
    now = utc_str(now_utc())
    if deadline <= now:
        raise err(400, "截止时刻必须晚于当前时间")

    earliest_open_at: str | None = None
    if payload.earliest_open_at is not None:
        earliest_open_at = utc_str(payload.earliest_open_at)
        if earliest_open_at < now:
            raise err(400, "最早开封时刻不能早于提交时间")
        if earliest_open_at >= deadline:
            raise err(400, "最早开封时刻必须早于截止时刻")

    with write_tx(db) as tx:
        # 事务一（与下方插入同一连接，拆成两个临界区）：锁内确定性推进旧请求并提交，
        # 保证到点定时开封/到期失效不会因随后拒绝新请求（409）而被回滚。
        pending_row = tx.execute(
            "SELECT id FROM open_requests WHERE document_id = ? AND status IN ('pending', 'scheduled')",
            (doc_id,),
        ).fetchone()
        if pending_row is not None:
            settle_request(tx, pending_row["id"], now)

    with write_tx(db) as tx:
        # 事务二：锁内重读，决定拒绝还是创建
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["status"] != "sealed":
            raise err(409, "资料已开封，重复开封被拒绝")
        still_active = tx.execute(
            "SELECT id FROM open_requests WHERE document_id = ? AND status IN ('pending', 'scheduled')",
            (doc_id,),
        ).fetchone()
        if still_active is not None:
            raise err(409, "已有进行中的开封请求，请先撤销或等待其到期")

        cur = tx.execute(
            "INSERT INTO open_requests (document_id, reason, deadline, earliest_open_at,"
            " status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (doc_id, payload.reason, deadline, earliest_open_at, now),
        )
        request_id = cur.lastrowid
        schedule_note = f"；最早开封时刻: {earliest_open_at}" if earliest_open_at else ""
        add_audit(
            tx,
            actor_id=user["id"],
            action="open_request_created",
            document_id=doc_id,
            request_id=request_id,
            detail=f"提交开封请求，理由: {payload.reason}；截止时刻: {deadline}{schedule_note}",
        )

    return {
        "request_id": request_id,
        "document_id": doc_id,
        "status": "pending",
        "reason": payload.reason,
        "deadline": deadline,
        "earliest_open_at": earliest_open_at,
    }


@app.post("/open-requests/{request_id}/approve")
def approve_open_request(
    request_id: int,
    payload: ApproveIn,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    """见证人同意：必须是该资料的三名指定见证人之一；摘要须与封存摘要一致。

    所有判定均在写事务内完成；任何拒绝路径都不写入数据（状态不变）。
    """
    meta = db.execute(
        """SELECT r.id AS request_id, r.document_id, r.status AS req_status,
                  r.deadline, d.status AS doc_status, d.digest
           FROM open_requests r JOIN documents d ON d.id = r.document_id
           WHERE r.id = ?""",
        (request_id,),
    ).fetchone()
    if meta is None:
        raise err(403, "无权操作该请求或请求不存在")
    is_witness = db.execute(
        "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
        (meta["document_id"], user["id"]),
    ).fetchone()
    if is_witness is None:
        raise err(403, "只有指定见证人可以同意")

    now = utc_str(now_utc())
    try:
        # 事务一：确定性推进并先提交。到点定时开封/到期失效即使随后让本次同意以 409
        # 失败，也已经落库，绝不随业务回滚而失效。
        with write_tx(db) as tx:
            settle_request(tx, request_id, now)
        # 事务二：锁内重读并完成“检查 + 写入”（短临界区，并发只产生一个终态）
        with write_tx(db) as tx:
            req = tx.execute(
                "SELECT * FROM open_requests WHERE id = ?", (request_id,)
            ).fetchone()
            doc = tx.execute(
                "SELECT * FROM documents WHERE id = ?", (req["document_id"],)
            ).fetchone()

            if doc["status"] != "sealed":
                raise err(409, "资料已开封，重复开封被拒绝")
            if req["status"] == "scheduled":
                raise err(409, "开封请求已集齐两名见证人同意，处于待开封状态，等待最早开封时刻")
            if req["status"] != "pending":
                raise err(409, f"开封请求当前状态为 {req['status']}，同意被拒绝")
            if payload.digest != doc["digest"]:
                # 不同意即不绑定本次请求：不落库
                raise err(409, "提交的摘要与封存时摘要不一致，拒绝同意")
            already = tx.execute(
                "SELECT 1 FROM approvals WHERE request_id = ? AND witness_id = ?",
                (request_id, user["id"]),
            ).fetchone()
            if already is not None:
                raise err(409, "你已同意本次请求，重复提交只计一次")

            tx.execute(
                "INSERT INTO approvals (request_id, witness_id, bound_digest, created_at)"
                " VALUES (?, ?, ?, ?)",
                (request_id, user["id"], doc["digest"], now),
            )
            add_audit(
                tx,
                actor_id=user["id"],
                action="approval",
                document_id=doc["id"],
                request_id=request_id,
                detail="见证人同意开封，已绑定本次请求与封存摘要",
            )

            count = tx.execute(
                "SELECT COUNT(*) AS c FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()["c"]

            result_status = "pending"
            if count >= 2:
                # 第二名不同见证人到位。UNIQUE(request_id, witness_id)
                # 保证两名同意必然来自不同见证人。
                if req["earliest_open_at"] is not None and now < req["earliest_open_at"]:
                    # 指定了最早开封时刻且尚未到点：只进入待开封，不泄露正文
                    tx.execute(
                        "UPDATE open_requests SET status='scheduled' WHERE id=?",
                        (request_id,),
                    )
                    add_audit(
                        tx,
                        actor_id=user["id"],
                        action="scheduled_armed",
                        document_id=doc["id"],
                        request_id=request_id,
                        detail="两名不同指定见证人均已同意；未到最早开封时刻，请求进入待开封状态",
                    )
                    result_status = "scheduled"
                else:
                    # 未指定最早开封时刻（两票即开封），或已到点：立即开封
                    _mark_opened(
                        tx, req, now, actor_id=user["id"],
                        detail="两名不同指定见证人均已同意，资料正式开封",
                    )
                    result_status = "opened"

            return {
                "request_id": request_id,
                "document_id": doc["id"],
                "approval_count": count,
                "required": 2,
                "status": result_status,
                "earliest_open_at": req["earliest_open_at"],
            }
    except sqlite3.IntegrityError:
        # 唯一约束兜底（并发重复同意）
        raise err(409, "你已同意本次请求，重复提交只计一次")


@app.post("/open-requests/{request_id}/revoke")
def revoke_open_request(
    request_id: int,
    payload: RevokeIn,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    meta = db.execute(
        """SELECT r.id, r.document_id, r.status, r.deadline, d.owner_id
           FROM open_requests r JOIN documents d ON d.id = r.document_id
           WHERE r.id = ?""",
        (request_id,),
    ).fetchone()
    if meta is None:
        raise err(403, "无权操作该请求或请求不存在")
    if meta["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以撤销开封请求")

    now = utc_str(now_utc())
    # 事务一：确定性推进并先提交。到点的定时开封即使随后让本次撤销以 409 失败，
    # 也已经落库，绝不因回滚而失效。
    with write_tx(db) as tx:
        req = settle_request(tx, request_id, now)
    # 事务二：锁内重读并执行撤销（短临界区，并发只能有一个终态）
    with write_tx(db) as tx:
        req = tx.execute("SELECT * FROM open_requests WHERE id = ?", (request_id,)).fetchone()
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (req["document_id"],)).fetchone()
        if doc["status"] != "sealed":
            raise err(409, "资料已开封，无法撤销")
        if req["status"] not in ("pending", "scheduled"):
            raise err(409, f"开封请求当前状态为 {req['status']}，无法撤销")
        # 仍为 scheduled 说明最早开封时刻未到，撤销仍然有效
        scheduled_note = "（最早开封时刻前撤销，已集齐的同意全部作废）" if req["status"] == "scheduled" else ""
        tx.execute(
            "UPDATE open_requests SET status='revoked', resolved_at=? WHERE id=?",
            (now, request_id),
        )
        add_audit(
            tx,
            actor_id=user["id"],
            action="revoked",
            document_id=req["document_id"],
            request_id=request_id,
            detail=f"封存人撤销开封请求，已有同意全部作废{scheduled_note}。"
                   f"原因: {payload.reason or '（未填写）'}",
        )
    return {"request_id": request_id, "status": "revoked"}


# ---------------------------------------------------------------------------
# 操作记录（按顺序持久保存；非参与方不可见）
# ---------------------------------------------------------------------------


@app.get("/documents/{doc_id}/audit")
def get_audit(
    doc_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    load_doc_for_participant(db, doc_id, user)
    rows = db.execute(
        """SELECT a.id, a.at, a.action, a.request_id, a.detail,
                  u.username AS actor
           FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id
           WHERE a.document_id = ?
           ORDER BY a.id""",
        (doc_id,),
    ).fetchall()
    return {"document_id": doc_id, "records": [dict(row) for row in rows]}


def main() -> None:  # pragma: no cover
    init_db()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
