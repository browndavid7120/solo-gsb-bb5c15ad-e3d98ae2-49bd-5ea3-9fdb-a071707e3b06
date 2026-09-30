#!/usr/bin/env bash
# 定时开封流程示例：
#   注册 -> 封存 -> 提交带 earliest_open_at 的开封请求
#   -> 两票在最早时刻前集齐，只进入 scheduled（待开封，无正文）
#   -> 到点自动开封 -> 开封后查询含正文；事后撤销无效
# 依赖: curl, python3
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
# 为便于演示，最早开封时刻取 5 秒后、截止时刻取 1 小时后
EARLIEST_OFFSET="${EARLIEST_OFFSET:-5}"

jget() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }
iso() { python3 -c "import datetime,sys; print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=float(sys.argv[1]))).isoformat())" "$1"; }

echo "== 1. 注册封存人与三名见证人 =="
OWNER_KEY=$(curl -sS -X POST "$BASE/register" -H 'Content-Type: application/json' -d '{"username":"sched-alice"}' | jget "['api_key']")
W1_KEY=$(curl -sS -X POST "$BASE/register" -H 'Content-Type: application/json' -d '{"username":"sched-bob"}'   | jget "['api_key']")
W2_KEY=$(curl -sS -X POST "$BASE/register" -H 'Content-Type: application/json' -d '{"username":"sched-carol"}' | jget "['api_key']")
W3_KEY=$(curl -sS -X POST "$BASE/register" -H 'Content-Type: application/json' -d '{"username":"sched-dave"}'  | jget "['api_key']")

echo "== 2. 封存资料 =="
BODY='定时封存遗嘱正文：由……（此处省略）'
SEAL_RESP=$(curl -sS -X POST "$BASE/documents" \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "$(python3 - "$BODY" <<'PY'
import json, sys
print(json.dumps({"body": sys.argv[1], "witnesses": ["sched-bob", "sched-carol", "sched-dave"]}))
PY
)")
echo "$SEAL_RESP"
DOC_ID=$(echo "$SEAL_RESP" | jget "['document_id']")
DIGEST=$(echo "$SEAL_RESP" | jget "['digest']")

echo "== 3. 提交带最早开封时刻（+${EARLIEST_OFFSET}s）的开封请求 =="
EARLIEST=$(iso "$EARLIEST_OFFSET")
DEADLINE=$(iso 3600)
REQ_RESP=$(curl -sS -X POST "$BASE/documents/$DOC_ID/open-requests" \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "$(python3 - "$EARLIEST" "$DEADLINE" <<'PY'
import json, sys
print(json.dumps({"reason": "家庭会议定时开封", "deadline": sys.argv[2], "earliest_open_at": sys.argv[1]}))
PY
)")
echo "$REQ_RESP"
REQ_ID=$(echo "$REQ_RESP" | jget "['request_id']")

echo "== 4. 两名见证人在最早时刻前集齐两票：只进入 scheduled =="
curl -sS -X POST "$BASE/open-requests/$REQ_ID/approve" -H "X-API-Key: $W1_KEY" \
  -H 'Content-Type: application/json' -d "{\"digest\":\"$DIGEST\"}" \
  | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), ensure_ascii=False))"
curl -sS -X POST "$BASE/open-requests/$REQ_ID/approve" -H "X-API-Key: $W2_KEY" \
  -H 'Content-Type: application/json' -d "{\"digest\":\"$DIGEST\"}" \
  | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), ensure_ascii=False))"

echo "== 5. 开封前查询：status=scheduled，含 earliest_open_at，但没有 body =="
curl -sS "$BASE/documents/$DOC_ID" -H "X-API-Key: $OWNER_KEY" \
  | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))"

echo "== 6. 等待到点（${EARLIEST_OFFSET}s），后台确定性开封 =="
sleep $((EARLIEST_OFFSET + 2))

echo "== 7. 开封后查询：正文可见 =="
curl -sS "$BASE/documents/$DOC_ID" -H "X-API-Key: $OWNER_KEY" \
  | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))"

echo "== 8. 开封生效后撤销被拒（409），不影响已开封状态 =="
curl -sS -o /dev/null -w 'revoke http=%{http_code}\n' -X POST "$BASE/open-requests/$REQ_ID/revoke" \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' -d '{"reason":"晚了"}'

echo "== 9. 审计顺序 =="
curl -sS "$BASE/documents/$DOC_ID/audit" -H "X-API-Key: $OWNER_KEY" \
  | python3 -c "import sys,json; print([r['action'] for r in json.load(sys.stdin)['records']])"
