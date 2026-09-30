"""并发同意/撤销只产生一个已提交结果；重启后状态一致。

使用真实 uvicorn 服务（后台线程）+ httpx 发起真正的并发 HTTP 请求。
"""
import datetime as dt
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    db = tmp_path / "live.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "1")

    import uvicorn

    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        pytest.fail("server did not start")

    yield base, str(db)

    server.should_exit = True
    thread.join(timeout=5)


def _reg(c, base, name):
    r = c.post(base + "/register", json={"username": name})
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def _digest(body: str) -> str:
    import hashlib

    return hashlib.sha256(body.encode()).hexdigest()


def test_concurrent_approvals_single_outcome(live_server):
    base, _ = live_server
    with httpx.Client(timeout=10) as c:
        owner = _reg(c, base, "owner")
        keys = [_reg(c, base, f"w{i}") for i in range(1, 4)]
        body = "并发封存正文 CONCURRENT-SECRET"
        dg = _digest(body)
        r = c.post(base + "/documents", json={"body": body, "witnesses": ["w1", "w2", "w3"]},
                   headers={"X-API-Key": owner})
        doc_id = r.json()["document_id"]
        deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
        r = c.post(f"{base}/documents/{doc_id}/open-requests",
                   json={"reason": "并发同意", "deadline": deadline}, headers={"X-API-Key": owner})
        req_id = r.json()["request_id"]

    # 两名见证人在屏障处同时发起同意；各自再发起一次重复重试
    barrier = threading.Barrier(2)

    def call(wkey):
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            r1 = c.post(f"{base}/open-requests/{req_id}/approve",
                        json={"digest": dg}, headers={"X-API-Key": wkey})
            r2 = c.post(f"{base}/open-requests/{req_id}/approve",
                        json={"digest": dg}, headers={"X-API-Key": wkey})
            return r1.status_code, r2.status_code, r1.json() if r1.status_code == 200 else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, keys[:2]))

    # 每人：首次成功（200），重复一次被拒（409）
    for first, second, _body in results:
        assert first == 200
        assert second == 409

    with httpx.Client(timeout=10) as c:
        h = {"X-API-Key": owner}
        got = c.get(f"{base}/documents/{doc_id}", headers=h).json()
        # 唯一结果：资料恰好开封一次
        assert got["status"] == "opened"
        assert got["body"] == body
        assert len(got["witness_approvals"]) == 2
        # 不能被第三个人再次开封：仍是 409
        r = c.post(f"{base}/open-requests/{req_id}/approve",
                   json={"digest": dg}, headers={"X-API-Key": keys[2]})
        assert r.status_code == 409
        records = c.get(f"{base}/documents/{doc_id}/audit", headers=h).json()["records"]
        assert [r["action"] for r in records].count("opened") == 1
        assert [r["action"] for r in records].count("approval") == 2


