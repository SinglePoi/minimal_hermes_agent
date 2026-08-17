# -*- coding: utf-8 -*-
"""
文件工具模块的回归测试（零依赖，直接运行）：
    python tests/test_file_tools.py

覆盖（对齐 Hermes tools/file_tools.py）：
    - read_file：行号、分页、字符预算截断、二进制拒绝
    - write_file：新建/覆盖/自动建目录、敏感路径拒绝
    - search_files：文件名/内容匹配、敏感文件与排除目录跳过
    - _check_sensitive_path：系统目录、.env、approval_allowlist.json、~/.ssh
    - patch：replace 模式（唯一/多次/replace_all/已应用/CRLF）+ 模糊匹配兜底
    - V4A 补丁：Update/Add/Delete/Move、多 hunk 已应用跳过、校验失败零写入、
      .. 穿越/敏感路径拒绝、陈旧检测（对齐 Hermes patch_parser/fuzzy_match/file_state）
    - WORKSPACE_ROOT：出界拒绝、相对路径锚定、未配置不限制
"""

import json
import os
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
    _check_workspace_path,
    _v4a_apply,
    _v4a_validate,
    fuzzy_find_and_replace,
    is_already_applied,
    patch_file_tool,
    parse_v4a_patch,
    read_file_tool,
    search_files_tool,
    write_file_tool,
)

# 现有用例在临时目录操作；测试进程内先拿掉开发者本机的 WORKSPACE_ROOT
_SAVED_WORKSPACE_ROOT = os.environ.pop("WORKSPACE_ROOT", None)


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


def test_patch_file() -> None:
    """patch：唯一替换、多次出现报错、replace_all、已应用 no-op、敏感拒绝、CRLF。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        target = root / "app.py"
        target.write_text("def old():\n    return 1\n", encoding="utf-8")

        data = json.loads(patch_file_tool(str(target), "def old():", "def new():"))
        check("patch 唯一替换成功", data["success"] is True and data["replaced"] == 1)
        check("patch 内容已变",
              target.read_text(encoding="utf-8") == "def new():\n    return 1\n")
        check("patch 返回 files_modified", "app.py" in data["files_modified"][0])

        # 找不到 → 报错
        data = json.loads(patch_file_tool(str(target), "def missing():", "x"))
        check("patch 找不到 old_string -> error", data["success"] is False)

        # 已应用检测：old 已不在、new 已在 → no_change 成功
        data = json.loads(patch_file_tool(str(target), "def old():", "def new():"))
        check("patch 已应用 -> no_change",
              data["success"] is True and data.get("no_change") is True)

        # 多次出现：不唯一报错，replace_all 后全部替换
        multi = root / "multi.txt"
        multi.write_text("a\nb\na\n", encoding="utf-8")
        data = json.loads(patch_file_tool(str(multi), "a", "X"))
        check("patch 多次出现不唯一 -> error", data["success"] is False)
        data = json.loads(patch_file_tool(str(multi), "a", "X", replace_all=True))
        check("patch replace_all 全部替换",
              data["success"] is True and data["replaced"] == 2
              and multi.read_text(encoding="utf-8") == "X\nb\nX\n")

        # 敏感路径拒绝
        env = root / ".env"
        env.write_text("KEY=1\n", encoding="utf-8")
        data = json.loads(patch_file_tool(str(env), "KEY=1", "KEY=2"))
        check("patch 拒绝 .env", data["success"] is False)

        # CRLF 文件里多行匹配（归一化后能找到）
        crlf = root / "win.txt"
        crlf.write_bytes(b"line1\r\nline2\r\nline3\r\n")
        data = json.loads(patch_file_tool(
            str(crlf), "line1\nline2", "line1\nline2-X"
        ))
        check("patch CRLF 多行匹配", data["success"] is True)
        check("patch 保留 CRLF 行尾",
              crlf.read_bytes() == b"line1\r\nline2-X\r\nline3\r\n")


def test_fuzzy_match() -> None:
    """模糊匹配策略链：缩进差异/空白折叠/无匹配/相似度 replace_all 拒绝。"""
    content = "def foo():\n    return 1\n"
    new_content, count, strategy, err = fuzzy_find_and_replace(
        content, "def foo():\n  return 1", "def bar():\n  return 2"
    )
    check("模糊：缩进差异 line_trimmed 命中",
          count == 1 and strategy == "line_trimmed" and err is None)
    check("模糊：替换内容正确", new_content == "def bar():\n  return 2\n")

    n2, c2, s2, _e2 = fuzzy_find_and_replace(
        "a = b   +   c\n", "b + c", "b - c"
    )
    check("模糊：空白折叠命中", c2 == 1 and s2 == "whitespace_normalized")
    check("模糊：空白折叠替换正确", n2 == "a = b - c\n")

    _n3, c3, _s3, _e3 = fuzzy_find_and_replace("hello world", "totally different", "x")
    check("模糊：完全无关文本不匹配", c3 == 0)

    _n4, c4, _s4, _e4 = fuzzy_find_and_replace("x\nx\n", "y", "z", replace_all=True)
    check("模糊：相似度策略不误匹配", c4 == 0)

    check("已应用判定 True",
          is_already_applied("def new():\n    pass\n", "def old():", "def new():\n    pass"))
    check("已应用判定短文本 False", is_already_applied("x", "a", "b") is False)


def test_patch_fuzzy_fallback() -> None:
    """replace 模式找不到精确文本时走模糊兜底。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "x.py"
        target.write_text("def foo():\n    return 1\n", encoding="utf-8")
        data = json.loads(patch_file_tool(
            str(target), "def foo():\n  return 1", "def bar():\n  return 2"
        ))
        check("replace 模糊兜底成功",
              data["success"] is True and data.get("strategy") == "line_trimmed")
        check("replace 模糊内容正确",
              target.read_text(encoding="utf-8") == "def bar():\n  return 2\n")


