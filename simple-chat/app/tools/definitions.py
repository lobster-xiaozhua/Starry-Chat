"""三个工具的 OpenAI tools 格式定义（对齐 ROADMAP v0.5 §4.1）。

工具描述只指导调用时机，不写安全规则——安全边界由沙盒 + 白名单 +
realpath + 资源上限承担（ROADMAP 不变量 §1.1）。
"""

from __future__ import annotations

from typing import Any


CALCULATE_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": (
            "用于需要精确算术、百分比或简单统计时。"
            "不要用于需要最新外部事实或需要读取文件的任务。"
            "支持运算符 + - * / ** % 与函数 sqrt/abs/round/min/max/pow。"
            "示例：{\"expression\":\"1287*0.17\"}。"
            "结果是数值或工具错误。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "maxLength": 512,
                    "description": "纯数学表达式，例如 (3+4)*2 或 sqrt(144)/2。",
                }
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    },
}


WEB_SEARCH_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "用于需要最新事实、时效性信息或可引用来源时。"
            "不要用于纯计算、读取本地文件或用户已经提供完整答案的任务。"
            "示例：{\"query\":\"StarrChat 0.2 发布时间\",\"max_results\":3}。"
            "工具不会直接联网，结果通过受控出口代理获取。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "要查询的事实或主题。",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "default": 3,
                    "description": "希望返回的结果数。",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


READ_FILE_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "用于用户明确要求查看白名单目录内的本地文本文件时。"
            "路径不明确时不要调用，先向用户询问路径。"
            "不要用于计算或外部事实。"
            "示例：{\"path\":\"reports/q3.md\",\"max_bytes\":65536}。"
            "二进制文件、.env、数据库与密钥文件会被拒绝。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "maxLength": 512,
                    "description": "相对白名单根目录的文本文件路径。",
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 262144,
                    "default": 65536,
                    "description": "希望读取的最大字节数；服务端仍会强制上限。",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


# 服务端强制策略表（ROADMAP §4.2）：timeout_ms / max_output_bytes / 网络
TOOL_POLICIES: dict[str, dict[str, Any]] = {
    "calculate": {"timeout_ms": 1000, "max_output_bytes": 4096, "network": "forbidden"},
    "read_file": {"timeout_ms": 2000, "max_output_bytes": 262144, "network": "forbidden"},
    "web_search": {"timeout_ms": 5000, "max_output_bytes": 65536, "network": "broker"},
}


ALL_DEFINITIONS: list[dict[str, Any]] = [
    CALCULATE_DEFINITION,
    WEB_SEARCH_DEFINITION,
    READ_FILE_DEFINITION,
]


def enabled_definitions(settings: Any) -> list[dict[str, Any]]:
    """按配置开关返回启用的工具定义列表。

    - TOOLS_ENABLED 关闭时返回空列表（不向模型暴露任何工具）；
    - 否则按各工具独立开关过滤。
    """
    if not settings.effective_tools_enabled:
        return []
    out: list[dict[str, Any]] = []
    if settings.tool_calculate_enabled:
        out.append(CALCULATE_DEFINITION)
    if settings.tool_web_search_enabled:
        out.append(WEB_SEARCH_DEFINITION)
    if settings.tool_read_file_enabled:
        out.append(READ_FILE_DEFINITION)
    return out
