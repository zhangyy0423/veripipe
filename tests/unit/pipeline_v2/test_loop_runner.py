import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline_v2.loop_runner import LoopConfig, run_loop


class LoopRunnerTest(unittest.TestCase):
    def legacy_loop_config(self, **kwargs) -> LoopConfig:
        kwargs.setdefault("stop_on_not_ready", False)
        return LoopConfig(enforce_knowledge_gate=False, **kwargs)

    def write_adapter(self, root: Path, *, profile: str = "bug-mining") -> Path:
        adapter = root / "adapter.json"
        adapter.write_text(
            json.dumps(
                {
                    "profile": profile,
                    "profile_description": f"{profile} test profile",
                    "profile_scope": "test-scope",
                    "profile_limitations": ["not full product readiness"],
                    "playwright": {
                        "spec_whitelist": [
                            {
                                "spec": "context-ribbon.spec.ts",
                                "semantic_map_entry_id": "sample.skill.filesystem.workspace-file-navigation-and-creation",
                            },
                            {
                                "spec": "workspace-pagination.spec.ts",
                                "semantic_map_entry_id": "sample.skill.filesystem.workspace-file-navigation-and-creation",
                            },
                            {
                                "spec": "wrench-panel.spec.ts",
                                "semantic_map_entry_id": "sample.skill.manage_skill.enable-required-skill-before-use",
                            },
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        return adapter

    def test_summary_explains_profile_coverage_blockers_and_next_step(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = self.write_adapter(root)
            preflight = mock.Mock(
                returncode=1,
                stdout=json.dumps(
                    {
                        "status": "not-ready",
                        "failed_count": 2,
                        "checks": [
                            {"name": "playwright.chromium", "status": "fail", "detail": "missing browser"},
                            {"name": "env.SAMPLE_COOKIE", "status": "fail", "detail": "missing or empty"},
                        ],
                    }
                ),
                stderr="secret-like stdout must not be copied",
            )
            run_command = mock.Mock(return_value=preflight)

            run_loop(
                self.legacy_loop_config(
                    adapter=adapter,
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    stop_on_not_ready=True,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["profile"], "bug-mining")
            self.assertEqual(summary["profile_scope"], "test-scope")
            self.assertEqual(summary["profile_limitations"], ["not full product readiness"])
            self.assertEqual(summary["profile_description"], "bug-mining test profile")
            self.assertEqual(summary["spec_count"], 3)
            self.assertEqual(summary["semantic_entry_count"], 2)
            self.assertEqual(
                summary["status_counts"],
                {
                    "environment-event": 0,
                    "not-ready": 1,
                    "observed": 0,
                    "processed": 0,
                    "reported": 0,
                    "skipped": 1,
                },
            )
            self.assertEqual(
                summary["round_stats"],
                [
                    {
                        "round": 1,
                        "status": "not-ready",
                        "executed": 0,
                        "skipped": 1,
                        "not_ready": 1,
                        "environment_event": 0,
                        "reported": 0,
                        "observed": 0,
                        "signals": 0,
                    }
                ],
            )
            self.assertEqual(summary["major_blocker"]["status"], "not-ready")
            self.assertIn("env.SAMPLE_COOKIE", summary["major_blocker"]["detail"])
            self.assertEqual(summary["next_step_category"], "runtime-preflight")
            self.assertNotIn("secret-like", json.dumps(summary, ensure_ascii=False))

    def test_knowledge_audit_blocks_stale_maps_before_preflight(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = self.write_adapter(root)
            semantic_map = root / "semantic-map.yaml"
            code_map = root / "code-map.yaml"
            semantic_map.write_text(
                "based_on_product_commit: old1234\nentries:\n- id: demo.entry\n",
                encoding="utf-8",
            )
            code_map.write_text(
                "based_on_product_commit: old1234\nmodules: []\nlinks: []\nedges: []\n",
                encoding="utf-8",
            )
            run_command = mock.Mock()

            result = run_loop(
                LoopConfig(
                    adapter=adapter,
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    semantic_map=semantic_map,
                    code_map=code_map,
                    product_head="new5678",
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 0)
            self.assertEqual(result.rounds, ())
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["knowledge_audit"]["status"], "fail")
            self.assertEqual(summary["next_step_category"], "add-code-link")
            self.assertIn("knowledge-audit-blocked", summary["stop_reason"])

    def test_knowledge_gate_blocks_missing_map_inputs_before_preflight(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = self.write_adapter(root)
            run_command = mock.Mock()

            run_loop(
                LoopConfig(
                    adapter=adapter,
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 0)
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            blocker_kinds = {item["kind"] for item in summary["knowledge_audit"]["blockers"]}
            self.assertEqual(blocker_kinds, {"knowledge_gate_missing_inputs"})
            self.assertIn("semantic_map", summary["knowledge_audit"]["blockers"][0]["detail"])
            self.assertIn("code_map", summary["knowledge_audit"]["blockers"][0]["detail"])

    def test_knowledge_audit_blocks_empty_target_plan_before_preflight(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = self.write_adapter(root)
            semantic_map = root / "semantic-map.yaml"
            code_map = root / "code-map.yaml"
            target_plan = root / "target-plan.json"
            semantic_map.write_text(
                "based_on_product_commit: abc1234\nentries:\n- id: demo.entry\n",
                encoding="utf-8",
            )
            code_map.write_text(
                "based_on_product_commit: abc1234\nmodules: []\nlinks: []\nedges: []\n",
                encoding="utf-8",
            )
            target_plan.write_text(
                json.dumps({"must_run_l3_relations": [], "must_run_l1_fuzz": [], "coverage_gaps": []}),
                encoding="utf-8",
            )
            run_command = mock.Mock()

            run_loop(
                LoopConfig(
                    adapter=adapter,
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    semantic_map=semantic_map,
                    code_map=code_map,
                    target_plan=target_plan,
                    product_head="abc1234",
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 0)
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            blocker_kinds = {item["kind"] for item in summary["knowledge_audit"]["blockers"]}
            self.assertEqual(blocker_kinds, {"empty_target_plan"})
            self.assertEqual(summary["semantic_entry_count"], 1)

    def test_knowledge_audit_blocks_l4_only_target_plan_before_preflight(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = self.write_adapter(root)
            semantic_map = root / "semantic-map.yaml"
            code_map = root / "code-map.yaml"
            target_plan = root / "target-plan.json"
            semantic_map.write_text(
                "based_on_product_commit: abc1234\nentries:\n- id: demo.entry\n",
                encoding="utf-8",
            )
            code_map.write_text(
                "based_on_product_commit: abc1234\nmodules: []\nlinks: []\nedges: []\n",
                encoding="utf-8",
            )
            target_plan.write_text(
                json.dumps({"coverage_gaps": [{"semantic_map_entry_id": "demo.entry"}]}),
                encoding="utf-8",
            )
            run_command = mock.Mock()

            run_loop(
                LoopConfig(
                    adapter=adapter,
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    semantic_map=semantic_map,
                    code_map=code_map,
                    target_plan=target_plan,
                    product_head="abc1234",
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 0)
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            blocker_kinds = {item["kind"] for item in summary["knowledge_audit"]["blockers"]}
            self.assertEqual(blocker_kinds, {"l4_only_target_plan"})
            self.assertEqual(summary["next_step_category"], "add-l3-oracle")

    def test_knowledge_audit_blocks_unreadable_product_head_before_preflight(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = self.write_adapter(root)
            semantic_map = root / "semantic-map.yaml"
            code_map = root / "code-map.yaml"
            semantic_map.write_text(
                "based_on_product_commit: abc1234\nentries:\n- id: demo.entry\n",
                encoding="utf-8",
            )
            code_map.write_text(
                "based_on_product_commit: abc1234\nmodules: []\nlinks: []\nedges: []\n",
                encoding="utf-8",
            )
            run_command = mock.Mock()

            run_loop(
                LoopConfig(
                    adapter=adapter,
                    product_cwd=root / "missing-product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    semantic_map=semantic_map,
                    code_map=code_map,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 0)
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            blocker_kinds = {item["kind"] for item in summary["knowledge_audit"]["blockers"]}
            self.assertEqual(blocker_kinds, {"product_head_unavailable"})
            self.assertEqual(summary["next_step_category"], "runtime-preflight")

    def test_knowledge_audit_records_understanding_profile_gaps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = self.write_adapter(root)
            semantic_map = root / "semantic-map.yaml"
            code_map = root / "code-map.yaml"
            understanding_profile = root / "understanding.json"
            target_plan = root / "target-plan.json"
            semantic_map.write_text(
                "based_on_product_commit: abc1234\nentries:\n- id: demo.entry\n",
                encoding="utf-8",
            )
            code_map.write_text(
                "based_on_product_commit: abc1234\nmodules: []\nlinks: []\nedges: []\n",
                encoding="utf-8",
            )
            understanding_profile.write_text(
                json.dumps(
                    {
                        "product": "demo",
                        "based_on_product_commit": "abc1234",
                        "domains": [
                            {
                                "id": "missing-ui",
                                "required_entry_ids": ["demo.ui.entry"],
                                "minimum_code_links": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            target_plan.write_text(
                json.dumps(
                    {
                        "must_run_l2_checks": [
                            {"spec": "context-ribbon.spec.ts", "semantic_map_entry_id": "demo.entry"}
                        ],
                        "must_run_l3_relations": [],
                        "must_run_l1_fuzz": [],
                        "coverage_gaps": [],
                    }
                ),
                encoding="utf-8",
            )
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            batch = mock.Mock(returncode=0, stdout=json.dumps({"status": "processed"}), stderr="")
            run_command = mock.Mock(side_effect=[preflight, batch])

            run_loop(
                LoopConfig(
                    adapter=adapter,
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    semantic_map=semantic_map,
                    code_map=code_map,
                    target_plan=target_plan,
                    understanding_profile=understanding_profile,
                    product_head="abc1234",
                    environment_retry_count=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            audit = summary["knowledge_audit"]
            self.assertEqual(audit["status"], "ok")
            self.assertEqual(audit["counts"]["understanding_domain_count"], 1)
            self.assertEqual(audit["counts"]["understanding_domain_missing_count"], 1)
            self.assertEqual(audit["product_understanding"][0]["id"], "missing-ui")
            self.assertEqual(summary["product_understanding_gap_count"], 1)
            self.assertEqual(summary["product_understanding_gaps"], ["missing-ui"])

    def test_knowledge_audit_blocks_missing_required_evidence_before_preflight(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = self.write_adapter(root)
            semantic_map = root / "semantic-map.yaml"
            code_map = root / "code-map.yaml"
            understanding_profile = root / "understanding.json"
            target_plan = root / "target-plan.json"
            semantic_map.write_text(
                "based_on_product_commit: abc1234\nentries:\n- id: demo.entry\n",
                encoding="utf-8",
            )
            code_map.write_text(
                "based_on_product_commit: abc1234\nmodules: []\nlinks: []\nedges: []\n",
                encoding="utf-8",
            )
            understanding_profile.write_text(
                json.dumps(
                    {
                        "product": "demo",
                        "based_on_product_commit": "abc1234",
                        "domains": [
                            {
                                "id": "missing-hard-evidence",
                                "required_entry_ids": ["demo.entry"],
                                "required_evidence_kinds": ["product_fact", "oracle", "gate"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            target_plan.write_text(
                json.dumps(
                    {
                        "must_run_l2_checks": [
                            {"spec": "context-ribbon.spec.ts", "semantic_map_entry_id": "demo.entry"}
                        ],
                        "must_run_l3_relations": [],
                        "must_run_l1_fuzz": [],
                        "coverage_gaps": [],
                    }
                ),
                encoding="utf-8",
            )
            run_command = mock.Mock()

            run_loop(
                LoopConfig(
                    adapter=adapter,
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    semantic_map=semantic_map,
                    code_map=code_map,
                    target_plan=target_plan,
                    understanding_profile=understanding_profile,
                    product_head="abc1234",
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 0)
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            blocker_kinds = {item["kind"] for item in summary["knowledge_audit"]["blockers"]}
            self.assertEqual(blocker_kinds, {"understanding_evidence_missing"})
            self.assertEqual(summary["next_step_category"], "add-ui-spec-link")

    def test_summary_promotes_knowledge_audit_target_and_evidence_chain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = self.write_adapter(root)
            semantic_map = root / "semantic-map.yaml"
            code_map = root / "code-map.yaml"
            target_plan = root / "target-plan.json"
            understanding_profile = root / "understanding.json"
            semantic_map.write_text(
                "based_on_product_commit: abc1234\nentries:\n- id: demo.entry\n",
                encoding="utf-8",
            )
            code_map.write_text(
                "based_on_product_commit: abc1234\nmodules: []\nlinks: []\nedges: []\n",
                encoding="utf-8",
            )
            target_plan.write_text(
                json.dumps(
                    {
                        "must_run_l2_checks": [
                            {"spec": "one.spec.ts", "semantic_map_entry_id": "demo.entry"}
                        ],
                        "must_run_l3_relations": [
                            {"scenario_id": "l3.demo", "semantic_map_entry_id": "demo.entry"}
                        ],
                        "must_run_l1_fuzz": [
                            {
                                "case_id": "bad-json",
                                "path": "/api/demo",
                                "semantic_map_entry_id": "demo.entry",
                            }
                        ],
                        "coverage_gaps": [
                            {"kind": "missing-oracle", "semantic_map_entry_id": "demo.gap"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            understanding_profile.write_text(
                json.dumps(
                    {
                        "product": "demo",
                        "based_on_product_commit": "abc1234",
                        "domains": [
                            {
                                "id": "demo-domain",
                                "notes": "Narrow profile keeps L3 checks in the full profile.",
                                "required_entry_ids": ["demo.entry"],
                                "minimum_code_links": 0,
                                "required_evidence_kinds": [
                                    "product_fact",
                                    "code_or_ui_spec",
                                    "oracle",
                                    "gate",
                                ],
                                "evidence_anchors": {
                                    "product_fact": [{"path": "docs/demo.md"}],
                                    "code_or_ui_spec": [{"path": "web/tests/integration/demo.spec.ts"}],
                                    "oracle": [{"level": "L2"}],
                                    "gate": [{"kind": "repo-local"}],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "product" / "docs").mkdir(parents=True)
            (root / "product" / "docs" / "demo.md").write_text("demo", encoding="utf-8")
            (root / "product" / "web" / "tests" / "integration").mkdir(parents=True)
            (root / "product" / "web" / "tests" / "integration" / "demo.spec.ts").write_text(
                "test", encoding="utf-8"
            )
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            batch = mock.Mock(
                returncode=0,
                stdout=json.dumps({"status": "processed", "oracle_levels": ["L2"]}),
                stderr="",
            )
            run_command = mock.Mock(side_effect=[preflight, batch])

            run_loop(
                LoopConfig(
                    adapter=adapter,
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    semantic_map=semantic_map,
                    code_map=code_map,
                    target_plan=target_plan,
                    understanding_profile=understanding_profile,
                    product_head="abc1234",
                    environment_retry_count=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["hit_semantic_entries"], ["demo.entry", "demo.gap"])
            self.assertEqual(summary["oracle_ran"], ["L2"])
            self.assertEqual(summary["planned_semantic_entries"], ["demo.entry", "demo.gap"])
            self.assertEqual(summary["planned_oracle_levels"], ["L1", "L2", "L3", "L4-suggestion"])
            self.assertEqual(summary["coverage_gap_count"], 1)
            self.assertEqual(summary["profile_spec_count"], 3)
            self.assertEqual(summary["spec_count"], 1)
            self.assertEqual(summary["target_plan_l2_count"], 1)
            self.assertEqual(summary["target_plan_l3_count"], 1)
            self.assertEqual(summary["target_plan_l1_count"], 1)
            self.assertEqual(summary["target_plan_gap_count"], 1)
            self.assertEqual(summary["target_scope_semantic_entry_count"], 2)
            self.assertEqual(summary["target_scope_oracle_levels"], ["L1", "L2", "L3", "L4-suggestion"])
            self.assertEqual(summary["target_scope_gap_count"], 1)
            self.assertEqual(summary["profile_understanding_gap_count"], 0)
            self.assertEqual(summary["profile_understanding_gaps"], [])
            self.assertEqual(summary["understanding_gap_scope"], "profile-adjusted")
            self.assertEqual(
                summary["evidence_chain"],
                [
                    {
                        "domain": "demo-domain",
                        "status": "covered",
                        "evidence_anchor_counts": {
                            "code_or_ui_spec": 1,
                            "gate": 1,
                            "oracle": 1,
                            "product_fact": 1,
                        },
                        "oracle_levels": [],
                        "owner_gated": False,
                        "runtime_gated": False,
                        "notes": "Narrow profile keeps L3 checks in the full profile.",
                    }
                ],
            )

    def test_knowledge_audit_paths_can_come_from_adapter_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = root / "adapter.json"
            semantic_map = root / "semantic-map.yaml"
            code_map = root / "code-map.yaml"
            understanding_profile = root / "understanding.json"
            target_plan = root / "target-plan.json"
            semantic_map.write_text(
                "based_on_product_commit: abc1234\nentries:\n- id: demo.entry\n",
                encoding="utf-8",
            )
            code_map.write_text(
                "based_on_product_commit: abc1234\nmodules: []\nlinks: []\nedges: []\n",
                encoding="utf-8",
            )
            understanding_profile.write_text(
                json.dumps(
                    {
                        "based_on_product_commit": "abc1234",
                        "domains": [{"id": "known-surface", "required_entry_ids": ["demo.entry"]}],
                    }
                ),
                encoding="utf-8",
            )
            target_plan.write_text(
                json.dumps(
                    {
                        "must_run_l2_checks": [
                            {"spec": "context-ribbon.spec.ts", "semantic_map_entry_id": "demo.entry"}
                        ],
                        "must_run_l3_relations": [],
                        "must_run_l1_fuzz": [],
                        "coverage_gaps": [],
                    }
                ),
                encoding="utf-8",
            )
            adapter.write_text(
                json.dumps(
                    {
                        "profile": "bug-mining",
                        "knowledge_audit": {
                            "semantic_map": str(semantic_map),
                            "code_map": str(code_map),
                            "understanding_profile": str(understanding_profile),
                            "target_plan": str(target_plan),
                        },
                        "playwright": {
                            "spec_whitelist": [
                                {"spec": "context-ribbon.spec.ts", "semantic_map_entry_id": "demo.entry"}
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            batch = mock.Mock(returncode=0, stdout=json.dumps({"status": "processed"}), stderr="")
            run_command = mock.Mock(side_effect=[preflight, batch])

            run_loop(
                LoopConfig(
                    adapter=adapter,
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    product_head="abc1234",
                    environment_retry_count=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["knowledge_audit"]["counts"]["semantic_entry_count"], 1)
            self.assertEqual(summary["knowledge_audit"]["counts"]["understanding_domain_count"], 1)

    def test_preflight_not_ready_skips_batch_and_records_round(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(
                returncode=1,
                stdout=json.dumps(
                    {
                        "status": "not-ready",
                        "failed_count": 1,
                        "checks": [{"name": "playwright.node", "status": "fail", "detail": "Node.js 18"}],
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(return_value=preflight)

            result = run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 1)
            self.assertEqual(result.rounds[0]["status"], "not-ready")
            self.assertEqual(result.rounds[0]["preflight_status"], "not-ready")
            self.assertFalse((root / "loop" / "test-run" / "round-0001" / "ledger.sqlite").exists())
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["round_count"], 1)
            self.assertEqual(summary["not_ready_count"], 1)

    def test_preflight_not_ready_is_fail_closed_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(
                returncode=1,
                stdout=json.dumps(
                    {
                        "status": "not-ready",
                        "failed_count": 1,
                        "checks": [{"name": "playwright.node", "status": "fail", "detail": "Node.js 18"}],
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(return_value=preflight)

            result = run_loop(
                LoopConfig(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=3,
                    sleep_sec=0,
                    enforce_knowledge_gate=False,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 1)
            self.assertEqual(len(result.rounds), 1)
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["stop_reason"], "not-ready preflight")
            self.assertEqual(summary["next_step_category"], "runtime-preflight")

    def test_preflight_non_json_failure_is_not_ready_blocker_without_raw_stderr_in_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=1, stdout="Traceback: failed", stderr="secret-like-token")
            run_command = mock.Mock(return_value=preflight)

            result = run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    stop_on_not_ready=True,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(result.rounds[0]["status"], "not-ready")
            round_dir = root / "loop" / "test-run" / "round-0001"
            preflight_json = json.loads((round_dir / "preflight.json").read_text(encoding="utf-8"))
            self.assertEqual(preflight_json["status"], "not-ready")
            self.assertIn("without JSON output", preflight_json["checks"][-1]["detail"])
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["next_step_category"], "runtime-preflight")
            self.assertEqual(summary["major_blocker"]["status"], "not-ready")
            self.assertNotIn("secret-like-token", json.dumps(summary, ensure_ascii=False))

    def test_preflight_ok_runs_batch_with_round_scoped_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            batch = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "environment-event",
                        "reported_count": 0,
                        "observed_count": 0,
                        "signal_count": 0,
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(side_effect=[preflight, batch])

            with mock.patch(
                "pipeline_v2.loop_runner.time.monotonic",
                side_effect=[10.0, 10.25, 20.0, 20.75],
            ):
                result = run_loop(
                    self.legacy_loop_config(
                        adapter=root / "adapter.json",
                        product_cwd=root / "product",
                        output_root=root / "loop",
                        run_id="test-run",
                        max_rounds=1,
                        sleep_sec=0,
                        timeout_sec=120,
                        environment_retry_count=0,
                    ),
                    run_command=run_command,
                    sleep=lambda _seconds: None,
                )

            self.assertEqual(run_command.call_count, 2)
            round_dir = (root / "loop" / "test-run" / "round-0001").resolve()
            preflight_cmd = run_command.call_args_list[0].args[0]
            batch_cmd = run_command.call_args_list[1].args[0]
            self.assertIn("--preflight", preflight_cmd)
            self.assertIn(f"--output-dir={round_dir / 'playwright-output'}", preflight_cmd)
            self.assertIn(f"--ledger={round_dir / 'ledger.sqlite'}", batch_cmd)
            self.assertIn(f"--queue={round_dir / 'queue.jsonl'}", batch_cmd)
            self.assertIn(f"--brief={round_dir / 'brief.md'}", batch_cmd)
            self.assertIn(f"--output-dir={round_dir / 'playwright-output'}", batch_cmd)
            self.assertIn(f"--suggestions={round_dir / 'suggestions.jsonl'}", batch_cmd)
            self.assertIn("--timeout-sec=120", batch_cmd)
            self.assertEqual(result.rounds[0]["status"], "environment-event")
            self.assertEqual(result.rounds[0]["preflight"]["duration_sec"], 0.25)
            self.assertEqual(result.rounds[0]["batch"]["duration_sec"], 0.75)
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["environment_event_count"], 1)
            self.assertEqual(summary["reported_total"], 0)
            self.assertIn("completed_at", summary)
            self.assertNotIn("stop_reason", summary)

    def test_processed_batch_environment_event_is_promoted_to_summary_blocker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            batch = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "processed",
                        "environment_event": True,
                        "environment_reasons": [],
                        "reported_count": 0,
                        "observed_count": 4,
                        "signal_count": 4,
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(side_effect=[preflight, batch])

            run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    environment_retry_count=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["processed_count"], 1)
            self.assertEqual(summary["environment_event_count"], 1)
            self.assertEqual(summary["status_counts"]["processed"], 1)
            self.assertEqual(summary["status_counts"]["environment-event"], 1)
            self.assertEqual(summary["round_stats"][0]["environment_event"], 1)
            self.assertEqual(summary["major_blocker"]["status"], "environment-event")
            self.assertIn("signals=4 observed=4 reported=0", summary["major_blocker"]["detail"])
            self.assertEqual(summary["next_step_category"], "runtime-preflight")

    def test_adapter_error_is_promoted_to_summary_blocker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            batch = mock.Mock(
                returncode=1,
                stdout=json.dumps(
                    {
                        "status": "adapter-error",
                        "error": "target plan must_run_l3_relations contains non-executable targets for adapter",
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(side_effect=[preflight, batch])

            run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status_counts"]["adapter-error"], 1)
            self.assertEqual(summary["major_blocker"]["status"], "adapter-error")
            self.assertIn("non-executable targets", summary["major_blocker"]["detail"])
            self.assertEqual(summary["next_step_category"], "add-l3-oracle")

    def test_adapter_runtime_defaults_can_supply_pre_batch_hook(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = root / "adapter.json"
            hook = root / "hooks" / "start-runtime.sh"
            hook.parent.mkdir()
            hook.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            adapter.write_text(
                json.dumps(
                    {
                        "profile": "auth-disabled-local",
                        "runtime": {
                            "pre_batch_hook": "hooks/start-runtime.sh",
                            "pre_batch_hook_timeout_sec": 17,
                        },
                        "playwright": {
                            "env": {
                                "SAMPLE_TEST_URL": "http://sample.example.test:8000",
                                "CI": "",
                            },
                            "spec_whitelist": [
                                {"spec": "modes-api.spec.ts", "semantic_map_entry_id": "demo.entry"}
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            hook_result = mock.Mock(returncode=0, stdout="runtime=ok\n", stderr="")
            batch = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "processed",
                        "reported_count": 0,
                        "observed_count": 1,
                        "signal_count": 1,
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(side_effect=[preflight, hook_result, batch])

            run_loop(
                self.legacy_loop_config(
                    adapter=adapter,
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    environment_retry_count=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 3)
            hook_call = run_command.call_args_list[1]
            self.assertEqual(hook_call.args[0], [str(hook)])
            self.assertEqual(hook_call.kwargs["timeout"], 17)
            self.assertEqual(hook_call.kwargs["env"]["SAMPLE_TEST_URL"], "http://sample.example.test:8000")
            self.assertEqual(hook_call.kwargs["env"]["CI"], "")
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["profile"], "auth-disabled-local")
            self.assertEqual(summary["processed_count"], 1)

    def test_batch_non_json_failure_is_environment_blocker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            batch = mock.Mock(returncode=1, stdout="Traceback: failed", stderr="secret-like-token")
            run_command = mock.Mock(side_effect=[preflight, batch])

            result = run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    environment_retry_count=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(result.rounds[0]["status"], "environment-event")
            round_dir = root / "loop" / "test-run" / "round-0001"
            batch_json = json.loads((round_dir / "batch.json").read_text(encoding="utf-8"))
            self.assertEqual(batch_json["status"], "environment-event")
            self.assertIn("without JSON output", batch_json["environment_reasons"][0])
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["next_step_category"], "runtime-preflight")
            self.assertEqual(summary["major_blocker"]["status"], "environment-event")
            self.assertNotIn("secret-like-token", json.dumps(summary, ensure_ascii=False))

    def test_repeated_environment_event_stops_loop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            batch = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "environment-event",
                        "environment_reasons": ["login page: 请输入用户名"],
                        "reported_count": 0,
                        "observed_count": 0,
                        "signal_count": 0,
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(side_effect=[preflight, batch, preflight, batch, preflight, batch, preflight, batch])

            result = run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=4,
                    sleep_sec=0,
                    stop_after_consecutive_blockers=3,
                    environment_retry_count=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(len(result.rounds), 3)
            self.assertEqual(run_command.call_count, 6)
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["round_count"], 3)
            self.assertEqual(summary["environment_event_count"], 3)
            self.assertIn("3 consecutive environment-event blockers", summary["stop_reason"])
            self.assertIn("login page", summary["stop_reason"])

    def test_different_blockers_do_not_trigger_repeated_stop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            login_batch = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "environment-event",
                        "environment_reasons": ["login page: 请输入用户名"],
                    }
                ),
                stderr="",
            )
            quota_batch = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "environment-event",
                        "environment_reasons": ["provider quota: 429"],
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(side_effect=[preflight, login_batch, preflight, quota_batch, preflight, login_batch])

            result = run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=3,
                    sleep_sec=0,
                    stop_after_consecutive_blockers=3,
                    environment_retry_count=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(len(result.rounds), 3)
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertNotIn("stop_reason", summary)

    def test_transient_environment_event_is_retried_before_recording_round(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            auth_blip = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "environment-event",
                        "environment_reasons": ["login page: 请输入用户名"],
                        "reported_count": 0,
                        "observed_count": 0,
                        "signal_count": 0,
                    }
                ),
                stderr="",
            )
            recovered = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "processed",
                        "reported_count": 0,
                        "observed_count": 1,
                        "signal_count": 1,
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(side_effect=[preflight, auth_blip, recovered])

            result = run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    environment_retry_count=1,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 3)
            self.assertEqual(result.rounds[0]["status"], "processed")
            self.assertEqual(len(result.rounds[0]["batch_attempts"]), 2)
            round_dir = root / "loop" / "test-run" / "round-0001"
            self.assertTrue((round_dir / "batch-attempt-1.json").exists())
            self.assertTrue((round_dir / "batch-attempt-2.json").exists())
            self.assertEqual(json.loads((round_dir / "batch.json").read_text(encoding="utf-8"))["status"], "processed")
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["environment_event_count"], 0)
            self.assertEqual(summary["processed_count"], 1)

    def test_pre_batch_hook_reruns_before_environment_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            hook = mock.Mock(returncode=0, stdout="auth_refresh_status=ok\n", stderr="")
            auth_blip = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "environment-event",
                        "environment_reasons": ["login page: 请输入用户名"],
                        "reported_count": 0,
                        "observed_count": 0,
                        "signal_count": 0,
                    }
                ),
                stderr="",
            )
            recovered = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "processed",
                        "reported_count": 0,
                        "observed_count": 1,
                        "signal_count": 1,
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(side_effect=[preflight, hook, auth_blip, hook, recovered])

            result = run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    pre_batch_hook=root / "refresh-auth.sh",
                    environment_retry_count=1,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 5)
            self.assertEqual(run_command.call_args_list[1].args[0], [str(root / "refresh-auth.sh")])
            self.assertEqual(run_command.call_args_list[3].args[0], [str(root / "refresh-auth.sh")])
            self.assertEqual(result.rounds[0]["status"], "processed")
            self.assertEqual(len(result.rounds[0]["pre_batch_hook_attempts"]), 2)
            round_dir = root / "loop" / "test-run" / "round-0001"
            self.assertTrue((round_dir / "pre-batch-hook.stdout").exists())
            self.assertTrue((round_dir / "pre-batch-hook-attempt-2.stdout").exists())
            summary = json.loads((round_dir.parent / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["environment_event_count"], 0)
            self.assertEqual(summary["processed_count"], 1)

    def test_pre_batch_hook_reruns_before_each_environment_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            hook = mock.Mock(returncode=0, stdout="auth_refresh_status=ok\n", stderr="")
            auth_blip = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "environment-event",
                        "environment_reasons": ["login page: 请输入用户名"],
                        "reported_count": 0,
                        "observed_count": 0,
                        "signal_count": 0,
                    }
                ),
                stderr="",
            )
            recovered = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "processed",
                        "reported_count": 0,
                        "observed_count": 1,
                        "signal_count": 1,
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(side_effect=[preflight, hook, auth_blip, hook, auth_blip, hook, recovered])

            result = run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    pre_batch_hook=root / "refresh-auth.sh",
                    environment_retry_count=2,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 7)
            self.assertEqual(run_command.call_args_list[1].args[0], [str(root / "refresh-auth.sh")])
            self.assertEqual(run_command.call_args_list[3].args[0], [str(root / "refresh-auth.sh")])
            self.assertEqual(run_command.call_args_list[5].args[0], [str(root / "refresh-auth.sh")])
            self.assertEqual(result.rounds[0]["status"], "processed")
            self.assertEqual(len(result.rounds[0]["batch_attempts"]), 3)
            self.assertEqual(len(result.rounds[0]["pre_batch_hook_attempts"]), 3)
            round_dir = root / "loop" / "test-run" / "round-0001"
            self.assertTrue((round_dir / "pre-batch-hook-attempt-2.stdout").exists())
            self.assertTrue((round_dir / "pre-batch-hook-attempt-3.stdout").exists())
            self.assertEqual(json.loads((round_dir / "batch.json").read_text(encoding="utf-8"))["status"], "processed")

    def test_pre_batch_hook_runs_before_every_batch_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            hook = mock.Mock(returncode=0, stdout="", stderr="")
            batch = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "processed",
                        "reported_count": 0,
                        "observed_count": 1,
                        "signal_count": 1,
                    }
                ),
                stderr="",
            )
            run_command = mock.Mock(side_effect=[preflight, hook, batch, preflight, hook, batch])

            result = run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=2,
                    sleep_sec=0,
                    pre_batch_hook=root / "refresh-auth.sh",
                    pre_batch_hook_timeout_sec=9,
                    environment_retry_count=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 6)
            self.assertEqual(result.rounds[0]["status"], "processed")
            self.assertEqual(result.rounds[1]["status"], "processed")
            hook_call_1 = run_command.call_args_list[1]
            hook_call_2 = run_command.call_args_list[4]
            self.assertEqual(hook_call_1.args[0], [str(root / "refresh-auth.sh")])
            self.assertEqual(hook_call_1.kwargs["timeout"], 9)
            self.assertEqual(hook_call_2.args[0], [str(root / "refresh-auth.sh")])
            self.assertTrue((root / "loop" / "test-run" / "round-0001" / "pre-batch-hook.stdout").exists())
            self.assertTrue((root / "loop" / "test-run" / "round-0002" / "pre-batch-hook.stdout").exists())

    def test_failed_pre_batch_hook_is_recorded_as_environment_event_without_batch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            hook = mock.Mock(returncode=1, stdout="", stderr="auth smoke failed")
            run_command = mock.Mock(side_effect=[preflight, hook])

            result = run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    pre_batch_hook=root / "refresh-auth.sh",
                    environment_retry_count=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_count, 2)
            self.assertEqual(result.rounds[0]["status"], "environment-event")
            self.assertIn("pre_batch_hook", result.rounds[0])
            self.assertNotIn("batch", result.rounds[0])
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["environment_event_count"], 1)
            self.assertEqual(summary["major_blocker"]["status"], "environment-event")
            self.assertIn("auth smoke failed", summary["major_blocker"]["detail"])
            hook_json = json.loads((root / "loop" / "test-run" / "round-0001" / "pre-batch-hook.json").read_text(encoding="utf-8"))
            self.assertEqual(hook_json["status"], "environment-event")
            self.assertIn("pre-batch hook failed", hook_json["environment_reasons"][0])
            self.assertNotIn("timed out", " ".join(hook_json["environment_reasons"]))

    def test_batch_timeout_is_recorded_as_environment_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok", "checks": []}), stderr="")
            run_command = mock.Mock(
                side_effect=[
                    preflight,
                    subprocess.TimeoutExpired(
                        cmd=["python3", "-m", "pipeline_v2.orchestrator"],
                        timeout=6,
                        output="",
                        stderr="still waiting",
                    ),
                ]
            )

            result = run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    timeout_sec=3,
                    environment_retry_count=0,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(result.rounds[0]["status"], "environment-event")
            self.assertEqual(run_command.call_args_list[1].kwargs["timeout"], 6)
            round_dir = root / "loop" / "test-run" / "round-0001"
            batch_json = json.loads((round_dir / "batch.json").read_text(encoding="utf-8"))
            self.assertEqual(batch_json["status"], "environment-event")
            self.assertIn("timed out after 6s", batch_json["environment_reasons"][0])
            batch_stderr = (round_dir / "batch.stderr").read_text(encoding="utf-8")
            self.assertIn("still waiting", batch_stderr)
            summary = json.loads((root / "loop" / "test-run" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["environment_event_count"], 1)

    def test_batch_runner_timeout_scales_with_playwright_spec_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = root / "adapter.json"
            adapter.write_text(
                json.dumps(
                    {
                        "playwright": {
                            "spec_whitelist": [
                                {"spec": "one.spec.ts", "semantic_map_entry_id": "one"},
                                {"spec": "two.spec.ts", "semantic_map_entry_id": "two"},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            preflight = mock.Mock(returncode=0, stdout=json.dumps({"status": "ok"}), stderr="")
            batch = mock.Mock(returncode=0, stdout=json.dumps({"status": "processed"}), stderr="")
            run_command = mock.Mock(side_effect=[preflight, batch])

            run_loop(
                self.legacy_loop_config(
                    adapter=adapter,
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    timeout_sec=30,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            preflight_call, batch_call = run_command.call_args_list
            self.assertEqual(preflight_call.kwargs["timeout"], 30)
            self.assertIn("--timeout-sec=30", batch_call.args[0])
            self.assertEqual(batch_call.kwargs["timeout"], 120)

    def test_preflight_invokes_orchestrator_with_runner_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_command = mock.Mock(return_value=mock.Mock(returncode=0, stdout=json.dumps({"status": "ok"}), stderr=""))

            run_loop(
                self.legacy_loop_config(
                    adapter=root / "adapter.json",
                    product_cwd=root / "product",
                    output_root=root / "loop",
                    run_id="test-run",
                    max_rounds=1,
                    sleep_sec=0,
                    timeout_sec=17,
                ),
                run_command=run_command,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(run_command.call_args_list[0].kwargs["timeout"], 17)


if __name__ == "__main__":
    unittest.main()
