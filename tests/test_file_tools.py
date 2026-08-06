# -*- coding: utf-8 -*-
"""
文件工具模块的回归测试（零依赖，直接运行）：
    python tests/test_file_tools.py

覆盖（对齐 Hermes tools/file_tools.py）：
    - read_file：行号、分页、字符预算截断、二进制拒绝
    - write_file：新建/覆盖/自动建目录、敏感路径拒绝
    - search_files：文件名/内容匹配、敏感文件与排除目录跳过
    - _check_sensitive_path：系统目录、.env、approval_allowlist.json、~/.ssh
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from file_tools import (  # noqa: E402
    _check_sensitive_path,
    read_file_tool,
    search_files_tool,
    write_file_tool,
)


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def test_read_file() -> None:
    """read_file：行号、分页、截断与二进制拒绝。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir) / "notes.txt"
        p.write_text("\n".join(f"line-{i}" for i in range(1, 11)), encoding="utf-8")

        data = json.loads(read_file_tool(str(p)))
        check("read_file success", data["success"] is True)
        check("read_file 带行号", "1 │ line-1" in data["content"])
        check("read_file total_lines=10", data["total_lines"] == 10)
        check("read_file 默认读满不截断", data["truncated"] is False)

        data = json.loads(read_file_tool(str(p), offset=8, limit=2))
        check("read_file 分页 offset=8", "8 │ line-8" in data["content"])
        check("read_file 分页不含第 10 行之后", "10 │ line-10" not in data["content"])
        check("read_file 分页触发 truncated + hint",
              data["truncated"] is True and "继续" in data["hint"])

        missing = json.loads(read_file_tool(str(Path(tmpdir) / "nope.txt")))
        check("read_file 文件不存在 -> error", missing["success"] is False)

        binary = Path(tmpdir) / "blob.bin"
        binary.write_bytes(b"\x00\x01\x02")
        denied = json.loads(read_file_tool(str(binary)))
        check("read_file 二进制拒绝", denied["success"] is False)


def test_write_file() -> None:
    """write_file：新建、覆盖、自动建父目录与敏感路径拒绝。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        data = json.loads(write_file_tool(str(root / "a" / "b.txt"), "hello"))
        check("write_file 自动建父目录", (root / "a" / "b.txt").exists())
        check("write_file 内容正确", (root / "a" / "b.txt").read_text(encoding="utf-8") == "hello")
        check("write_file 返回 resolved_path", data["resolved_path"].endswith("b.txt"))
        check("write_file files_modified", "b.txt" in data["files_modified"][0])

        write_file_tool(str(root / "a" / "b.txt"), "overwritten")
        check("write_file 覆盖同名文件",
              (root / "a" / "b.txt").read_text(encoding="utf-8") == "overwritten")

        denied = json.loads(write_file_tool(str(root / ".env"), "KEY=secret"))
        check("write_file 拒绝 .env", denied["success"] is False)

        denied = json.loads(write_file_tool(str(root / "approval_allowlist.json"), "[]"))
        check("write_file 拒绝 approval_allowlist.json", denied["success"] is False)

        denied = json.loads(write_file_tool("~/.ssh/authorized_keys", "ssh-rsa xxx"))
        check("write_file 拒绝 ~/.ssh", denied["success"] is False)

        # 读取不再拒绝敏感文件：密钥被脱敏打码（file_read 哨兵）
        env_file = root / ".env"
        env_file.write_text("DEEPSEEK_API_KEY=sk-abcdef1234567890xyz\n", encoding="utf-8")
        read = json.loads(read_file_tool(str(env_file)))
        check("read_file 可读 .env", read["success"] is True)
        check("read_file 密钥已打码",
              "sk-abcdef1234567890xyz" not in read["content"]
              and "redacted" in read["content"])


def test_search_files() -> None:
    """search_files：文件名/内容匹配，跳过敏感与排除目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "src").mkdir()
        (root / "src" / "weather.py").write_text("def get_weather():\n    return 25\n", encoding="utf-8")
        (root / "src" / "other.txt").write_text("nothing here\n", encoding="utf-8")
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("weather in git config\n", encoding="utf-8")
        (root / ".env").write_text("DEEPSEEK_API_KEY=sk-secret-weather\n", encoding="utf-8")

        data = json.loads(search_files_tool(str(root), "weather"))
        check("search_files success", data["success"] is True)
        names = [m["path"].replace("\\", "/") for m in data["matches"]]
        check("search_files 文件名命中", "src/weather.py" in names)
        weather_entry = next(m for m in data["matches"] if "weather.py" in m["path"])
        check("search_files 内容命中",
              weather_entry.get("type") in ("content", "name+content")
              and weather_entry.get("lines"))
        check("search_files 跳过 .git", not any(".git" in n for n in names))
        check("search_files 跳过 .env", not any(n.endswith(".env") for n in names))

        data = json.loads(search_files_tool(str(root), "nothing"))
        check("search_files 其他内容命中",
              any("other.txt" in m["path"].replace("\\", "/") for m in data["matches"]))


def test_sensitive_path_checks() -> None:
    """_check_sensitive_path 的边界用例。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        check("普通文件不敏感", _check_sensitive_path(str(root / "x.txt")) is None)
        check(".env 敏感", _check_sensitive_path(str(root / ".env")) is not None)
        check("approval_allowlist.json 敏感",
              _check_sensitive_path(str(root / "approval_allowlist.json")) is not None)
        check("系统目录敏感", _check_sensitive_path("C:/Windows/System32/x.txt") is not None)
        check("~/.ssh 敏感", _check_sensitive_path("~/.ssh/known_hosts") is not None)
        check(".bashrc 敏感", _check_sensitive_path("~/.bashrc") is not None)


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 文件工具回归测试 ==")
    for test_fn in (
        test_read_file,
        test_write_file,
        test_search_files,
        test_sensitive_path_checks,
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
