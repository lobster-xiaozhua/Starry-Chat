# 可观测性（PR-3）

本项目的指标由 `app/metrics.py` 手写实现（Prometheus text format，无
prometheus_client 依赖），经 `GET /metrics` 暴露。抓取与告警（Prometheus server、
Grafana）属部署侧配置，本文只给 PromQL 片段与解读方法。

> **醒目标注：没有首 token 延迟指标就不要做性能优化。**
> 所有“优化”必须以 `chat_first_token_seconds` 的 P95 变化为准；
> 改动前先抓基线，改动后对比同口径分位数。凭感觉优化 = 制造回归。

## 指标清单

| 指标 | 类型 | labels | 说明 |
|---|---|---|---|
| `chat_requests_total` | counter | model, error_code, status | 对话请求总数；成功时 error_code 为空 |
| `chat_first_token_seconds` | histogram | model | 【最关键】首 token 延迟（service 收到请求 → 第一个 delta 产出） |
| `chat_duration_seconds` | histogram | model | 完整流耗时（done / error / cancel 均计入） |
| `chat_tokens_total` | counter | model, role | token 用量（prompt / completion） |
| `chat_active_streams` | gauge | — | 当前进行中的流数 |
| `conversation_messages_total` | counter | role | 落库消息数 |
| `llm_retries_total` | counter | model, attempt | 上游连接建立阶段的重试次数 |
| `db_query_seconds` | histogram | op | SQLite 查询耗时（按操作分类） |
| `locks_contended_total` | counter | — | 409 CONVERSATION_BUSY 次数（会话级并发冲突） |
| `context_truncated_total` | counter | — | 当前用户消息因超预算被截断的次数 |
| `process_resident_memory_bytes` | gauge | — | 进程 RSS（压测/内存泄漏观测） |

histogram 采用 exponential buckets（如 1ms → 8.192s），分位数由采集端按累计桶估算。

## PromQL 片段（最小面板）

```promql
# QPS（按错误码拆分，error_code="" 为成功）
sum(rate(chat_requests_total[5m]))
sum by (error_code) (rate(chat_requests_total[5m]))

# P95 / P99 首 token 延迟 ——【性能优化的唯一裁判】
histogram_quantile(0.95, sum by (le) (rate(chat_first_token_seconds_bucket[5m])))
histogram_quantile(0.99, sum by (le) (rate(chat_first_token_seconds_bucket[5m])))

# 活跃流数（容量水位）
chat_active_streams

# token 成本/小时（按百万单价换算，价格来自 /api/admin/cost 的 price_per_million）
sum(rate(chat_tokens_total{role="prompt"}[1h]))     / 1e6 * <prompt_price>
+ sum(rate(chat_tokens_total{role="completion"}[1h])) / 1e6 * <completion_price>

# 错误率（按错误码）
sum(rate(chat_requests_total{error_code!=""}[5m]))
  / sum(rate(chat_requests_total[5m]))

# 上下文截断次数（用户消息被裁剪的频率；过高说明 MAX_CONTEXT_TOKENS 偏小）
increase(context_truncated_total[1h])

# DB 查询 P99（按操作）
histogram_quantile(0.99, sum by (le, op) (rate(db_query_seconds_bucket[5m])))
```

## 与 Grafana 的关系

`docs/grafana_dashboard.json` 未提供（避免绑定部署形态）。把上面的片段
逐个贴进 Grafana 的 Prometheus 面板即可得到最小看板：QPS、P95 首 token、
活跃流数、token 成本/小时、错误率(by code)、上下文截断、DB P99。

## 抓取配置示例

```yaml
scrape_configs:
  - job_name: simple-chat
    scrape_interval: 15s
    metrics_path: /metrics
    static_configs:
      - targets: ["127.0.0.1:8000"]
```

`/metrics` 不参与限流与鉴权；生产环境建议仅监听 127.0.0.1，或由
`docs/nginx.conf` 的反向代理加 IP 白名单后暴露。
