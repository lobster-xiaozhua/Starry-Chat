# Simple-Chat 生产级代码评审报告

> 评审对象：`simple-chat/`（Python 3.11+ FastAPI + SQLite + OpenAI 兼容接口 + 原生 JS）
> 评审方式：glob 全量列出 → 逐文件 read（app/ 全部模块 + 前端 app.js/index.html + tests/ + scripts/ + README/AGENTS/.env.example）→ 运行 74 项测试 + 对抗性探针 → 逐条核查 12 个怀疑点。
> 评审结论先行：核心链路稳健（74/74 测试通过），但存在 **1 个 P0 并发数据一致性缺陷** 与若干生产就绪缺口。

---

## 一、总评（成熟度评分与一句话结论）

**综合成熟度：6.5 / 10**（架构清晰、流式/SSE/多轮/异常映射均正确；失分点：并发锁 TTL 驱逐导致同会话可并发写库（数据不一致）、限流回环白名单在生产反代下失效、marked/DOMPurify 走 CDN 违反"无 CDN"约束、配置文件三处自相矛盾、两个重试脚本已死）。

**一句话结论**：这是一份"能跑、契约清晰、但离生产就绪还差最后几步"的代码——先修 P0 的并发锁驱逐，再堵 P1 的限流失效与 CDN 依赖，P2 按技术债表渐进偿还即可。

---

## 二、8 维度打分表（含证据行号）

| 维度 | 分 | 证据 / 说明 |
|---|---|---|
| 架构设计 | 8 | 分层清晰（router 仅适配、service 纯逻辑、client 收敛 LLM、db 集中 SQL）；但 `index.html` 引入 jsdelivr CDN（违反"无 CDN"约束），`scripts/retry_5xx.py` 等死脚本仍入库。 |
| 流式正确性 | 8 | SSE 帧格式 `event:/data:/\n\n` 严格（service.py:51、router.py:40-56 解析）；取消链路闭合（router.py:73-86 → service.py:345-358 → client.py:238-261）。缺首 token 延迟指标。 |
| 上下文工程 | 8 | `build_context` 预算裁剪 + 角色交替校验 + 至少保最新一条（service.py:173-230）；对抗探针确认当前问题 100% 进入上下文（见疑点①）。 |
| 并发与一致性 | 6 | 同会话 409 互斥正常（test_chat / test_service 均验证）；但 `_get_lock` 的 TTL 清理会驱逐**仍被持有**的锁（service.py:64-66），长流（>300s）下可两并发流同时写库 → **P0 数据不一致**。 |
| 安全 | 6 | ID 遍历防护正确（`_get_conversation` 统一 404，service.py:107-118）；XSS 经 escapeHtml + DOMPurify/手动剥离缓解（app.js:53-113）；但回环白名单生产失效（rate_limit.py:58，**P1**），且 CDN 引入供应链风险（**P1**）。 |
| 可观测性 | 5 | 结构化 JSON 日志（log.py）+ 请求/LLM 日志齐全；**缺 TTFT（首 token 延迟）指标**，且 `llm_call` 日志不含 `user_id`（log.py / client.py:264-268），无法按用户聚合 token 成本。 |
| 测试充分度 | 7 | 74 项覆盖流式顺序、多轮、错误映射、并发 409、级联删除、SSE 格式、限流；**缺口**：无"客户端断开→上游取消"测试、无锁 TTL 驱逐测试、重试脚本已死（疑点⑩⑨）。 |
| 文档一致性 | 5 | README 内部矛盾：:90-91 写 `MAX_CONTEXT_TOKENS=256000`、`LLM_MAX_RETRIES=5`，而 :311 写 `MAX_CONTEXT_TOKENS=4096`/`MAX_RESPONSE_TOKENS=1024`；代码 `llm_max_retries=2`（config.py:22）且被硬编码 `_MAX_RETRIES=2`（client.py:36）忽略；`.env.example` 缺 `MAX_RESPONSE_TOKENS`。 |

### 12 个怀疑点逐条核查（结论速查）

