# -*- coding: utf-8 -*-
"""knowledge_audit 单测：离线验证知识地图完备度和选靶 gate。"""
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from pipeline_v2 import knowledge_audit
from pipeline_v2.knowledge_audit import audit_knowledge, render_audit_markdown


SEMANTIC_MAP = {
    "based_on_product_commit": "abc1234",
    "entries": [
        {
            "id": "demo.skill.change_mode.intent-to-mode",
            "skill": {"name": "change_mode"},
            "expected_terminal_state": {
                "machine_check": {"field": "current_mode", "op": "eq", "value_from": "target_mode"}
            },
        },
        {
            "id": "demo.skill.manage_skill.enable",
            "skill": {"name": "manage_skill"},
            "expected_terminal_state": {
                "machine_check": {"field": "enabled_tools", "op": "contains_key", "value": "tool_a"}
            },
        },
        {
            "id": "demo.skill.notepad.create",
            "skill": {"name": "manage_notepad"},
            "expected_terminal_state": {"assertion": "notepad exists"},
        },
    ],
}

CODE_MAP = {
    "based_on_product_commit": "abc1234",
    "modules": [
        {
            "id": "internal/server",
            "files": ["internal/server/server.go", "internal/server/ws.go"],
            "exports": [{"symbol": "DispatchCommand", "file": "internal/server/ws.go"}],
            "endpoints": [
                {"kind": "http", "method": "POST", "route": "/api/sessions", "file": "internal/server/server.go"},
                {"kind": "http", "method": "GET", "route": "/api/sessions", "file": "internal/server/server.go"},
                {"kind": "ws", "route": "/api/chat/ws/{session_id}", "file": "internal/server/ws.go"},
                {"kind": "ws_command", "command": "set_mode", "file": "internal/server/ws.go"},
            ],
        }
    ],
    "edges": [],
    "links": [
        {
            "from_kind": "ws_command",
            "from": "set_mode",
            "from_file": "internal/server/ws.go",
            "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
        }
    ],
}

ADAPTER = {
    "profile": "http-l3",
    "http_driver": {
        "scenarios": [
            {
                "scenario_id": "l3.set_mode",
                "strategy": "metamorphic",
                "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
            }
        ]
    },
    "l1_fuzz": {
        "cases": [
            {"id": "bad-session-json", "method": "POST", "path": "/api/sessions", "raw_body": "{bad"}
        ]
    },
}

CASES = [
    {
        "id": "case-1",
        "fingerprint": "case-1",
        "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
        "oracle_level": "L3",
        "based_on_product_commit": "abc1234",
    }
]

EXCLUDED = [{"id": "lead-1", "status": "excluded"}]

UNDERSTANDING_PROFILE = {
    "product": "demo",
    "based_on_product_commit": "abc1234",
    "domains": [
        {
            "id": "control-plane",
            "title": "HTTP/WS control plane",
            "required_entry_ids": ["demo.skill.change_mode.intent-to-mode"],
            "minimum_code_links": 1,
            "required_oracle_levels": ["L2", "L3"],
        },
        {
            "id": "frontend-ui",
            "title": "Frontend UI workflows",
            "required_entry_ids": ["demo.skill.frontend.workflow"],
            "minimum_code_links": 1,
            "required_oracle_levels": ["L2"],
        },
        {
            "id": "test-data-contract",
            "title": "Test data contract",
            "required_entry_ids": [],
            "minimum_entries": 1,
        },
    ],
}

TARGET_PLAN = {
    "must_run_l3_relations": [
        {"scenario_id": "l3.set_mode", "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode"}
    ],
    "must_run_l1_fuzz": [
        {"case_id": "bad-session-json", "semantic_map_entry_id": "demo.auth.session-contract"}
    ],
    "coverage_gaps": [{"kind": "entry_without_l3_relation"}],
}


