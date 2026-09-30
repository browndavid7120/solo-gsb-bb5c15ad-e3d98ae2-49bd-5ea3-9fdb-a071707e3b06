"""SQLite 连接、建表与事务。

写事务一律使用 BEGIN IMMEDIATE：进入事务即拿写锁，配合短事务，
把“检查状态 -> 写入”放入同一个串行化临界区，保证并发同意/撤销
只能有一个已提交结果。
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .config import settings

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    key_hash    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY,
    owner_id     INTEGER NOT NULL REFERENCES users(id),
    body         TEXT NOT NULL,
    digest       TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('sealed', 'opened')),
    opened_at    TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_witnesses (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    position     INTEGER NOT NULL,
    UNIQUE(document_id, user_id)
);

CREATE TABLE IF NOT EXISTS open_requests (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id),
    reason       TEXT NOT NULL,
    deadline     TEXT NOT NULL,            -- UTC，固定微秒精度，可直接按字典序比较
    status       TEXT NOT NULL CHECK (status IN ('pending', 'opened', 'revoked', 'expired')),
    created_at   TEXT NOT NULL,
    resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_open_requests_document ON open_requests(document_id);

CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY,
    request_id   INTEGER NOT NULL REFERENCES open_requests(id) ON DELETE CASCADE,
    witness_id   INTEGER NOT NULL REFERENCES users(id),
    bound_digest TEXT NOT NULL,            -- 同意时绑定的原摘要
    created_at   TEXT NOT NULL,
    UNIQUE(request_id, witness_id)         -- 同一人对同一请求重试只计一次
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,       -- 自增序号即操作的全局顺序
    at          TEXT NOT NULL,
    actor_id    INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,
    document_id INTEGER,
    request_id  INTEGER,
    detail      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_document ON audit_log(document_id);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    # check_same_thread=False：FastAPI 的 sync 依赖/端点可能被调度到不同的
    # 线程池工作线程；安全性由 BEGIN IMMEDIATE 短事务 + busy_timeout 保证。
    conn = sqlite3.connect(
        db_path or settings.db_path,
        timeout=30.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str | None = None) -> None:
    """幂等初始化：创建库表并开启 WAL / 外键（容器初始化时与应用启动时各调用一次也安全）。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即加写锁的写事务；异常回滚并原样抛出。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_str(dt: datetime) -> str:
    """统一格式：带 +00:00 的 UTC 时间，微秒固定 6 位，保证字符串可排序比较。"""
    if dt.tzinfo is None:
        raise ValueError("deadline 必须带时区信息")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")
