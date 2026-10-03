#!/usr/bin/env bash
# v0.2 认证端到端验收（ROADMAP 验收入口）：注册 → 登录 → Cookie 发消息 →
# 读取自己的会话；A 的 Cookie 访问 B 的会话必须 404；登出后 Cookie 失效；
# 伪造 Cookie 401；登录失败触发独立限流 429。
#
# 前置：
#   1) 服务以 AUTH_ENABLED=true 启动（生产默认开；开发需显式打开）
#   2) 上游 LLM 可用（与 scripts/e2e.sh 相同）
# 用法：bash scripts/e2e_auth.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
BASE="${BASE%/}"
API="$BASE/api/chat"
AUTH="$BASE/api/auth"
RUN_ID="$$-$(date +%s)"
LOGIN_MAX="${LOGIN_MAX:-10}"          # 期望与服务端 AUTH_RATE_LIMIT_MAX 一致
COOKIE_JAR_A="$(mktemp)"
COOKIE_JAR_B="$(mktemp)"
cleanup() { rm -f "$COOKIE_JAR_A" "$COOKIE_JAR_B"; }
trap cleanup EXIT

for command in curl jq grep; do
  command -v "$command" >/dev/null 2>&1 || {
    printf '缺少依赖：%s\n' "$command" >&2
    exit 1
  }
done

step() { printf '\n==> %s\n' "$1"; }
ok() { printf '[ok] %s\n' "$1"; }
fail() { printf '[fail] %s\n' "$1" >&2; exit 1; }

# 从 SSE 文本中取事件的第一条 data 行
first_event_data() {
  local event="$1" file="$2"
  awk -v wanted="event: $event" '
    $0 == wanted { if (getline > 0 && $0 ~ /^data: /) { sub(/^data: /, ""); print; exit } }
  ' "$file"
}

step "health"
curl -sf "$BASE/healthz" | jq -e '.status == "ok"' >/dev/null || fail "healthz 异常"
ok "health"

step "auth enabled check"
# 未带 Cookie 访问 chat：认证开启应 401（若返回 200/其他，说明服务未开认证）
UNAUTH_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$API" \
  -H "Content-Type: application/json" -d '{"message":"hi"}')"
[ "$UNAUTH_STATUS" = "401" ] || fail "服务未开启 AUTH_ENABLED（未登录请求返回 $UNAUTH_STATUS）"
ok "auth enabled (401 without cookie)"

step "register A / B"
USER_A="alice-$RUN_ID"
USER_B="bob-$RUN_ID"
curl -sf -c "$COOKIE_JAR_A" -X POST "$AUTH/register" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$USER_A\",\"password\":\"alice-password-123\"}" \
  | jq -e --arg u "$USER_A" '.user.username == $u' >/dev/null || fail "注册 A 失败"
curl -sf -c "$COOKIE_JAR_B" -X POST "$AUTH/register" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$USER_B\",\"password\":\"bob-password-123\"}" \
  | jq -e --arg u "$USER_B" '.user.username == $u' >/dev/null || fail "注册 B 失败"
grep -q "sc_session" "$COOKIE_JAR_A" || fail "注册未下发会话 Cookie"
ok "register A/B"

step "cookie must be HttpOnly + SameSite=Strict"
SET_COOKIE="$(curl -sS -D - -o /dev/null -X POST "$AUTH/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$USER_A\",\"password\":\"alice-password-123\"}" \
  | grep -i '^set-cookie:' | tr -d '\r')"
grep -qi 'httponly' <<<"$SET_COOKIE" || fail "Cookie 缺少 HttpOnly：$SET_COOKIE"
grep -qi 'samesite=strict' <<<"$SET_COOKIE" || fail "Cookie 缺少 SameSite=Strict：$SET_COOKIE"
ok "cookie attributes"

step "forged cookie -> 401"
FORGED_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' "$API/conversations" \
  -H "Cookie: sc_session=forged.token")"
[ "$FORGED_STATUS" = "401" ] || fail "伪造 Cookie 应 401，实际 $FORGED_STATUS"
ok "forged cookie rejected"

step "A sends message with cookie"
SSE_LOG="$(mktemp)"
curl -sS -N -f -b "$COOKIE_JAR_A" -X POST "$API" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"message":"只回答：认证测试","stream":true}' \
  --max-time 60 | tee "$SSE_LOG" >/dev/null
DONE_JSON="$(first_event_data done "$SSE_LOG")"
CONV_A="$(jq -er '.conversation_id' <<<"$DONE_JSON")" || fail "A 未拿到 conversation_id"
rm -f "$SSE_LOG"
ok "A message sent (conversation=$CONV_A)"

step "A reads own conversation"
curl -sf -b "$COOKIE_JAR_A" "$API/conversations/$CONV_A/messages" \
  | jq -e '.messages | length >= 2' >/dev/null || fail "A 读取自己的会话失败"
ok "A reads own conversation"

step "B cannot read/delete A's conversation (404)"
READ_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' \
  -b "$COOKIE_JAR_B" "$API/conversations/$CONV_A/messages")"
[ "$READ_STATUS" = "404" ] || fail "B 读 A 会话应 404，实际 $READ_STATUS"
DEL_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE \
  -b "$COOKIE_JAR_B" "$API/conversations/$CONV_A")"
[ "$DEL_STATUS" = "404" ] || fail "B 删 A 会话应 404，实际 $DEL_STATUS"
B_COUNT="$(curl -sf -b "$COOKIE_JAR_B" "$API/conversations" | jq '.conversations | length')"
[ "$B_COUNT" = "0" ] || fail "B 的会话列表应为空，实际 $B_COUNT"
ok "A/B isolation"

step "logout invalidates cookie"
curl -sf -b "$COOKIE_JAR_A" -c "$COOKIE_JAR_A" -X POST "$AUTH/logout" -o /dev/null
ME_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' -b "$COOKIE_JAR_A" "$AUTH/me")"
[ "$ME_STATUS" = "401" ] || fail "登出后 /me 应 401，实际 $ME_STATUS"
ok "logout"

step "login failure rate limit"
RATE_USER="ratelimit-$RUN_ID"
LIMITED=""
for i in $(seq 1 $((LOGIN_MAX + 3))); do
  STATUS="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$AUTH/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$RATE_USER\",\"password\":\"wrong-password\"}")"
  if [ "$STATUS" = "429" ]; then LIMITED="yes"; break; fi
  [ "$STATUS" = "401" ] || fail "第 $i 次失败登录应 401，实际 $STATUS"
done
[ -n "$LIMITED" ] || fail "连续失败登录未触发 429（检查 AUTH_RATE_LIMIT_* 配置）"
ok "login rate limit"

printf '\nv0.2 认证端到端验收通过。\n'
