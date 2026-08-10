# -*- coding: utf-8 -*-
"""
文档抽取模块的回归测试（零依赖，直接运行）：
    python tests/test_read_extract.py

覆盖（对齐 Hermes tools/read_extract.py）：
    - 扩展名判定：.ipynb/.docx/.xlsx 可抽取，其余不可
    - docx：段落文本、tab/换行、空文档报错、损坏 zip 报错
    - xlsx：共享字符串/内联串/布尔/错误值、隐藏表跳过、空表报错
    - ipynb：markdown/code/raw 分节、nbformat3 兼容、无单元报错
    - read_file 集成：docx 自动抽取（extracted_document）、分页、损坏回退报错
"""

import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from file_tools import read_file_tool  # noqa: E402
from read_extract import (  # noqa: E402
    ExtractionError,
    extract_document_text,
    is_extractable_document,
)


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _make_docx(path: Path) -> None:
    """用 zipfile 造一个最小 docx（两段，第二段含 tab 与 br）。"""
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:r><w:t>第一段</w:t></w:r></w:p>"
        '<w:p><w:r><w:t>第二段</w:t><w:tab/><w:t>A</w:t><w:br/><w:t>B</w:t></w:r></w:p>'
        "</w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", document_xml)


def _make_xlsx(path: Path) -> None:
    """用 zipfile 造一个最小 xlsx：Sheet1 可见（共享串/数字/内联串），Hidden 隐藏。"""
    workbook_xml = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        '<sheet name="Sheet1" sheetId="1" r:id="rId1"/>'
        '<sheet name="Hidden" sheetId="2" state="hidden" r:id="rId2"/>'
        "</sheets>"
        "</workbook>"
    )
    rels_xml = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet2.xml"/>'
        "</Relationships>"
    )
    shared_xml = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>苹果</t></si>"
        "<si><t>香蕉</t></si>"
        "</sst>"
    )
    sheet1_xml = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        '<row r="1">'
        '<c r="A1" t="s"><v>0</v></c>'
        '<c r="B1"><v>42</v></c>'
        '<c r="C1" t="b"><v>1</v></c>'
        "</row>"
        '<row r="2">'
        '<c r="A2" t="s"><v>1</v></c>'
        '<c r="B2" t="inlineStr"><is><t>直填</t></is></c>'
        "</row>"
        "</sheetData>"
        "</worksheet>"
    )
    sheet2_xml = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData><row r=\"1\"><c r=\"A1\" t=\"s\"><v>0</v></c></row></sheetData>"
        "</worksheet>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1_xml)
        zf.writestr("xl/worksheets/sheet2.xml", sheet2_xml)


