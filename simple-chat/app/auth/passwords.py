"""密码哈希：标准库 hashlib.scrypt + 随机 salt；不引入任何第三方依赖。

存储格式（单字段自描述，便于未来升级参数）：
    scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>

- salt 为 16 字节随机值（secrets.token_bytes），每个用户不同。
- 校验使用 hmac.compare_digest 常量时间比较，避免时序侧信道。
- 绝不落明文，也绝不把 hash 返回给调用方以外的层（路由只回 user_id/username）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from app.config import settings

_ALGO = "scrypt"
_SALT_BYTES = 16


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=32,
        # maxmem 默认 0 → OpenSSL 上限约 32MB；N=16384,r=8 需约 16MB，留足余量
        maxmem=64 * 1024 * 1024,
    )


def hash_password(password: str) -> str:
    """生成密码哈希串（含随机 salt 与成本参数）。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    n, r, p = settings.scrypt_n, settings.scrypt_r, settings.scrypt_p
    digest = _derive(password, salt, n, r, p)
    return f"{_ALGO}${n}${r}${p}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验密码；stored 格式非法时返回 False（不抛异常）。"""
    if not stored:
        return False
    try:
        algo, n_s, r_s, p_s, salt_s, hash_s = stored.split("$")
        if algo != _ALGO:
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _b64d(salt_s)
        expected = _b64d(hash_s)
    except (ValueError, TypeError):
        return False
    try:
        actual = _derive(password, salt, n, r, p)
    except (ValueError, OverflowError):
        return False
    return hmac.compare_digest(actual, expected)


__all__ = ["hash_password", "verify_password"]
