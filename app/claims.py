"""分片认领令牌支持。

客户端可在 ``POST /api/v1/sessions/{id}/chunk-claims`` 上为尚未接收的
半开区间申请一个临时独占令牌，并在 ``PUT /chunks`` 时通过 ``X-Chunk-Claim``
头出示。与下载票据/幂等键一致，服务端**不**保存令牌明文，只保存
``SHA-256(token)``，明文仅在创建响应中返回这一次。

令牌为 URL-safe 可见 ASCII 串（无空白，可安全放入 HTTP 头），长度 1～128。
"""
from __future__ import annotations

import hashlib
import re

# 与幂等键同一取值域：可见 ASCII 0x21(!)–0x7E(~)；头内无空白，可直接透传。
_TOKEN_RE = re.compile(r"[!-~]{1,128}")

CLAIM_REQUIREMENT = "1-128 visible ASCII characters (0x21-0x7E)"


def is_valid_token(raw: str) -> bool:
    """令牌格式校验：1～128 个可见 ASCII 字符。"""
    return bool(_TOKEN_RE.fullmatch(raw))


def token_hash(token: str) -> str:
    """令牌的 SHA-256（十六进制）；明文令牌绝不落库。"""
    return hashlib.sha256(token.encode("ascii")).hexdigest()
