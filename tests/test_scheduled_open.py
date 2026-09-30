"""定时开封（最早开封时刻 earliest_open_at）规则。"""
import datetime as dt
import time

from conftest import auth, register, seal

BODY = "定时开封正文：SCHEDULED-SECRET"


def _iso(offset_seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_seconds)).isoformat()


def _deadline_value(v) -> str:
    return v if isinstance(v, str) else _iso(v)


def _setup(client):
    owner = register(client, "s-owner")
    w1 = register(client, "s-w1")
    w2 = register(client, "s-w2")
    w3 = register(client, "s-w3")
    d = seal(client, owner, BODY, ["s-w1", "s-w2", "s-w3"])
    return owner, w1, w2, w3, d["document_id"], d["digest"]


def _create(client, owner, doc_id, *, earliest=2.0, deadline=3600):
    return client.post(
        f"/documents/{doc_id}/open-requests",
        json={
            "reason": "定时开封",
            "deadline": _iso(deadline),
            "earliest_open_at": _iso(earliest),
        },
        headers=auth(owner),
    )


def test_earliest_validation(client):
    owner, w1, w2, w3, doc_id, _ = _setup(client)

    def create(earliest, deadline):
        return client.post(
            f"/documents/{doc_id}/open-requests",
            json={"reason": "校验", "deadline": _deadline_value(deadline),
                  "earliest_open_at": earliest},
            headers=auth(owner),
        )

    # 最早开封时刻早于提交时间 -> 400
    r = create(_iso(-5), 3600)
    assert r.status_code == 400 and BODY not in r.text
    # 最早开封时刻等于截止时刻 -> 400（必须严格早于）
    same = _iso(600)
    r = create(same, same)
    assert r.status_code == 400
    # 最早开封时刻晚于截止时刻 -> 400
    r = create(_iso(7200), same)
    assert r.status_code == 400
    # 不带时区 -> 422，且不回显理由
    r = create("2026-10-01T12:00:00", 3600)
    assert r.status_code == 422 and "定时" not in r.text

    # 合法：成功创建，响应含 earliest_open_at
    r = create(_iso(30), 3600)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending" and body["earliest_open_at"] is not None


def test_two_votes_before_earliest_enter_scheduled_and_open_at_time(client, sweep):
    owner, w1, w2, w3, doc_id, dg = _setup(client)
    r = _create(client, owner, doc_id, earliest=5, deadline=3600)
    req_id = r.json()["request_id"]
    assert r.json()["earliest_open_at"] is not None

    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w1))
    assert r.status_code == 200 and r.json()["status"] == "pending"

    # 第二票在最早开封时刻前集齐：只进入 scheduled，不开封
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w2))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "scheduled"
    assert r.json()["approval_count"] == 2

    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed"
    assert "body" not in got
    info = got["latest_open_request"]
    assert info["status"] == "scheduled"
    assert info["earliest_open_at"] is not None
    assert info["approval_count"] == 2

    # scheduled 后任何人再同意都被拒（状态不变）
    assert client.post(f"/open-requests/{req_id}/approve", json={"digest": dg},
                       headers=auth(w3)).status_code == 409
    assert client.post(f"/open-requests/{req_id}/approve", json={"digest": dg},
                       headers=auth(w2)).status_code == 409

    # 到点前撤销仍然有效
    assert client.post(f"/open-requests/{req_id}/revoke", json={"reason": "改主意"},
                       headers=auth(owner)).status_code == 200
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["latest_open_request"]["status"] == "revoked"

    # 扫描过了最早开封时刻也不能把已撤销请求变成开封
    time.sleep(5.2)
    assert sweep() == 0
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed" and "body" not in got