| # | 怀疑点 | 结论 | 证据 |
|---|---|---|---|
| ① | build_context rows[0] 角色 / 当前问题必现 | ✅ 无 bug | send_message 先在 build_context 前落当前用户消息（service.py:263-266），故 rows[0] 恒为 user；budget floor=`estimate_tokens([rows[0]])`（:200）保证 kept 永非空、当前问题必入上下文 |
| ② | lock.locked() 与 acquire 原子性 / finally 释放的锁归属 / 条目何时永不释放 | ⚠️ 发现 P0 | 检查与 acquire 间无 await→原子✅；释放即当前任务持有锁✅；但 TTL 清理每次调用全表扫描按 `now-ts>300` 删除，**会驱逐仍持有的锁**（service.py:64-66）→ 同会话并发 |
| ③ | yield error 后是否 return / 断流留空 assistant / CancelledError 是否 aclose | ✅ 基本无 bug | error 后无显式 return 但进 finally 自然结束（建议补 return，P2）；assistant 仅流成功后写（:316-322）断流不留空✅；chat_stream 在 CancelledError 调 `await stream.aclose()`（client.py:242,261）✅ |
| ④ | StreamingResponse 是否含 X-Accel-Buffering:no | ✅ 满足 | router.py:33-37 已含 `"X-Accel-Buffering":"no"` |
| ⑤ | chunked 无 Content-Length 放行 / 回环白名单生产失效 | ⚠️ 部分 bug | chunked 放行为设计已知（body_limit.py 注释）；**回环白名单无条件放行（rate_limit.py:58）在生产反代下=限流失效 → P1** |
| ⑥ | app.js XSS 防护 / DOMPurify 是否 vendor | ⚠️ 发现 P1 | escapeHtml 转义 <>&"'(app.js:53-56)；assistant 走 marked→DOMPurify 或手动剥离 on*/javascript:/data:（:80,96-113），`<svg onload>`/<math href=javascript:>/<details ontoggle>` 均被缓解✅；但 **marked/DOMPurify 来自 jsdelivr CDN（index.html），断网/CDN 故障退化为纯文本且违反"无 CDN" → P1**；FORBID_ATTR:["style"] 仅移除行内 style（展示影响，非 bug） |
| ⑦ | BadRequestError 是否一律 CONTEXT_OVERFLOW / 流式是否绝不重试 / 无 tool 却写"不要执行工具" | ⚠️ 已知限制 | map_openai_error 将 BadRequestError 一律→CONTEXT_OVERFLOW（client.py:90-91），temperature:999→用户见"上下文过长"（误导但符合规格）；流式仅连接建立阶段重试、首 token 后不重试（:163-183）✅；SYSTEM_PROMPT 提"不要执行工具"但无 tool 定义，属无害冗余✅ |
| ⑧ | config vs README vs .env.example 一致性 | ❌ 不一致 | README:90/91 写 5 / 256000，:311 写 4096 / 1024；代码 2（config.py:22 + client.py:36 硬编码忽略）；.env.example 同 README=5 但缺 MAX_RESPONSE_TOKENS → **P2** |
| ⑨ | retry_5xx.py / retry_e2e.py 引用符号是否存在 | ✅ 已修复（PR-2 删除） | 两个死脚本 import 了 client.py 中不存在的一组类/常量/方法名 → 运行即崩 → PR-2 已删除两脚本，权威 e2e 为 scripts/e2e.sh |
| ⑩ | ASGITransport 覆盖 / 断开取消测试 | ⚠️ 测试缺口 | ASGITransport 覆盖 \n\n 与帧粘连（test_chat.parse_sse）✅；但**无**"客户端断开→上游 LLM 取消"自动化测试 → **P2** |
| ⑪ | WAL + busy_timeout 50 并发是否 database is locked | ⚠️ 可接受 | db.py 设 WAL + busy_timeout=5000；单写者 + WAL 读不阻塞，50 并发不易锁；高写并发下仍可能等待，属可接受范围 |
| ⑫ | 首 token 延迟 / 按 user_id 聚合成本 | ⚠️ 缺口 | 无 TTFT 指标（chat_stream 仅末日志带总延迟，client.py:264-268）；llm_call 日志无 user_id，无法按用户聚合成本 → **P2** |

