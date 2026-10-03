"""v0.3 纯规则路由：按消息长度/关键词正则映射到白名单内的模型 id。

设计约束（对齐 ROADMAP v0.3 MVI）：
- 禁止用模型做路由：本模块零 LLM 调用，纯字符串/正则匹配。
- 优先级：请求显式白名单 model > ROUTING_RULES（按顺序首条命中）> 默认 LLM_MODEL。
- 模型 id 必须在白名单内，否则拒绝（ValidationError）。
- 路由关闭时（settings.effective_routing_enabled=False）一律走默认 LLM_MODEL，
  不读 rules、不做白名单二次校验（仍校验显式 model 在白名单内，但白名单退化为
  [LLM_MODEL]）。

路由规则条目格式（pydantic 兼容 dict）：
    {
      "match": {"min_len": 200, "keywords": ["代码", "bug"]},
      "model": "gpt-4o"
    }
match 字段均可省略；min_len 为消息字符长度下限，keywords 为正则列表（任一命中即触发）。
"""

from __future__ import annotations

import re

from app.config import settings
from app.errors import ValidationError


def _matches(rule: dict, message: str) -> bool:
    """单条规则是否命中。match 字段全部可选；空 match 视为永真（兜底）。"""
    match = rule.get("match") or {}
    min_len = match.get("min_len")
    if isinstance(min_len, int) and min_len > 0 and len(message) < min_len:
        return False
    keywords = match.get("keywords") or []
    if keywords:
        # 任一关键词正则命中即触发；非法正则降级为字面量匹配，绝不抛异常。
        for kw in keywords:
            try:
                if re.search(kw, message):
                    return True
            except re.error:
                if kw in message:
                    return True
        return False
    # 既无 min_len 限制又无 keywords：空 match 永真
    if not isinstance(min_len, int) or min_len <= 0:
        return True
    # 仅有 min_len 且已满足
    return True


def select_model(
    message: str,
    *,
    explicit_model: str | None = None,
) -> str:
    """选择实际调用的模型 id。

    返回值必在 settings.effective_model_whitelist 内；显式 model 不在白名单时
    抛 ValidationError（422）。路由关闭时仅校验白名单（退化为 [LLM_MODEL]）。
    """
    whitelist = settings.effective_model_whitelist

    # 1) 请求显式 model：必须在白名单内
    if explicit_model:
        if explicit_model not in whitelist:
            raise ValidationError(
                f"model {explicit_model!r} 不在白名单内"
            )
        return explicit_model

    # 2) 路由关闭：走默认模型
    if not settings.effective_routing_enabled:
        return settings.llm_model

    # 3) 按顺序匹配首条命中规则
    for rule in settings.effective_routing_rules:
        target = rule.get("model")
        if not target:
            continue
        if target not in whitelist:
            # 配置错误的规则跳过，不抛异常（不影响可用性）
            continue
        if _matches(rule, message):
            return target

    # 4) 兜底默认模型
    return settings.llm_model


__all__ = ["select_model"]