def test_scheduled_opens_at_time_even_if_sweep_delayed(client, sweep):
    """两票集齐进入 scheduled；扫描延迟到 deadline 之后才运行，仍必须开封。"""
    owner, w1, w2, w3, doc_id, dg = _setup(client)
    # 最早开封时刻 +1.2s，截止时刻 +2.2s；直到 +3.2s 才扫描（已过截止时刻）
    r = _create(client, owner, doc_id, earliest=1.2, deadline=2.2)
    req_id = r.json()["request_id"]
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w1))
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w2))

    time.sleep(3.2)
    changed = sweep()
    assert changed == 1

    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "opened"
    assert got["body"] == BODY
    assert got["open_request"]["status"] == "opened"
    assert got["open_request"]["earliest_open_at"] is not None

    # 开封生效后的撤销不能使其失效
    r = client.post(f"/open-requests/{req_id}/revoke", json={"reason": "晚了"}, headers=auth(owner))
    assert r.status_code == 409
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "opened" and got["body"] == BODY

    records = client.get(f"/documents/{doc_id}/audit", headers=auth(owner)).json()["records"]
    actions = [r["action"] for r in records]
    assert actions.count("opened") == 1
    assert "scheduled_armed" in actions
    assert "revoked" not in actions


def test_revoke_after_due_opens_instead_of_revoking(client, monkeypatch):
    """到点后、扫描前调用撤销：请求必须先开封，撤销被拒，终态唯一。"""
    # 放大扫描间隔，确保是撤销接口自身（而非后台扫描）在临界区内完成到点开封
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "3600")
    owner, w1, w2, w3, doc_id, dg = _setup(client)
    r = _create(client, owner, doc_id, earliest=1.0, deadline=3600)
    req_id = r.json()["request_id"]
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w1))
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w2))

    time.sleep(1.6)
    # 不调用 sweep，直接撤销：到点开封优先
    r = client.post(f"/open-requests/{req_id}/revoke", json={"reason": "试试"}, headers=auth(owner))
    assert r.status_code == 409
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "opened" and got["body"] == BODY
    assert got["open_request"]["status"] == "opened"


def test_one_vote_at_time_then_second_before_deadline_opens_immediately(client):
    """到最早开封时刻时只有一票：保持 pending；截止前补齐第二票立即开封。"""
    owner, w1, w2, w3, doc_id, dg = _setup(client)
    r = _create(client, owner, doc_id, earliest=1.0, deadline=3600)
    req_id = r.json()["request_id"]
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w1))

    time.sleep(1.5)  # 过了最早开封时刻但仍只有一票：不开封、不过期
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed"
    assert got["latest_open_request"]["status"] == "pending"

    # 补齐第二票：立即开封（无需再等到某个时刻）
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w2))
    assert r.status_code == 200 and r.json()["status"] == "opened"
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["body"] == BODY


def test_never_two_votes_expires_and_old_votes_unusable(client, sweep):
    """填了最早开封时刻但截止前始终不足两票：到期失效，旧票不能复用。"""
    owner, w1, w2, w3, doc_id, dg = _setup(client)
    r = _create(client, owner, doc_id, earliest=0.5, deadline=1.2)
    req_id = r.json()["request_id"]
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w1))

    time.sleep(1.8)
    assert sweep() == 1
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed"
    assert got["latest_open_request"]["status"] == "expired"

    # 截止后补第二票被拒
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w2))
    assert r.status_code == 409

    # 新请求从 0 票开始，旧票不复用
    r = _create(client, owner, doc_id, earliest=0.5, deadline=3600)
    assert r.status_code == 201
    req2 = r.json()["request_id"]
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["latest_open_request"]["approval_count"] == 0


def test_omitted_earliest_keeps_immediate_behavior(client):
    """不填 earliest_open_at：响应与行为完全沿用旧规则（两票即开封）。"""
    owner, w1, w2, w3, doc_id, dg = _setup(client)
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "老规则", "deadline": _iso(3600)},
        headers=auth(owner),
    )
    assert r.status_code == 201
    assert r.json()["earliest_open_at"] is None
    req_id = r.json()["request_id"]

    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w1))
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w2))
    assert r.json()["status"] == "opened" and r.json()["earliest_open_at"] is None
