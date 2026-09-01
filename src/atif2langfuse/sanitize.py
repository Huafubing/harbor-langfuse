# -*- coding: utf-8 -*-
"""脱敏层。

structural 模式实现"保结构弃内容"：
- dict 的键（字段名）保留，字符串叶子值替换为 {"_len": 长度, "_sha256_8": 哈希前8位}
- 数字/布尔原样保留（token 数、判分、耗时等结构信息）
- full 模式仅做超长截断，适用于内网调试
"""
from __future__ import annotations

import hashlib
from typing import Any

MAX_FULL_LEN = 65536


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]


def sanitize(value: Any, mode: str = "structural", _depth: int = 0) -> Any:
    if mode == "full":
        if isinstance(value, str) and len(value) > MAX_FULL_LEN:
            return value[:MAX_FULL_LEN] + "...[truncated %d chars]" % len(value)
        return value

    if _depth > 12:
        return {"_truncated": True}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if value == "":
            return ""
        return {"_len": len(value), "_sha256_8": _sha8(value)}
    if isinstance(value, dict):
        return {str(k): sanitize(v, mode, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, mode, _depth + 1) for v in value]
    return {"_repr": type(value).__name__}
