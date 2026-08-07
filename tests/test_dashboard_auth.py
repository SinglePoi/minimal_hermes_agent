# -*- coding: utf-8 -*-
"""用户名密码登录 + session cookie 回归测试（零依赖，直接运行）：
    python tests/test_dashboard_auth.py

覆盖：
    - scrypt 哈希往返/错误密码/非法格式
    - HMAC 会话签名往返/篡改/过期/密钥不匹配
    - complete_password_login 正确/错误/未知用户名/未启用
    - HTTP 全流程：未登录跳 /login、登录成功种 HttpOnly cookie、
      带 cookie 放行、/api/auth/me、登出清 cookie、过期会话 401、
      与 Bearer token 并存、登录失败审计
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import dashboard_auth  # noqa: E402
from test_server import ServerFixture, http_json  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随 302，方便断言跳转目标。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_raw(url: str, headers: dict | None = None, payload: dict | None = None, method: str = "GET"):
    """发请求不跟随重定向，返回 (status, headers, body)。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")


def _set_env(**kwargs) -> None:
    """临时设置环境变量（调用方负责在 finally 里清理）。"""
    for key, value in kwargs.items():
        os.environ[key] = value


def _pop_env(*keys) -> None:
    for key in keys:
        os.environ.pop(key, None)


def test_password_hashing() -> None:
    """scrypt 哈希：往返正确、错误密码/非法格式返回 False。"""
    encoded = dashboard_auth.hash_password("s3cret")
    check("哈希格式 scrypt$...", encoded.startswith("scrypt$"))
    check("正确密码校验通过", dashboard_auth._verify_password("s3cret", encoded))
    check("错误密码校验失败", not dashboard_auth._verify_password("wrong", encoded))
    check("非法格式返回 False", not dashboard_auth._verify_password("x", "not-a-hash"))


def test_session_signing() -> None:
    """HMAC 会话：往返、篡改拒绝、过期拒绝、密钥不匹配拒绝。"""
    secret = b"test-secret-32-bytes-0123456789"
    payload = {"sub": "admin", "kind": "access", "exp": int(time.time()) + 3600}
    token = dashboard_auth._sign(payload, secret)
    check("签名往返解析", dashboard_auth._unsign(token, secret) == payload)
    tampered = ("A" if token[0] != "A" else "B") + token[1:]
    check("篡改 token 拒绝", dashboard_auth._unsign(tampered, secret) is None)
    expired = dashboard_auth._sign(
        {"sub": "admin", "kind": "access", "exp": int(time.time()) - 10}, secret
    )
    check("过期载荷解析仍返回内容（过期判断在 verify_session）",
          dashboard_auth._unsign(expired, secret) is not None)
    other = dashboard_auth._sign(payload, b"another-secret-key-00000000")
    check("密钥不匹配拒绝", dashboard_auth._unsign(other, secret) is None)


def test_complete_password_login() -> None:
    """登录：正确返回 token、错误密码/未知用户/未启用返回 None。"""
    # 清掉可能来自开发者 .env 的干扰键（load_dotenv 已把它们注入 os.environ）
    _pop_env("DASHBOARD_PASSWORD_HASH", "DASHBOARD_SESSION_TTL_SECONDS")
    _set_env(
        DASHBOARD_USERNAME="admin",
        DASHBOARD_PASSWORD="s3cret",
        DASHBOARD_AUTH_SECRET="x" * 32,
    )
    try:
        token = dashboard_auth.complete_password_login("admin", "s3cret")
        check("正确用户名密码返回 token", token is not None)
        check("token 可验证", dashboard_auth.verify_session(token or "") is not None)
        check("错误密码返回 None", dashboard_auth.complete_password_login("admin", "bad") is None)
        check("未知用户名返回 None", dashboard_auth.complete_password_login("hacker", "s3cret") is None)
        check("大小写敏感", dashboard_auth.complete_password_login("Admin", "s3cret") is None)
        check("会话载荷含用户名", dashboard_auth.session_username(
            dashboard_auth.verify_session(token or "") or {}) == "admin")
    finally:
        _pop_env("DASHBOARD_USERNAME", "DASHBOARD_PASSWORD", "DASHBOARD_AUTH_SECRET")

    # 未启用：不配置用户名/密码时登录一律失败
    _pop_env("DASHBOARD_USERNAME", "DASHBOARD_PASSWORD")
    check("未启用登录返回 None", dashboard_auth.complete_password_login("a", "b") is None)