def test_v4a_parse() -> None:
    """V4A 解析：四类操作、CRLF、无定界、空补丁、解析错误。"""
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@ hint @@\n"
        " old\n"
        "-x\n"
        "+y\n"
        "*** Add File: b.py\n"
        "+content\n"
        "*** Delete File: c.py\n"
        "*** Move File: d.py -> e.py\n"
        "*** End Patch\n"
    )
    ops, err = parse_v4a_patch(patch)
    check("V4A 解析四操作", err is None and len(ops) == 4)
    check("V4A 操作类型顺序",
          [op.operation.value for op in ops] == ["update", "add", "delete", "move"])
    check("V4A Move 目标路径", ops[3].new_path == "e.py")
    check("V4A Add 内容行", [ln.content for ln in ops[1].hunks[0].lines] == ["content"])

    ops2, err2 = parse_v4a_patch(patch.replace("\n", "\r\n"))
    check("V4A CRLF 补丁解析", err2 is None and len(ops2) == 4)

    ops3, err3 = parse_v4a_patch("*** Update File: a.py\n-x\n+y\n")
    check("V4A 无 Begin/End 定界解析", err3 is None and len(ops3) == 1)

    ops4, err4 = parse_v4a_patch("nothing here")
    check("V4A 空补丁 -> 空列表", ops4 == [] and err4 is None)

    ops5, err5 = parse_v4a_patch("*** Begin Patch\n*** Update File: a.py\n*** End Patch\n")
    check("V4A UPDATE 无 hunk 报错", ops5 == [] and err5 is not None)
    ops6, err6 = parse_v4a_patch("*** Begin Patch\n*** Move File: a.py\n*** End Patch\n")
    check("V4A 畸形 Move 行 -> 空操作（与 Hermes 一致）", ops6 == [] and err6 is None)


