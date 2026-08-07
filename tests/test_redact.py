# -*- coding: utf-8 -*-
"""
敏感文本脱敏模块的回归测试（零依赖，直接运行）：
    python tests/test_redact.py

覆盖（对齐 Hermes agent/redact.py）：
    - mask_secret / _mask_token 打码格式
    - 前缀密钥（sk-、ghp_、glpat- 等）、环境变量、JSON、YAML、请求头、
      私钥、JWT、URL userinfo
    - file_read 哨兵（不可复用、不泄露头尾）
    - force 开关与 code_file 跳过赋值类规则
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import redact  # noqa: E402
from redact import mask_secret, redact_sensitive_text  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def test_mask_helpers() -> None:
    """mask_secret / _mask_token 的格式。"""
    check("mask_secret 长密钥保留头尾",
          mask_secret("sk-proj-abcdef1234567890") == "sk-p...7890")
    check("mask_secret 短值整体替换", mask_secret("short") == "***")
    check("mask_secret 空值返回空串", mask_secret("") == "")
    token = redact._mask_token("sk-proj-abcdef1234567890")
    check("_mask_token 头6尾4", token == "sk-pro" + "..." + "7890")


def test_prefix_keys() -> None:
    """已知前缀密钥打码。"""
    out = redact_sensitive_text("我的 key 是 sk-abcdef1234567890xyz 别外传")
    check("sk- 密钥被打码", "sk-abcdef1234567890xyz" not in out and "..." in out)
    check("sk- 打码保留头6尾4", "sk-abc" in out and "0xyz" in out)

    out = redact_sensitive_text("token=ghp_ABCDEFGHIJKLMNOPQRST123456")
    check("ghp_ GitHub token 被打码", "ghp_ABCDEFGHIJKLMNOPQRST123456" not in out)

    out = redact_sensitive_text("AIzaSyA1234567890abcdefghijklmnopqrstuvwxyz")
    check("AIza Google key 被打码", "AIzaSyA1234567890" not in out)


def test_env_json_yaml() -> None:
    """环境变量 / JSON / YAML 赋值打码，普通键与代码引用不误伤。"""
    out = redact_sensitive_text(
        "DEEPSEEK_API_KEY=sk-abcdef1234567890\nMODEL=deepseek-chat"
    )
    check("DEEPSEEK_API_KEY 值被打码", "sk-abcdef1234567890" not in out)
    check("普通键 MODEL 不动", "MODEL=deepseek-chat" in out)

    out = redact_sensitive_text('{"apiKey": "sk-abcdef1234567890", "name": "骨架"}')
    check("JSON apiKey 打码", "sk-abcdef1234567890" not in out and '"name": "骨架"' in out)

    out = redact_sensitive_text("password: hunter2\nport: 8080")
    check("YAML password 打码", "hunter2" not in out and "port: 8080" in out)

    out = redact_sensitive_text("KEY = os.getenv(\"DEEPSEEK_API_KEY\")")
    check("os.getenv 引用变量名不误伤", "os.getenv" in out)

    out = redact_sensitive_text("DEEPSEEK_API_KEY=plainvalue123", code_file=True)
    check("code_file 跳过赋值规则（非前缀值）", "DEEPSEEK_API_KEY=plainvalue123" in out)
    out = redact_sensitive_text("DEEPSEEK_API_KEY=plainvalue123")
    check("非 code_file 下同一值被赋值规则打码", "plainvalue123" not in out)
    out = redact_sensitive_text(
        "DEEPSEEK_API_KEY=sk-abcdef1234567890", code_file=True
    )
    check("code_file 下前缀密钥仍打码（对齐 Hermes）",
          "sk-abcdef1234567890" not in out)


def test_headers_keys_jwt_url() -> None:
    """请求头 / 私钥 / JWT / URL userinfo 打码。"""
    out = redact_sensitive_text("Authorization: Bearer abcdef1234567890xyz")
    check("Authorization Bearer 打码", "abcdef1234567890xyz" not in out)
    check("Authorization 保留认证方式", "Authorization: Bearer" in out)

    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact_sensitive_text(pem)
    check("私钥块整体替换", "MIIEowIBAAKCAQEA" not in out and "redacted" in out)

    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.signature123"
    out = redact_sensitive_text(f"jwt={jwt}")
    check("JWT 打码", jwt not in out)

    out = redact_sensitive_text("https://admin:s3cret@api.example.com/v1")
    check("URL userinfo 密码打码", "s3cret" not in out and "admin:***@" in out)


def test_special_redaction() -> None:
    """脱敏专项：DB 连接串 / 手机号 / URL 查询参数。"""
    # DB 连接串：只打码密码
    out = redact_sensitive_text(
        "postgres://app:hunter2@db.example.com:5432/prod"
    )
    check("postgres 连接串密码打码", "hunter2" not in out and "app:***@" in out)
    out = redact_sensitive_text("redis://:r3dispass@cache:6379/0")
    check("redis 连接串密码打码", "r3dispass" not in out)
    out = redact_sensitive_text(
        "mongodb+srv://admin:secret123@cluster.mongodb.net/db"
    )
    check("mongodb+srv 连接串密码打码", "secret123" not in out)
    out = redact_sensitive_text("https://example.com/index")
    check("普通 https 不带密码不动", "example.com/index" in out)

    # 手机号
    out = redact_sensitive_text("联系电话 13812345678，别外传")
    check("大陆手机号打码", "13812345678" not in out and "138****5678" in out)
    out = redact_sensitive_text("国际号码 +8613812345678")
    check("E.164 手机号打码", "+8613812345678" not in out and "+86****5678" in out)
    out = redact_sensitive_text("版本号 1.0.20260807 正常")
    check("日期数字不误伤", "20260807" in out)

    # URL 查询参数
    out = redact_sensitive_text(
        "https://example.com/cb?code=ABC123&state=xyz&token=sekret"
    )
    check("查询参数 code/token 打码", "ABC123" not in out and "sekret" not in out)
    check("非敏感参数 state 保留", "state=xyz" in out)
    out = redact_sensitive_text("https://example.com/search?q=hello&limit=10")
    check("非敏感查询串不动", "q=hello&limit=10" in out)
    out = redact_sensitive_text(
        "https://example.com/api?token_count=3&session_id=abc"
    )
    check("token_count/session_id 不误伤",
          "token_count=3" in out and "session_id=abc" in out)
    out = redact_sensitive_text(
        "https://example.com/cb?access_token=xxx&x-amz-signature=yyy"
    )
    check("access_token/x-amz-signature 打码", "xxx" not in out and "yyy" not in out)


def test_file_read_sentinel() -> None:
    """file_read 模式：不可复用哨兵，不保留密钥字节。"""
    out = redact_sensitive_text(
        "DEEPSEEK_API_KEY=sk-abcdef1234567890xyz", file_read=True
    )
    check("file_read 打码环境变量值",
          "sk-abcdef1234567890xyz" not in out and "«redacted:sk-…»" in out)

    out = redact_sensitive_text("token sk-abcdef1234567890xyz", file_read=True)
    check("file_read 前缀密钥 -> 哨兵", "«redacted:sk-…»" in out)
    check("file_read 哨兵不泄露字节",
          "sk-ab" not in out and "90xyz" not in out)


def test_force_and_switch() -> None:
    """force 强制打码；关闭开关后非 force 原样返回。"""
    original = redact._REDACT_ENABLED
    try:
        redact._REDACT_ENABLED = False
        out = redact_sensitive_text("sk-abcdef1234567890xyz")
        check("开关关闭 -> 不处理", "sk-abcdef1234567890xyz" in out)
        out = redact_sensitive_text("sk-abcdef1234567890xyz", force=True)
        check("开关关闭 + force -> 仍打码", "sk-abcdef1234567890xyz" not in out)
    finally:
        redact._REDACT_ENABLED = original


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 敏感脱敏回归测试 ==")
    for test_fn in (
        test_mask_helpers,
        test_prefix_keys,
        test_env_json_yaml,
        test_headers_keys_jwt_url,
        test_special_redaction,
        test_file_read_sentinel,
        test_force_and_switch,
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