---

## 三、P0 清单（数据不一致 / 正确性错误）

### P0-1：并发锁 TTL 清理会驱逐"仍被持有"的锁，导致同会话两并发流同时写库（数据不一致）

**【根因】** `_get_lock`（service.py:60-78）每次调用都全表扫描 `_conv_locks`，按 `now - ts > _LOCK_TTL(300s)` 删除条目，但**未排除当前仍被 in-flight 流持有的锁**。一旦某条回复流耗时超过 300s（或恰好在持有期触发了 `_get_lock` 清理），旧条目被 pop 并新建一把锁对象；第二个同会话请求拿到**新锁**而非被 409 拦截，于是两个 `_stream_response` 并发执行，各自独立完成 `_add_message(assistant)`，最终同一轮用户消息产生**两条 assistant 消息**，且二者裁剪上下文时互不可见 → 数据不一致。

**【最小复现命令】**（独立脚本，不写入 tests/）
```bash
.venv/bin/python - <<'PY'
import asyncio, sys; sys.path.insert(0, '.')
from app.chat import service
async def main():
    cid = "conv-p0"
    lock = asyncio.Lock(); await lock.acquire()
    # 模拟一把"仍被持有、但 ts 已超 TTL"的锁被塞进池
    service._conv_locks[cid] = (lock, __import__('time').monotonic() - 400)
    got = service._get_lock(cid)          # 当前实现会把它 pop 掉并返回新锁
    print("驱逐了持有锁?", got is not lock, "| 池中残留:", cid in service._conv_locks)
    # 真实影响：第二请求不会被 409
asyncio.run(main())
PY
```
预期输出应显示"驱逐了持有锁? False"且池中仍含该会话；当前实现会打印 `True` 并丢失条目。

**【补丁 diff】**（service.py，单处 ≤2 行）
```diff
 def _get_lock(conv_id: str) -> asyncio.Lock:
     now = time.monotonic()
     # 清理过期项
-    expired = [k for k, (_, ts) in _conv_locks.items() if now - ts > _LOCK_TTL]
+    expired = [k for k, (lk, ts) in _conv_locks.items()
+               if now - ts > _LOCK_TTL and not lk.locked()]
     for k in expired:
         _conv_locks.pop(k, None)
```

**【断言测试】**（独立脚本）
```python
# 1) 持有锁且超 TTL 的条目必须"不被清理"
assert service._get_lock(cid) is held_lock          # 返回同一把锁
assert cid in service._conv_locks                    # 条目仍在
# 2) 同会话第二个请求仍被 409（模拟 send_message 的 lock.locked() 分支）
#    用 make_slow_fake 起第一流持有锁，第二请求应抛 ConversationBusyError
```

---

## 四、P1 清单

### P1-1：限流回环白名单在生产反代场景下等于限流失效（安全/可用性）
- **根因**：`rate_limit.py:58` `if ip in _LOOPBACK: return await call_next(request)` 无任何环境判断；docstring 称"开发本机调试用"，实现却全局生效。生产跑在同源反代（Nginx→uvicorn 127.0.0.1）且无 X-Forwarded-For 时，`request.client.host` 为回环地址 → 全员绕过限流。
- **补丁 diff**（rate_limit.py）
```diff
         ip = get_client_ip(request)
-        if ip in _LOOPBACK:
-            return await call_next(request)
+        # 仅非生产环境放行本地回环（本机调试）；生产环境一律限流，
+        # 避免反代回源为 127.0.0.1/::1 时全员绕过限流。
+        if ip in _LOOPBACK and not settings.is_production:
+            return await call_next(request)
```
- **断言**：`settings.is_production=True` + `rate_limit_enabled=True` + `get_client_ip→"127.0.0.1"` 无 XFF，连发 31 次应出现 429（现有 `test_rate_limit_*` 用外部 IP，不受影响仍绿）。

