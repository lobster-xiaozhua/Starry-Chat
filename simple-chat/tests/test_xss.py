"""T2（PR-2 改动 2 Layer C）：XSS 注入向量 → escapeFallback 净化断言。

前端净化器是 JS（app.js 的 escapeFallback），故用 node 执行 tests/xss_harness.cjs：
从 app.js 提取真实实现，加载 vendor/marked.min.js，走「markdown 渲染 → 兜底净化」
完整链路后输出 JSON，这里对结果做断言。

断言：
- 注入向量渲染后不含 onload / onerror / ontoggle / javascript: 协议
- 不含 <svg / <math / <details / <iframe 标签
- 反例（不得误杀）：<pre><code class="language-js">…</code></pre> 的 class 与引号保留
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "web" / "static" / "app.js"
MARKED_JS = ROOT / "app" / "web" / "static" / "vendor" / "marked.min.js"
HARNESS = ROOT / "tests" / "xss_harness.cjs"

# 渲染后的 innerHTML 不得包含的危险片段
DANGEROUS_RE = r"onload|onerror|ontoggle|javascript:|<svg|<math|<details|<iframe"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available for JS sanitizer test"
)


def _run_harness() -> list[dict]:
    proc = subprocess.run(
        ["node", str(HARNESS), str(APP_JS), str(MARKED_JS)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        pytest.fail(f"xss harness failed: {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_xss_vectors():
    """6 个注入向量经渲染 + 兜底净化后不得残留可执行内容。"""
    results = _run_harness()
    assert len(results) == 7  # 6 个注入向量 + 1 个反例
    for item in results[:6]:
        out = item["out"]
        assert re.search(DANGEROUS_RE, out, re.IGNORECASE) is None, (
            f"vector not sanitized: {item}"
        )


def test_benign_code_block_not_over_stripped():
    """反例：pre/code 的 class 与引号必须保留（不得误杀合法代码块）。"""
    results = _run_harness()
    out = results[-1]["out"]
    assert 'class="language-js"' in out
    assert "<pre>" in out and "<code" in out
