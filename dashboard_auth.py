# -*- coding: utf-8 -*-
"""用户名密码登录 + 无状态 session cookie（对齐 Hermes plugins/dashboard_auth/basic）。

Hermes 对应实现：plugins/dashboard_auth/basic/__init__.py——
  - 密码用 stdlib hashlib.scrypt 哈希（scrypt$n$r$p$salt$dk 格式），不存明文
  - 会话是无状态 HMAC-SHA256 签名 token（JSON payload + 签名后缀，base64url），
    无需服务端会话表；cookie HttpOnly + SameSite=Lax
  - 未知用户名也跑一次 dummy hash，避免"用户不存在"的时序侧信道

骨架简化（对齐 Hermes basic 的降级路径）：
  - 只签发 access session，无 refresh token 轮换（过期后重新登录即可）
  - DASHBOARD_AUTH_SECRET 未配置时每次启动随机生成，重启后旧会话失效
    （与 Hermes basic 未配 secret 时的行为一致）
  - 密码哈希由本文件 CLI 生成：python dashboard_auth.py hash-password <密码>

环境变量：
  DASHBOARD_USERNAME          登录用户名（启用人机登录的必要项）
  DASHBOARD_PASSWORD_HASH     scrypt 哈希（推荐，避免明文落盘）
  DASHBOARD_PASSWORD          明文密码（备用；配置 hash 时优先用 hash）
  DASHBOARD_AUTH_SECRET       会话签名密钥（≥16 字节；未配置则进程内随机）
  DASHBOARD_SESSION_TTL_SECONDS  会话有效期秒数（默认 12h）
  DASHBOARD_COOKIE_SECURE      HTTPS 部署时设 true，给 cookie 加 Secure
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from typing import Optional

# 与 Hermes basic 相同的 scrypt 参数（RFC 7914，交互登录推荐值，~16 MiB）
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16

# HMAC-SHA256 签名长度（二进制后缀，无分隔符，避免与载荷混淆）
_SIG_LEN = hashlib.sha256().digest_size

# 会话 cookie 名（Hermes 用 hermes_session_at；骨架单会话因此用一个名）
COOKIE_NAME = "agent_session"

_DEFAULT_TTL_SECONDS = 12 * 60 * 60  # 12h，对齐 Hermes basic

# 未配置 DASHBOARD_AUTH_SECRET 时的进程内随机密钥（重启即失效，幂等可用）
_PROCESS_SECRET = secrets.token_bytes(32)


def hash_password(password: str) -> str:
    """把明文密码转成 ``scrypt$n$r$p$<salt_b64>$<dk_b64>`` 哈希串。"""
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=0,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
    )


def _verify_password(password: str, encoded: str) -> bool:
    """常量时间校验 scrypt 哈希；哈希串格式非法返回 False。"""
    try:
        scheme, n_s, r_s, p_s, salt_b64, dk_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=0,
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


# 固定 dummy 哈希：未知用户名也跑一次 scrypt，防"用户名不存在"时序侧信道
_DUMMY_HASH = hash_password("dummy-password-for-constant-time-verify")


def _sign(payload: dict, secret: bytes) -> str:
    """把 JSON 载荷签名成 base64url token（载荷 + HMAC-SHA256 后缀）。"""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + sig).decode()


def _unsign(token: str, secret: bytes) -> Optional[dict]:
    """校验并解析签名 token；签名无效或格式非法返回 None。"""
    try:
        blob = base64.urlsafe_b64decode(token.encode())
        if len(blob) <= _SIG_LEN:
            return None
        raw, sig = blob[:-_SIG_LEN], blob[-_SIG_LEN:]
        expected = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        return json.loads(raw)
    except Exception:
        return None


# ---------------- 配置读取 ----------------


def _auth_username() -> str:
    """读取登录用户名（环境变量注入，不留空）。"""
    return os.environ.get("DASHBOARD_USERNAME", "").strip()


def _stored_password_hash() -> str:
    """读取密码哈希：优先 DASHBOARD_PASSWORD_HASH，其次明文 DASHBOARD_PASSWORD 现场哈希。"""
    encoded = os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip()
    if encoded:
        return encoded
    plain = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    return hash_password(plain) if plain else ""


def auth_enabled() -> bool:
    """人机登录是否启用：用户名与密码（hash 或明文）都配置了才算。"""
    return bool(_auth_username() and _stored_password_hash())


def _secret() -> bytes:
    """会话签名密钥：优先 DASHBOARD_AUTH_SECRET，否则进程内随机（重启失效）。"""
    configured = os.environ.get("DASHBOARD_AUTH_SECRET", "").strip()
    return configured.encode("utf-8") if configured else _PROCESS_SECRET


def session_ttl_seconds() -> int:
    """会话有效期秒数（默认 12h，下限 60s 防误配）。"""
    try:
        return max(60, int(os.environ.get("DASHBOARD_SESSION_TTL_SECONDS", _DEFAULT_TTL_SECONDS)))
    except (TypeError, ValueError):
        return _DEFAULT_TTL_SECONDS


def cookie_secure() -> bool:
    """cookie 是否加 Secure（HTTPS 部署设 DASHBOARD_COOKIE_SECURE=true）。"""
    return os.environ.get("DASHBOARD_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")


# ---------------- 登录与会话 ----------------


def complete_password_login(username: str, password: str) -> Optional[str]:
    """校验用户名/密码，成功返回 HMAC 签名会话 token，失败返回 None。

    对齐 Hermes basic：未知用户名也跑一次 dummy hash，用户名比较用常量时间，
    避免"用户不存在/密码错误"的时序侧信道。
    """
    expected_user = _auth_username()
    if not auth_enabled() or not expected_user:
        return None
    username_ok = hmac.compare_digest(
        username.encode("utf-8"), expected_user.encode("utf-8")
    )
    target_hash = _stored_password_hash() if username_ok else _DUMMY_HASH
    if not (username_ok and _verify_password(password, target_hash)):
        return None
    now = int(time.time())
    return _sign(
        {"sub": expected_user, "kind": "access", "exp": now + session_ttl_seconds()},
        _secret(),
    )


def verify_session(token: str) -> Optional[dict]:
    """校验会话 token：签名有效且未过期返回载荷，否则 None。"""
    payload = _unsign(token, _secret())
    if (
        payload is None
        or payload.get("kind") != "access"
        or int(payload.get("exp", 0)) <= int(time.time())
    ):
        return None
    return payload


def session_username(payload: dict) -> str:
    """从会话载荷取用户名。"""
    return str(payload.get("sub", ""))


def session_cookie_value(token: str, max_age: int) -> str:
    """构造 Set-Cookie 值：HttpOnly + SameSite=Lax + Max-Age；HTTPS 可开 Secure。"""
    parts = [
        f"{COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    if cookie_secure():
        parts.append("Secure")
    return "; ".join(parts)


def clear_session_cookie_value() -> str:
    """注销/过期时清 cookie（Max-Age=0）。"""
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def read_session_token(cookie_header: str) -> str:
    """从 Cookie 头解析会话 token；没有则返回空串。"""
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value
    return ""


def main(argv: Optional[list[str]] = None) -> int:
    """CLI：python dashboard_auth.py hash-password <密码> 生成 scrypt 哈希。"""
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) == 2 and args[0] == "hash-password":
        print(hash_password(args[1]))
        return 0
    print("用法：python dashboard_auth.py hash-password <密码>")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