### P1-2：marked / DOMPurify 经 jsdelivr CDN 引入（违反"无 CDN"硬约束 + 断网不可渲染）
- **根因**：`index.html` 两行 `<script src="https://cdn.jsdelivr.net/...` 未 vendor。断网/CDN 故障 → assistant 消息退化为纯文本（app.js:84 兜底），且引入供应链风险；与"不引入 CDN"的项目约束冲突。
- **补丁方向**（PR 动作）：将 `marked@12.0.2` 与 `dompurify@3.1.6` 下载至 `app/web/static/vendor/` 并改为本地引用；同时把"离线兜底"从"纯文本"升级为"本地 vendored DOMPurify 清洗"。
- **断言**：`grep -rn "cdn\|jsdelivr\|unpkg" app/web/static` 应为空；离线（断网）下打开页面仍可对 assistant 消息做 markdown 渲染与清洗。

---

## 五、P2 清单

1. **LLM_MAX_RETRIES 配置被硬编码忽略**（config drift）：`client.py:36 _MAX_RETRIES = 2` 无视 `settings.llm_max_retries`（config.py:22，README/.env 写 5）。补丁：改为 `_MAX_RETRIES = settings.llm_max_retries`（1 行），下方 `range(_MAX_RETRIES+1)` 与 `attempt == _MAX_RETRIES` 自动生效。
2. **body_limit 返回 413 却带 VALIDATION_ERROR 码**（契约不一致）：`body_limit.py` 应新增 `ErrorCode.BODY_TOO_LARGE(http_status=413)` 并使用之（errors.py 三处映射 + body_limit.py 1 行）。
3. **重试脚本已死**（scripts/retry_5xx.py、retry_e2e.py）：引用了 client.py 中不存在的一组符号名，运行即崩，制造"脚本在但没真测"的假象。补丁：PR-2 已直接删除两个脚本，并在 README/AGENTS.md 标注以 `scripts/e2e.sh` 为唯一权威 e2e。
4. **缺"客户端断开→上游取消"测试**（测试缺口）：新增用例，用 ASGITransport + 中途 `response.aclose()`，断言上游 `chat_stream` 收到取消且 `_release_lock` 被调用、无孤立 assistant 行。
5. **缺 TTFT 与按 user_id 的成本可观测性**：在 `chat_stream` 首个 delta 时记录 `ttft_ms` 并 `logger.info`；LLM 日志 `extra` 注入 `user_id`（router 层传入），使计费可按用户聚合。
6. **文档三处自相矛盾**（疑点⑧）：统一 README / .env.example / config.py 的 `LLM_MAX_RETRIES`(5)、`MAX_CONTEXT_TOKENS`(256000)、`MAX_RESPONSE_TOKENS`(4096)；README "成本模型"段落的 4096/1024 示例改为明确标注"最小示例值"。

---

## 六、技术债偿还触发条件表（"该还的时机"= 可观测条件）

| 技术债 | 当前状态 | 该还的时机（可观测条件） | 动作 |
|---|---|---|---|
| P0 锁 TTL 驱逐 | 已定位 | 任何"长流（>300s）对话"日志出现，或监控发现同会话 assistant 行数 > 用户轮次 | 合入 P0-1 补丁 |
| P1 限流失效 | 已定位 | 生产 `X-Forwarded-For` 缺失率 > 0 且反代回源 127.0.0.1 | 合入 P1-1；上线前必做 |
| P1 CDN 依赖 | 已定位 | 任意一次 `curl -I` CDN 超时，或安全扫描标记第三方脚本 | vendor 化；上线前必做 |
| P2 配置漂移 | 已知 | 用户反馈"我设了 LLM_MAX_RETRIES=5 但只重试 2 次" | 改 1 行后回归 |
| P2 死脚本 | 已知 | CI 跑 `scripts/*.py` 即报 ImportError | 重写或删除 |
| P2 断开取消测试 | 缺口 | 线上出现"用户离开后 LLM 仍计费/后台仍写库"工单 | 补测试 + 断言 |
| P2 TTFT/成本 | 缺口 | 出现"慢"投诉但无法定位首 token 还是整段 | 加指标后复盘 |
| P2 文档矛盾 | 已知 | 新人按 README 配参无效 | 文档 PR |