class KnowledgeAuditTest(unittest.TestCase):
    def test_git_head_returns_full_sha_for_explicit_product_head_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            product = Path(tmpdir) / "product"
            product.mkdir()
            (product / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(product), "init"], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "-C", str(product), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(product), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(product), "add", "."], check=True)
            subprocess.run(["git", "-C", str(product), "commit", "-m", "seed"], check=True, stdout=subprocess.PIPE)
            full_head = subprocess.run(
                ["git", "-C", str(product), "rev-parse", "HEAD"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            self.assertEqual(knowledge_audit._git_head(product), full_head)

    def test_audit_summarizes_links_oracles_cases_and_gaps(self):
        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=ADAPTER,
            cases=CASES,
            excluded_leads=EXCLUDED,
            target_plan=TARGET_PLAN,
            product_head="abc1234",
        )

        data = report.to_dict()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["selection_policy"], ["L2", "L3", "L1", "L4-suggestion"])
        self.assertEqual(data["mutation_testing_default"], "disabled")
        self.assertEqual(data["counts"]["semantic_entry_count"], 3)
        self.assertEqual(data["counts"]["machine_check_entry_count"], 2)
        self.assertEqual(data["counts"]["code_link_count"], 1)
        self.assertEqual(data["counts"]["linked_semantic_entry_count"], 1)
        self.assertEqual(data["counts"]["http_ws_endpoint_count"], 3)
        self.assertEqual(data["counts"]["ws_command_count"], 1)
        self.assertEqual(data["counts"]["l3_relation_count"], 1)
        self.assertEqual(data["counts"]["l3_entry_count"], 1)
        self.assertEqual(data["counts"]["l1_fuzz_case_count"], 1)
        self.assertEqual(data["counts"]["l1_covered_endpoint_count"], 1)
        self.assertEqual(data["counts"]["case_count"], 1)
        self.assertEqual(data["counts"]["excluded_lead_count"], 1)
        self.assertEqual(data["counts"]["handled_finding_count"], 0)
        self.assertEqual(data["counts"]["finding_covered_entry_count"], 0)
        self.assertEqual(data["counts"]["case_or_finding_covered_entry_count"], 1)
        self.assertEqual(data["counts"]["target_plan_l3_count"], 1)
        self.assertEqual(data["counts"]["target_plan_l1_count"], 1)
        self.assertEqual(data["counts"]["target_plan_gap_count"], 1)
        self.assertEqual(data["counts"]["playwright_testdata_gate_count"], 0)
        self.assertEqual(data["counts"]["playwright_testdata_gated_spec_count"], 0)
        self.assertEqual(data["counts"]["playwright_testdata_gate_required_env_count"], 0)
        self.assertEqual(data["target_summary"]["oracle_levels"], ["L1", "L3", "L4-suggestion"])
        self.assertEqual(
            data["target_summary"]["semantic_entry_ids"],
            ["demo.auth.session-contract", "demo.skill.change_mode.intent-to-mode"],
        )
        self.assertEqual(data["target_summary"]["l1_endpoints"], [])
        endpoint_gaps = {
            (item["method"], item["route"])
            for item in data["gaps"]["endpoints_without_l1_fuzz"]
        }
        self.assertIn(("GET", "/api/sessions"), endpoint_gaps)
        self.assertNotIn(("POST", "/api/sessions"), endpoint_gaps)
        self.assertEqual(
            data["gaps"]["entries_without_code_links"],
            ["demo.skill.manage_skill.enable", "demo.skill.notepad.create"],
        )
        self.assertIn("demo.skill.manage_skill.enable", data["gaps"]["entries_without_l3_relations"])
        self.assertEqual(
            data["gaps"]["entries_without_case_or_finding"],
            ["demo.skill.manage_skill.enable", "demo.skill.notepad.create"],
        )
        self.assertEqual(data["blockers"], [])

    def test_top_level_l3_relations_count_as_l3_coverage(self):
        adapter = {
            "l3_relations": [
                {
                    "scenario_id": "l3.owner_gated.notepad_referenceable",
                    "strategy": "stateful_owner_gated",
                    "semantic_map_entry_id": "demo.skill.notepad.create",
                    "execution_gate": "owner-gated",
                }
            ]
        }

        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=adapter,
            product_head="abc1234",
        )

        data = report.to_dict()
        self.assertEqual(data["counts"]["l3_relation_count"], 1)
        self.assertEqual(data["counts"]["l3_entry_count"], 1)
        self.assertNotIn(
            "demo.skill.notepad.create",
            data["gaps"]["entries_without_l3_relations"],
        )

    def test_excluded_leads_count_as_handled_findings_not_cases(self):
        excluded = [
            {
                "id": "current-non-bug",
                "status": "excluded",
                "semantic_map_entry_id": "demo.skill.manage_skill.enable",
                "oracle_level": "L2",
                "based_on_product_commit": "abc1234",
            },
            {
                "id": "owner-gated-l4",
                "status": "owner-gated-lead",
                "semantic_map_entry_ids": ["demo.skill.notepad.create"],
                "related_semantic_map_entry_ids": ["demo.skill.change_mode.intent-to-mode"],
                "oracle_level": "L4-suggestion",
                "based_on_product_commit": "abc1234",
            },
            {
                "id": "runtime-target-ready",
                "status": "runtime-target-ready",
                "semantic_map_entry_ids": [
                    "demo.skill.manage_skill.enable",
                    "demo.skill.notepad.create",
                ],
                "oracle_level": "L2",
                "based_on_product_commit": "abc1234",
            },
            {
                "id": "open-lead-does-not-cover",
                "status": "open-lead",
                "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
                "based_on_product_commit": "abc1234",
            },
            {
                "id": "stale-excluded",
                "status": "excluded",
                "semantic_map_entry_id": "demo.skill.notepad.create",
                "based_on_product_commit": "old1234",
            },
            {
                "id": "missing-commit-excluded",
                "status": "excluded",
                "semantic_map_entry_id": "demo.skill.notepad.create",
            },
        ]

        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=ADAPTER,
            cases=CASES,
            excluded_leads=excluded,
            product_head="abc1234",
        )

        data = report.to_dict()
        self.assertEqual(data["counts"]["case_covered_entry_count"], 1)
        self.assertEqual(data["counts"]["fresh_finding_count"], 3)
        self.assertEqual(data["counts"]["handled_finding_count"], 3)
        self.assertEqual(data["counts"]["stale_finding_count"], 2)
        self.assertEqual(data["counts"]["finding_covered_entry_count"], 2)
        self.assertEqual(data["counts"]["case_or_finding_covered_entry_count"], 3)
        self.assertEqual(
            data["gaps"]["entries_without_cases"],
            ["demo.skill.manage_skill.enable", "demo.skill.notepad.create"],
        )
        self.assertEqual(data["gaps"]["entries_without_case_or_finding"], [])
        self.assertEqual(
            {item["id"] for item in data["gaps"]["stale_findings"]},
            {"stale-excluded", "missing-commit-excluded"},
        )

        markdown = render_audit_markdown(report)
        self.assertIn("handled findings: fresh 3 / stale 2", markdown)
        self.assertIn("entries without confirmed cases: 2", markdown)
        self.assertIn("entries without case/finding: 0", markdown)

    def test_l1_endpoint_coverage_requires_exact_route_shape(self):
        code_map = {
            "based_on_product_commit": "abc1234",
            "modules": [
                {
                    "id": "internal/server",
                    "endpoints": [
                        {"kind": "http", "method": "GET", "route": "/api/sessions/{id}"},
                        {"kind": "http", "method": "GET", "route": "/api/sessions/{id}/history"},
                    ],
                }
            ],
            "links": [],
        }
        adapter = {
            "l1_fuzz": {
                "cases": [
                    {
                        "id": "session-history-no-crash",
                        "method": "GET",
                        "path": "/api/sessions/s1/history",
                    }
                ]
            }
        }

        data = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=code_map,
            adapter=adapter,
            product_head="abc1234",
        ).to_dict()

        self.assertEqual(data["counts"]["l1_covered_endpoint_count"], 1)
        self.assertEqual(
            data["gaps"]["endpoints_without_l1_fuzz"],
            [{"kind": "http", "method": "GET", "route": "/api/sessions/{id}"}],
        )

    def test_non_confirmed_cases_do_not_count_as_case_coverage(self):
        cases = [
            {
                "id": "candidate",
                "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
                "oracle_level": "L3",
                "status": "candidate",
                "based_on_product_commit": "abc1234",
            },
            {
                "id": "l4-suggestion",
                "semantic_map_entry_id": "demo.skill.manage_skill.enable",
                "oracle_level": "L4-suggestion",
                "status": "reported",
                "based_on_product_commit": "abc1234",
            },
            {
                "id": "false-positive",
                "semantic_map_entry_id": "demo.skill.notepad.create",
                "oracle_level": "L2",
                "status": "reported",
                "triage_result": "false-positive",
                "based_on_product_commit": "abc1234",
            },
            {
                "id": "owner-review-required",
                "semantic_map_entry_id": "demo.skill.manage_skill.enable",
                "oracle_level": "L2",
                "status": "reported",
                "triage_result": "owner-review-required",
                "based_on_product_commit": "abc1234",
            },
            {
                "id": "reported",
                "semantic_map_entry_id": "demo.skill.notepad.create",
                "oracle_level": "L2",
                "status": "reported",
                "triage_result": "contract-mismatch-code",
                "based_on_product_commit": "abc1234",
            },
        ]

        data = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=ADAPTER,
            cases=cases,
            product_head="abc1234",
        ).to_dict()

        self.assertEqual(data["counts"]["case_count"], 5)
        self.assertEqual(data["counts"]["fresh_case_count"], 1)
        self.assertEqual(data["counts"]["case_covered_entry_count"], 1)
        self.assertEqual(
            data["gaps"]["entries_without_cases"],
            ["demo.skill.change_mode.intent-to-mode", "demo.skill.manage_skill.enable"],
        )

    def test_audit_counts_playwright_testdata_gates(self):
        adapter = {
            "profile": "bug-mining",
            "playwright": {
                "testdata_gates": [
                    {
                        "id": "fixture-a",
                        "specs": ["workspace-pagination.spec.ts", "list-model-servings.spec.ts"],
                        "required_env": ["SAMPLE_COOKIE", "SAMPLE_WORKSPACE_ID"],
                    },
                    {
                        "id": "fixture-b",
                        "specs": ["workspace-pagination.spec.ts"],
                        "required_env": ["SAMPLE_COOKIE"],
                    },
                ]
            },
        }

        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=adapter,
            product_head="abc1234",
        )

        data = report.to_dict()
        self.assertEqual(data["counts"]["playwright_testdata_gate_count"], 2)
        self.assertEqual(data["counts"]["playwright_testdata_gated_spec_count"], 2)
        self.assertEqual(data["counts"]["playwright_testdata_gate_required_env_count"], 2)
        markdown = render_audit_markdown(report)
        self.assertIn("playwright testdata gates: 2", markdown)

    def test_understanding_profile_surfaces_product_level_gaps(self):
        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=ADAPTER,
            cases=CASES,
            excluded_leads=EXCLUDED,
            target_plan=TARGET_PLAN,
            understanding_profile=UNDERSTANDING_PROFILE,
            product_head="abc1234",
        )

        data = report.to_dict()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["counts"]["understanding_domain_count"], 3)
        self.assertEqual(data["counts"]["understanding_domain_covered_count"], 1)
        self.assertEqual(data["counts"]["understanding_domain_missing_count"], 2)
        self.assertEqual(data["next_step_category"], "add-code-link")
        domains = {item["id"]: item for item in data["product_understanding"]}
        self.assertEqual(domains["control-plane"]["status"], "covered")
        self.assertEqual(domains["frontend-ui"]["status"], "missing")
        self.assertEqual(domains["frontend-ui"]["missing_entry_ids"], ["demo.skill.frontend.workflow"])
        gap_ids = {item["id"] for item in data["gaps"]["understanding_domains"]}
        self.assertEqual(gap_ids, {"frontend-ui", "test-data-contract"})

    def test_adapter_can_downgrade_understanding_domain_for_narrow_profile(self):
        understanding = {
            "product": "demo",
            "based_on_product_commit": "abc1234",
            "domains": [
                {
                    "id": "l2-seed-only",
                    "title": "L2 seed only domain",
                    "required_entry_ids": ["demo.skill.manage_skill.enable"],
                    "minimum_code_links": 0,
                    "required_oracle_levels": ["L2", "L3"],
                    "notes": "Full profile expects L3.",
                }
            ],
        }
        l2_only_adapter = {
            "profile": "auth-disabled-local",
            "knowledge_audit": {
                "understanding_domain_overrides": {
                    "l2-seed-only": {
                        "required_oracle_levels": ["L2"],
                        "notes_append": "Narrow profile checks only L2 seed targets; L3 remains in http-l3.",
                    }
                }
            },
        }

        base_report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=ADAPTER,
            cases=CASES,
            excluded_leads=EXCLUDED,
            target_plan=TARGET_PLAN,
            understanding_profile=understanding,
            product_head="abc1234",
        )
        base_domain = base_report.to_dict()["product_understanding"][0]
        self.assertEqual(base_domain["status"], "partial")
        self.assertEqual(base_domain["missing_oracle_levels"], ["L3"])

        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=l2_only_adapter,
            cases=CASES,
            excluded_leads=EXCLUDED,
            target_plan=TARGET_PLAN,
            understanding_profile=understanding,
            product_head="abc1234",
        )

        data = report.to_dict()
        domains = {item["id"]: item for item in data["product_understanding"]}
        self.assertEqual(domains["l2-seed-only"]["status"], "covered")
        self.assertEqual(domains["l2-seed-only"]["missing_oracle_levels"], [])
        self.assertEqual(domains["l2-seed-only"]["oracle_levels"], ["L2"])
        self.assertIn("Narrow profile checks only L2 seed targets", domains["l2-seed-only"]["notes"])
        self.assertEqual(data["counts"]["understanding_domain_covered_count"], 1)

        markdown = render_audit_markdown(report)
        self.assertIn("l2-seed-only notes: Full profile expects L3.", markdown)
        self.assertIn("Narrow profile checks only L2 seed targets", markdown)

    def test_adapter_override_cannot_remove_required_evidence_gate(self):
        understanding = {
            "product": "demo",
            "based_on_product_commit": "abc1234",
            "domains": [
                {
                    "id": "evidence-hard-gate",
                    "required_entry_ids": ["demo.skill.manage_skill.enable"],
                    "minimum_code_links": 0,
                    "required_oracle_levels": ["L2", "L3"],
                    "required_evidence_kinds": ["product_fact"],
                }
            ],
        }
        adapter = {
            "profile": "auth-disabled-local",
            "knowledge_audit": {
                "understanding_domain_overrides": {
                    "evidence-hard-gate": {
                        "minimum_entries": 0,
                        "minimum_code_links": 0,
                        "required_link_kinds": [],
                        "required_oracle_levels": ["L2"],
                        "required_evidence_kinds": [],
                        "runtime_gated": False,
                        "owner_gated": False,
                        "blocking": False,
                    }
                }
            },
        }

        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=adapter,
            cases=CASES,
            excluded_leads=EXCLUDED,
            target_plan=TARGET_PLAN,
            understanding_profile=understanding,
            product_head="abc1234",
        )

        data = report.to_dict()
        self.assertEqual(data["status"], "fail")
        self.assertEqual(data["blockers"][0]["kind"], "understanding_evidence_missing")
        self.assertEqual(data["next_step_category"], "add-ui-spec-link")
        domain = data["product_understanding"][0]
        self.assertEqual(domain["status"], "partial")
        self.assertEqual(domain["missing_oracle_levels"], [])
        self.assertEqual(domain["missing_evidence_kinds"], ["product_fact"])

    def test_surface_links_are_audited_without_satisfying_backend_code_links(self):
        semantic_map = {
            "based_on_product_commit": "abc1234",
            "entries": SEMANTIC_MAP["entries"]
            + [
                {
                    "id": "demo.skill.frontend.workflow",
                    "skill": {"name": "frontend"},
                    "expected_terminal_state": {
                        "machine_check": {"field": "visible", "op": "eq", "value": True}
                    },
                }
            ],
        }
        code_map = json.loads(json.dumps(CODE_MAP))
        code_map["links"].append(
            {
                "from_kind": "playwright_spec",
                "from": "context-ribbon.spec.ts",
                "from_file": "web/tests/integration/context-ribbon.spec.ts",
                "semantic_map_entry_id": "demo.skill.frontend.workflow",
            }
        )
        understanding = {
            "domains": [
                {
                    "id": "frontend-spec-covered",
                    "required_entry_ids": ["demo.skill.frontend.workflow"],
                    "required_link_kinds": ["playwright_spec"],
                    "minimum_code_links": 0,
                    "required_oracle_levels": ["L2"],
                },
                {
                    "id": "frontend-needs-backend-code",
                    "required_entry_ids": ["demo.skill.frontend.workflow"],
                    "required_link_kinds": ["playwright_spec"],
                    "minimum_code_links": 1,
                    "required_oracle_levels": ["L2"],
                },
            ]
        }

        data = audit_knowledge(
            semantic_map=semantic_map,
            code_map=code_map,
            adapter=ADAPTER,
            understanding_profile=understanding,
            product_head="abc1234",
        ).to_dict()

        self.assertEqual(data["counts"]["knowledge_link_count"], 2)
        self.assertEqual(data["counts"]["code_link_count"], 1)
        self.assertEqual(data["counts"]["surface_link_count"], 1)
        self.assertEqual(data["counts"]["playwright_spec_link_count"], 1)
        domains = {item["id"]: item for item in data["product_understanding"]}
        self.assertEqual(domains["frontend-spec-covered"]["status"], "covered")
        self.assertEqual(domains["frontend-spec-covered"]["link_kind_counts"], {"playwright_spec": 1})
        self.assertEqual(domains["frontend-needs-backend-code"]["status"], "partial")
        self.assertEqual(domains["frontend-needs-backend-code"]["code_link_count"], 0)
        self.assertEqual(domains["frontend-needs-backend-code"]["missing_link_kinds"], [])

    def test_audit_blocks_stale_maps_and_empty_target_plan(self):
        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=ADAPTER,
            target_plan={"must_run_l3_relations": [], "must_run_l1_fuzz": [], "coverage_gaps": []},
            product_head="def5678",
        )

        data = report.to_dict()
        self.assertEqual(data["status"], "fail")
        blocker_kinds = {item["kind"] for item in data["blockers"]}
        self.assertEqual(blocker_kinds, {"semantic_map_stale", "code_map_stale", "empty_target_plan"})
        self.assertEqual(data["next_step_category"], "add-code-link")

    def test_audit_blocks_maps_missing_commit_when_product_head_is_known(self):
        semantic = {"entries": SEMANTIC_MAP["entries"]}
        code = {"modules": CODE_MAP["modules"], "edges": [], "links": CODE_MAP["links"]}

        report = audit_knowledge(
            semantic_map=semantic,
            code_map=code,
            adapter=ADAPTER,
            product_head="abc1234",
        )

        data = report.to_dict()
        self.assertEqual(data["status"], "fail")
        blocker_kinds = {item["kind"] for item in data["blockers"]}
        self.assertEqual(blocker_kinds, {"semantic_map_missing_commit", "code_map_missing_commit"})
        self.assertEqual(data["next_step_category"], "add-code-link")

    def test_audit_blocks_stale_understanding_profile(self):
        understanding = dict(UNDERSTANDING_PROFILE)
        understanding["based_on_product_commit"] = "old1234"

        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=ADAPTER,
            understanding_profile=understanding,
            product_head="abc1234",
        )

        data = report.to_dict()
        self.assertEqual(data["status"], "fail")
        blocker_kinds = {item["kind"] for item in data["blockers"]}
        self.assertEqual(blocker_kinds, {"understanding_profile_stale"})
        self.assertEqual(data["next_step_category"], "add-code-link")

    def test_audit_blocks_l4_only_target_plan(self):
        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=ADAPTER,
            target_plan={
                "coverage_gaps": [
                    {
                        "kind": "entry_without_l3_relation",
                        "semantic_map_entry_id": "demo.skill.notepad.create",
                    }
                ]
            },
            product_head="abc1234",
        )

        data = report.to_dict()
        self.assertEqual(data["status"], "fail")
        blocker_kinds = {item["kind"] for item in data["blockers"]}
        self.assertEqual(blocker_kinds, {"l4_only_target_plan"})
        self.assertEqual(data["next_step_category"], "add-l3-oracle")

    def test_stale_cases_do_not_count_as_current_coverage(self):
        stale_cases = [
            {
                "id": "old-case",
                "fingerprint": "old-case",
                "semantic_map_entry_id": "demo.skill.change_mode.intent-to-mode",
                "oracle_level": "L3",
                "based_on_product_commit": "old1234",
            }
        ]

        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=ADAPTER,
            cases=stale_cases,
            product_head="abc1234",
        )

        data = report.to_dict()
        self.assertEqual(data["counts"]["case_count"], 1)
        self.assertEqual(data["counts"]["fresh_case_count"], 0)
        self.assertEqual(data["counts"]["stale_case_count"], 1)
        self.assertEqual(data["counts"]["case_covered_entry_count"], 0)
        self.assertIn("demo.skill.change_mode.intent-to-mode", data["gaps"]["entries_without_cases"])
        self.assertEqual(data["gaps"]["stale_cases"][0]["id"], "old-case")

    def test_markdown_and_cli_render_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            semantic_path = root / "semantic-map.yaml"
            code_path = root / "code-map.yaml"
            adapter_path = root / "adapter.json"
            cases_path = root / "cases.json"
            excluded_path = root / "excluded.json"
            target_path = root / "target-plan.json"
            understanding_path = root / "understanding.json"
            semantic_path.write_text(yaml.safe_dump(SEMANTIC_MAP, sort_keys=False), encoding="utf-8")
            code_path.write_text(yaml.safe_dump(CODE_MAP, sort_keys=False), encoding="utf-8")
            adapter_path.write_text(json.dumps(ADAPTER), encoding="utf-8")
            cases_path.write_text(json.dumps(CASES), encoding="utf-8")
            excluded_path.write_text(json.dumps(EXCLUDED), encoding="utf-8")
            target_path.write_text(json.dumps(TARGET_PLAN), encoding="utf-8")
            understanding_path.write_text(json.dumps(UNDERSTANDING_PROFILE), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = knowledge_audit._main(
                    [
                        "--semantic-map",
                        str(semantic_path),
                        "--code-map",
                        str(code_path),
                        "--adapter",
                        str(adapter_path),
                        "--cases",
                        str(cases_path),
                        "--excluded-leads",
                        str(excluded_path),
                        "--target-plan",
                        str(target_path),
                        "--understanding-profile",
                        str(understanding_path),
                        "--product-head",
                        "abc1234",
                        "--format",
                        "json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["counts"]["understanding_domain_count"], 3)

    def test_cli_loads_target_plan_from_adapter_knowledge_audit_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            semantic_path = root / "semantic-map.yaml"
            code_path = root / "code-map.yaml"
            adapter_path = root / "adapter.json"
            target_path = root / "target-plan.json"
            semantic_path.write_text(yaml.safe_dump(SEMANTIC_MAP, sort_keys=False), encoding="utf-8")
            code_path.write_text(yaml.safe_dump(CODE_MAP, sort_keys=False), encoding="utf-8")
            target_path.write_text(json.dumps(TARGET_PLAN), encoding="utf-8")
            adapter = dict(ADAPTER)
            adapter["knowledge_audit"] = {"target_plan": str(target_path)}
            adapter_path.write_text(json.dumps(adapter), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = knowledge_audit._main(
                    [
                        "--semantic-map",
                        str(semantic_path),
                        "--code-map",
                        str(code_path),
                        "--adapter",
                        str(adapter_path),
                        "--product-head",
                        "abc1234",
                        "--format",
                        "json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["counts"]["target_plan_l3_count"], 1)
            self.assertEqual(payload["counts"]["target_plan_l1_count"], 1)
            self.assertEqual(payload["counts"]["target_plan_gap_count"], 1)

        markdown = render_audit_markdown(
            audit_knowledge(
                semantic_map=SEMANTIC_MAP,
                code_map=CODE_MAP,
                adapter=ADAPTER,
                understanding_profile=UNDERSTANDING_PROFILE,
                product_head="abc1234",
            )
        )
        self.assertIn("知识图谱审计", markdown)
        self.assertIn("产品理解覆盖面", markdown)
        self.assertIn("默认不启用全量 mutation", markdown)

    def test_markdown_renders_surface_link_and_missing_kind_details(self):
        semantic_map = {
            "based_on_product_commit": "abc1234",
            "entries": SEMANTIC_MAP["entries"]
            + [
                {
                    "id": "demo.skill.frontend.workflow",
                    "skill": {"name": "frontend"},
                    "expected_terminal_state": {
                        "machine_check": {"field": "visible", "op": "eq", "value": True}
                    },
                }
            ],
        }
        code_map = json.loads(json.dumps(CODE_MAP))
        code_map["links"].append(
            {
                "from_kind": "playwright_spec",
                "from": "context-ribbon.spec.ts",
                "from_file": "web/tests/integration/context-ribbon.spec.ts",
                "semantic_map_entry_id": "demo.skill.frontend.workflow",
            }
        )
        understanding = {
            "domains": [
                {
                    "id": "frontend-ui",
                    "required_entry_ids": ["demo.skill.frontend.workflow"],
                    "required_link_kinds": ["playwright_spec", "ui_component"],
                    "minimum_code_links": 0,
                    "required_oracle_levels": ["L2"],
                }
            ]
        }

        markdown = render_audit_markdown(
            audit_knowledge(
                semantic_map=semantic_map,
                code_map=code_map,
                adapter=ADAPTER,
                understanding_profile=understanding,
                product_head="abc1234",
            )
        )

        self.assertIn("knowledge links: 2；backend: 1；surface: 1", markdown)
        self.assertIn("surface detail: playwright_spec=1", markdown)
        self.assertIn("missing link kinds: ui_component", markdown)

    def test_understanding_profile_requires_structured_evidence_anchors(self):
        understanding = {
            "domains": [
                {
                    "id": "auth-session",
                    "required_entry_ids": ["demo.skill.change_mode.intent-to-mode"],
                    "minimum_code_links": 1,
                    "required_oracle_levels": ["L2", "L3"],
                    "required_evidence_kinds": [
                        "product_fact",
                        "code_or_ui_spec",
                        "oracle",
                        "gate",
                    ],
                    "evidence_anchors": {
                        "product_fact": [
                            {
                                "path": "web/tests/integration/auth-sso.spec.ts",
                                "summary": "session cookie is reused by protected REST calls",
                            }
                        ],
                        "code_or_ui_spec": [
                            {
                                "path": "internal/server/auth.go",
                                "summary": "auth middleware owns app session lookup",
                            }
                        ],
                        "oracle": [
                            {"level": "L2", "summary": "machine_check observes authenticated session"}
                        ],
                        "gate": [
                            {"kind": "owner", "summary": "real SSO requires owner-provided account"}
                        ],
                    },
                },
                {
                    "id": "frontend-ui",
                    "required_entry_ids": ["demo.skill.change_mode.intent-to-mode"],
                    "minimum_code_links": 1,
                    "required_oracle_levels": ["L2"],
                    "required_evidence_kinds": ["product_fact", "code_or_ui_spec"],
                    "evidence_anchors": {
                        "product_fact": [
                            {
                                "path": "web/tests/integration/context-ribbon.spec.ts",
                                "summary": "ribbon spec describes attachment workflow",
                            }
                        ]
                    },
                },
            ]
        }

        report = audit_knowledge(
            semantic_map=SEMANTIC_MAP,
            code_map=CODE_MAP,
            adapter=ADAPTER,
            cases=CASES,
            understanding_profile=understanding,
            product_head="abc1234",
        )
        data = report.to_dict()
        self.assertEqual(data["status"], "fail")
        self.assertIn("understanding_evidence_missing", {item["kind"] for item in data["blockers"]})
        domains = {item["id"]: item for item in data["product_understanding"]}

        self.assertEqual(domains["auth-session"]["status"], "covered")
        self.assertEqual(domains["auth-session"]["evidence_anchor_count"], 4)
        self.assertEqual(domains["auth-session"]["missing_evidence_kinds"], [])
        self.assertEqual(domains["frontend-ui"]["status"], "partial")
        self.assertEqual(domains["frontend-ui"]["missing_evidence_kinds"], ["code_or_ui_spec"])
        self.assertEqual(data["counts"]["understanding_evidence_anchor_count"], 5)

        markdown = render_audit_markdown(report)
        self.assertIn("evidence: facts/code-ui/oracle/gate = 1/1/1/1", markdown)
        self.assertIn("auth-session product_fact: web/tests/integration/auth-sso.spec.ts", markdown)
        self.assertIn("frontend-ui missing evidence kinds: code_or_ui_spec", markdown)

    def test_understanding_evidence_anchors_must_resolve_under_allowed_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            product = root / "product"
            product.mkdir()
            (product / "docs").mkdir()
            (product / "docs" / "fact.md").write_text("fact", encoding="utf-8")
            understanding = {
                "domains": [
                    {
                        "id": "bad-anchor",
                        "required_entry_ids": ["demo.skill.change_mode.intent-to-mode"],
                        "minimum_code_links": 1,
                        "required_oracle_levels": ["L2", "L3"],
                        "required_evidence_kinds": ["product_fact", "code_or_ui_spec", "oracle", "gate"],
                        "evidence_anchors": {
                            "product_fact": [{"path": "docs/fact.md"}],
                            "code_or_ui_spec": [{"path": "../outside.go"}],
                            "oracle": [{"level": "L5"}],
                            "gate": [{"kind": "bad gate kind"}],
                        },
                    }
                ]
            }

            data = audit_knowledge(
                semantic_map=SEMANTIC_MAP,
                code_map=CODE_MAP,
                adapter=ADAPTER,
                cases=CASES,
                understanding_profile=understanding,
                product_head="abc1234",
                product_repo=product,
                automation_root=root / "automation",
            ).to_dict()

        domain = data["product_understanding"][0]
        self.assertEqual(data["status"], "fail")
        self.assertIn("understanding_evidence_missing", {item["kind"] for item in data["blockers"]})
        self.assertEqual(domain["status"], "partial")
        self.assertEqual(domain["missing_evidence_kinds"], ["code_or_ui_spec", "gate", "oracle"])
        reasons = {item["reason"] for item in domain["invalid_evidence_anchors"]}
        self.assertIn("path must not contain parent traversal", reasons)
        self.assertIn("oracle level must be one of L1, L2, L3", reasons)
        self.assertIn("gate kind must be a safe slug", reasons)


if __name__ == "__main__":
    unittest.main()