def test_v4a_apply() -> None:
    """V4A 应用：多操作、Move、多 hunk 已应用跳过、校验失败零写入、无改动报错。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        app = root / "app.py"
        app.write_text("def old():\n    return 1\n", encoding="utf-8")
        extra = root / "extra.txt"
        extra.write_text("x\n", encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            f"*** Update File: {app}\n"
            " def old():\n"
            "-    return 1\n"
            "+    return 2\n"
            f"*** Add File: {root / 'new.py'}\n"
            "+print(1)\n"
            f"*** Delete File: {extra}\n"
            "*** End Patch\n"
        )
        data = json.loads(patch_file_tool(
            mode="patch", patch=patch, path="", old_string="", new_string=""
        ))
        check("V4A 多操作成功", data["success"] is True)
        check("V4A files_modified", "app.py" in data["files_modified"][0])
        check("V4A files_created", "new.py" in data["files_created"][0])
        check("V4A files_deleted", "extra.txt" in data["files_deleted"][0])
        check("V4A 内容已改", app.read_text(encoding="utf-8") == "def old():\n    return 2\n")
        check("V4A 新文件已建", (root / "new.py").read_text(encoding="utf-8") == "print(1)")
        check("V4A 旧文件已删", not extra.exists())
        check("V4A diff 含更新", "return 2" in data.get("diff", ""))

        patch2 = (
            f"*** Begin Patch\n"
            f"*** Move File: {app} -> {root / 'src/app.py'}\n"
            "*** End Patch\n"
        )
        data2 = json.loads(patch_file_tool(
            mode="patch", patch=patch2, path="", old_string="", new_string=""
        ))
        check("V4A Move 成功", data2["success"] is True)
        check("V4A Move 目录自动建", (root / "src/app.py").exists() and not app.exists())

        before = (root / "src/app.py").read_text(encoding="utf-8")
        patch3 = (
            f"*** Begin Patch\n"
            f"*** Update File: {root / 'src/app.py'}\n"
            "- def nonexistent():\n"
            "+ x\n"
            "*** End Patch\n"
        )
        data3 = json.loads(patch_file_tool(
            mode="patch", patch=patch3, path="", old_string="", new_string=""
        ))
        check("V4A 校验失败", data3["success"] is False and "not found" in data3.get("error", ""))
        check("V4A 校验失败零写入",
              (root / "src/app.py").read_text(encoding="utf-8") == before)

        patch4 = (
            f"*** Begin Patch\n"
            f"*** Update File: {root / 'src/app.py'}\n"
            " def old():\n"
            "*** End Patch\n"
        )
        data4 = json.loads(patch_file_tool(
            mode="patch", patch=patch4, path="", old_string="", new_string=""
        ))
        check("V4A 纯上下文无改动报错",
              data4["success"] is False and "no changes" in data4.get("error", ""))

        app2 = root / "app2.py"
        app2.write_text("line1\nline2\nline3\n", encoding="utf-8")
        patch5 = (
            "*** Begin Patch\n"
            f"*** Update File: {app2}\n"
            "@@ hunk1 @@\n"
            " line1\n"
            "-line2\n"
            "+line2-X\n"
            " line3\n"
            "@@ hunk2 already applied @@\n"
            " line1\n"
            "-line2\n"
            "+line2-X\n"
            " line3\n"
            "*** End Patch\n"
        )
        data5 = json.loads(patch_file_tool(
            mode="patch", patch=patch5, path="", old_string="", new_string=""
        ))
        check("V4A 多 hunk 已应用跳过", data5["success"] is True
              and app2.read_text(encoding="utf-8") == "line1\nline2-X\nline3\n")


def test_v4a_security() -> None:
    """V4A 安全：.. 穿越拒绝、敏感路径拒绝、Move 两端检查、绝对路径允许。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        data = json.loads(patch_file_tool(
            mode="patch",
            patch="*** Begin Patch\n*** Update File: ../escape.txt\n- a\n+ b\n*** End Patch\n",
            path="", old_string="", new_string="",
        ))
        check("V4A .. 穿越拒绝", data["success"] is False and "traversal" in data.get("error", ""))

        data2 = json.loads(patch_file_tool(
            mode="patch",
            patch="*** Begin Patch\n*** Add File: .env\n+KEY=x\n*** End Patch\n",
            path="", old_string="", new_string="",
        ))
        check("V4A 敏感路径拒绝",
              data2["success"] is False and "sensitive" in data2.get("error", "").lower())

        data3 = json.loads(patch_file_tool(
            mode="patch",
            patch="*** Begin Patch\n*** Move File: a.txt -> ../escape.txt\n*** End Patch\n",
            path="", old_string="", new_string="",
        ))
        check("V4A Move 目标穿越拒绝", data3["success"] is False)

        target = root / "abs.txt"
        target.write_text("a\n", encoding="utf-8")
        data4 = json.loads(patch_file_tool(
            mode="patch",
            patch=f"*** Begin Patch\n*** Update File: {target}\n-a\n+b\n*** End Patch\n",
            path="", old_string="", new_string="",
        ))
        check("V4A 绝对路径允许", data4["success"] is True
              and target.read_text(encoding="utf-8") == "b\n")


