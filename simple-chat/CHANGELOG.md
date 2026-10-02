# 更新日志

本项目的所有重要变更记录在此文件。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## v0.2.0 — 2026-10-02

本版本包含 PR-1（并发与上下文正确性）、PR-2（安全与部署加固）、
PR-3（观测性与真实链路验证）三批变更。

### Added（PR-3）

- `GET /metrics`：Prometheus text format 指标端点（`app/metrics.py` 纯手写，
  不引入 prometheus_client），含首 token 延迟直方图等 11 项指标
- `GET /api/admin/cost`：按用户聚合的 token 成本账单（SQL 聚合 + 60s 缓存 +
  `MODEL_PRICE` 配置化；未配置 `ADMIN_API_KEY` 时返回 501）
- `tests/test_e2e_sse.py`：真实 uvicorn + respx + aiohttp 的端到端 SSE 链路测试
  （帧格式 / 帧顺序 / 断开 / 429 / 超时 / 粘帧 / Unicode / 大响应 / 并发 409）
- `scripts/load_test.py`：aiohttp 负载压测（80/10/10 混合流量，QPS 与分位数统计，
  P95 首 token > 2s 或错误率 > 1% 时退出码 1）
- `docs/observability.md`：PromQL 片段与最小面板说明
- `requirements-dev.txt`：respx / aiohttp（仅开发依赖）

### Fixed（PR-3）

- **客户端断开后上游 LLM 流不再被取消**：uvicorn 0.30 断开后仅丢弃 send()、
  Starlette 0.46（spec 2.4）又移除了 disconnect 监听——两者组合导致断开后
  流继续跑完、持续计费。新增 `DisconnectAwareStreamingResponse` 恢复 receive
  监听，断开即取消整条生成器链
- **上游流从不关闭**：openai `AsyncStream` 的关闭方法是 `close()` 而非
  `aclose()`，原代码静默失败导致每个成功流都泄漏一条 HTTP 连接
- `_do_stream` 生成器关闭时确定性 aclose 内层 `chat_stream`（原先只能等 GC）
- 流中途超时（连接建立后）现在映射为 `INTERNAL_ERROR`（可重试），而
  建立连接阶段的超时仍为 `RATE_LIMITED`
- BadRequestError 三级判断从 body dict 提取 `error.code`（openai SDK 错误码
  位于 `body["error"]["code"]`）

### Fixed（PR-1）

- 上下文裁剪不保底：当前用户消息必在上下文末尾；单条超预算时截断发送
  （`[内容已截断]`），绝不丢弃用户问题
- 会话锁 TOCTOU：`_ConversationGate` owner-id 模式 + meta 锁原子检查，
  同会话并发返回 409 CONVERSATION_BUSY
- 幽灵 assistant 消息：assistant 行仅在流成功结束且内容非空时落库，
  取消/错误路径零残留

### Fixed（PR-2）

- SSE 响应头（X-Accel-Buffering / charset / no-transform / keep-alive）
- XSS 三层防御：vendor 化 marked+DOMPurify（SRI）+ CSP meta + escapeFallback 兜底
- 错误码映射（BadRequestError 三级判断；新增 VALIDATION_ERROR / AUTH_ERROR /
  MODEL_UNAVAILABLE 前端文案）
- 限流白名单仅 development 生效；X-Forwarded-For 仅在 `TRUSTED_PROXIES` 非空时采信
- 请求体上限（流式累加，超限立即 413）
- 新增 `docs/nginx.conf`（SSE 反代）与 `scripts/backup.sh`（SQLite 在线热备）
- 配置一致性：`LLM_MAX_RETRIES=3` 读配置、`MAX_CONTEXT_TOKENS=8192`、
  删除死脚本 retry_5xx.py / retry_e2e.py

### 明确不做（v0.2）

- 分布式追踪（v0.4）、告警规则（v0.3，需 Prometheus server）、
  多模型路由（v0.3）、日志采集（v0.4，需 Loki/ELK）

## v0.1.0 — 初始版本

- FastAPI + SQLite（WAL）+ 流式 SSE 对话，前端原生 JS
