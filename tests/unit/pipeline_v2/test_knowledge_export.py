# -*- coding: utf-8 -*-
"""knowledge_export 单测：fixture 真相源 → 校验 frontmatter / wikilink / 幂等。"""
import tempfile
import unittest
import re
from pathlib import Path

from pipeline_v2.knowledge_export import TruthSources, export_knowledge


SEMANTIC_MAP = {
    "schema_version": 1,
    "product": "demo",
    "based_on_product_commit": "abc1234",
    "entries": [
        {
            "id": "demo.skill.change_mode.intent-to-mode",
            "skill": {"name": "change_mode", "description": "切换到匹配的任务模式"},
            "flow": {"summary": "识别意图后切换模式。"},
            "expected_terminal_state": {
                "assertion": "current_mode_equals_target",
                "assertion_type": "mode_state",
            },
        },
        {
            "id": "demo.skill.change_mode.mode-switch-idempotent",
            "skill": {"name": "change_mode", "description": "切换到匹配的任务模式"},
            "flow": {"summary": "重复切同 mode 幂等。"},
            "expected_terminal_state": {
                "assertion": "idempotent_toolset",
                "assertion_type": "mode_state",
            },
        },
    ],
}

CODE_MAP = {
    "schema_version": 1,
    "product": "demo",
    "based_on_product_commit": "abc1234",
    "modules": [
        {
            "id": "internal/server",
            "package": "server",
            "files": ["internal/server/ws.go"],
            "exports": [
                {"symbol": "DispatchCommand", "recv": "(s *Server)", "file": "internal/server/ws.go", "line": 800},
            ],
            "endpoints": [
                {"kind": "http", "method": "POST", "route": "/api/sessions", "handler": "handleCreate", "file": "internal/server/server.go", "line": 172},
                {"kind": "ws_command", "command": "set_mode", "file": "internal/server/ws.go", "line": 924},
            ],
            "entrypoints": [],
        },
        {
            "id": "internal/empty",
            "package": "empty",
            "files": ["internal/empty/x.go"],
            "exports": [],
            "endpoints": [],
            "entrypoints": [],
        },
    ],
    "edges": [],
    "links": [
        {
            "from_kind": "ws_command",
            "from": "set_mode",
            "from_file": "internal/server/ws.go",
            "from_line": 924,
            "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
            "confidence": "heuristic",
        }
    ],
}

CASES = [
    {
        "fingerprint": "fp-aaa111",
        "oracle_level": "L3",
        "status": "reported",
        "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
        "summary": "差分关系破坏",
        "conclusion": "立案",
        "based_on_product_commit": "abc1234",
    }
]

EXCLUDED = [
    {
        "id": "set_model-provider-echo",
        "oracle_level": "L3",
        "status": "excluded",
        "root_cause": "重路由是预期语义",
        "conclusion": "非 bug",
    }
]


class KnowledgeExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "knowledge"
        self.sources = TruthSources(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            cases=CASES,
            excluded_leads=EXCLUDED,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_generates_expected_structure(self):
        report = export_knowledge(self.sources, self.out)
        self.assertTrue((self.out / "index.md").exists())
        self.assertTrue((self.out / "skills" / "skill-change_mode.md").exists())
        self.assertTrue((self.out / "code" / "module-internal-server.md").exists())
        self.assertTrue((self.out / "code" / "ws_command-set_mode.md").exists())
        # 空模块不出页
        self.assertFalse((self.out / "code" / "module-internal-empty.md").exists())
        self.assertEqual(report["skill_pages"], 1)
        self.assertEqual(report["code_pages"], 1)
        self.assertEqual(report["command_pages"], 1)
        # cases + excluded
        self.assertEqual(report["finding_pages"], 2)

    def test_skill_page_frontmatter_and_wikilink(self):
        export_knowledge(self.sources, self.out)
        text = (self.out / "skills" / "skill-change_mode.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("type: skill", text)
        self.assertIn("skill: change_mode", text)
        self.assertIn("based_on_product_commit: abc1234", text)
        # links 关联的 ws_command 应渲染成 wikilink
        self.assertIn("[[ws_command-set_mode|set_mode]]", text)
        # 两条 entry：一条已覆盖、一条未覆盖
        self.assertIn("✅ 已覆盖", text)
        self.assertIn("⬜ 未覆盖", text)

    def test_mixed_link_kinds_do_not_create_dangling_wikilinks(self):
        code_map = {
            **CODE_MAP,
            "links": CODE_MAP["links"]
            + [
                {
                    "from_kind": "symbol",
                    "from": "DispatchCommand",
                    "from_file": "internal/server/ws.go",
                    "from_line": 800,
                    "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
                },
                {
                    "from_kind": "playwright_spec",
                    "from": "context-ribbon.spec.ts",
                    "from_file": "web/tests/integration/context-ribbon.spec.ts",
                    "semantic_map_entry_id": "demo.skill.change_mode.mode-switch-idempotent",
                },
            ],
        }
        export_knowledge(
            TruthSources(
                semantic_map=SEMANTIC_MAP,
                code_map=code_map,
                cases=CASES,
                excluded_leads=EXCLUDED,
            ),
            self.out,
        )

        skill_page = (self.out / "skills" / "skill-change_mode.md").read_text(encoding="utf-8")
        self.assertIn("[[ws_command-set_mode|set_mode]]", skill_page)
        self.assertIn("`symbol: DispatchCommand`", skill_page)
        self.assertIn("`playwright_spec: context-ribbon.spec.ts`", skill_page)

        page_stems = {path.stem for path in self.out.rglob("*.md")}
        dangling = []
        for path in self.out.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"\[\[([^\]|#]+)", text):
                if target not in page_stems:
                    dangling.append(f"{path.name}:{target}")
        self.assertEqual(dangling, [])

    def test_index_coverage_snapshot(self):
        export_knowledge(self.sources, self.out)
        text = (self.out / "index.md").read_text(encoding="utf-8")
        self.assertIn("type: index", text)
        # 2 entry, 1 covered
        self.assertIn("entry_total: 2", text)
        self.assertIn("entry_covered: 1", text)
        self.assertIn("[[skill-change_mode|change_mode]]", text)
        self.assertIn("[[module-internal-server|internal/server]]", text)
        self.assertIn("[[ws_command-set_mode|set_mode]]", text)

    def test_stale_case_exports_but_does_not_count_current_coverage(self):
        stale_sources = TruthSources(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            cases=[
                {
                    "fingerprint": "fp-stale",
                    "oracle_level": "L3",
                    "status": "reported",
                    "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
                    "summary": "旧 commit 证据",
                    "based_on_product_commit": "old1234",
                }
            ],
        )

        report = export_knowledge(stale_sources, self.out)

        self.assertEqual(report["coverage"], {"total": 2, "covered": 0})
        index = (self.out / "index.md").read_text(encoding="utf-8")
        self.assertIn("entry_covered: 0", index)
        finding = (self.out / "findings" / "finding-fp-stale.md").read_text(encoding="utf-8")
        self.assertIn("based_on_product_commit: old1234", finding)

    def test_non_confirmed_cases_export_but_do_not_count_current_coverage(self):
        sources = TruthSources(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            cases=[
                CASES[0],
                {
                    "id": "owner-review-required",
                    "oracle_level": "L2",
                    "status": "reported",
                    "triage_result": "owner-review-required",
                    "semantic_map_entry_id": "demo.skill.change_mode.mode-switch-idempotent",
                    "based_on_product_commit": "abc1234",
                },
                {
                    "id": "l4-suggestion",
                    "oracle_level": "L4-suggestion",
                    "status": "reported",
                    "semantic_map_entry_id": "demo.skill.change_mode.mode-switch-idempotent",
                    "based_on_product_commit": "abc1234",
                },
                {
                    "id": "false-positive",
                    "oracle_level": "L2",
                    "status": "reported",
                    "triage_result": "false-positive",
                    "semantic_map_entry_id": "demo.skill.change_mode.mode-switch-idempotent",
                    "based_on_product_commit": "abc1234",
                },
            ],
        )

        report = export_knowledge(sources, self.out)

        self.assertEqual(report["coverage"], {"total": 2, "covered": 1})
        self.assertEqual(report["finding_pages"], 4)
        index = (self.out / "index.md").read_text(encoding="utf-8")
        self.assertIn("语义图 entry 覆盖：1/2", index)
        skill_page = (self.out / "skills" / "skill-change_mode.md").read_text(encoding="utf-8")
        self.assertIn("demo.skill.change_mode.mode-switch-idempotent — ⬜ 未覆盖", skill_page)
        self.assertTrue((self.out / "findings" / "finding-owner-review-required.md").exists())
        self.assertTrue((self.out / "findings" / "finding-l4-suggestion.md").exists())

    def test_ws_command_page_links_code_and_semantic_entries(self):
        export_knowledge(self.sources, self.out)
        text = (self.out / "code" / "ws_command-set_mode.md").read_text(encoding="utf-8")
        self.assertIn("type: ws-command", text)
        self.assertIn("command: set_mode", text)
        self.assertIn("[[module-internal-server|internal/server]]", text)
        self.assertIn("demo.skill.change_mode.intent-to-mode", text)

    def test_finding_pages_include_case_and_excluded(self):
        export_knowledge(self.sources, self.out)
        case_page = (self.out / "findings" / "finding-fp-aaa111.md").read_text(encoding="utf-8")
        self.assertIn("oracle_level: L3", case_page)
        self.assertIn("type: finding", case_page)
        excl_page = (self.out / "findings" / "finding-set_model-provider-echo.md").read_text(encoding="utf-8")
        self.assertIn("非 bug", excl_page)

    def test_idempotent_rerun_no_changes(self):
        export_knowledge(self.sources, self.out)
        files = sorted(self.out.rglob("*.md"))
        first = {p: (p.read_text(encoding="utf-8"), p.stat().st_mtime_ns) for p in files}
        # rerun
        export_knowledge(self.sources, self.out)
        for p in files:
            content, mtime = first[p]
            self.assertEqual(p.read_text(encoding="utf-8"), content)
            # 内容未变则不重写 → mtime 不变
            self.assertEqual(p.stat().st_mtime_ns, mtime)

    def test_does_not_write_back_to_truth_sources(self):
        # 真相源是传入的不可变 dict 视图；导出不应修改它们
        before_entries = len(SEMANTIC_MAP["entries"])
        export_knowledge(self.sources, self.out)
        self.assertEqual(len(SEMANTIC_MAP["entries"]), before_entries)
        self.assertEqual(SEMANTIC_MAP["based_on_product_commit"], "abc1234")


if __name__ == "__main__":
    unittest.main()