def _make_ipynb(path: Path, nbformat: int = 4) -> None:
    """造一个最小 ipynb（markdown + code 两个单元）。"""
    if nbformat == 3:
        nb = {
            "worksheets": [{
                "cells": [
                    {"cell_type": "markdown", "source": ["# 旧版标题"]},
                    {"cell_type": "code", "source": ["print('old')"]},
                ]
            }],
        }
    else:
        nb = {
            "cells": [
                {"cell_type": "markdown", "source": ["# 标题\n", "说明文字"]},
                {"cell_type": "code", "source": ["print(1)\n"]},
                {"cell_type": "raw", "source": ["raw 内容"]},
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    path.write_text(json.dumps(nb, ensure_ascii=False), encoding="utf-8")


def test_extensions() -> None:
    """扩展名判定。"""
    check("docx 可抽取", is_extractable_document("a.docx") is True)
    check("xlsx 可抽取", is_extractable_document("b.XLSX") is True)  # 大小写不敏感
    check("ipynb 可抽取", is_extractable_document("c.ipynb") is True)
    check("txt 不可抽取", is_extractable_document("d.txt") is False)
    check("无扩展名不可抽取", is_extractable_document("e") is False)
    check("目录路径不可抽取", is_extractable_document("docs/") is False)


def test_docx() -> None:
    """docx：段落/tab/换行、空文档报错、损坏 zip 报错。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "doc.docx"
        _make_docx(path)
        text = extract_document_text(str(path))
        check("docx 第一段", "第一段" in text)
        check("docx 第二段 tab 变制表符", "第二段\tA" in text)
        check("docx br 变两行", "A\nB" in text)

        empty = Path(tmpdir) / "empty.docx"
        with zipfile.ZipFile(empty, "w") as zf:
            zf.writestr("word/document.xml", "<w:document/>")
        try:
            extract_document_text(str(empty))
            check("空 docx 报错", False)
        except ExtractionError:
            check("空 docx 报错", True)

        bad = Path(tmpdir) / "bad.docx"
        bad.write_bytes(b"not a zip")
        try:
            extract_document_text(str(bad))
            check("损坏 docx 报错", False)
        except ExtractionError:
            check("损坏 docx 报错", True)


def test_xlsx() -> None:
    """xlsx：共享字符串/数字/布尔/内联串、隐藏表跳过、空表报错。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "book.xlsx"
        _make_xlsx(path)
        text = extract_document_text(str(path))
        check("xlsx 含表头", "# ── Sheet: Sheet1 ──" in text)
        check("xlsx 共享字符串", "苹果\t42" in text)
        check("xlsx 布尔值", "TRUE" in text)
        check("xlsx 内联串", "香蕉\t直填" in text)
        check("xlsx 隐藏表跳过", "Hidden" not in text)

        empty = Path(tmpdir) / "empty.xlsx"
        with zipfile.ZipFile(empty, "w") as zf:
            zf.writestr("xl/workbook.xml",
                        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')
        try:
            extract_document_text(str(empty))
            check("无可见表 xlsx 报错", False)
        except ExtractionError:
            check("无可见表 xlsx 报错", True)

        bad = Path(tmpdir) / "bad.xlsx"
        bad.write_bytes(b"garbage")
        try:
            extract_document_text(str(bad))
            check("损坏 xlsx 报错", False)
        except ExtractionError:
            check("损坏 xlsx 报错", True)


def test_ipynb() -> None:
    """ipynb：分节标签、nbformat3 兼容、无单元/坏 JSON 报错。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "nb.ipynb"
        _make_ipynb(path, nbformat=4)
        text = extract_document_text(str(path))
        check("ipynb Markdown 分节", "# ── Markdown cell 1 ──" in text)
        check("ipynb Code 分节", "# ── Code cell 1 ──" in text and "print(1)" in text)
        check("ipynb Raw 无编号", "# ── Raw cell ──" in text and "raw 内容" in text)

        old = Path(tmpdir) / "old.ipynb"
        _make_ipynb(old, nbformat=3)
        old_text = extract_document_text(str(old))
        check("ipynb nbformat3 兼容", "旧版标题" in old_text and "print('old')" in old_text)

        empty = Path(tmpdir) / "empty.ipynb"
        empty.write_text(json.dumps({"cells": []}), encoding="utf-8")
        try:
            extract_document_text(str(empty))
            check("无单元 ipynb 报错", False)
        except ExtractionError:
            check("无单元 ipynb 报错", True)

        bad = Path(tmpdir) / "bad.ipynb"
        bad.write_text("{not json", encoding="utf-8")
        try:
            extract_document_text(str(bad))
            check("坏 JSON ipynb 报错", False)
        except ExtractionError:
            check("坏 JSON ipynb 报错", True)


def test_read_file_integration() -> None:
    """read_file 集成：docx 自动抽取、分页、损坏回退报错、普通二进制仍拒绝。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        docx = root / "doc.docx"
        _make_docx(docx)

        data = json.loads(read_file_tool(str(docx)))
        check("read_file docx success", data["success"] is True)
        check("read_file 标记 extracted_document", data.get("extracted_document") is True)
        check("read_file docx 内容带行号", "│ 第一段" in data["content"])
        # br 让第二段拆成两行：第一段 / 第二段\tA / B → 共 3 行
        check("read_file docx total_lines=3", data["total_lines"] == 3)

        page = json.loads(read_file_tool(str(docx), offset=2, limit=1))
        check("read_file docx 分页",
              page["total_lines"] == 3 and "第二段" in page["content"])

        bad = root / "bad.docx"
        bad.write_bytes(b"not a zip")
        data2 = json.loads(read_file_tool(str(bad)))
        check("read_file 损坏 docx 报错",
              data2["success"] is False and "文档抽取失败" in data2.get("error", ""))

        binary = root / "data.bin"
        binary.write_bytes(b"\x00\x01\x02")
        data3 = json.loads(read_file_tool(str(binary)))
        check("read_file 普通二进制仍拒绝",
              data3["success"] is False and "二进制" in data3.get("error", ""))


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 文档抽取回归测试 ==")
    for test_fn in (
        test_extensions,
        test_docx,
        test_xlsx,
        test_ipynb,
        test_read_file_integration,
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