def test_v4a_stale_detection() -> None:
    """陈旧检测：校验后文件被外部修改，应用阶段拦截且不覆盖外部内容。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "stale.py"
        target.write_text("aaa\n", encoding="utf-8")
        patch = f"*** Begin Patch\n*** Update File: {target}\n-aaa\n+bbb\n*** End Patch\n"
        ops, err = parse_v4a_patch(patch)
        check("陈旧测试 parse ok", err is None and len(ops) == 1)
        validation_errors, mtimes = _v4a_validate(ops)
        check("陈旧测试 validate ok", validation_errors == [])
        target.write_text("external change\n", encoding="utf-8")  # 模拟校验后被外部修改
        # 显式改 mtime，避免两次快速写入撞同一时间戳（Windows NTFS 粒度抖动）
        os.utime(target, ns=(target.stat().st_atime_ns, 1))
        result = _v4a_apply(ops, mtimes)
        check("陈旧检测拦截", result["success"] is False and "陈旧检测" in result.get("error", ""))
        check("陈旧检测不覆盖外部修改",
              target.read_text(encoding="utf-8") == "external change\n")


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


def test_workspace_root() -> None:
    """WORKSPACE_ROOT：出界拒绝、相对路径锚定到工作区、未配置不限制。"""
    old = os.environ.get("WORKSPACE_ROOT")
    old_cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            ws = base / "ws"
            outside = base / "outside"
            ws.mkdir()
            outside.mkdir()
            (ws / "ok.txt").write_text("inside\n", encoding="utf-8")
            (outside / "secret.txt").write_text("leaked\n", encoding="utf-8")
            os.environ["WORKSPACE_ROOT"] = str(ws)

            data = json.loads(read_file_tool(str(ws / "ok.txt")))
            check("工作区内可读", data["success"] is True and "inside" in data["content"])

            data = json.loads(read_file_tool(str(outside / "secret.txt")))
            check(
                "工作区外 read 拒绝",
                data["success"] is False
                and "outside the allowed workspace" in data.get("error", ""),
            )

            data = json.loads(write_file_tool(str(outside / "x.txt"), "nope"))
            check("工作区外 write 拒绝", data["success"] is False)
            check("工作区外未落盘", not (outside / "x.txt").exists())

            data = json.loads(write_file_tool(str(ws / "new.txt"), "ok"))
            check(
                "工作区内 write 允许",
                data["success"] is True
                and (ws / "new.txt").read_text(encoding="utf-8") == "ok",
            )

            data = json.loads(search_files_tool(str(outside), "leaked"))
            check("工作区外 search 拒绝", data["success"] is False)

            data = json.loads(search_files_tool(str(ws), "inside"))
            check("工作区内 search 允许", data["success"] is True)

            os.chdir(str(outside))
            data = json.loads(read_file_tool("ok.txt"))
            check("相对路径锚定工作区", data["success"] is True and "inside" in data["content"])

            data = json.loads(read_file_tool(str(Path("..") / "outside" / "secret.txt")))
            check("相对路径逃逸拒绝", data["success"] is False)
            os.chdir(old_cwd)

            data = json.loads(patch_file_tool(str(outside / "secret.txt"), "leaked", "changed"))
            check("工作区外 patch 拒绝", data["success"] is False)

            data = json.loads(patch_file_tool(
                mode="patch",
                patch=(
                    "*** Begin Patch\n"
                    f"*** Add File: {outside / 'evil.txt'}\n"
                    "+x\n"
                    "*** End Patch\n"
                ),
                path="", old_string="", new_string="",
            ))
            check(
                "工作区外 V4A 拒绝",
                data["success"] is False
                and "outside the allowed workspace" in data.get("error", ""),
            )
            check("工作区外 V4A 未落盘", not (outside / "evil.txt").exists())

            check(
                "_check_workspace_path 出界有消息",
                _check_workspace_path(str(outside / "secret.txt")) is not None,
            )
            check(
                "_check_workspace_path 区内 None",
                _check_workspace_path(str(ws / "ok.txt")) is None,
            )

            os.environ.pop("WORKSPACE_ROOT", None)
            data = json.loads(read_file_tool(str(outside / "secret.txt")))
            check(
                "未配置 WORKSPACE_ROOT 不限制",
                data["success"] is True and "leaked" in data["content"],
            )
    finally:
        os.chdir(old_cwd)
        if old is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = old


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 文件工具回归测试 ==")
    for test_fn in (
        test_read_file,
        test_write_file,
        test_search_files,
        test_patch_file,
        test_fuzzy_match,
        test_patch_fuzzy_fallback,
        test_v4a_parse,
        test_v4a_apply,
        test_v4a_security,
        test_v4a_stale_detection,
        test_sensitive_path_checks,
        test_workspace_root,
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
    try:
        main()
    finally:
        if _SAVED_WORKSPACE_ROOT is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = _SAVED_WORKSPACE_ROOT