---

## 七、与商业级对话产品的架构差距（仅架构层）

1. **无鉴权/多租户**：`get_current_user_id` 仅取 `X-User-Id` 头（deps.py），生产需 JWT/OAuth + 租户隔离；商业产品默认多租户 + 配额。
2. **限流为单实例内存实现**：rate_limit.py 注释自承多实例需 Redis+Lua；商业产品通常集中式限流 + 按租户配额。
3. **上下文仅"裁剪"无"压缩"**：build_context 只做截断与角色校验，无摘要/记忆压缩；商业产品普遍有长期记忆/摘要层（属 v0.3 范畴，不在本次范围）。
4. **无工具/函数调用与多模型路由**：client.py 无 tool 定义（SYSTEM_PROMPT 的"不要执行工具"为冗余约束）；商业产品有插件/agent 编排（明确不在当前 MVP）。
5. **可观测性浅层**：缺 TTFT、缺按用户/会话的成本账单、缺追踪（trace_id 跨 LLM 调用）；商业产品有完整 LLM 可观测栈（如 Langfuse/自建）。
6. **持久化为单文件 SQLite**：并发写靠 WAL+busy_timeout 顶住，水平扩展需换 Postgres（约束明确禁止，故仅记录差距）。
7. **无流式断点续传/重试前端层**：前端 `retry()` 仅重发整条，商业产品常做增量/去重。
8. **无评测/防护栏**：缺输入审核、输出合规、敏感信息 redaction 流水线（log.py 仅做密钥脱敏）。

---

## 八、下一步 3 个 PR 的验收清单

### PR-1（P0：并发数据一致性）验收
- [ ] `service.py` 仅改 `_get_lock` 清理条件（≤2 行），其余文件零改动。
- [ ] 独立断言脚本：持有且超 TTL 的锁条目**不被驱逐**；同会话第二请求仍抛 `ConversationBusyError`（409）。
- [ ] `pytest -q` 仍 74 passed；新增/手工复现 P0 复现脚本输出符合预期。
- [ ] 不破坏既有 409 行为（test_concurrent_same_conversation / test_concurrent_send_on_same_conversation_returns_409 绿）。

### PR-2（P1：生产就绪）验收
- [ ] `rate_limit.py` 回环白名单改为仅非生产放行；生产 `is_production=True` 下回环 IP 被限流。
- [ ] marked/DOMPurify 已 vendor 至 `app/web/static/vendor/`，`index.html` 改本地引用；`grep -rn "cdn\|jsdelivr" app/web/static` 为空。
- [ ] 断网回归：页面仍可对 assistant 消息做 markdown 渲染 + DOMPurify 清洗（无 CDN 也能用）。
- [ ] 既有限流测试全绿（外部 IP 用例不受影响）。

### PR-3（P2：一致性+可观测+清理）验收
- [ ] `_MAX_RETRIES = settings.llm_max_retries` 生效；env 设 5 时重试 5 次（断言）。
- [ ] 新增 `BODY_TOO_LARGE` 错误码，body 超限返回 413 + 该 code。
- [ ] 死脚本重写或删除；CI 不再 ImportError。
- [ ] 新增"客户端断开取消"测试，断言上游取消 + 锁释放 + 无孤立 assistant。
- [ ] LLM 日志注入 `user_id` 与 `ttft_ms`；README/.env.example/config.py 三处配置对齐。
- [ ] 全部 74 测试 + 新增测试绿；`git diff --stat` 仅含预期文件，单文件改动 ≤120 行。

---
*报告到此为止，未改动任何源码；待确认后按 PR-1 → PR-2 → PR-3 顺序实施外科手术式补丁。*