def test_concurrent_approve_vs_revoke_single_outcome(live_server):
    base, _ = live_server
    with httpx.Client(timeout=10) as c:
        owner = _reg(c, base, "own2")
        w1 = _reg(c, base, "a1")
        w2 = _reg(c, base, "a2")
        w3 = _reg(c, base, "a3")
        body = "撤销赛跑正文 RACE-SECRET"
        dg = _digest(body)
        doc_id = c.post(base + "/documents", json={"body": body, "witnesses": ["a1", "a2", "a3"]},
                        headers={"X-API-Key": owner}).json()["document_id"]
        deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
        req_id = c.post(f"{base}/documents/{doc_id}/open-requests",
                        json={"reason": "赛跑", "deadline": deadline},
                        headers={"X-API-Key": owner}).json()["request_id"]
        # 先攒一票
        c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
               headers={"X-API-Key": w1})

    # 第二票同意 与 封存人撤销同时发生
    barrier = threading.Barrier(2)

    def approve():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(f"{base}/open-requests/{req_id}/approve",
                          json={"digest": dg}, headers={"X-API-Key": w2}).status_code

    def revoke():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(f"{base}/open-requests/{req_id}/revoke",
                          json={"reason": "撤销赛跑"}, headers={"X-API-Key": owner}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        ap_code, rv_code = list(pool.map(lambda f: f(), [approve, revoke]))

    with httpx.Client(timeout=10) as c:
        h = {"X-API-Key": owner}
        got = c.get(f"{base}/documents/{doc_id}", headers=h).json()
        # 只有一个已提交终态：要么开封（同意胜），要么仍封存且请求 revoked
        if 200 in (ap_code, rv_code):
            statuses = set()
            if ap_code == 200:
                statuses.add("opened")
            if rv_code == 200:
                statuses.add("sealed-revoked")
            assert len(statuses) == 1, (ap_code, rv_code)
        if got["status"] == "opened":
            assert rv_code == 409
            assert got["body"] == body
            assert got["open_request"]["status"] == "opened"
        else:
            assert ap_code == 409
            assert rv_code == 200
            assert "body" not in got
            assert got["latest_open_request"]["status"] == "revoked"
        records = c.get(f"{base}/documents/{doc_id}/audit", headers=h).json()["records"]
        assert [r["action"] for r in records].count("opened") <= 1
        assert [r["action"] for r in records].count("revoked") <= 1


def test_state_consistent_across_restart(live_server, monkeypatch):
    base, db_path = live_server
    with httpx.Client(timeout=10) as c:
        owner = _reg(c, base, "persist-owner")
        w1 = _reg(c, base, "p1")
        w2 = _reg(c, base, "p2")
        w3 = _reg(c, base, "p3")
        body = "重启一致性正文 RESTART-SECRET"
        dg = _digest(body)
        doc_id = c.post(base + "/documents", json={"body": body, "witnesses": ["p1", "p2", "p3"]},
                        headers={"X-API-Key": owner}).json()["document_id"]
        deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()
        req_id = c.post(f"{base}/documents/{doc_id}/open-requests",
                        json={"reason": "重启前同意一票", "deadline": deadline},
                        headers={"X-API-Key": owner}).json()["request_id"]
        r = c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
                   headers={"X-API-Key": w1})
        assert r.status_code == 200

    # 关掉服务再用同一个数据库文件启动
    monkeypatch.setenv("SEAL_DB_PATH", db_path)
    import uvicorn

    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base2 = f"http://127.0.0.1:{port}"
    deadline_ts = time.time() + 10
    while time.time() < deadline_ts:
        try:
            if httpx.get(base2 + "/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)

    try:
        with httpx.Client(timeout=10) as c:
            h = {"X-API-Key": owner}
            got = c.get(f"{base2}/documents/{doc_id}", headers=h).json()
            # 重启后仍是封存，旧同意保留且计数正确
            assert got["status"] == "sealed"
            assert "body" not in got
            assert got["latest_open_request"]["approval_count"] == 1
            assert got["latest_open_request"]["status"] == "pending"

            # w1 重复同意仍只算一次；w2 同意后开封
            r = c.post(f"{base2}/open-requests/{req_id}/approve", json={"digest": dg},
                       headers={"X-API-Key": w1})
            assert r.status_code == 409
            r = c.post(f"{base2}/open-requests/{req_id}/approve", json={"digest": dg},
                       headers={"X-API-Key": w2})
            assert r.status_code == 200 and r.json()["status"] == "opened"

            got = c.get(f"{base2}/documents/{doc_id}", headers=h).json()
            assert got["body"] == body
            records = c.get(f"{base2}/documents/{doc_id}/audit", headers=h).json()["records"]
            actions = [r["action"] for r in records]
            assert actions == ["seal", "open_request_created", "approval", "approval", "opened"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    # 再次以“冷读库”方式验证：已开封状态持久保持，不可重复开封
    import sqlite3

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status FROM documents WHERE id = ?", (doc_id,)).fetchone()
    assert row[0] == "opened"
    row = conn.execute("SELECT status FROM open_requests WHERE id = ?", (req_id,)).fetchone()
    assert row[0] == "opened"
    conn.close()
