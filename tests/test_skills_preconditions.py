# -*- coding: utf-8 -*-
"""
Skills 前置条件检查的回归测试（零依赖，直接运行）：
    python tests/test_skills_preconditions.py

覆盖（对齐 Hermes agent/skill_utils.py::extract_skill_conditions +
agent/prompt_builder.py::_skill_should_show + tools/skills_tool.py）：
    - 嵌套 frontmatter 解析：prerequisites / metadata.hermes / 块式列表
    - 条件激活过滤：requires_tools / fallback_for_tools / toolsets / 向后兼容
    - 索引与 skills_list 的条件过滤
    - 加载期前置检查：env 缺失 → setup_needed；commands 仅 advisory
    - skill_view 的 readiness 字段与 .env 兜底查询
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

import skills  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def test_nested_frontmatter() -> None:
    """嵌套 frontmatter：prerequisites / metadata.hermes / 块式列表。"""
    content = (
        "---\n"
        "name: demo\n"
        "prerequisites:\n"
        "  env_vars: [API_KEY, TOKEN]\n"
        "  commands:\n"
        "    - git\n"
        "    - curl\n"
        "metadata:\n"
        "  hermes:\n"
        "    requires_tools: [terminal, web_search]\n"
        "    fallback_for_tools: [web_fetch]\n"
        "---\n"
        "# 正文\n"
    )
    meta, body = skills.parse_frontmatter(content)
    check("frontmatter 正文分离", body.strip().startswith("# 正文"))
    prereqs = meta.get("prerequisites")
    check("prerequisites 解析为 dict", isinstance(prereqs, dict))
    check("env_vars 流式列表", prereqs.get("env_vars") == ["API_KEY", "TOKEN"])
    check("commands 块式列表", prereqs.get("commands") == ["git", "curl"])
    hermes = (meta.get("metadata") or {}).get("hermes")
    check("metadata.hermes 两级嵌套", isinstance(hermes, dict))
    check("requires_tools 解析", hermes.get("requires_tools") == ["terminal", "web_search"])
    check("fallback_for_tools 解析", hermes.get("fallback_for_tools") == ["web_fetch"])

    modern = (
        "---\n"
        "name: modern\n"
        "required_environment_variables:\n"
        "  - name: API_KEY\n"
        "    prompt: Enter your key\n"
        "  - name: OPTIONAL_VAR\n"
        "    optional: true\n"
        "---\n"
        "# 正文\n"
    )
    meta2, _ = skills.parse_frontmatter(modern)
    required = meta2.get("required_environment_variables")
    check("required_environment_variables 块式映射列表",
          isinstance(required, list) and len(required) == 2)
    check("映射项 name/prompt/optional 解析",
          required[0].get("name") == "API_KEY"
          and required[0].get("prompt") == "Enter your key"
          and required[1].get("optional") is True)

    malformed = "---\nmetadata: not-a-dict\n---\n正文"
    meta3, _ = skills.parse_frontmatter(malformed)
    check("metadata 非 dict 不崩溃", skills.extract_skill_conditions(meta3)
          == {"fallback_for_toolsets": [], "requires_toolsets": [],
              "fallback_for_tools": [], "requires_tools": []})


def test_extract_conditions() -> None:
    """extract_skill_conditions 返回四组字段。"""
    meta = skills.parse_frontmatter(
        "---\n"
        "name: x\n"
        "metadata:\n"
        "  hermes:\n"
        "    requires_tools: [a, b]\n"
        "    fallback_for_tools: [c]\n"
        "    requires_toolsets: [ts1]\n"
        "    fallback_for_toolsets: [ts2]\n"
        "---\n"
        "正文"
    )[0]
    cond = skills.extract_skill_conditions(meta)
    check("四组条件齐全", cond == {
        "requires_tools": ["a", "b"],
        "fallback_for_tools": ["c"],
        "requires_toolsets": ["ts1"],
        "fallback_for_toolsets": ["ts2"],
    })
    check("无条件 -> 全空", skills.extract_skill_conditions({})
          == {"requires_tools": [], "fallback_for_tools": [],
              "requires_toolsets": [], "fallback_for_toolsets": []})


def test_conditions_ok() -> None:
    """skill_conditions_ok：requires/fallback/toolsets/向后兼容。"""
    check("无可用信息 -> 全部显示", skills.skill_conditions_ok(
        {"requires_tools": ["gh"]}) is True)
    check("requires_tools 已具备 -> 放行", skills.skill_conditions_ok(
        {"requires_tools": ["gh"]}, available_tools={"gh", "terminal"}) is True)
    check("requires_tools 缺失 -> 隐藏", skills.skill_conditions_ok(
        {"requires_tools": ["gh"]}, available_tools={"terminal"}) is False)
    check("fallback_for_tools 主工具在 -> 隐藏", skills.skill_conditions_ok(
        {"fallback_for_tools": ["web_fetch"]},
        available_tools={"web_fetch", "terminal"}) is False)
    check("fallback_for_tools 主工具缺 -> 放行", skills.skill_conditions_ok(
        {"fallback_for_tools": ["web_fetch"]},
        available_tools={"terminal"}) is True)
    check("requires_toolsets 已具备 -> 放行", skills.skill_conditions_ok(
        {"requires_toolsets": ["ts1"]},
        available_tools=set(), available_toolsets={"ts1"}) is True)
    check("requires_toolsets 缺失 -> 隐藏", skills.skill_conditions_ok(
        {"requires_toolsets": ["ts1"]},
        available_tools=set(), available_toolsets=set()) is False)
    check("fallback_for_toolsets 在 -> 隐藏", skills.skill_conditions_ok(
        {"fallback_for_toolsets": ["ts1"]},
        available_tools=set(), available_toolsets={"ts1"}) is False)


def _temp_skills_dir(tmpdir: Path) -> None:
    """在临时目录里建三个示例技能：普通 / 需要 terminal / 兜底 web_fetch。"""
    skills_dir = tmpdir / "skills"
    (skills_dir / "plain-skill" / "SKILL.md").parent.mkdir(parents=True)
    (skills_dir / "plain-skill" / "SKILL.md").write_text(
        "---\nname: plain-skill\ndescription: 无条件的技能\n---\n内容", encoding="utf-8")
    (skills_dir / "needs-terminal" / "SKILL.md").parent.mkdir(parents=True)
    (skills_dir / "needs-terminal" / "SKILL.md").write_text(
        "---\nname: needs-terminal\nmetadata:\n  hermes:\n"
        "    requires_tools: [terminal]\n---\n内容", encoding="utf-8")
    (skills_dir / "fallback-skill" / "SKILL.md").parent.mkdir(parents=True)
    (skills_dir / "fallback-skill" / "SKILL.md").write_text(
        "---\nname: fallback-skill\nmetadata:\n  hermes:\n"
        "    fallback_for_tools: [web_fetch]\n---\n内容", encoding="utf-8")
    skills.SKILLS_DIR = skills_dir


def test_discover_filter() -> None:
    """discover_skills 条件过滤 + 条目带 conditions。"""
    original_dir = skills.SKILLS_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            _temp_skills_dir(Path(tmpdir))
            check("无可用信息 -> 三个全列", len(skills.discover_skills()) == 3)
            with_tools = skills.discover_skills(available_tools={"terminal"})
            names = {s["name"] for s in with_tools}
            check("requires_tools 满足 -> 保留", "needs-terminal" in names)
            check("fallback 主工具缺 -> 保留", "fallback-skill" in names)
            check("普通技能保留", "plain-skill" in names)
            without = skills.discover_skills(available_tools={"web_fetch"})
            names2 = {s["name"] for s in without}
            check("requires_tools 缺失 -> 隐藏", "needs-terminal" not in names2)
            check("fallback 主工具在 -> 隐藏", "fallback-skill" not in names2)
            entry = next(s for s in with_tools if s["name"] == "needs-terminal")
            check("条目带 conditions", entry.get("conditions", {}).get("requires_tools") == ["terminal"])
    finally:
        skills.SKILLS_DIR = original_dir


def test_index_and_list_filter() -> None:
    """build_skills_index / skills_list 条件过滤。"""
    original_dir = skills.SKILLS_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            _temp_skills_dir(Path(tmpdir))
            full_index = skills.build_skills_index()
            check("无过滤索引含三个", all(n in full_index for n in (
                "plain-skill", "needs-terminal", "fallback-skill")))
            filtered = skills.build_skills_index(available_tools={"terminal", "web_fetch"})
            check("过滤后索引含 needs-terminal", "needs-terminal" in filtered)
            check("过滤后索引隐藏 fallback", "fallback-skill" not in filtered)

            data = json.loads(skills.skills_list(available_tools={"terminal", "web_fetch"}))
            check("skills_list 条件过滤", data["count"] == 2
                  and {s["name"] for s in data["skills"]} == {"plain-skill", "needs-terminal"})
            check("skills_list 保持最小元数据",
                  all(set(s) == {"name", "description", "category"} for s in data["skills"]))
    finally:
        skills.SKILLS_DIR = original_dir


def test_readiness_env() -> None:
    """加载期 env 检查：缺失 -> setup_needed；补齐 -> available；optional 不阻塞。"""
    req_var = "SKILL_TEST_REQ_VAR"
    opt_var = "SKILL_TEST_OPT_VAR"
    original_dir = skills.SKILLS_DIR
    saved_req = os.environ.get(req_var)
    saved_opt = os.environ.get(opt_var)
    try:
        os.environ.pop(req_var, None)
        os.environ.pop(opt_var, None)
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            (skills_dir / "env-skill" / "SKILL.md").parent.mkdir(parents=True)
            (skills_dir / "env-skill" / "SKILL.md").write_text(
                "---\nname: env-skill\n"
                "prerequisites:\n"
                "  env_vars: [SKILL_TEST_REQ_VAR]\n"
                "required_environment_variables:\n"
                "  - name: SKILL_TEST_OPT_VAR\n"
                "    optional: true\n"
                "---\n内容", encoding="utf-8")
            skills.SKILLS_DIR = skills_dir
            data = json.loads(skills.skill_view("env-skill"))
            check("缺失 env -> setup_needed", data["setup_needed"] is True)
            check("readiness_status=setup_needed",
                  data["readiness_status"] == "setup_needed")
            check("missing_required_environment_variables 列出缺项",
                  data["missing_required_environment_variables"] == [req_var])
            check("optional 缺失不进缺失清单", opt_var not in data["missing_required_environment_variables"])
            check("setup_note 提示", "Setup needed" in (data["setup_note"] or ""))

            os.environ[req_var] = "abc"
            data2 = json.loads(skills.skill_view("env-skill"))
            check("补齐 env -> available", data2["setup_needed"] is False
                  and data2["readiness_status"] == "available")
            check("补齐后缺失清单为空", data2["missing_required_environment_variables"] == [])
    finally:
        for var, saved in ((req_var, saved_req), (opt_var, saved_opt)):
            if saved is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = saved
        skills.SKILLS_DIR = original_dir


def test_readiness_commands_advisory() -> None:
    """commands 前置仅 advisory：缺失列出但不阻塞（对齐 Hermes 语义）。"""
    original_dir = skills.SKILLS_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            (skills_dir / "cmd-skill" / "SKILL.md").parent.mkdir(parents=True)
            (skills_dir / "cmd-skill" / "SKILL.md").write_text(
                "---\nname: cmd-skill\n"
                "prerequisites:\n"
                "  commands: [definitely-not-a-real-cmd-xyz]\n"
                "---\n内容", encoding="utf-8")
            skills.SKILLS_DIR = skills_dir
            data = json.loads(skills.skill_view("cmd-skill"))
            check("缺失命令被列出（advisory）",
                  data["missing_required_commands"] == ["definitely-not-a-real-cmd-xyz"])
            check("命令缺失不触发 setup_needed", data["setup_needed"] is False)
            check("readiness_status=available", data["readiness_status"] == "available")
    finally:
        skills.SKILLS_DIR = original_dir


def test_skill_view_readiness_fields() -> None:
    """skill_view 主视图带 readiness；子文件视图不带。"""
    original_dir = skills.SKILLS_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            (skills_dir / "view-skill" / "references").mkdir(parents=True)
            (skills_dir / "view-skill" / "SKILL.md").write_text(
                "---\nname: view-skill\nmetadata:\n  hermes:\n"
                "    requires_tools: [terminal]\n---\n正文", encoding="utf-8")
            (skills_dir / "view-skill" / "references" / "doc.md").write_text(
                "# 子文档", encoding="utf-8")
            skills.SKILLS_DIR = skills_dir
            data = json.loads(skills.skill_view("view-skill"))
            check("主视图带 readiness 键", "setup_needed" in data
                  and "readiness_status" in data
                  and "required_environment_variables" in data)
            check("主视图带 metadata", data.get("metadata", {}).get("hermes", {})
                  .get("requires_tools") == ["terminal"])
            sub = json.loads(skills.skill_view("view-skill", "references/doc.md"))
            check("子文件视图不带 readiness", "setup_needed" not in sub
                  and sub.get("content", "").strip() == "# 子文档")
    finally:
        skills.SKILLS_DIR = original_dir


def test_env_lookup() -> None:
    """_env_lookup：os.environ 优先、.env 兜底、空值视为缺失。"""
    var = "SKILL_ENV_LOOKUP_TEST"
    saved = os.environ.get(var)
    original_base = skills.BASE_DIR
    try:
        os.environ.pop(var, None)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / ".env").write_text(
                f"# comment\n{var}=from-dotenv\nOTHER=1\n", encoding="utf-8")
            skills.BASE_DIR = tmp
            check(".env 兜底读取", skills._env_lookup(var) == "from-dotenv")
            os.environ[var] = "from-os"
            check("os.environ 优先", skills._env_lookup(var) == "from-os")
            os.environ[var] = ""
            check("空值视为缺失", skills._env_lookup(var) == "")
    finally:
        if saved is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = saved
        skills.BASE_DIR = original_base


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== Skills 前置条件检查回归测试 ==")
    for test_fn in (
        test_nested_frontmatter,
        test_extract_conditions,
        test_conditions_ok,
        test_discover_filter,
        test_index_and_list_filter,
        test_readiness_env,
        test_readiness_commands_advisory,
        test_skill_view_readiness_fields,
        test_env_lookup,
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
