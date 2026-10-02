#!/usr/bin/env python3
"""负载压测脚本（PR-3 改动 4）：aiohttp 异步压测，验证观测指标与容量。

用法：
  python scripts/load_test.py --concurrency 50 --duration 600 --url http://localhost:8000

流量配比：
  80%  POST /api/chat          （流式，读至 done 或 error 帧）
  10%  GET  /api/conversations （列表）
  10%  GET  /healthz

统计输出：
  QPS、P50/P95/P99 首 token 延迟、P95/P99 完整耗时、error 分布、tokens/s、
  客户端 RSS 增长、服务端 RSS 增长（经 /metrics 的 process_resident_memory_bytes）、
  "database is locked" 出现次数。

退出码：
  0  通过（P95 首 token <= 2s 且 error_rate <= 1%）
  1  未达标（P95 首 token > 2s 或 error_rate > 1%）
  2  运行前检查失败（SQLite 未开 WAL）

依赖：aiohttp（见 requirements-dev.txt），不进生产依赖。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import signal
import sqlite3
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

try:
    import aiohttp
except ImportError:
    print("ERROR: aiohttp is required (pip install -r requirements-dev.txt)", file=sys.stderr)
    sys.exit(2)


# ───────────────────────── 工具 ─────────────────────────


def pctl(values: list[float], p: float) -> float:
    """线性插值分位数。"""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    frac = k - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def read_rss_bytes() -> int:
    """读取本进程 RSS（Linux）；其他平台返回 0。"""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


async def fetch_server_rss(session: aiohttp.ClientSession, url: str) -> float:
    """经 /metrics 读取服务端 process_resident_memory_bytes。"""
    try:
        async with session.get(f"{url}/metrics") as resp:
            async for line in resp.content:
                text = line.decode("utf-8", "replace").strip()
                if text.startswith("process_resident_memory_bytes "):
                    return float(text.split()[1])
    except Exception:
        pass
    return 0.0


# ───────────────────────── 运行前检查 ─────────────────────────


def preflight_db_check(db_path: str) -> None:
    """WAL 检查：未开 WAL 的高并发写入必然撞 "database is locked"，直接拒绝压测。

    busy_timeout 由 app/db.py 每连接设置（PRAGMA busy_timeout = 5000，代码保证，
    该 PRAGMA 不落盘无法从外部校验）；WAL 是持久化的，可在此验证。
    """
    p = Path(db_path)
    if not p.exists():
        print(f"WARNING: database file not found: {db_path}（跳过 WAL 检查）")
        return
    mode = sqlite3.connect(str(p)).execute("PRAGMA journal_mode").fetchone()[0]
    if mode.lower() != "wal":
        print(
            f"WARNING: SQLite journal_mode = {mode!r}（非 WAL）。\n"
            "         高并发写入将出现 'database is locked'。请先在 app/db.py 确认\n"
            "         PRAGMA journal_mode = WAL 后再压测。",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"preflight ok: journal_mode=wal ({db_path}); busy_timeout 由 app/db.py 每连接设置 5000ms")


# ───────────────────────── 请求工作体 ─────────────────────────


class Stats:
    def __init__(self) -> None:
        self.first_token: list[float] = []
        self.full_duration: list[float] = []
        self.qps_times: list[float] = []
        self.errors: Counter[str] = Counter()
        self.locked_errors = 0
        self.tokens = 0
        self.requests = 0
        self._lock = asyncio.Lock()

    async def record(
        self,
        *,
        ok: bool,
        err: str | None = None,
        first: float | None = None,
        full: float | None = None,
        tokens: int = 0,
        locked: bool = False,
    ) -> None:
        async with self._lock:
            self.requests += 1
            self.qps_times.append(time.monotonic())
            if not ok:
                self.errors[err or "unknown"] += 1
                if locked:
                    self.locked_errors += 1
                return
            if first is not None:
                self.first_token.append(first)
            if full is not None:
                self.full_duration.append(full)
            self.tokens += tokens


def random_message() -> str:
    words = ["你好", "介绍一下自己", "写一首短诗", "今天天气如何", "讲个笑话", "翻译成英文：你好"]
    return random.choice(words)


async def chat_task(session, url, stats, deadline):
    """流式对话：读至 done / error 帧，统计首 token 与完整耗时。"""
    if time.monotonic() >= deadline:
        return
    start = time.monotonic()
    first = None
    tokens = 0
    err = None
    locked = False
    try:
        async with session.post(
            f"{url}/api/chat", json={"message": random_message()}
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                err = f"HTTP {resp.status}: {body[:120]}"
                locked = "database is locked" in body
            else:
                buf = b""
                async for chunk in resp.content.iter_any():
                    buf += chunk
                    while b"\n\n" in buf:
                        raw, buf = buf.split(b"\n\n", 1)
                        line = raw.decode("utf-8", "replace")
                        if line.startswith("event: token"):
                            if first is None:
                                first = time.monotonic() - start
                            tokens += 1
                        elif line.startswith("event: error"):
                            data = line[len("data:"):].strip()
                            err = f"SSE error {data[:120]}"
                            locked = "database is locked" in line
                        elif line.startswith("event: done"):
                            pass
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        err = f"client: {type(exc).__name__}"
    full = time.monotonic() - start
    await stats.record(ok=err is None, err=err, first=first, full=full, tokens=tokens, locked=locked)


async def conversations_task(session, url, stats, deadline):
    if time.monotonic() >= deadline:
        return
    start = time.monotonic()
    err = None
    try:
        async with session.get(f"{url}/api/chat/conversations") as resp:
            if resp.status != 200:
                err = f"HTTP {resp.status}"
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        err = f"client: {type(exc).__name__}"
    await stats.record(ok=err is None, err=err, full=time.monotonic() - start)


async def health_task(session, url, stats, deadline):
    if time.monotonic() >= deadline:
        return
    start = time.monotonic()
    err = None
    try:
        async with session.get(f"{url}/healthz") as resp:
            if resp.status != 200:
                err = f"HTTP {resp.status}"
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        err = f"client: {type(exc).__name__}"
    await stats.record(ok=err is None, err=err, full=time.monotonic() - start)


async def worker(session, url, stats, deadline, rng: random.Random):
    """单个虚拟用户：按 80/10/10 配比循环发压直到截止。"""
    tasks = {"chat": chat_task, "conv": conversations_task, "health": health_task}
    while time.monotonic() < deadline:
        roll = rng.random()
        kind = "chat" if roll < 0.8 else ("conv" if roll < 0.9 else "health")
        await tasks[kind](session, url, stats, deadline)
        # 轻微抖动，避免整步齐拍
        await asyncio.sleep(rng.uniform(0.01, 0.05))


# ───────────────────────── 主流程 ─────────────────────────


async def run(args) -> int:
    preflight_db_check(args.db)

    stats = Stats()
    deadline = time.monotonic() + args.duration
    rng = random.Random()
    connector = aiohttp.TCPConnector(limit=args.concurrency + 10)
    rss_before = read_rss_bytes()

    timeout = aiohttp.ClientTimeout(total=max(60, args.duration))
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        server_rss_before = await fetch_server_rss(session, args.url)
        print(
            f"load test: url={args.url} concurrency={args.concurrency} "
            f"duration={args.duration}s mix=80/10/10"
        )
        started = time.monotonic()
        tasks = [
            asyncio.create_task(worker(session, args.url, stats, deadline, rng))
            for _ in range(args.concurrency)
        ]
        # 周期性进度输出，Ctrl-C 可提前终止
        done_task = asyncio.gather(*tasks)
        try:
            while not done_task.done():
                await asyncio.sleep(min(10, args.duration))
                elapsed = time.monotonic() - started
                if elapsed > 0:
                    print(
                        f"  [{elapsed:6.0f}s] requests={stats.requests} "
                        f"errors={sum(stats.errors.values())} "
                        f"p50_first={pctl(stats.first_token, 50):.3f}s"
                    )
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    wall = time.monotonic() - started
    server_rss_after = await fetch_server_rss(session, args.url)
    rss_after = read_rss_bytes()

    total = stats.requests
    err_total = sum(stats.errors.values())
    err_rate = err_total / total * 100 if total else 0.0
    qps = total / wall if wall else 0.0

    print("\n════════════ 压测结果 ════════════")
    print(f"duration(s)          : {wall:.1f}")
    print(f"requests             : {total}  (QPS {qps:.1f})")
    print(f"errors               : {err_total} ({err_rate:.2f}%)")
    for code, cnt in stats.errors.most_common(10):
        print(f"    {cnt:6d}  {code}")
    print(f"'database is locked' : {stats.locked_errors}")
    print(f"tokens/s             : {stats.tokens / wall:.0f}")
    print("\nfirst token latency (chat only):")
    print(f"  P50 {pctl(stats.first_token, 50):.3f}s  P95 {pctl(stats.first_token, 95):.3f}s  P99 {pctl(stats.first_token, 99):.3f}s")
    print("full duration (all requests):")
    print(f"  P95 {pctl(stats.full_duration, 95):.3f}s  P99 {pctl(stats.full_duration, 99):.3f}s")
    print(f"\nclient RSS growth    : {(rss_after - rss_before) / 1024 / 1024:+.1f} MB")
    if server_rss_after:
        print(f"server RSS growth    : {(server_rss_after - server_rss_before) / 1024 / 1024:+.1f} MB")

    p95_first = pctl(stats.first_token, 95)
    if p95_first > 2.0:
        print(f"\nFAIL: P95 first token {p95_first:.3f}s > 2s", file=sys.stderr)
        return 1
    if err_rate > 1.0:
        print(f"\nFAIL: error rate {err_rate:.2f}% > 1%", file=sys.stderr)
        return 1
    print("\nPASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Simple Chat 负载压测")
    parser.add_argument("--concurrency", type=int, default=50, help="并发虚拟用户数（默认 50）")
    parser.add_argument("--duration", type=int, default=600, help="压测时长秒（默认 600）")
    parser.add_argument("--url", default="http://localhost:8000", help="目标服务地址")
    parser.add_argument(
        "--db", default="./data/chat.db", help="SQLite 路径（WAL 预检用，默认 ./data/chat.db）"
    )
    args = parser.parse_args()

    if args.duration <= 0 or args.concurrency <= 0:
        parser.error("--duration 与 --concurrency 必须为正数")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
