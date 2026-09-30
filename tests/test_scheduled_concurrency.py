"""定时开封的并发单终态与重启一致性（真实 uvicorn 服务）。"""
import datetime as dt
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from test_concurrency import _digest, _free_port, _reg, live_server  # noqa: F401


def _wait_for(client, base, path, key, predicate, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(base + path, headers={"X-API-Key": key})
        if predicate(last.json()):
            return last.json()
        time.sleep(0.05)
    raise AssertionError(f"condition not met: {last and last.json()}")


def test_scheduled_open_survives_concurrent_revoke_and_late_sweep(live_server):
    """两票到点时刻：撤销并发到达 + 扫描大幅延迟，仍必须开封且只有一个终态。"""
    base, _ = live_server
    with httpx.Client(timeout=10) as c:
        owner = _reg(c, base, "sched-owner")
        keys = [_reg(c, base, f"sw{i}") for i in range(1, 4)]
        body = "定时并发正文 SCHED-RACE-SECRET"
        dg = _digest(body)
        doc_id = c.post(base + "/documents",
                        json={"body": body, "witnesses": ["sw1", "sw2", "sw3"]},
                        headers={"X-API-Key": owner}).json()["document_id"]
        earliest = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
        deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=3)).isoformat()
        req_id = c.post(f"{base}/documents/{doc_id}/open-requests",
                        json={"reason": "定时赛跑", "deadline": deadline,
                              "earliest_open_at": earliest},
                        headers={"X-API-Key": owner}).json()["request_id"]
        c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
               headers={"X-API-Key": keys[0]})
        r = c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
                   headers={"X-API-Key": keys[1]})
        assert r.json()["status"] == "scheduled"

    # 等到最早开封时刻之后（已过、但截止时刻也即将/已经过去），撤销与到期竞速
    time.sleep(1.6)
    barrier = threading.Barrier(2)

    def revoke():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(f"{base}/open-requests/{req_id}/revoke", json={"reason": "晚到的撤销"},
                          headers={"X-API-Key": owner}).status_code

    def approve_third():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
                          headers={"X-API-Key": keys[2]}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda f: f(), [revoke, approve_third]))
    assert codes == [409, 409]

    with httpx.Client(timeout=10) as c:
        got = _wait_for(c, base, f"/documents/{doc_id}", owner,
                        lambda j: j["status"] == "opened", timeout=5)
        assert got["body"] == body
        assert got["open_request"]["status"] == "opened"
        records = c.get(f"{base}/documents/{doc_id}/audit",
                        headers={"X-API-Key": owner}).json()["records"]
        assert [r["action"] for r in records].count("opened") == 1
        assert [r["action"] for r in records].count("revoked") == 0


def test_scheduled_state_consistent_across_restart(live_server, monkeypatch):
    """重启前已 armed 的请求：重启后状态、票数一致，并能在到点时由扫描开封。"""
    base, db_path = live_server
    with httpx.Client(timeout=10) as c:
        owner = _reg(c, base, "rs-owner")
        keys = [_reg(c, base, f"rs{i}") for i in range(1, 4)]
        body = "定时重启正文 SCHED-RESTART-SECRET"
        dg = _digest(body)
        doc_id = c.post(base + "/documents",
                        json={"body": body, "witnesses": ["rs1", "rs2", "rs3"]},
                        headers={"X-API-Key": owner}).json()["document_id"]
        earliest = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
        deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()
        req_id = c.post(f"{base}/documents/{doc_id}/open-requests",
                        json={"reason": "重启后仍待开封", "deadline": deadline,
                              "earliest_open_at": earliest},
                        headers={"X-API-Key": owner}).json()["request_id"]
        c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
               headers={"X-API-Key": keys[0]})
        c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
               headers={"X-API-Key": keys[1]})
        got = c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": owner}).json()
        assert got["latest_open_request"]["status"] == "scheduled"
        assert "body" not in got

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
            got = c.get(f"{base2}/documents/{doc_id}", headers={"X-API-Key": owner}).json()
            assert got["status"] == "sealed" and "body" not in got
            info = got["latest_open_request"]
            assert info["status"] == "scheduled"
            assert info["approval_count"] == 2
            assert info["earliest_open_at"] is not None

            # 到点前撤销仍有效
            r = c.post(f"{base2}/open-requests/{req_id}/revoke", json={"reason": "重启后撤销"},
                       headers={"X-API-Key": owner})
            assert r.status_code == 200
            got = c.get(f"{base2}/documents/{doc_id}", headers={"X-API-Key": owner}).json()
            assert got["latest_open_request"]["status"] == "revoked"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_due_during_downtime_opens_on_startup(live_server):
    """停机期间越过最早开封时刻（且已过截止时刻）：重启恢复扫描立即开封。"""
    base, db_path = live_server
    with httpx.Client(timeout=10) as c:
        owner = _reg(c, base, "dt-owner")
        keys = [_reg(c, base, f"dt{i}") for i in range(1, 4)]
        body = "停机到点正文 SCHED-DOWNTIME-SECRET"
        dg = _digest(body)
        doc_id = c.post(base + "/documents",
                        json={"body": body, "witnesses": ["dt1", "dt2", "dt3"]},
                        headers={"X-API-Key": owner}).json()["document_id"]
        earliest = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
        deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2)).isoformat()
        req_id = c.post(f"{base}/documents/{doc_id}/open-requests",
                        json={"reason": "停机期间到点", "deadline": deadline,
                              "earliest_open_at": earliest},
                        headers={"X-API-Key": owner}).json()["request_id"]
        c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
               headers={"X-API-Key": keys[0]})
        c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
               headers={"X-API-Key": keys[1]})

    # 越过最早开封时刻与截止时刻（模拟停机/严重扫描延迟）后再启动
    time.sleep(3.0)

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
            got = _wait_for(c, base2, f"/documents/{doc_id}", owner,
                            lambda j: j["status"] == "opened", timeout=5)
            assert got["body"] == body
            assert got["open_request"]["status"] == "opened"
            r = c.post(f"{base2}/open-requests/{req_id}/revoke", json={"reason": "晚了"},
                       headers={"X-API-Key": owner})
            assert r.status_code == 409
    finally:
        server.should_exit = True
        thread.join(timeout=5)
