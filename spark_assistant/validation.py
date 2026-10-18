"""Shared input contracts; independent of browser, Qt and storage."""
import re


def normalize_recipient(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请选择或填写好友备注")
    value = value.strip()
    if len(value) > 200:
        raise ValueError("好友备注不能超过 200 字符")
    return value


def validate_message(value: object) -> str:
    if not isinstance(value, str) or not re.search(r"[^\s\u200b\ufeff]", value):
        raise ValueError("消息不能为空")
    if len(value) > 500 or "\n" in value or "\r" in value:
        raise ValueError("消息仅支持不超过 500 字符的单行文字")
    return value
