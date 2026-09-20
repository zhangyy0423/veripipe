# -*- coding: utf-8 -*-
"""change_impact 单测：fixture diff + fixture code-map 离线验证变更驱动测试清单。"""
import tempfile
import unittest
from pathlib import Path

from pipeline_v2.change_impact import (
    _load_adapter_target_plan,
    build_profile_seed_plan,
    build_test_plan_from_dicts,
    compute_impacted,
    render_plan_markdown,
)
from pipeline_v2.code_map import code_map_from_dict


CODE_MAP = {
    "schema_version": 1,
    "product": "demo",
    "based_on_product_commit": "base123",
    "parser": "tree-sitter-go",
    "modules": [
        {
            "id": "internal/server",
            "package": "server",
            "files": ["internal/server/server.go", "internal/server/ws.go"],
            "exports": [
                {"symbol": "Route", "recv": "(s *Server)", "file": "internal/server/server.go", "line": 100},
                {"symbol": "DispatchCommand", "recv": "(s *Server)", "file": "internal/server/ws.go", "line": 800},
            ],
            "endpoints": [
                {"kind": "http", "method": "POST", "route": "/api/sessions", "handler": "handleCreate", "file": "internal/server/server.go", "line": 172},
                {"kind": "http", "method": "GET", "route": "/api/sessions", "handler": "handleList", "file": "internal/server/server.go", "line": 173},
                {"kind": "http", "method": "GET", "route": "/api/env", "handler": "handleGetEnv", "file": "internal/server/server.go", "line": 194},
                {"kind": "http", "method": "PUT", "route": "/api/workspace", "handler": "handlePutWorkspace", "file": "internal/server/server.go", "line": 195},
                {"kind": "ws", "method": "", "route": "/api/chat/ws/{session_id}", "handler": "handleWebSocket", "file": "internal/server/ws.go", "line": 106},
                {"kind": "ws_command", "command": "set_mode", "file": "internal/server/ws.go", "line": 924},
                {"kind": "ws_command", "command": "set_permission_mode", "file": "internal/server/ws.go", "line": 838},
            ],
            "entrypoints": [],
        },
        {
            "id": "internal/agent",
            "package": "agent",
            "files": ["internal/agent/agent.go"],
            "exports": [
                {"symbol": "Run", "recv": "(a *Agent)", "file": "internal/agent/agent.go", "line": 20},
            ],
            "endpoints": [],
            "entrypoints": [],
        },
    ],
    "edges": [
        {"caller": "DispatchCommand", "callee": "Run", "caller_file": "internal/server/ws.go", "caller_line": 800},
    ],
    "links": [
        {
            "from_kind": "ws_command",
            "from": "set_mode",
            "from_file": "internal/server/ws.go",
            "from_line": 924,
            "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
            "confidence": "heuristic",
        },
            {
                "from_kind": "ws_command",
                "from": "set_permission_mode",
                "from_file": "internal/server/ws.go",
                "from_line": 838,
                "semantic_map_entry_id": "demo.control.permission-mode.session-state",
                "confidence": "heuristic",
            }
    ],
}

ADAPTER = {
    "product": "demo",
    "http_driver": {
        "scenarios": [
            {
                "scenario_id": "l3.metamorphic.set_mode_idempotent",
                "strategy": "metamorphic",
                "relation_id": "set_mode-idempotent",
                "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
            },
            {
                "scenario_id": "l3.metamorphic.set_permission_mode_idempotent",
                "strategy": "metamorphic_stateful",
                "relation_id": "set_permission_mode-idempotent",
                "semantic_map_entry_id": "demo.control.permission-mode.session-state",
            },
            {
                "scenario_id": "l3.unrelated",
                "strategy": "differential",
                "relation_id": "other",
                "semantic_map_entry_id": "demo.skill.other.flow",
            },
        ]
    },
    "l1_fuzz": {
        "cases": [
            {
                "id": "malformed-json-sessions",
                "method": "POST",
                "path": "/api/sessions",
                "raw_body": "{bad",
                "semantic_map_entry_id": "demo.auth.session-contract",
            },
            {
                "id": "get-env-no-crash",
                "method": "GET",
                "path": "/api/env",
                "raw_body": "",
                "semantic_map_entry_id": "demo.auth.env-contract",
            },
            {
                "id": "malformed-json-workspace",
                "method": "PUT",
                "path": "/api/workspace",
                "raw_body": "{bad",
                "semantic_map_entry_id": "demo.auth.env-contract",
            },
            {"id": "unrelated-fuzz", "method": "GET", "path": "/api/health", "raw_body": ""},
        ]
    },
}


