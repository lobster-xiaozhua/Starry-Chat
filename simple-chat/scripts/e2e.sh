#!/usr/bin/env bash
# 端到端验收：健康检查、SSE、多轮会话、列表/删除、限流与坏输入。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
BASE="${BASE%/}"
API="$BASE/api/chat"
SSE_LOG="${SSE_LOG:-/tmp/sse.log}"
SECOND_SSE_LOG="${SECOND_SSE_LOG:-/tmp/sse-second.log}"
RATE_LIMIT_IP="${RATE_LIMIT_IP:-198.51.100.42}"
RATE_LIMIT_USER="${RATE_LIMIT_USER:-e2e-rate-limit-$$}"

for command in curl jq grep awk; do
  command -v "$command" >/dev/null 2>&1 || {
    printf '缺少依赖：%s\n' "$command" >&2
    exit 1
  }
done

step() {
  printf '\n==> %s\n' "$1"
}

ok() {
  printf '[ok] %s\n' "$1"
}

fail() {
  printf '[fail] %s\n' "$1" >&2
  exit 1
}

# 从一个 SSE 文件中取指定事件的第一条 data 行。
first_event_data() {
  local event="$1"
  local file="$2"
  awk -v wanted="event: $event" '
    $0 == wanted {
      if (getline > 0 && $0 ~ /^data: /) {
        sub(/^data: /, "")
        print
        exit
      }
    }
  ' "$file"
}

# 输出指定 SSE 事件的所有 data 行。
all_event_data() {
  local event="$1"
  local file="$2"
  awk -v wanted="event: $event" '
    $0 == wanted {
      if (getline > 0 && $0 ~ /^data: /) {
        sub(/^data: /, "")
        print
      }
    }
  ' "$file"
}

step "health"
curl -sf "$BASE/healthz" | jq .
ok "health"

step "send stream"
: >"$SSE_LOG"
curl -sS -N -f -X POST "$API" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"message":"1+1等于几？","stream":true}' \
  --max-time 60 \
  | tee "$SSE_LOG"
ok "send stream"

step "parse SSE"
TOKEN_COUNT="$(grep -c '^event: token$' "$SSE_LOG" || true)"
[ "$TOKEN_COUNT" -ge 1 ] || fail "未收到 event: token"
grep -q '^event: done$' "$SSE_LOG" || fail "未收到 event: done"
DONE_JSON="$(first_event_data done "$SSE_LOG")"
[ -n "$DONE_JSON" ] || fail "done 事件缺少 data"
CONV_ID="$(jq -er '.conversation_id' <<<"$DONE_JSON")"
[ -n "$CONV_ID" ] || fail "done 事件缺少 conversation_id"
printf 'conversation_id=%s, token_events=%s\n' "$CONV_ID" "$TOKEN_COUNT"
ok "parse SSE"

step "multi-turn"
SECOND_PAYLOAD="$(jq -cn \
  --arg conversation_id "$CONV_ID" \
  '{conversation_id:$conversation_id,message:"请只回答数字 2。",stream:true}')"
: >"$SECOND_SSE_LOG"
curl -sS -N -f -X POST "$API" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d "$SECOND_PAYLOAD" \
  --max-time 60 \
  | tee "$SECOND_SSE_LOG"
grep -q '^event: done$' "$SECOND_SSE_LOG" || fail "第二轮未收到 event: done"
ANSWER="$(all_event_data token "$SECOND_SSE_LOG" | jq -r '.delta' | tr -d '\n')"
[ -n "$ANSWER" ] || fail "第二轮没有 token 内容"
grep -q '2' <<<"$ANSWER" || fail "第二轮回答未包含 2：$ANSWER"
printf 'answer=%s\n' "$ANSWER"
ok "multi-turn"

step "list"
LIST_JSON="$(curl -sf "$API/conversations")"
printf '%s\n' "$LIST_JSON" | jq .
CONVERSATION_COUNT="$(jq -er '((.conversations // .) | length)' <<<"$LIST_JSON")"
[ "$CONVERSATION_COUNT" -ge 1 ] || fail "会话列表为空"
ok "list"

step "delete"
DELETE_BODY="$(mktemp)"
DELETE_STATUS="$(curl -sS -o "$DELETE_BODY" -w '%{http_code}' \
  -X DELETE "$API/conversations/$CONV_ID")"
# 当前 API 按契约返回 204；兼容返回 {"success":true} 的部署版本。
if [ "$DELETE_STATUS" = "204" ]; then
  :
elif [ "$DELETE_STATUS" = "200" ]; then
  jq -e '.success == true' "$DELETE_BODY" >/dev/null || fail "删除响应未确认 success"
else
  fail "删除会话返回 HTTP $DELETE_STATUS"
fi
rm -f "$DELETE_BODY"
ok "delete"

step "rate limit"
# 生产限流按 user_id + IP 计数；使用文档保留网段的 XFF，避免命中 localhost 白名单，
# 同时隔离前面验收步骤产生的请求。
for request_number in $(seq 1 35); do
  status="$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "X-User-Id: $RATE_LIMIT_USER" \
    -H "X-Forwarded-For: $RATE_LIMIT_IP" \
    "$API/conversations")"
  if [ "$request_number" -le 30 ]; then
    [[ "$status" =~ ^2[0-9][0-9]$ ]] || \
      fail "第 ${request_number} 次请求应成功，实际 HTTP $status"
  else
    [ "$status" = "429" ] || \
      fail "第 ${request_number} 次请求应返回 429，实际 HTTP $status"
  fi
done
printf '第 31-35 次请求均返回 429\n'
ok "rate limit"

step "bad input"
BAD_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST "$API" \
  -H "Content-Type: application/json" \
  -d '{"message":""}')"
[ "$BAD_STATUS" = "422" ] || fail "空消息应返回 422，实际 HTTP $BAD_STATUS"
ok "bad input"

printf '\n端到端验收通过。\n'
