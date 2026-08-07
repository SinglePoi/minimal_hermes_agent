# -*- coding: utf-8 -*-
"""
Skills 模块的回归测试（零依赖，直接运行）：
    python tests/test_skills.py

覆盖（对齐 Hermes agent/skill_utils.py + tools/skills_tool.py）：
    - frontmatter 解析：BOM 剥离、引号、列表、缺失围栏兜底
    - 技能发现：只认 SKILL.md、支持文件不单独成技能、平台过滤
    - 系统提示词技能索引
    - skills_list 元数据列表
    - skill_view 全文/子文件加载、路径穿越与越界拒绝
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

import skills  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def test_frontmatter_parsing() -> None:
    """frontmatter 解析：BOM / 引号 / 列表 / 围栏兜底。"""
    with_bom = (
        "\ufeff---\n"
        'name: release-check\n'
        'description: "发版检查清单规范"\n'
        "platforms: [windows, linux]\n"
        "---\n"
        "# 正文\n"
        "内容……"
    )
    meta, body = skills.parse_frontmatter(with_bom)
    check("BOM 被剥离且元数据解析", meta.get("name") == "release-check")
    check("引号描述被剥离", meta.get("description") == "发版检查清单规范")
    check("列表值解析", meta.get("platforms") == ["windows", "linux"])
    check("正文与 frontmatter 分离", body.strip().startswith("# 正文"))

    no_fence = "plain text without frontmatter"
    meta, body = skills.parse_frontmatter(no_fence)
    check("无 frontmatter -> 空元数据 + 全文为正文",
          meta == {} and body == no_fence)

    open_fence = "---\nname: x\n没有结束围栏"
    meta, _body = skills.parse_frontmatter(open_fence)
    check("缺结束围栏 -> 视为无 frontmatter", meta == {})


def test_discovery_and_index() -> None:
    """示例技能被发现，索引与列表内容正确。"""
    found = {s["name"]: s for s in skills.discover_skills()}
    check("示例技能 release-check 被发现", "release-check" in found)
    check("描述解析", "发版" in found["release-check"]["description"])
    check("references/ 不单独成技能", all(s["name"] != "rollback" for s in skills.discover_skills()))

    index = skills.build_skills_index()
    check("技能索引含标题", index.startswith("## 可用技能（Skills）"))
    check("技能索引含示例技能", "release-check" in index)

    data = json.loads(skills.skills_list())
    check("skills_list success", data["success"] is True)
    check("skills_list count=1", data["count"] == 1)
    check("skills_list 只含元数据",
          all(set(s) == {"name", "description", "category"} for s in data["skills"]))


def test_skill_view() -> None:
    """skill_view：全文加载、子文件加载、非法名字拒绝。"""
    data = json.loads(skills.skill_view("release-check"))
    check("skill_view success", data["success"] is True)
    check("skill_view 正文不含 frontmatter", "name:" not in data["content"])
    check("skill_view 正文含标题", "# 发版检查清单（示例）" in data["content"])
    check("skill_view 列出子文件", "references/rollback.md" in data["files"])

    sub = json.loads(skills.skill_view("release-check", "references/rollback.md"))
    check("skill_view 子文件加载", sub["success"] is True and "回滚说明" in sub["content"])

    missing = json.loads(skills.skill_view("no-such-skill"))
    check("不存在的技能 -> success=False", missing["success"] is False)

    traversal = json.loads(skills.skill_view("../minimal_agent.py"))
    check("路径穿越名字被拒绝", traversal["success"] is False)

    traversal2 = json.loads(skills.skill_view("release-check", "../../minimal_agent.py"))
    check("子文件路径穿越被拒绝", traversal2["success"] is False)

    absolute = json.loads(skills.skill_view(str(ROOT / "minimal_agent.py")))
    check("绝对路径被拒绝", absolute["success"] is False)


def test_platform_filter() -> None:
    """platforms 过滤：不匹配当前系统的技能不列入（用临时目录注入）。"""
    original_dir = skills.SKILLS_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            (skills_dir / "linux-only").mkdir(parents=True)
            (skills_dir / "linux-only" / "SKILL.md").write_text(
                "---\nname: linux-only\nplatforms: [linux]\n---\n内容",
                encoding="utf-8",
            )
            (skills_dir / "anywhere").mkdir()
            (skills_dir / "anywhere" / "SKILL.md").write_text(
                "---\nname: anywhere\n---\n内容",
                encoding="utf-8",
            )
            skills.SKILLS_DIR = skills_dir
            found = {s["name"] for s in skills.discover_skills()}
            check("platforms 不匹配被过滤",
                  "anywhere" in found and "linux-only" not in found)
    finally:
        skills.SKILLS_DIR = original_dir


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== Skills 回归测试 ==")
    for test_fn in (
        test_frontmatter_parsing,
        test_discovery_and_index,
        test_skill_view,
        test_platform_filter,
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