class ComputeImpactedTest(unittest.TestCase):
    def setUp(self):
        self.cm = code_map_from_dict(CODE_MAP)

    def test_ws_file_change_impacts_command_endpoint_and_entry(self):
        impacted = compute_impacted(["internal/server/ws.go"], self.cm)
        self.assertIn("internal/server", impacted.modules)
        self.assertIn("set_mode", impacted.ws_commands)
        self.assertIn("demo.skill.change_mode.intent-to-mode", impacted.semantic_map_entry_ids)
        routes = {e["route"] for e in impacted.endpoints}
        self.assertIn("/api/chat/ws/{session_id}", routes)

    def test_agent_change_propagates_to_transitive_callers(self):
        impacted = compute_impacted(["internal/agent/agent.go"], self.cm)
        self.assertIn("Run", impacted.symbols)
        # Run <- DispatchCommand
        self.assertIn("DispatchCommand", impacted.transitive_symbols)

    def test_unrelated_file_yields_no_impact(self):
        impacted = compute_impacted(["docs/readme.md"], self.cm)
        self.assertEqual(impacted.modules, [])
        self.assertEqual(impacted.semantic_map_entry_ids, [])


class BuildTestPlanTest(unittest.TestCase):
    def test_plan_selects_relevant_l3_and_l1_and_flags_gaps(self):
        plan = build_test_plan_from_dicts(
            ["internal/server/ws.go", "internal/server/server.go"],
            CODE_MAP,
            ADAPTER,
            anchor_commit="base123",
        )
        # 必跑 L3：只命中 change_mode entry 的 scenario，不含 unrelated
        l3_ids = {r["scenario_id"] for r in plan.must_run_l3_relations}
        self.assertIn("l3.metamorphic.set_mode_idempotent", l3_ids)
        self.assertIn("l3.metamorphic.set_permission_mode_idempotent", l3_ids)
        self.assertNotIn("l3.unrelated", l3_ids)
        l3_target = next(r for r in plan.must_run_l3_relations if r["scenario_id"] == "l3.metamorphic.set_mode_idempotent")
        self.assertEqual(l3_target["oracle_level"], "L3")
        self.assertIn("semantic entry", l3_target["selection_reason"])
        # 必跑 L1：命中 /api/sessions 端点，不含 /api/health
        l1_ids = {c["case_id"] for c in plan.must_run_l1_fuzz}
        self.assertIn("malformed-json-sessions", l1_ids)
        self.assertIn("get-env-no-crash", l1_ids)
        self.assertIn("malformed-json-workspace", l1_ids)
        self.assertNotIn("unrelated-fuzz", l1_ids)
        l1_target = next(c for c in plan.must_run_l1_fuzz if c["case_id"] == "malformed-json-sessions")
        self.assertEqual(l1_target["oracle_level"], "L1")
        self.assertEqual(l1_target["semantic_map_entry_id"], "demo.auth.session-contract")
        self.assertEqual(l1_target["matched_endpoint"], "POST /api/sessions")
        self.assertIn("bounded fuzz", l1_target["selection_reason"])
        endpoint_gaps = [g for g in plan.coverage_gaps if g["kind"] == "endpoint_without_l1_fuzz"]
        self.assertIn(
            {"kind": "http", "method": "GET", "route": "/api/sessions"},
            [g["endpoint"] for g in endpoint_gaps],
        )
        self.assertTrue(plan.has_targets)
        self.assertEqual(plan.anchor_commit, "base123")

    def test_ws_endpoint_without_fuzz_is_flagged_as_gap(self):
        plan = build_test_plan_from_dicts(
            ["internal/server/ws.go"], CODE_MAP, ADAPTER
        )
        gap_kinds = {g["kind"] for g in plan.coverage_gaps}
        # ws 端点 /api/chat/ws/{session_id} 无 fuzz case → endpoint_without_l1_fuzz
        self.assertIn("endpoint_without_l1_fuzz", gap_kinds)
        gap = next(g for g in plan.coverage_gaps if g["kind"] == "endpoint_without_l1_fuzz")
        self.assertEqual(gap["target_channel"], "L4-suggestion")

    def test_entry_without_l3_relation_is_flagged(self):
        # 构造一条改动只命中没有任何 L3 关系的 entry
        cm = dict(CODE_MAP)
        adapter = {"http_driver": {"scenarios": []}, "l1_fuzz": {"cases": []}}
        plan = build_test_plan_from_dicts(["internal/server/ws.go"], cm, adapter)
        kinds = {g["kind"] for g in plan.coverage_gaps}
        self.assertIn("entry_without_l3_relation", kinds)

    def test_top_level_l3_relations_do_not_become_executable_targets(self):
        code_map = {
            **CODE_MAP,
            "links": CODE_MAP["links"]
            + [
                {
                    "from_kind": "doc",
                    "from": "filesystem skill",
                    "from_file": "assets/prompts/skills/filesystem/SKILL.md",
                    "semantic_map_entry_id": "demo.skill.filesystem.created-resource-id-is-reusable",
                }
            ],
        }
        adapter = {
            "l3_relations": [
                {
                    "scenario_id": "l3.owner_gated.created_resource_referenceable_after_create",
                    "strategy": "stateful_owner_gated",
                    "relation_id": "created-resource-id-reusable-after-create",
                    "semantic_map_entry_id": "demo.skill.filesystem.created-resource-id-is-reusable",
                    "execution_gate": "owner-gated",
                }
            ]
        }

        plan = build_test_plan_from_dicts(
            ["assets/prompts/skills/filesystem/SKILL.md"],
            code_map,
            adapter,
        )

        self.assertEqual(plan.must_run_l3_relations, [])
        self.assertEqual(len(plan.coverage_gaps), 1)
        self.assertEqual(
            plan.coverage_gaps[0]["semantic_map_entry_id"],
            "demo.skill.filesystem.created-resource-id-is-reusable",
        )
        self.assertEqual(plan.coverage_gaps[0]["target_channel"], "owner-gate")

    def test_no_change_no_targets(self):
        plan = build_test_plan_from_dicts([], CODE_MAP, ADAPTER)
        self.assertFalse(plan.has_targets)
        self.assertEqual(plan.coverage_gaps, [])

    def test_profile_seed_plan_selects_adapter_whitelist_l2_targets(self):
        adapter = {
            "playwright": {
                "web_subdir": "web",
                "spec_dir": "tests/integration",
                "spec_whitelist": [
                    {
                        "spec": "version-consistency.spec.ts",
                        "semantic_map_entry_id": "demo.release.version",
                    },
                    {
                        "spec": "contract-version-consistency.spec.ts",
                        "semantic_map_entry_id": "demo.release.contract",
                    },
                ],
            },
        }

        plan = build_profile_seed_plan(adapter, anchor_commit="base123")

        self.assertTrue(plan.has_targets)
        self.assertEqual(plan.anchor_commit, "base123")
        self.assertEqual(plan.coverage_gaps, [])
        self.assertEqual(plan.impacted.changed_files, [])
        self.assertEqual(
            plan.impacted.semantic_map_entry_ids,
            ["demo.release.contract", "demo.release.version"],
        )
        self.assertEqual(
            {item["spec"] for item in plan.must_run_l2_checks},
            {"contract-version-consistency.spec.ts", "version-consistency.spec.ts"},
        )
        for item in plan.must_run_l2_checks:
            self.assertEqual(item["oracle_level"], "L2")
            self.assertIn("profile seed", item["selection_reason"])

    def test_profile_seed_plan_respects_explicit_target_plan_subset(self):
        adapter = {
            "playwright": {
                "web_subdir": "web",
                "spec_dir": "tests/integration",
                "spec_whitelist": [
                    {
                        "spec": "loadable.spec.ts",
                        "semantic_map_entry_id": "demo.loadable",
                    },
                    {
                        "spec": "blocked.spec.ts",
                        "semantic_map_entry_id": "demo.blocked",
                    },
                ],
            },
        }
        explicit_target_plan = {
            "must_run_l2_checks": [
                {
                    "spec": "loadable.spec.ts",
                    "semantic_map_entry_id": "demo.loadable",
                    "oracle_level": "L2",
                    "selection_reason": "curated runnable seed",
                }
            ]
        }

        plan = build_profile_seed_plan(
            adapter,
            anchor_commit="base123",
            explicit_target_plan=explicit_target_plan,
        )

        self.assertTrue(plan.has_targets)
        self.assertEqual(
            [item["spec"] for item in plan.must_run_l2_checks],
            ["loadable.spec.ts"],
        )
        self.assertEqual(plan.impacted.semantic_map_entry_ids, ["demo.loadable"])
        self.assertEqual(
            plan.must_run_l2_checks[0]["selection_reason"],
            "profile seed target from explicit target plan",
        )

    def test_profile_seed_plan_ignores_existing_empty_target_plan(self):
        adapter = {
            "playwright": {
                "web_subdir": "web",
                "spec_dir": "tests/integration",
                "spec_whitelist": [
                    {
                        "spec": "loadable.spec.ts",
                        "semantic_map_entry_id": "demo.loadable",
                    }
                ],
            },
        }

        plan = build_profile_seed_plan(
            adapter,
            anchor_commit="base123",
            explicit_target_plan={
                "must_run_l2_checks": [],
                "must_run_l3_relations": [],
                "must_run_l1_fuzz": [],
                "coverage_gaps": [],
            },
        )

        self.assertTrue(plan.has_targets)
        self.assertEqual([item["spec"] for item in plan.must_run_l2_checks], ["loadable.spec.ts"])
        self.assertEqual(
            plan.must_run_l2_checks[0]["selection_reason"],
            "profile seed target from adapter whitelist",
        )

    def test_declared_missing_target_plan_fails_closed_without_whitelist_fallback(self):
        adapter = {
            "knowledge_audit": {"target_plan": "missing-target-plan.json"},
            "playwright": {
                "web_subdir": "web",
                "spec_dir": "tests/integration",
                "spec_whitelist": [
                    {
                        "spec": "blocked.spec.ts",
                        "semantic_map_entry_id": "demo.blocked",
                    },
                ],
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            explicit_target_plan = _load_adapter_target_plan(
                adapter_path=Path(tmp) / "adapter.json",
                adapter=adapter,
            )

        plan = build_profile_seed_plan(
            adapter,
            anchor_commit="base123",
            explicit_target_plan=explicit_target_plan,
        )

        self.assertFalse(plan.has_targets)
        self.assertEqual(plan.must_run_l2_checks, [])
        self.assertEqual(plan.impacted.semantic_map_entry_ids, [])

    def test_markdown_render_contains_sections(self):
        plan = build_test_plan_from_dicts(["internal/server/ws.go"], CODE_MAP, ADAPTER)
        md = render_plan_markdown(plan)
        self.assertIn("针对性测试清单", md)
        self.assertIn("必跑 — L2 machine/spec checks", md)
        self.assertIn("必跑 — 现有 L3 关系", md)
        self.assertIn("必跑 — L1 fuzz", md)
        self.assertIn("补缺建议", md)

    def test_playwright_spec_change_selects_l2_spec_target(self):
        code_map = {
            **CODE_MAP,
            "links": CODE_MAP["links"]
            + [
                {
                    "from_kind": "playwright_spec",
                    "from": "context-ribbon.spec.ts",
                    "from_file": "web/tests/integration/context-ribbon.spec.ts",
                    "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
                }
            ],
        }
        adapter = {
            **ADAPTER,
            "playwright": {
                "web_subdir": "web",
                "spec_dir": "tests/integration",
                "spec_whitelist": [
                    {
                        "spec": "context-ribbon.spec.ts",
                        "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
                    }
                ],
            },
        }

        plan = build_test_plan_from_dicts(
            ["web/tests/integration/context-ribbon.spec.ts"],
            code_map,
            adapter,
        )

        self.assertTrue(plan.has_targets)
        self.assertEqual(plan.impacted.semantic_map_entry_ids, ["demo.skill.change_mode.intent-to-mode"])
        self.assertEqual(len(plan.must_run_l2_checks), 1)
        target = plan.must_run_l2_checks[0]
        self.assertEqual(target["oracle_level"], "L2")
        self.assertEqual(target["spec"], "context-ribbon.spec.ts")
        self.assertEqual(target["semantic_map_entry_id"], "demo.skill.change_mode.intent-to-mode")
        self.assertIn("impacted semantic entry", target["selection_reason"])

    def test_playwright_spec_links_create_l2_targets_without_adapter_whitelist_entry(self):
        code_map = {
            **CODE_MAP,
            "links": CODE_MAP["links"]
            + [
                {
                    "from_kind": "playwright_spec",
                    "from": "context-ribbon.spec.ts",
                    "from_file": "web/tests/integration/context-ribbon.spec.ts",
                    "semantic_map_entry_id": "demo.ui.context-ribbon",
                },
                {
                    "from_kind": "playwright_spec",
                    "from": "context-ribbon.spec.ts",
                    "from_file": "web/tests/integration/context-ribbon.spec.ts",
                    "semantic_map_entry_id": "demo.ui.workspace-selector",
                },
            ],
        }
        adapter = {
            **ADAPTER,
            "playwright": {
                "web_subdir": "web",
                "spec_dir": "tests/integration",
                "spec_whitelist": [
                    {
                        "spec": "context-ribbon.spec.ts",
                        "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
                    }
                ],
            },
        }

        plan = build_test_plan_from_dicts(
            ["web/tests/integration/context-ribbon.spec.ts"],
            code_map,
            adapter,
        )

        targets = {
            item["semantic_map_entry_id"]: item
            for item in plan.must_run_l2_checks
        }
        self.assertIn("demo.ui.context-ribbon", targets)
        self.assertIn("demo.ui.workspace-selector", targets)
        self.assertEqual(targets["demo.ui.context-ribbon"]["spec"], "context-ribbon.spec.ts")
        self.assertEqual(targets["demo.ui.context-ribbon"]["oracle_level"], "L2")
        self.assertIn("code-map playwright_spec", targets["demo.ui.context-ribbon"]["selection_reason"])

    def test_doc_links_create_targets_for_skill_document_changes(self):
        code_map = {
            **CODE_MAP,
            "links": CODE_MAP["links"]
            + [
                {
                    "from_kind": "doc",
                    "from": "change_mode skill",
                    "from_file": "assets/prompts/skills/change_mode/SKILL.md",
                    "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
                },
                {
                    "from_kind": "playwright_spec",
                    "from": "change-mode.spec.ts",
                    "from_file": "web/tests/integration/change-mode.spec.ts",
                    "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
                },
            ],
        }

        plan = build_test_plan_from_dicts(
            ["assets/prompts/skills/change_mode/SKILL.md"],
            code_map,
            ADAPTER,
        )

        self.assertEqual(plan.impacted.semantic_map_entry_ids, ["demo.skill.change_mode.intent-to-mode"])
        self.assertTrue(plan.has_targets)
        self.assertEqual(plan.must_run_l3_relations[0]["oracle_level"], "L3")
        l2_targets = {item["spec"] for item in plan.must_run_l2_checks}
        self.assertEqual(l2_targets, {"change-mode.spec.ts"})


if __name__ == "__main__":
    unittest.main()
