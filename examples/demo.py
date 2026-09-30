#!/usr/bin/env python3
"""完整流程调用示例（纯标准库，无需第三方依赖）。

用法: python examples/demo.py [base_url]
"""
import datetime as dt
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, key: str | None = None, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main() -> None:
    # 1. 注册
    users = {}
    for name in ["alice", "bob", "carol", "dave"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    owner, w1, w2 = users["alice"], users["bob"], users["carol"]

    # 2. 封存
    text = "遗嘱正文：Python 示例中的秘密内容"
    status, sealed = call("POST", "/documents", owner,
                          {"body": text, "witnesses": ["bob", "carol", "dave"]})
    doc_id, digest = sealed["document_id"], sealed["digest"]
    print("sealed:", sealed)

    # 3. 开封前查询：注意没有 body
    _, before = call("GET", f"/documents/{doc_id}", owner)
    assert "body" not in before, "开封前泄露了正文！"
    print("before open:", before)

    # 4. 提交开封请求（理由 + 截止时刻）
    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)).isoformat()
    status, req = call("POST", f"/documents/{doc_id}/open-requests", owner,
                       {"reason": "家庭会议开封", "deadline": deadline})
    req_id = req["request_id"]

    # 5. 两名不同见证人分别同意，且必须绑定原摘要
    print("approve w1:", call("POST", f"/open-requests/{req_id}/approve", w1, {"digest": digest})[1])
    # 同一人重试 -> 409，只计一次
    print("approve w1 retry:", call("POST", f"/open-requests/{req_id}/approve", w1, {"digest": digest}))
    print("approve w2:", call("POST", f"/open-requests/{req_id}/approve", w2, {"digest": digest})[1])

    # 6. 开封后查询：正文/摘要/理由/见证记录
    _, after = call("GET", f"/documents/{doc_id}", owner)
    assert after["body"] == text
    print("after open:", json.dumps(after, ensure_ascii=False, indent=2))

    # 7. 操作记录
    _, audit = call("GET", f"/documents/{doc_id}/audit", owner)
    print("audit:", [r["action"] for r in audit["records"]])

    # 8. 摘要必须自行计算并一致
    assert hashlib.sha256(text.encode()).hexdigest() == digest

    # 9. 定时开封（可选 earliest_open_at）：另封一份演示
    text2 = "定时开封遗嘱正文：Python 示例"
    _, sealed2 = call("POST", "/documents", owner,
                      {"body": text2, "witnesses": ["bob", "carol", "dave"]})
    doc2, digest2 = sealed2["document_id"], sealed2["digest"]
    earliest = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)).isoformat()
    deadline2 = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
    _, req2 = call("POST", f"/documents/{doc2}/open-requests", owner,
                   {"reason": "家庭会议定时开封", "deadline": deadline2,
                    "earliest_open_at": earliest})
    assert req2["earliest_open_at"] is not None
    print("scheduled request:", req2["request_id"], req2["earliest_open_at"])

    print("approve w1:", call("POST", f"/open-requests/{req2['request_id']}/approve", w1,
                              {"digest": digest2})[1]["status"])
    # 两票在最早时刻前集齐：只进入 scheduled，不开封
    second = call("POST", f"/open-requests/{req2['request_id']}/approve", w2,
                  {"digest": digest2})[1]
    assert second["status"] == "scheduled", second
    _, waiting = call("GET", f"/documents/{doc2}", owner)
    assert waiting["status"] == "sealed" and "body" not in waiting
    print("waiting status:", waiting["latest_open_request"]["status"],
          waiting["latest_open_request"]["earliest_open_at"])

    # 到点后由后台扫描自动开封（循环查询，最多等 10 秒）
    for _ in range(50):
        _, after2 = call("GET", f"/documents/{doc2}", owner)
        if after2["status"] == "opened":
            break
        time.sleep(0.2)
    assert after2["body"] == text2
    print("scheduled open done:", after2["open_request"]["status"])


if __name__ == "__main__":
    main()
