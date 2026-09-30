"""双人见证资料封存服务。

关键规则：
- 本地账号注册后凭 API Key（X-API-Key）操作，服务端只存密钥哈希；
- 封存后正文不可改；开封前任何查询与错误响应都不返回正文；
- 开封请求需两名不同指定见证人在截止时刻前分别同意，同一人重试只计一次；
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
# 过期扫描：把已到截止时刻仍 pending 的请求置为 expired
# ---------------------------------------------------------------------------


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
        """把 deadline<=now 的 pending 请求标记为 expired；返回失效条数。"""
        conn = connect()
        try:
            with write_tx(conn) as tx:
                now = utc_str(now_utc())
                rows = tx.execute(
                    "SELECT id, document_id FROM open_requests"
                    " WHERE status = 'pending' AND deadline <= ?",
                    (now,),
                ).fetchall()
                for row in rows:
                    tx.execute(
                        "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    add_audit(
                        tx,
                        actor_id=None,
                        action="request_expired",
                        document_id=row["document_id"],
                        request_id=row["id"],
                        detail="开封请求到期前未取得两名不同见证人同意，请求失效，已有同意作废",
                    )
                return len(rows)
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
    version="1.0.0",
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

    with write_tx(db) as tx:
        # 锁内重读，避免与同意/撤销/扫描并发
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["status"] != "sealed":
            raise err(409, "资料已开封，重复开封被拒绝")
        pending = tx.execute(
            "SELECT id, deadline FROM open_requests WHERE document_id = ? AND status = 'pending'",
            (doc_id,),
        ).fetchone()
        if pending is not None:
            if pending["deadline"] <= now:
                # 理论上扫描器很快会处理；在同一临界区内顺手确定性失效
                tx.execute(
                    "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
                    (now, pending["id"]),
                )
                add_audit(
                    tx,
                    actor_id=None,
                    action="request_expired",
                    document_id=doc_id,
                    request_id=pending["id"],
                    detail="旧开封请求已过截止时刻，提交新请求时确认失效，旧同意作废",
                )
            else:
                raise err(409, "已有进行中的开封请求，请先撤销或等待其到期")

        cur = tx.execute(
            "INSERT INTO open_requests (document_id, reason, deadline, status, created_at)"
            " VALUES (?, ?, ?, 'pending', ?)",
            (doc_id, payload.reason, deadline, now),
        )
        request_id = cur.lastrowid
        add_audit(
            tx,
            actor_id=user["id"],
            action="open_request_created",
            document_id=doc_id,
            request_id=request_id,
            detail=f"提交开封请求，理由: {payload.reason}；截止时刻: {deadline}",
        )

    return {
        "request_id": request_id,
        "document_id": doc_id,
        "status": "pending",
        "reason": payload.reason,
        "deadline": deadline,
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
        with write_tx(db) as tx:
            req = tx.execute(
                "SELECT * FROM open_requests WHERE id = ?", (request_id,)
            ).fetchone()
            doc = tx.execute(
                "SELECT * FROM documents WHERE id = ?", (req["document_id"],)
            ).fetchone()

            if doc["status"] != "sealed":
                raise err(409, "资料已开封，重复开封被拒绝")
            if req["status"] != "pending":
                raise err(409, f"开封请求当前状态为 {req['status']}，同意被拒绝")
            if req["deadline"] <= now:
                raise err(409, "已过截止时刻，同意被拒绝，请求即将失效")
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

            opened = False
            if count >= 2:
                # 第二名不同见证人到位：开封。UNIQUE(request_id, witness_id)
                # 保证两名同意必然来自不同见证人。
                tx.execute(
                    "UPDATE documents SET status='opened', opened_at=? WHERE id=?",
                    (now, doc["id"]),
                )
                tx.execute(
                    "UPDATE open_requests SET status='opened', resolved_at=? WHERE id=?",
                    (now, request_id),
                )
                add_audit(
                    tx,
                    actor_id=user["id"],
                    action="opened",
                    document_id=doc["id"],
                    request_id=request_id,
                    detail="两名不同指定见证人均已同意，资料正式开封",
                )
                opened = True

            return {
                "request_id": request_id,
                "document_id": doc["id"],
                "approval_count": count,
                "required": 2,
                "status": "opened" if opened else "pending",
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
    with write_tx(db) as tx:
        req = tx.execute("SELECT * FROM open_requests WHERE id = ?", (request_id,)).fetchone()
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (req["document_id"],)).fetchone()
        if doc["status"] != "sealed":
            raise err(409, "资料已开封，无法撤销")
        if req["status"] != "pending":
            raise err(409, f"开封请求当前状态为 {req['status']}，无法撤销")
        if req["deadline"] <= now:
            raise err(409, "已过截止时刻，不能撤销，请求即将失效")
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
            detail=f"封存人撤销开封请求，已有同意全部作废。原因: {payload.reason or '（未填写）'}",
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
