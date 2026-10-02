"""Prometheus text format 指标（PR-3 改动 1）：纯手写，不引入 prometheus_client。

local-first 约束：prometheus_client 会拉入 10+ 传递依赖，这里用普通 Python
数据结构 + int/float += 实现（GIL 保证单字段更新原子，asyncio 单线程下无竞争）。

指标清单（缺一不可）：
  chat_requests_total         counter   labels: model, error_code, status
  chat_first_token_seconds    histogram labels: model        # 【最关键】首 token 延迟
  chat_duration_seconds       histogram labels: model        # p50/p95/p99 完整耗时
  chat_tokens_total           counter   labels: model, role(prompt|completion)
  chat_active_streams         gauge
  conversation_messages_total counter   labels: role
  llm_retries_total           counter   labels: model, attempt
  db_query_seconds            histogram labels: op
  locks_contended_total       counter
  context_truncated_total     counter

实现约定：
- histogram 用 exponential buckets（start * factor**i），分位数在采集端
  （Prometheus 的 histogram_quantile）按桶估算——这是 Prometheus 官方惯例。
- 首 token 延迟计时起点 = service 收到请求（send_message 入口），终点 =
  第一个 delta 产出。网络传输到前端的部分不在服务端可控范围内，不计入。
- /metrics 端点本身不产生任何 chat_* 指标更新（避免自增风暴）。
- trace_id 用 contextvars 透传（由 main.RequestLogMiddleware 设置），供慢查询
  等内部日志关联请求，避免层层传参；trace_id 不作为指标 label（高基数）。
"""

from __future__ import annotations

import contextvars
import logging
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ───────────────── trace_id（contextvars，避免层层传参） ─────────────────

trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def _escape_label(value: str) -> str:
    """Prometheus label 值转义：反斜杠 / 双引号 / 换行。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\\\n")


def _format_bound(bound: float) -> str:
    """桶边界渲染：0.001 / 0.002 / 1.024 …（exponential 桶无超长小数，:g 足够）。"""
    return f"{bound:g}"


def _render_labels(pairs: list[tuple[str, str]]) -> str:
    inner = ",".join(f'{k}="{_escape_label(str(v))}"' for k, v in pairs)
    return "{" + inner + "}" if inner else ""


# ───────────────────────── 指标类型 ─────────────────────────


class Counter:
    """counter：只增。values[(label values...)] -> float（int += 原子）。"""

    def __init__(self, name: str, doc: str, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self.doc = doc
        self.labels = labels
        self.values: dict[tuple[str, ...], float] = {}

    def inc(self, value: float = 1.0, **label_values: str) -> None:
        key = tuple(str(label_values.get(l, "")) for l in self.labels)
        self.values[key] = self.values.get(key, 0.0) + value

    def get(self, **label_values: str) -> float:
        key = tuple(str(label_values.get(l, "")) for l in self.labels)
        return self.values.get(key, 0.0)

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.doc}", f"# TYPE {self.name} counter"]
        for key, value in self.values.items():
            lines.append(f"{self.name}{_render_labels(list(zip(self.labels, key)))} {value:g}")
        if not self.values:
            # 无样本时输出 0 样例行，保证指标名在抓取端始终可见
            lines.append(f"{self.name} 0")
        return "\n".join(lines) + "\n"


class Gauge:
    """gauge：可增可减。"""

    def __init__(self, name: str, doc: str, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self.doc = doc
        self.labels = labels
        self.values: dict[tuple[str, ...], float] = {(): 0.0}

    def inc(self, value: float = 1.0, **label_values: str) -> None:
        key = tuple(str(label_values.get(l, "")) for l in self.labels)
        self.values[key] = self.values.get(key, 0.0) + value

    def dec(self, value: float = 1.0, **label_values: str) -> None:
        self.inc(-value, **label_values)

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.doc}", f"# TYPE {self.name} gauge"]
        for key, value in self.values.items():
            lines.append(f"{self.name}{_render_labels(list(zip(self.labels, key)))} {value:g}")
        return "\n".join(lines) + "\n"


class Histogram:
    """histogram：exponential buckets，_bucket/_sum/_count 三件套。

    桶边界 = start * factor**i（i = 0..count-1），隐式 +Inf 兜底。
    分位数不在本模块计算——Prometheus 惯例是采集端用 histogram_quantile
    按累计桶估算（见 docs/observability.md 的 PromQL 片段）。
    """

    def __init__(
        self,
        name: str,
        doc: str,
        labels: tuple[str, ...] = (),
        *,
        start: float,
        factor: float,
        count: int,
    ) -> None:
        self.name = name
        self.doc = doc
        self.labels = labels
        self.bounds = [start * factor**i for i in range(count)]
        # key -> {"buckets": [累计计数...], "sum": float, "count": int}
        self.data: dict[tuple[str, ...], dict] = {}

    def observe(self, value: float, **label_values: str) -> None:
        key = tuple(str(label_values.get(l, "")) for l in self.labels)
        entry = self.data.get(key)
        if entry is None:
            entry = {"buckets": [0] * len(self.bounds), "sum": 0.0, "count": 0}
            self.data[key] = entry
        # 累计桶：每个 >= value 的边界都 +1
        for i, bound in enumerate(self.bounds):
            if value <= bound:
                entry["buckets"][i] += 1
        entry["count"] += 1
        entry["sum"] += value

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.doc}", f"# TYPE {self.name} histogram"]
        for key, entry in self.data.items():
            base = list(zip(self.labels, key))
            for bound, cnt in zip(self.bounds, entry["buckets"]):
                pairs = [*base, ("le", _format_bound(bound))]
                lines.append(f"{self.name}_bucket{_render_labels(pairs)} {cnt}")
            pairs_inf = [*base, ("le", "+Inf")]
            lines.append(f"{self.name}_bucket{_render_labels(pairs_inf)} {entry['count']}")
            lines.append(f"{self.name}_sum{_render_labels(base)} {entry['sum']:.6f}")
            lines.append(f"{self.name}_count{_render_labels(base)} {entry['count']}")
        return "\n".join(lines) + "\n"


# ───────────────────── 指标实例（全项目唯一定义点） ─────────────────────

chat_requests_total = Counter(
    "chat_requests_total",
    "Total chat requests by model, error_code (empty on success) and status",
    ("model", "error_code", "status"),
)

chat_first_token_seconds = Histogram(
    "chat_first_token_seconds",
    "Time from service receiving the request to first delta produced (THE key metric)",
    ("model",),
    start=0.001,
    factor=2.0,
    count=14,  # 1ms .. 8.192s
)

chat_duration_seconds = Histogram(
    "chat_duration_seconds",
    "Full stream duration from request to done/error/cancel",
    ("model",),
    start=0.05,
    factor=2.0,
    count=13,  # 50ms .. 204.8s
)

chat_tokens_total = Counter(
    "chat_tokens_total",
    "Token usage by model and role (prompt|completion)",
    ("model", "role"),
)

chat_active_streams = Gauge(
    "chat_active_streams",
    "Number of chat streams currently open",
)

conversation_messages_total = Counter(
    "conversation_messages_total",
    "Messages persisted by role",
    ("role",),
)

llm_retries_total = Counter(
    "llm_retries_total",
    "LLM retry attempts during connection establishment",
    ("model", "attempt"),
)

db_query_seconds = Histogram(
    "db_query_seconds",
    "SQLite query duration by op",
    ("op",),
    start=0.0005,
    factor=2.0,
    count=13,  # 0.5ms .. 2.048s
)

locks_contended_total = Counter(
    "locks_contended_total",
    "Concurrent requests rejected with 409 CONVERSATION_BUSY",
)

context_truncated_total = Counter(
    "context_truncated_total",
    "Times the current user message had to be truncated to fit the context budget",
)

process_resident_memory_bytes = Gauge(
    "process_resident_memory_bytes",
    "Resident memory of this process (from /proc/self/status VmRSS)",
)

_ALL_METRICS = (
    chat_requests_total,
    chat_first_token_seconds,
    chat_duration_seconds,
    chat_tokens_total,
    chat_active_streams,
    conversation_messages_total,
    llm_retries_total,
    db_query_seconds,
    locks_contended_total,
    context_truncated_total,
    process_resident_memory_bytes,
)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def render() -> str:
    """渲染全部指标为 Prometheus text format。"""
    return "".join(m.render() for m in _ALL_METRICS)


@contextmanager
def db_time(op: str):
    """计时一次 DB 操作（sync CM 包 await 区域即可，monotonic 进出各取一次）。"""
    t0 = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - t0
        db_query_seconds.observe(elapsed, op=op)
        if elapsed > 1.0:
            logger.warning(
                "slow db query op=%s elapsed=%.3fs trace_id=%s",
                op,
                elapsed,
                trace_id.get(""),
            )


def refresh_process_memory() -> None:
    """从 /proc/self/status 读取 RSS（Linux；其他平台静默跳过）。"""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    process_resident_memory_bytes.values[()] = float(kb * 1024)
                    return
    except OSError:
        pass


def reset() -> None:
    """清零所有指标（仅测试使用；生产进程内不应调用）。"""
    for m in _ALL_METRICS:
        if isinstance(m, Histogram):
            m.data.clear()
        else:
            m.values.clear()
    chat_active_streams.values[()] = 0.0
    process_resident_memory_bytes.values[()] = 0.0