def test_login_http_flow() -> None:
    """HTTP 全流程：302 跳登录、登录种 cookie、cookie 放行、登出、过期、审计。"""
    fx = ServerFixture()
    try:
        # 基线：未配置人机登录时首页正常（无跳转）
        status, _, _ = http_raw(f"{fx.base}/")
        check("未启用登录时首页 200", status == 200)

        _set_env(
            DASHBOARD_USERNAME="admin",
            DASHBOARD_PASSWORD="s3cret",
            DASHBOARD_AUTH_SECRET="x" * 32,
        )
        audit_dir = Path(fx.tmp.name)
        os.environ["AUDIT_LOG_PATH"] = str(audit_dir / "audit.log")

        # 未登录访问首页 → 302 到 /login
        status, headers, _ = http_raw(f"{fx.base}/")
        check("未登录首页 302", status == 302)
        check("302 指向 /login", headers.get("Location") == "/login")

        # 登录页公开可访问
        status, _, body = http_raw(f"{fx.base}/login")
        check("登录页 200", status == 200)
        check("登录页含表单", "login-form" in body)

        # 探测端点：login_available=true
        cfg = http_json("GET", f"{fx.base}/api/auth/config")
        check("auth/config 报告可用登录", cfg.get("login_available") is True)

        # 未登录访问 API → 401
        status, _, _ = http_raw(f"{fx.base}/sessions")
        check("未登录 API 401", status == 401)

        # 错误密码 → 401
        status, _, _ = http_raw(
            f"{fx.base}/api/auth/login",
            method="POST",
            payload={"username": "admin", "password": "wrong"},
        )
        check("错误密码 401", status == 401)

        # 正确登录 → 200 + Set-Cookie（HttpOnly / SameSite=Lax / Max-Age）
        status, headers, body = http_raw(
            f"{fx.base}/api/auth/login",
            method="POST",
            payload={"username": "admin", "password": "s3cret"},
        )
        set_cookie = headers.get("Set-Cookie", "")
        check("登录成功 200", status == 200)
        check("返回 ok", '"ok": true' in body)
        check("Set-Cookie 含 agent_session", set_cookie.startswith("agent_session="))
        check("cookie HttpOnly", "HttpOnly" in set_cookie)
        check("cookie SameSite=Lax", "SameSite=Lax" in set_cookie)
        check("cookie Max-Age 默认 12h", "Max-Age=43200" in set_cookie)

        cookie_value = set_cookie.split(";")[0]

        # 带 cookie 访问 API → 200
        sessions = http_json(
            "GET", f"{fx.base}/sessions", headers={"Cookie": cookie_value}
        )
        check("带 cookie 会话列表 200", "sessions" in sessions)

        # /api/auth/me 返回用户名
        me = http_json("GET", f"{fx.base}/api/auth/me", headers={"Cookie": cookie_value})
        check("/api/auth/me 返回用户名", me.get("username") == "admin")
        check("/api/auth/me 返回过期时间", me.get("expires_at", 0) > 0)

        # 会话过期：手工构造过期签名的 cookie → 401（确定性，不等真实 TTL）
        expired_token = dashboard_auth._sign(
            {"sub": "admin", "kind": "access", "exp": int(time.time()) - 10},
            dashboard_auth._secret(),
        )
        status, _, _ = http_raw(
            f"{fx.base}/sessions",
            headers={"Cookie": f"agent_session={expired_token}"},
        )
        check("过期会话 401", status == 401)

        # 重新登录，测登出
        status, headers, _ = http_raw(
            f"{fx.base}/api/auth/login",
            method="POST",
            payload={"username": "admin", "password": "s3cret"},
        )
        cookie_value = headers.get("Set-Cookie", "").split(";")[0]
        status, headers, _ = http_raw(
            f"{fx.base}/api/auth/logout", method="POST"
        )
        check("登出 200", status == 200)
        check("登出清 cookie", "Max-Age=0" in headers.get("Set-Cookie", ""))

        # Bearer token 与登录并存：带 SERVER_AUTH_TOKEN 时仍可机器访问
        _set_env(SERVER_AUTH_TOKEN="machine-token")
        sessions = http_json(
            "GET", f"{fx.base}/sessions",
            headers={"Authorization": "Bearer machine-token"},
        )
        check("Bearer token 与登录并存", "sessions" in sessions)

        # 审计：登录失败/成功均有记录，含尝试用户名，且密码不打明文
        lines = (audit_dir / "audit.log").read_text(encoding="utf-8").strip().splitlines()
        entries = [json.loads(line) for line in lines]
        login_fails = [e for e in entries if e.get("action") == "auth:login" and e["status"] == 401]
        login_oks = [e for e in entries if e.get("action") == "auth:login" and e["status"] == 200]
        check("审计含登录失败", len(login_fails) >= 1)
        check("审计含登录成功", len(login_oks) >= 2)
        check("审计记录尝试用户名", login_fails[0].get("identity") == "admin")
        dump = json.dumps(entries, ensure_ascii=False)
        check("审计不打密码明文", "s3cret" not in dump)
    finally:
        _pop_env(
            "DASHBOARD_USERNAME",
            "DASHBOARD_PASSWORD",
            "DASHBOARD_AUTH_SECRET",
            "SERVER_AUTH_TOKEN",
        )
        os.environ.pop("AUDIT_LOG_PATH", None)
        fx.close()


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 用户名密码登录 + session cookie 回归测试 ==")
    for test_fn in (
        test_password_hashing,
        test_session_signing,
        test_complete_password_login,
        test_login_http_flow,
    ):
        print(f"[{test_fn.__name__}]")
        test_fn()
    print()
    if _failures:
        print(f"共 {len(_failures)} 个用例失败：")
        for label in _failures:
            print(f"  - {label}")
        sys.exit(1)
    print("全部用例通过 ✅")


if __name__ == "__main__":
    main()
