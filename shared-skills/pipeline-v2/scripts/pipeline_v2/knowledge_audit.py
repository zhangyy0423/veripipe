"""knowledge_audit.py — 产品中立的知识地图完备度与选靶审计。

输入均来自 semantic-map、code-map、adapter、台账投影和 change-impact 产物；
本模块不认识任何具体产品，也不读取产品业务路径。它回答三件事：

- 当前知识图谱/代码地图是否与产品 HEAD 对齐；
- entry、code link、L3 relation、L1 fuzz、case/finding 覆盖是否足以解释测试选择；
- 是否可以继续跑 loop，或应先刷新地图/补选靶。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

try:  # pragma: no cover - PyYAML 是测试/运行依赖，兜底保留清晰报错
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore


SELECTION_POLICY = ["L2", "L3", "L1", "L4-suggestion"]
MUTATION_TESTING_DEFAULT = "disabled"
BACKEND_LINK_KINDS = {"symbol", "export", "ws_command", "http_endpoint", "endpoint", "handler", "module"}
SURFACE_LINK_KINDS = {"playwright_spec", "ui_component", "source_path", "config_env", "testdata_fixture", "fixture", "doc"}
PATH_EVIDENCE_KINDS = {"product_fact", "code_or_ui_spec"}
ORACLE_EVIDENCE_LEVELS = {"L1", "L2", "L3"}
GATE_KIND_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class KnowledgeAuditReport:
    status: str
    counts: Mapping[str, int]
    gaps: Mapping[str, Any]
    product_understanding: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    target_summary: Mapping[str, Any] = field(default_factory=dict)
    blockers: Sequence[Mapping[str, str]] = field(default_factory=tuple)
    next_step_category: str = "run-targeted-mining"
    selection_policy: Sequence[str] = field(default_factory=lambda: tuple(SELECTION_POLICY))
    mutation_testing_default: str = MUTATION_TESTING_DEFAULT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "selection_policy": list(self.selection_policy),
            "mutation_testing_default": self.mutation_testing_default,
            "counts": dict(self.counts),
            "gaps": dict(self.gaps),
            "product_understanding": [dict(item) for item in self.product_understanding],
            "target_summary": dict(self.target_summary),
            "blockers": [dict(item) for item in self.blockers],
            "next_step_category": self.next_step_category,
        }


def audit_knowledge(
    *,
    semantic_map: Mapping[str, Any],
    code_map: Mapping[str, Any],
    adapter: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]] = (),
    excluded_leads: Sequence[Mapping[str, Any]] = (),
    target_plan: Optional[Mapping[str, Any]] = None,
    understanding_profile: Optional[Mapping[str, Any]] = None,
    product_head: str = "",
    product_repo: Optional[str | Path] = None,
    automation_root: Optional[str | Path] = None,
) -> KnowledgeAuditReport:
    entries = _semantic_entries(semantic_map)
    entry_ids = {str(entry.get("id") or "") for entry in entries if entry.get("id")}
    machine_check_ids = {
        str(entry.get("id") or "")
        for entry in entries
        if isinstance((entry.get("expected_terminal_state") or {}).get("machine_check"), Mapping)
    }

    links = [link for link in _list(code_map.get("links")) if isinstance(link, Mapping)]
    backend_links = [link for link in links if _is_backend_link_kind(str(link.get("from_kind") or ""))]
    surface_links = [link for link in links if _is_surface_link_kind(str(link.get("from_kind") or ""))]
    linked_entry_ids = {
        str(link.get("semantic_map_entry_id") or "")
        for link in links
        if str(link.get("semantic_map_entry_id") or "")
    }
    backend_linked_entry_ids = {
        str(link.get("semantic_map_entry_id") or "")
        for link in backend_links
        if str(link.get("semantic_map_entry_id") or "")
    }
    link_kinds_by_entry = _link_kinds_by_entry(links)
    link_kind_counts = _link_kind_counts(links)

    http_ws_endpoints = _http_ws_endpoints(code_map)
    ws_commands = _ws_commands(code_map)
    scenarios = _l3_scenarios(adapter)
    l3_entry_ids = {
        str(scenario.get("semantic_map_entry_id") or "")
        for scenario in scenarios
        if str(scenario.get("semantic_map_entry_id") or "")
    }
    l1_cases = _l1_cases(adapter)
    l1_covered_endpoint_keys = _l1_covered_endpoint_keys(http_ws_endpoints, l1_cases)

    expected_head = str(product_head or "").strip()
    fresh_cases, stale_cases = _split_fresh_cases(cases, product_head=expected_head)
    case_entry_ids = {
        str(case.get("semantic_map_entry_id") or "")
        for case in fresh_cases
        if str(case.get("semantic_map_entry_id") or "")
    }
    current_findings, stale_findings = _split_current_findings(
        excluded_leads,
        product_head=expected_head,
    )
    finding_entry_ids = {
        entry_id
        for finding in current_findings
        for entry_id in _finding_semantic_entry_ids(finding)
        if entry_id
    }
    case_or_finding_entry_ids = case_entry_ids | finding_entry_ids
    oracle_levels_by_entry = _oracle_levels_by_entry(
        entry_ids=entry_ids,
        machine_check_ids=machine_check_ids,
        l3_entry_ids=l3_entry_ids,
        l1_cases=l1_cases,
        cases=fresh_cases,
    )

    target_l2 = _target_items(target_plan, "must_run_l2_checks")
    target_l3 = _target_items(target_plan, "must_run_l3_relations")
    target_l1 = _target_items(target_plan, "must_run_l1_fuzz")
    target_gaps = _target_items(target_plan, "coverage_gaps")
    target_summary = _target_summary(target_l2, target_l3, target_l1, target_gaps)
    testdata_gates = _adapter_playwright_testdata_gates(adapter)
    gated_specs = _testdata_gate_specs(testdata_gates)
    gated_env_names = _testdata_gate_required_env(testdata_gates)
    understanding_domains, understanding_counts = _audit_understanding_domains(
        understanding_profile=_understanding_profile_for_adapter(understanding_profile, adapter),
        entry_ids=entry_ids,
        backend_linked_entry_ids=backend_linked_entry_ids,
        link_kinds_by_entry=link_kinds_by_entry,
        oracle_levels_by_entry=oracle_levels_by_entry,
        case_entry_ids=case_entry_ids,
        product_repo=Path(product_repo).resolve() if product_repo else None,
        automation_root=Path(automation_root).resolve() if automation_root else None,
    )

    counts = {
        "semantic_entry_count": len(entry_ids),
        "machine_check_entry_count": len(machine_check_ids),
        "module_count": len(_list(code_map.get("modules"))),
        "knowledge_link_count": len(links),
        "code_link_count": len(backend_links),
        "surface_link_count": len(surface_links),
        "linked_semantic_entry_count": len(linked_entry_ids & entry_ids),
        "backend_linked_semantic_entry_count": len(backend_linked_entry_ids & entry_ids),
        "playwright_spec_link_count": link_kind_counts.get("playwright_spec", 0),
        "ui_component_link_count": link_kind_counts.get("ui_component", 0),
        "source_path_link_count": link_kind_counts.get("source_path", 0),
        "config_env_link_count": link_kind_counts.get("config_env", 0),
        "testdata_fixture_link_count": link_kind_counts.get("testdata_fixture", 0),
        "doc_link_count": link_kind_counts.get("doc", 0),
        "http_ws_endpoint_count": len(http_ws_endpoints),
        "ws_command_count": len(ws_commands),
        "l3_relation_count": len(scenarios),
        "l3_entry_count": len(l3_entry_ids & entry_ids),
        "l1_fuzz_case_count": len(l1_cases),
        "l1_covered_endpoint_count": len(l1_covered_endpoint_keys),
        "case_count": len(cases),
        "fresh_case_count": len(fresh_cases),
        "stale_case_count": len(stale_cases),
        "case_covered_entry_count": len(case_entry_ids & entry_ids),
        "excluded_lead_count": len(excluded_leads),
        "handled_finding_count": len(current_findings),
        "fresh_finding_count": len(current_findings),
        "stale_finding_count": len(stale_findings),
        "finding_covered_entry_count": len(finding_entry_ids & entry_ids),
        "case_or_finding_covered_entry_count": len(case_or_finding_entry_ids & entry_ids),
        "target_plan_l2_count": len(target_l2),
        "target_plan_l3_count": len(target_l3),
        "target_plan_l1_count": len(target_l1),
        "target_plan_gap_count": len(target_gaps),
        "playwright_testdata_gate_count": len(testdata_gates),
        "playwright_testdata_gated_spec_count": len(gated_specs),
        "playwright_testdata_gate_required_env_count": len(gated_env_names),
    }
    counts.update(understanding_counts)

    endpoint_gaps = [
        {
            "kind": endpoint.get("kind", ""),
            "method": endpoint.get("method", ""),
            "route": endpoint.get("route", ""),
        }
        for endpoint in http_ws_endpoints
        if _endpoint_key(str(endpoint.get("method") or ""), str(endpoint.get("route") or ""))
        not in l1_covered_endpoint_keys
    ]
    gaps = {
        "entries_without_code_links": sorted(entry_ids - backend_linked_entry_ids),
        "entries_without_l3_relations": sorted(entry_ids - l3_entry_ids),
        "entries_without_cases": sorted(entry_ids - case_entry_ids),
        "entries_without_case_or_finding": sorted(entry_ids - case_or_finding_entry_ids),
        "stale_cases": stale_cases,
        "stale_findings": stale_findings,
        "endpoints_without_l1_fuzz": endpoint_gaps,
        "understanding_domains": [
            domain for domain in understanding_domains if domain.get("status") != "covered"
        ],
    }

    blockers: List[Mapping[str, str]] = []
    semantic_commit = str(semantic_map.get("based_on_product_commit") or "")
    code_commit = str(code_map.get("based_on_product_commit") or "")
    if expected_head and not semantic_commit:
        blockers.append(
            {
                "kind": "semantic_map_missing_commit",
                "detail": "semantic-map missing based_on_product_commit",
            }
        )
    elif expected_head and semantic_commit != expected_head:
        blockers.append(
            {
                "kind": "semantic_map_stale",
                "detail": f"semantic-map commit {semantic_commit} != product HEAD {expected_head}",
            }
        )
    if expected_head and not code_commit:
        blockers.append(
            {
                "kind": "code_map_missing_commit",
                "detail": "code-map missing based_on_product_commit",
            }
        )
    elif expected_head and code_commit != expected_head:
        blockers.append(
            {
                "kind": "code_map_stale",
                "detail": f"code-map commit {code_commit} != product HEAD {expected_head}",
            }
        )
    if target_plan is not None and not (target_l2 or target_l3 or target_l1 or target_gaps):
        blockers.append(
            {
                "kind": "empty_target_plan",
                "detail": "change-impact target plan has no L2, L3, L1, or coverage-gap target",
            }
        )
    elif target_plan is not None and target_gaps and not (target_l2 or target_l3 or target_l1):
        blockers.append(
            {
                "kind": "l4_only_target_plan",
                "detail": "target plan only contains L4 suggestions; add L2/L3/L1 machine oracle before loop execution",
            }
        )
    if expected_head and isinstance(understanding_profile, Mapping):
        understanding_commit = str(understanding_profile.get("based_on_product_commit") or "")
        if not understanding_commit:
            blockers.append(
                {
                    "kind": "understanding_profile_missing_commit",
                    "detail": "understanding profile missing based_on_product_commit",
                }
            )
        elif understanding_commit != expected_head:
            blockers.append(
                {
                    "kind": "understanding_profile_stale",
                    "detail": (
                        f"understanding profile commit {understanding_commit} "
                        f"!= product HEAD {expected_head}"
                    ),
                }
            )
    for domain in understanding_domains:
        if domain.get("status") == "covered":
            continue
        missing_evidence = [
            str(item)
            for item in _list(domain.get("missing_evidence_kinds"))
            if str(item).strip()
        ]
        if missing_evidence:
            blockers.append(
                {
                    "kind": "understanding_evidence_missing",
                    "detail": (
                        f"{domain.get('id')}: missing required evidence "
                        f"{', '.join(missing_evidence)}"
                    ),
                }
            )
            continue
        if not domain.get("blocking"):
            continue
        blockers.append(
            {
                "kind": "understanding_domain_incomplete",
                "detail": f"{domain.get('id')}: {domain.get('status')}",
            }
        )

    return KnowledgeAuditReport(
        status="fail" if blockers else "ok",
        counts=counts,
        gaps=gaps,
        product_understanding=tuple(understanding_domains),
        target_summary=target_summary,
        blockers=tuple(blockers),
        next_step_category=_next_step_category(blockers, counts),
    )


def render_audit_markdown(report: KnowledgeAuditReport) -> str:
    data = report.to_dict()
    counts = data["counts"]
    lines = [
        "## 知识图谱审计",
        f"- 状态: `{data['status']}`",
        f"- 下一步: `{data['next_step_category']}`",
        f"- 选靶顺序: {' → '.join(data['selection_policy'])}",
        "- mutation: 默认不启用全量 mutation；只有无更强 oracle 且有 sandbox/testdata gate 时再考虑。",
        "",
        "### 覆盖统计",
        f"- semantic entries: {counts['semantic_entry_count']}；machine_check: {counts['machine_check_entry_count']}",
        f"- knowledge links: {counts['knowledge_link_count']}；backend: {counts['code_link_count']}；surface: {counts['surface_link_count']}",
        f"- linked entries: {counts['linked_semantic_entry_count']}；backend linked entries: {counts['backend_linked_semantic_entry_count']}",
        f"- surface detail: playwright_spec={counts['playwright_spec_link_count']} ui_component={counts['ui_component_link_count']} source_path={counts['source_path_link_count']} config_env={counts['config_env_link_count']} testdata_fixture={counts['testdata_fixture_link_count']} doc={counts['doc_link_count']}",
        f"- HTTP/WS endpoints: {counts['http_ws_endpoint_count']}；WS commands: {counts['ws_command_count']}",
        f"- L3 relations: {counts['l3_relation_count']}；L1 fuzz cases: {counts['l1_fuzz_case_count']}",
        f"- cases: {counts['case_count']}（fresh {counts.get('fresh_case_count', 0)} / stale {counts.get('stale_case_count', 0)}）；excluded leads: {counts['excluded_lead_count']}",
        f"- handled findings: fresh {counts.get('fresh_finding_count', 0)} / stale {counts.get('stale_finding_count', 0)}；case/finding covered entries: {counts.get('case_or_finding_covered_entry_count', 0)}",
        f"- target plan: L2={counts['target_plan_l2_count']} L3={counts['target_plan_l3_count']} L1={counts['target_plan_l1_count']} gaps={counts['target_plan_gap_count']}",
        f"- playwright testdata gates: {counts.get('playwright_testdata_gate_count', 0)}；gated specs: {counts.get('playwright_testdata_gated_spec_count', 0)}；required env names: {counts.get('playwright_testdata_gate_required_env_count', 0)}",
    ]
    target_summary = data.get("target_summary", {})
    if target_summary:
        levels = ", ".join(target_summary.get("oracle_levels", [])) or "—"
        lines.append(f"- target oracle levels: {levels}")
        lines.append(f"- target semantic entries: {len(target_summary.get('semantic_entry_ids', []))}")
    if data.get("product_understanding"):
        lines.extend(["", "### 产品理解覆盖面"])
        lines.append(
            "| domain | status | entries | code links | evidence | oracle levels | gate |"
        )
        lines.append("|---|---:|---:|---:|---|---|---|")
        for domain in data["product_understanding"]:
            gate = "owner" if domain.get("owner_gated") else "runtime" if domain.get("runtime_gated") else "repo"
            levels = ",".join(domain.get("oracle_levels", [])) or "—"
            evidence = _evidence_counts_label(domain.get("evidence_anchor_counts", {}))
            lines.append(
                "| {id} | {status} | {present}/{required} | {links} | {evidence} | {levels} | {gate} |".format(
                    id=domain.get("id", ""),
                    status=domain.get("status", ""),
                    present=domain.get("present_entry_count", 0),
                    required=domain.get("required_entry_count", 0),
                    links=domain.get("code_link_count", 0),
                    evidence=evidence,
                    levels=levels,
                    gate=gate,
                )
            )
            missing_kinds = domain.get("missing_link_kinds") or []
            if domain.get("evidence_anchor_count"):
                lines.append(f"  - {domain.get('id', '')} evidence: {evidence}")
            if missing_kinds:
                lines.append(
                    f"  - {domain.get('id', '')} missing link kinds: {', '.join(missing_kinds)}"
                )
            missing_evidence = domain.get("missing_evidence_kinds") or []
            if missing_evidence:
                lines.append(
                    f"  - {domain.get('id', '')} missing evidence kinds: {', '.join(missing_evidence)}"
                )
            notes = str(domain.get("notes") or "").strip()
            if notes:
                lines.append(f"  - {domain.get('id', '')} notes: {notes}")
            anchors = domain.get("evidence_anchors") or {}
            if isinstance(anchors, Mapping):
                for kind in sorted(anchors):
                    anchor_labels = [
                        _evidence_anchor_label(item)
                        for item in _list(anchors.get(kind))
                        if isinstance(item, Mapping)
                    ]
                    if anchor_labels:
                        lines.append(
                            f"  - {domain.get('id', '')} {kind}: {'; '.join(anchor_labels)}"
                        )
    lines.extend(["", "### Blockers"])
    if data["blockers"]:
        for blocker in data["blockers"]:
            lines.append(f"- [{blocker['kind']}] {blocker['detail']}")
    else:
        lines.append("- （无阻断项）")
    lines.extend(["", "### Gaps"])
    gaps = data["gaps"]
    lines.append(f"- entries without code links: {len(gaps['entries_without_code_links'])}")
    lines.append(f"- entries without L3 relations: {len(gaps['entries_without_l3_relations'])}")
    lines.append(f"- entries without confirmed cases: {len(gaps['entries_without_cases'])}")
    lines.append(f"- entries without case/finding: {len(gaps.get('entries_without_case_or_finding', []))}")
    lines.append(f"- stale cases not counted as current coverage: {len(gaps.get('stale_cases', []))}")
    lines.append(f"- stale findings not counted as current coverage: {len(gaps.get('stale_findings', []))}")
    lines.append(f"- endpoints without L1 fuzz: {len(gaps['endpoints_without_l1_fuzz'])}")
    lines.append(f"- product understanding gaps: {len(gaps.get('understanding_domains', []))}")
    return "\n".join(lines) + "\n"


def load_mapping(path: str | Path) -> Mapping[str, Any]:
    p = Path(path)
    if p.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:  # pragma: no cover
            raise RuntimeError("PyYAML required to load YAML")
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    else:
        data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError(f"expected mapping at {p}")
    return data


def load_sequence(path: Optional[str | Path]) -> Sequence[Mapping[str, Any]]:
    if path is None:
        return ()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"expected list at {path}")
    return [item for item in data if isinstance(item, Mapping)]


def _semantic_entries(semantic_map: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    return [item for item in _list(semantic_map.get("entries")) if isinstance(item, Mapping)]


def _list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


def _http_ws_endpoints(code_map: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    endpoints: List[Mapping[str, Any]] = []
    for module in _list(code_map.get("modules")):
        if not isinstance(module, Mapping):
            continue
        for endpoint in _list(module.get("endpoints")):
            if isinstance(endpoint, Mapping) and endpoint.get("kind") in {"http", "ws"}:
                endpoints.append(endpoint)
    return endpoints


def _ws_commands(code_map: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    commands: List[Mapping[str, Any]] = []
    for module in _list(code_map.get("modules")):
        if not isinstance(module, Mapping):
            continue
        for endpoint in _list(module.get("endpoints")):
            if isinstance(endpoint, Mapping) and endpoint.get("kind") == "ws_command":
                commands.append(endpoint)
    return commands


def _l3_scenarios(adapter: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    driver = adapter.get("http_driver")
    driver_scenarios = driver.get("scenarios") if isinstance(driver, Mapping) else []
    return [
        item
        for item in _list(driver_scenarios) + _list(adapter.get("l3_relations"))
        if isinstance(item, Mapping)
    ]


def _l1_cases(adapter: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    fuzz = adapter.get("l1_fuzz")
    if not isinstance(fuzz, Mapping):
        return []
    return [item for item in _list(fuzz.get("cases")) if isinstance(item, Mapping)]


def _target_items(target_plan: Optional[Mapping[str, Any]], key: str) -> List[Mapping[str, Any]]:
    if target_plan is None:
        return []
    return [item for item in _list(target_plan.get(key)) if isinstance(item, Mapping)]


def _target_summary(
    target_l2: Sequence[Mapping[str, Any]],
    target_l3: Sequence[Mapping[str, Any]],
    target_l1: Sequence[Mapping[str, Any]],
    target_gaps: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    levels: Set[str] = set()
    if target_l2:
        levels.add("L2")
    if target_l3:
        levels.add("L3")
    if target_l1:
        levels.add("L1")
    if target_gaps:
        levels.add("L4-suggestion")

    semantic_ids = sorted(
        {
            str(item.get("semantic_map_entry_id") or "")
            for item in list(target_l2) + list(target_l3) + list(target_l1) + list(target_gaps)
            if str(item.get("semantic_map_entry_id") or "")
        }
    )
    l1_endpoints = sorted(
        {
            str(item.get("matched_endpoint") or item.get("path") or "")
            for item in target_l1
            if str(item.get("matched_endpoint") or item.get("path") or "")
        }
    )
    return {
        "oracle_levels": [level for level in ["L1", "L2", "L3", "L4-suggestion"] if level in levels],
        "semantic_entry_ids": semantic_ids,
        "l2_specs": sorted(
            {
                str(item.get("spec") or item.get("spec_path") or "")
                for item in target_l2
                if str(item.get("spec") or item.get("spec_path") or "")
            }
        ),
        "l1_endpoints": l1_endpoints,
        "coverage_gap_count": len(target_gaps),
    }


def _adapter_playwright_testdata_gates(adapter: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    playwright = adapter.get("playwright")
    if not isinstance(playwright, Mapping):
        return []
    return [item for item in _list(playwright.get("testdata_gates")) if isinstance(item, Mapping)]


def _testdata_gate_specs(gates: Sequence[Mapping[str, Any]]) -> Set[str]:
    specs: Set[str] = set()
    for gate in gates:
        for spec in _list(gate.get("specs")):
            text = str(spec or "").strip()
            if text:
                specs.add(text)
    return specs


def _testdata_gate_required_env(gates: Sequence[Mapping[str, Any]]) -> Set[str]:
    names: Set[str] = set()
    for gate in gates:
        for name in _list(gate.get("required_env")):
            text = str(name or "").strip()
            if text:
                names.add(text)
    return names


def _oracle_levels_by_entry(
    *,
    entry_ids: Set[str],
    machine_check_ids: Set[str],
    l3_entry_ids: Set[str],
    l1_cases: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
) -> Dict[str, Set[str]]:
    levels: Dict[str, Set[str]] = {entry_id: set() for entry_id in entry_ids}
    for entry_id in machine_check_ids:
        levels.setdefault(entry_id, set()).add("L2")
    for entry_id in l3_entry_ids:
        levels.setdefault(entry_id, set()).add("L3")
    for case in l1_cases:
        entry_id = str(case.get("semantic_map_entry_id") or "")
        if entry_id:
            levels.setdefault(entry_id, set()).add("L1")
    for case in cases:
        entry_id = str(case.get("semantic_map_entry_id") or "")
        oracle_level = str(case.get("oracle_level") or "")
        if entry_id and oracle_level:
            levels.setdefault(entry_id, set()).add(oracle_level)
    return levels


def _split_fresh_cases(
    cases: Sequence[Mapping[str, Any]],
    *,
    product_head: str,
) -> tuple[List[Mapping[str, Any]], List[Dict[str, str]]]:
    fresh: List[Mapping[str, Any]] = []
    stale: List[Dict[str, str]] = []
    expected_head = str(product_head or "").strip()
    for case in cases:
        if not _is_case_coverage_candidate(case):
            continue
        if not expected_head:
            fresh.append(case)
            continue
        case_commit = str(case.get("based_on_product_commit") or "").strip()
        if case_commit == expected_head:
            fresh.append(case)
            continue
        stale.append(
            {
                "id": str(case.get("id") or case.get("fingerprint") or ""),
                "semantic_map_entry_id": str(case.get("semantic_map_entry_id") or ""),
                "based_on_product_commit": case_commit,
                "expected_product_commit": expected_head,
            }
        )
    return fresh, stale


def _is_case_coverage_candidate(case: Mapping[str, Any]) -> bool:
    oracle_level = str(case.get("oracle_level") or "").strip()
    if oracle_level not in ORACLE_EVIDENCE_LEVELS:
        return False
    status = str(case.get("status") or "").strip().lower()
    if status in {
        "candidate",
        "observing",
        "intermittent",
        "expired",
        "environment-event",
        "vetoed",
        "false-positive",
    }:
        return False
    triage_result = str(case.get("triage_result") or "").strip().lower()
    if triage_result in {"false-positive", "owner-review-required"}:
        return False
    return True


def _split_current_findings(
    findings: Sequence[Mapping[str, Any]],
    *,
    product_head: str,
) -> tuple[List[Mapping[str, Any]], List[Dict[str, Any]]]:
    current: List[Mapping[str, Any]] = []
    stale: List[Dict[str, Any]] = []
    expected_head = str(product_head or "").strip()
    for finding in findings:
        if not _is_handled_finding(finding):
            continue
        if not expected_head:
            current.append(finding)
            continue
        finding_commit = str(finding.get("based_on_product_commit") or "").strip()
        if finding_commit == expected_head:
            current.append(finding)
            continue
        stale.append(
            {
                "id": str(finding.get("id") or finding.get("fingerprint") or ""),
                "status": str(finding.get("status") or ""),
                "semantic_map_entry_ids": _finding_semantic_entry_ids(finding),
                "based_on_product_commit": finding_commit,
                "expected_product_commit": expected_head,
            }
        )
    return current, stale


def _is_handled_finding(finding: Mapping[str, Any]) -> bool:
    status = str(finding.get("status") or "").strip().lower()
    if not status:
        return False
    if status == "open-lead" or status.startswith("open-"):
        return False
    handled_statuses = {
        "excluded",
        "non-bug",
        "expected",
        "expected-behavior",
        "owner-gated",
        "owner-gated-lead",
        "runtime-blocked",
        "runtime-blocked-lead",
        "runtime-target-ready",
        "suggestion",
        "l4-suggestion",
    }
    return (
        status in handled_statuses
        or status.startswith("superseded")
        or "owner-gated" in status
        or "runtime-blocked" in status
        or status.endswith("-blocked")
    )


def _finding_semantic_entry_ids(finding: Mapping[str, Any]) -> List[str]:
    ids: List[str] = []
    single = str(finding.get("semantic_map_entry_id") or "").strip()
    if single:
        ids.append(single)
    for item in _list(finding.get("semantic_map_entry_ids")):
        text = str(item or "").strip()
        if text:
            ids.append(text)
    return sorted(set(ids))


def _audit_understanding_domains(
    *,
    understanding_profile: Optional[Mapping[str, Any]],
    entry_ids: Set[str],
    backend_linked_entry_ids: Set[str],
    link_kinds_by_entry: Mapping[str, Set[str]],
    oracle_levels_by_entry: Mapping[str, Set[str]],
    case_entry_ids: Set[str],
    product_repo: Optional[Path],
    automation_root: Optional[Path],
) -> tuple[List[Mapping[str, Any]], Dict[str, int]]:
    domains = []
    if isinstance(understanding_profile, Mapping):
        domains = [item for item in _list(understanding_profile.get("domains")) if isinstance(item, Mapping)]

    reports: List[Mapping[str, Any]] = []
    for domain in domains:
        domain_id = str(domain.get("id") or "").strip()
        if not domain_id:
            continue
        required_ids = [
            str(item).strip()
            for item in _list(domain.get("required_entry_ids"))
            if str(item).strip()
        ]
        present_ids = sorted([entry_id for entry_id in required_ids if entry_id in entry_ids])
        missing_ids = sorted([entry_id for entry_id in required_ids if entry_id not in entry_ids])
        linked_ids = sorted([entry_id for entry_id in required_ids if entry_id in backend_linked_entry_ids])
        case_ids = sorted([entry_id for entry_id in required_ids if entry_id in case_entry_ids])
        domain_link_kind_counts = _domain_link_kind_counts(required_ids, link_kinds_by_entry)
        required_link_kinds = [
            str(item).strip()
            for item in _list(domain.get("required_link_kinds"))
            if str(item).strip()
        ]
        missing_link_kinds = sorted(
            [kind for kind in required_link_kinds if domain_link_kind_counts.get(kind, 0) == 0]
        )
        oracle_levels = sorted(
            {
                level
                for entry_id in required_ids
                for level in oracle_levels_by_entry.get(entry_id, set())
                if level
            },
            key=_oracle_sort_key,
        )
        required_oracles = [
            str(item).strip()
            for item in _list(domain.get("required_oracle_levels"))
            if str(item).strip()
        ]
        missing_oracles = [level for level in required_oracles if level not in oracle_levels]
        evidence_anchors, invalid_evidence_anchors = _evidence_anchors_by_kind(
            domain,
            product_repo=product_repo,
            automation_root=automation_root,
        )
        evidence_counts = {kind: len(items) for kind, items in evidence_anchors.items()}
        evidence_anchor_count = sum(evidence_counts.values())
        required_evidence_kinds = [
            str(item).strip()
            for item in _list(domain.get("required_evidence_kinds"))
            if str(item).strip()
        ]
        missing_evidence_kinds = sorted(
            [kind for kind in required_evidence_kinds if evidence_counts.get(kind, 0) == 0]
        )
        minimum_entries = _non_negative_int(domain.get("minimum_entries"), len(required_ids))
        minimum_code_links = _non_negative_int(domain.get("minimum_code_links"), 0)

        covered = (
            len(present_ids) >= minimum_entries
            and len(linked_ids) >= minimum_code_links
            and not missing_ids
            and not missing_oracles
            and not missing_link_kinds
            and not missing_evidence_kinds
        )
        has_any_signal = bool(
            present_ids
            or linked_ids
            or oracle_levels
            or case_ids
            or domain_link_kind_counts
            or evidence_anchor_count
        )
        status = "covered" if covered else "partial" if has_any_signal else "missing"

        reports.append(
            {
                "id": domain_id,
                "title": str(domain.get("title") or domain_id),
                "status": status,
                "required_entry_count": len(required_ids),
                "present_entry_count": len(present_ids),
                "missing_entry_ids": missing_ids,
                "code_link_count": len(linked_ids),
                "link_kind_counts": dict(sorted(domain_link_kind_counts.items())),
                "missing_link_kinds": missing_link_kinds,
                "case_entry_count": len(case_ids),
                "oracle_levels": oracle_levels,
                "missing_oracle_levels": missing_oracles,
                "evidence_anchor_count": evidence_anchor_count,
                "evidence_anchor_counts": dict(sorted(evidence_counts.items())),
                "evidence_anchors": evidence_anchors,
                "invalid_evidence_anchors": invalid_evidence_anchors,
                "missing_evidence_kinds": missing_evidence_kinds,
                "owner_gated": bool(domain.get("owner_gated")),
                "runtime_gated": bool(domain.get("runtime_gated")),
                "blocking": bool(domain.get("blocking")),
                "notes": str(domain.get("notes") or ""),
            }
        )

    counts = {
        "understanding_domain_count": len(reports),
        "understanding_domain_covered_count": sum(1 for item in reports if item.get("status") == "covered"),
        "understanding_domain_partial_count": sum(1 for item in reports if item.get("status") == "partial"),
        "understanding_domain_missing_count": sum(1 for item in reports if item.get("status") == "missing"),
        "understanding_domain_owner_gated_count": sum(1 for item in reports if item.get("owner_gated")),
        "understanding_domain_runtime_gated_count": sum(1 for item in reports if item.get("runtime_gated")),
        "understanding_evidence_anchor_count": sum(
            int(item.get("evidence_anchor_count") or 0) for item in reports
        ),
    }
    return reports, counts


def _understanding_profile_for_adapter(
    understanding_profile: Optional[Mapping[str, Any]],
    adapter: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    if not isinstance(understanding_profile, Mapping):
        return understanding_profile
    audit_config = adapter.get("knowledge_audit")
    if not isinstance(audit_config, Mapping):
        return understanding_profile
    overrides = audit_config.get("understanding_domain_overrides")
    if not isinstance(overrides, Mapping):
        return understanding_profile

    domains: List[Any] = []
    changed = False
    for domain in _list(understanding_profile.get("domains")):
        if not isinstance(domain, Mapping):
            domains.append(domain)
            continue
        domain_id = str(domain.get("id") or "")
        override = overrides.get(domain_id)
        if not isinstance(override, Mapping):
            domains.append(domain)
            continue
        merged = dict(domain)
        if "required_oracle_levels" in override:
            merged["required_oracle_levels"] = override["required_oracle_levels"]
        notes_append = str(override.get("notes_append") or "").strip()
        if notes_append:
            existing_notes = str(merged.get("notes") or "").strip()
            merged["notes"] = f"{existing_notes} {notes_append}".strip()
        domains.append(merged)
        changed = True

    if not changed:
        return understanding_profile
    merged_profile = dict(understanding_profile)
    merged_profile["domains"] = domains
    return merged_profile


def _evidence_anchors_by_kind(
    domain: Mapping[str, Any],
    *,
    product_repo: Optional[Path],
    automation_root: Optional[Path],
) -> tuple[Dict[str, List[Mapping[str, Any]]], List[Mapping[str, str]]]:
    raw = domain.get("evidence_anchors")
    if not isinstance(raw, Mapping):
        return {}, []
    anchors: Dict[str, List[Mapping[str, Any]]] = {}
    invalid: List[Mapping[str, str]] = []
    for kind, items in raw.items():
        normalized_kind = str(kind).strip()
        if not normalized_kind:
            continue
        normalized_items: List[Mapping[str, Any]] = []
        for item in _list(items):
            if not isinstance(item, Mapping):
                invalid.append(
                    {
                        "kind": normalized_kind,
                        "reason": "evidence anchor must be an object",
                        "value": str(item).strip(),
                    }
                )
                continue
            normalized_item = dict(item)
            invalid_reason = _invalid_evidence_anchor_reason(
                normalized_kind,
                normalized_item,
                product_repo=product_repo,
                automation_root=automation_root,
            )
            if invalid_reason:
                invalid.append(
                    {
                        "kind": normalized_kind,
                        "reason": invalid_reason,
                        "value": str(
                            normalized_item.get("path")
                            or normalized_item.get("level")
                            or normalized_item.get("kind")
                            or normalized_item.get("summary")
                            or ""
                        ),
                    }
                )
                continue
            normalized_items.append(normalized_item)
        if normalized_items:
            anchors[normalized_kind] = normalized_items
    return anchors, invalid


def _invalid_evidence_anchor_reason(
    kind: str,
    anchor: Mapping[str, Any],
    *,
    product_repo: Optional[Path],
    automation_root: Optional[Path],
) -> str:
    if kind in PATH_EVIDENCE_KINDS:
        path = str(anchor.get("path") or "").strip()
        if not path:
            return "path is required"
        return _invalid_anchor_path_reason(path, product_repo=product_repo, automation_root=automation_root)
    if anchor.get("path") is not None:
        reason = _invalid_anchor_path_reason(
            str(anchor.get("path") or ""),
            product_repo=product_repo,
            automation_root=automation_root,
        )
        if reason:
            return reason
    if kind == "oracle":
        level = str(anchor.get("level") or "").strip()
        if level not in ORACLE_EVIDENCE_LEVELS:
            return "oracle level must be one of L1, L2, L3"
    if kind == "gate":
        gate_kind = str(anchor.get("kind") or "").strip()
        if not gate_kind:
            return "gate kind is required"
        if not GATE_KIND_PATTERN.fullmatch(gate_kind):
            return "gate kind must be a safe slug"
    return ""


def _invalid_anchor_path_reason(
    value: str,
    *,
    product_repo: Optional[Path],
    automation_root: Optional[Path],
) -> str:
    text = str(value or "").strip()
    if not text:
        return "path is required"
    path = Path(text)
    if path.is_absolute() or text.startswith("~"):
        return "path must be a safe relative path"
    if any(part == ".." for part in path.parts):
        return "path must not contain parent traversal"
    if text.endswith(".env") or "/.env" in text:
        return "secret-bearing env files are not valid evidence anchors"
    roots = [root for root in (product_repo, automation_root) if root is not None]
    if roots and not any((root / path).exists() for root in roots):
        return "path does not exist under allowed roots"
    return ""


def _evidence_counts_label(counts: Any) -> str:
    if not isinstance(counts, Mapping):
        counts = {}
    return "facts/code-ui/oracle/gate = {}/{}/{}/{}".format(
        int(counts.get("product_fact") or 0),
        int(counts.get("code_or_ui_spec") or 0),
        int(counts.get("oracle") or 0),
        int(counts.get("gate") or 0),
    )


def _evidence_anchor_label(anchor: Mapping[str, Any]) -> str:
    path = str(anchor.get("path") or "").strip()
    level = str(anchor.get("level") or "").strip()
    kind = str(anchor.get("kind") or "").strip()
    summary = str(anchor.get("summary") or "").strip()
    label = path or level or kind or summary
    if summary and summary != label:
        return f"{label} ({summary})"
    return label


def _is_backend_link_kind(kind: str) -> bool:
    return str(kind).strip() in BACKEND_LINK_KINDS


def _is_surface_link_kind(kind: str) -> bool:
    return str(kind).strip() in SURFACE_LINK_KINDS


def _link_kind_counts(links: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for link in links:
        kind = str(link.get("from_kind") or "").strip()
        if not kind:
            continue
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _link_kinds_by_entry(links: Sequence[Mapping[str, Any]]) -> Dict[str, Set[str]]:
    by_entry: Dict[str, Set[str]] = {}
    for link in links:
        entry_id = str(link.get("semantic_map_entry_id") or "").strip()
        kind = str(link.get("from_kind") or "").strip()
        if entry_id and kind:
            by_entry.setdefault(entry_id, set()).add(kind)
    return by_entry


def _domain_link_kind_counts(
    required_ids: Sequence[str],
    link_kinds_by_entry: Mapping[str, Set[str]],
) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for entry_id in required_ids:
        for kind in link_kinds_by_entry.get(entry_id, set()):
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _non_negative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _oracle_sort_key(level: str) -> tuple[int, str]:
    order = {"L1": 1, "L2": 2, "L3": 3, "L4": 4, "L4-suggestion": 5}
    return order.get(level, 99), level


def _endpoint_key(method: str, route: str) -> str:
    return f"{str(method or '').upper()} {route}"


def _l1_covered_endpoint_keys(
    endpoints: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
) -> Set[str]:
    endpoint_keys: Set[str] = set()
    for endpoint in endpoints:
        route = str(endpoint.get("route") or "")
        endpoint_method = str(endpoint.get("method") or "").upper()
        if not route:
            continue
        for case in cases:
            case_method = str(case.get("method") or "").upper()
            if not endpoint_method or endpoint_method != case_method:
                continue
            if _route_matches_endpoint(str(case.get("path") or ""), route):
                endpoint_keys.add(_endpoint_key(endpoint_method, route))
                break
    return endpoint_keys


def _route_matches_endpoint(case_path: str, endpoint_route: str) -> bool:
    case_parts = _norm_path(case_path).split("/")
    route_parts = _norm_path(endpoint_route).split("/")
    if len(case_parts) != len(route_parts):
        return False
    for index, segment in enumerate(route_parts):
        if segment.startswith("{") and segment.endswith("}"):
            continue
        if index >= len(case_parts) or case_parts[index] != segment:
            return False
    return True


def _norm_path(path: str) -> str:
    return str(path).strip().lstrip("./").strip("/")


def _next_step_category(blockers: Sequence[Mapping[str, str]], counts: Mapping[str, int]) -> str:
    kinds = {str(item.get("kind") or "") for item in blockers}
    if kinds & {"semantic_map_stale", "code_map_stale", "semantic_map_missing_commit", "code_map_missing_commit"}:
        return "add-code-link"
    if "empty_target_plan" in kinds:
        return "add-code-link"
    if "l4_only_target_plan" in kinds:
        return "add-l3-oracle"
    if kinds & {"understanding_profile_stale", "understanding_profile_missing_commit"}:
        return "add-code-link"
    if "understanding_evidence_missing" in kinds:
        return "add-ui-spec-link"
    if "understanding_domain_incomplete" in kinds:
        return "add-code-link"
    if counts.get("l3_relation_count", 0) == 0 and counts.get("l1_fuzz_case_count", 0) == 0:
        return "add-l3-oracle"
    if counts.get("l3_relation_count", 0) == 0:
        return "add-l3-oracle"
    if counts.get("l1_fuzz_case_count", 0) == 0:
        return "add-l1-boundary"
    if counts.get("understanding_domain_missing_count", 0) > 0:
        return "add-code-link"
    if counts.get("understanding_domain_partial_count", 0) > 0:
        return "add-ui-spec-link"
    return "run-targeted-mining"


def _git_head(product_repo: str | Path) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(product_repo), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.strip()


def _adapter_audit_path(adapter_path: str | Path, adapter: Mapping[str, Any], key: str) -> Optional[Path]:
    audit = adapter.get("knowledge_audit")
    if not isinstance(audit, Mapping):
        return None
    value = audit.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if path.is_absolute():
        return path

    adapter_file = Path(adapter_path)
    candidates = [
        Path.cwd() / path,
        adapter_file.parent / path,
    ]
    if len(adapter_file.parents) >= 3:
        candidates.append(adapter_file.parents[2] / path)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="审计知识图谱/代码地图/选靶计划是否足以驱动挖掘。")
    parser.add_argument("--semantic-map", required=True)
    parser.add_argument("--code-map", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--cases")
    parser.add_argument("--excluded-leads")
    parser.add_argument("--target-plan")
    parser.add_argument("--understanding-profile")
    parser.add_argument("--product-head")
    parser.add_argument("--product-repo")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    args = parser.parse_args(argv)

    product_head = args.product_head or (_git_head(args.product_repo) if args.product_repo else "")
    adapter = load_mapping(args.adapter)
    target_plan_path = Path(args.target_plan) if args.target_plan else _adapter_audit_path(args.adapter, adapter, "target_plan")
    target_plan = load_mapping(target_plan_path) if target_plan_path else None
    report = audit_knowledge(
        semantic_map=load_mapping(args.semantic_map),
        code_map=load_mapping(args.code_map),
        adapter=adapter,
        cases=load_sequence(args.cases),
        excluded_leads=load_sequence(args.excluded_leads),
        target_plan=target_plan,
        understanding_profile=load_mapping(args.understanding_profile) if args.understanding_profile else None,
        product_head=product_head,
        product_repo=args.product_repo,
        automation_root=Path.cwd(),
    )
    if args.format == "markdown":
        print(render_audit_markdown(report), end="")
    else:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.status == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
