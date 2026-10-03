"""v0.3 多模型路由验收测试。

覆盖 ROADMAP v0.3 验收标准：
- 同一输入按规则选中预期模型（长度/关键词正则）
- 白名单外 model 被拒（422 VALIDATION_ERROR）
- 路由关闭时走默认 LLM_MODEL
- 清空 ROUTING_RULES/FALLBACK_MODELS 即空转默认模型

纯函数 + 配置 monkeypatch，不触网、不依赖 DB。
"""

import pytest

from app.config import settings
from app.errors import ValidationError
from app.routing import select_model

DEFAULT = settings.llm_model


def _cfg(monkeypatch, **kw):
    """批量 patch settings 字段，返回 None。"""
    for k, v in kw.items():
        monkeypatch.setattr(settings, k, v)


def test_explicit_whitelisted_model_wins(monkeypatch):
    _cfg(
        monkeypatch,
        routing_enabled=True,
        model_whitelist=[DEFAULT, "gpt-4o"],
        routing_rules=[],
    )
    assert select_model("任意输入", explicit_model="gpt-4o") == "gpt-4o"


def test_explicit_model_outside_whitelist_rejected(monkeypatch):
    _cfg(
        monkeypatch,
        routing_enabled=True,
        model_whitelist=[DEFAULT],
        routing_rules=[],
    )
    with pytest.raises(ValidationError):
        select_model("任意输入", explicit_model="gpt-4o")


def test_min_len_rule_matches(monkeypatch):
    _cfg(
        monkeypatch,
        routing_enabled=True,
        model_whitelist=[DEFAULT, "big-model"],
        routing_rules=[
            {"match": {"min_len": 100}, "model": "big-model"},
        ],
    )
    short = "x" * 50
    long_ = "x" * 150
    assert select_model(short) == DEFAULT
    assert select_model(long_) == "big-model"


def test_keyword_rule_matches(monkeypatch):
    _cfg(
        monkeypatch,
        routing_enabled=True,
        model_whitelist=[DEFAULT, "code-model"],
        routing_rules=[
            {"match": {"keywords": [r"代码", r"bug"]}, "model": "code-model"},
        ],
    )
    assert select_model("请帮我修这个 bug") == "code-model"
    assert select_model("今天天气不错") == DEFAULT


def test_first_matching_rule_wins(monkeypatch):
    _cfg(
        monkeypatch,
        routing_enabled=True,
        model_whitelist=[DEFAULT, "a-model", "b-model"],
        routing_rules=[
            {"match": {"min_len": 50}, "model": "a-model"},
            {"match": {"min_len": 10}, "model": "b-model"},
        ],
    )
    # 长输入命中第一条
    assert select_model("x" * 100) == "a-model"
    # 短输入不命中第一条，命中第二条
    assert select_model("x" * 20) == "b-model"
    # 极短输入都不命中
    assert select_model("x") == DEFAULT


def test_routing_disabled_falls_back_to_default(monkeypatch):
    _cfg(
        monkeypatch,
        routing_enabled=False,
        model_whitelist=[DEFAULT, "gpt-4o"],
        routing_rules=[{"match": {"min_len": 1}, "model": "gpt-4o"}],
    )
    # 路由关闭：即便规则会命中也不读，走默认
    assert select_model("任意输入") == DEFAULT


def test_empty_rules_falls_back_to_default(monkeypatch):
    _cfg(
        monkeypatch,
        routing_enabled=True,
        model_whitelist=[DEFAULT, "gpt-4o"],
        routing_rules=[],
    )
    assert select_model("任意输入") == DEFAULT


def test_rule_target_not_in_whitelist_skipped(monkeypatch):
    """配置错误的规则（target 不在白名单）应跳过，不影响可用性。"""
    _cfg(
        monkeypatch,
        routing_enabled=True,
        model_whitelist=[DEFAULT],
        routing_rules=[{"match": {"min_len": 1}, "model": "ghost-model"}],
    )
    assert select_model("任意输入") == DEFAULT


def test_invalid_regex_falls_back_to_literal(monkeypatch):
    """非法正则降级为字面量匹配，绝不抛异常。"""
    _cfg(
        monkeypatch,
        routing_enabled=True,
        model_whitelist=[DEFAULT, "lit-model"],
        routing_rules=[
            {"match": {"keywords": ["(unclosed"]}, "model": "lit-model"},
        ],
    )
    # 非法正则降级为字面量，仍能匹配字面出现 "(unclosed" 的输入
    assert select_model("here is (unclosed paren") == "lit-model"
    assert select_model("无关输入") == DEFAULT


def test_explicit_model_still_respects_whitelist_when_disabled(monkeypatch):
    """路由关闭时白名单退化为 [LLM_MODEL]，显式 model 仍校验。"""
    _cfg(
        monkeypatch,
        routing_enabled=False,
        model_whitelist=[],
    )
    # 白名单空 → 退化为 [LLM_MODEL]；显式 DEFAULT 通过
    assert select_model("任意输入", explicit_model=DEFAULT) == DEFAULT
    with pytest.raises(ValidationError):
        select_model("任意输入", explicit_model="other-model")


def test_min_len_zero_means_no_constraint(monkeypatch):
    """min_len=0 视为不限制（与省略一致）。"""
    _cfg(
        monkeypatch,
        routing_enabled=True,
        model_whitelist=[DEFAULT, "zero-model"],
        routing_rules=[{"match": {"min_len": 0}, "model": "zero-model"}],
    )
    assert select_model("x") == "zero-model"


def test_keywords_with_min_len_combined(monkeypatch):
    """min_len + keywords 同时存在：需都满足才命中。"""
    _cfg(
        monkeypatch,
        routing_enabled=True,
        model_whitelist=[DEFAULT, "combo-model"],
        routing_rules=[
            {"match": {"min_len": 50, "keywords": ["bug"]}, "model": "combo-model"},
        ],
    )
    # 短且含 bug：不命中（min_len 不满足）
    assert select_model("bug") == DEFAULT
    # 长但不含 bug：不命中（keywords 不满足）
    assert select_model("x" * 100) == DEFAULT
    # 长且含 bug：命中
    assert select_model("x" * 50 + " bug") == "combo-model"
