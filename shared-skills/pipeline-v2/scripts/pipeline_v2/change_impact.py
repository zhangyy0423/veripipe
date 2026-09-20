"""change_impact.py — 变更驱动挖掘：改动文件 → 受影响项 → 针对性测试清单。

设计依据：knowledge-map-plan.md §C。产品新 commit/PR 后：
  改动文件列表（git diff --name-only）
    ──code-map 反查──► 受影响 模块 / 端点 / WS command / 符号 / 调用波及面
    ──links / adapter 反查──► 关联的 semantic_map_entry / 已有 L3 关系 / L1 fuzz 端点
  ──► 针对性测试清单：
       · 必跑：受影响 entry 覆盖的现有 L3 关系 + 命中该端点的 L1 fuzz case
       · 补缺：受影响但无 L3 关系 / 无 fuzz 覆盖的 → 选靶建议（喂 L4 / 人工补）

铁律：本模块产品中立——不出现任何产品名 / 写死端点 / 产品路径。
所有产品具体信息均来自传入的 code-map / semantic-map / adapter 配置数据结构。
纯函数，离线可单测（fixture diff + fixture code-map）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from .code_map import CodeMap, code_map_from_dict


@dataclass
class ImpactedItems:
    """受影响项（按 code-map 反查得出）。"""

    changed_files: List[str] = field(default_factory=list)
    modules: List[str] = field(default_factory=list)
    endpoints: List[Dict[str, Any]] = field(default_factory=list)
    ws_commands: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    transitive_symbols: List[str] = field(default_factory=list)
    semantic_map_entry_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "changed_files": self.changed_files,
            "modules": self.modules,
            "endpoints": self.endpoints,
            "ws_commands": self.ws_commands,
            "symbols": self.symbols,
            "transitive_symbols": self.transitive_symbols,
            "semantic_map_entry_ids": self.semantic_map_entry_ids,
        }


@dataclass
class TargetedTestPlan:
    """针对性测试清单。"""

    impacted: ImpactedItems
    must_run_l2_checks: List[Dict[str, Any]] = field(default_factory=list)
    must_run_l3_relations: List[Dict[str, Any]] = field(default_factory=list)
    must_run_l1_fuzz: List[Dict[str, Any]] = field(default_factory=list)
    coverage_gaps: List[Dict[str, Any]] = field(default_factory=list)
    anchor_commit: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "anchor_commit": self.anchor_commit,
            "impacted": self.impacted.to_dict(),
            "must_run_l2_checks": self.must_run_l2_checks,
            "must_run_l3_relations": self.must_run_l3_relations,
            "must_run_l1_fuzz": self.must_run_l1_fuzz,
            "coverage_gaps": self.coverage_gaps,
        }

    @property
    def has_targets(self) -> bool:
        return bool(self.must_run_l2_checks or self.must_run_l3_relations or self.must_run_l1_fuzz)


def _norm(path: str) -> str:
    return str(path).strip().lstrip("./").rstrip("/")


def compute_impacted(
    changed_files: Sequence[str],
    code_map: CodeMap,
    *,
    transitive_depth: int = 2,
) -> ImpactedItems:
    """从改动文件反查受影响的模块/端点/符号/关联 entry。"""
    changed = sorted({_norm(f) for f in changed_files if str(f).strip()})

    modules = code_map.modules_for_files(changed)
    module_ids = sorted({str(m.get("id", "")) for m in modules if m.get("id")})

    endpoints = code_map.endpoints_in_files(changed)
    http_ws = [e for e in endpoints if e.get("kind") in ("http", "ws")]
    ws_commands = sorted(
        {str(e.get("command", "")) for e in endpoints if e.get("kind") == "ws_command"}
    )

    # 受影响符号：改动文件里的导出符号
    symbols: Set[str] = set()
    for f in changed:
        for exp in code_map.exports_in_file(f):
            sym = str(exp.get("symbol", ""))
            if sym:
                symbols.add(sym)

    # 反向波及面：哪些符号（间接）调用了受影响符号
    transitive: Set[str] = set()
    for sym in symbols:
        transitive |= code_map.transitive_callers(sym, max_depth=transitive_depth)
    transitive -= symbols

    # 关联 semantic entry：文件直链 + WS command 链
    entry_ids: Set[str] = set(code_map.entries_for_files(changed))
    for cmd in ws_commands:
        for eid in code_map.entries_for_symbol(cmd):
            entry_ids.add(eid)

    return ImpactedItems(
        changed_files=changed,
        modules=module_ids,
        endpoints=[dict(e) for e in http_ws],
        ws_commands=ws_commands,
        symbols=sorted(symbols),
        transitive_symbols=sorted(transitive),
        semantic_map_entry_ids=sorted(entry_ids),
    )


def _l3_scenarios(adapter: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    driver = adapter.get("http_driver") or {}
    scenarios = driver.get("scenarios") if isinstance(driver, Mapping) else []
    return [
        s
        for s in list(scenarios or [])
        if isinstance(s, Mapping)
    ]


def _l3_relation_catalog_entry_ids(adapter: Mapping[str, Any]) -> Set[str]:
    """Top-level L3 relation catalog entries are knowledge/gate metadata, not executable targets."""

    entry_ids: Set[str] = set()
    relations = adapter.get("l3_relations")
    if not isinstance(relations, list):
        return entry_ids
    for item in relations:
        if not isinstance(item, Mapping):
            continue
        entry_id = str(item.get("semantic_map_entry_id") or "").strip()
        if entry_id:
            entry_ids.add(entry_id)
    return entry_ids


def _l1_cases(adapter: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    fuzz = adapter.get("l1_fuzz") or {}
    cases = fuzz.get("cases") or []
    return [c for c in cases if isinstance(c, Mapping)]


def _playwright_specs(adapter: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    playwright = adapter.get("playwright") or {}
    if not isinstance(playwright, Mapping):
        return []
    whitelist = playwright.get("spec_whitelist") or []
    if not isinstance(whitelist, list):
        return []
    web_subdir = _norm(str(playwright.get("web_subdir") or ""))
    spec_dir = _norm(str(playwright.get("spec_dir") or ""))
    specs: List[Mapping[str, Any]] = []
    for item in whitelist:
        if not isinstance(item, Mapping):
            continue
        spec = str(item.get("spec") or "").strip()
        entry_id = str(item.get("semantic_map_entry_id") or "").strip()
        if not spec or not entry_id:
            continue
        path_parts = [part for part in [web_subdir, spec_dir, spec] if part]
        spec_path = "/".join(path_parts)
        specs.append(
            {
                "spec": spec,
                "spec_path": spec_path,
                "semantic_map_entry_id": entry_id,
                "kind": item.get("kind", ""),
                "reason": item.get("reason", ""),
            }
        )
    return specs


def _playwright_spec_links(code_map: CodeMap) -> List[Mapping[str, Any]]:
    specs: List[Mapping[str, Any]] = []
    for link in code_map.links:
        if not isinstance(link, Mapping):
            continue
        if str(link.get("from_kind") or "") != "playwright_spec":
            continue
        entry_id = str(link.get("semantic_map_entry_id") or "").strip()
        spec_path = _norm(str(link.get("from_file") or ""))
        if not entry_id or not spec_path:
            continue
        spec = str(link.get("from") or "").strip() or spec_path.split("/")[-1]
        specs.append(
            {
                "spec": spec,
                "spec_path": spec_path,
                "semantic_map_entry_id": entry_id,
                "kind": "code_map_link",
                "reason": "code-map playwright_spec link",
            }
        )
    return specs


def _route_matches_endpoint(case_path: str, endpoint_route: str) -> bool:
    cp = _norm(case_path)
    er = _norm(endpoint_route)
    if not cp or not er:
        return False
    # 端点 route 含路径参数（{id}），按段前缀比对
    cp_parts = cp.split("/")
    er_parts = er.split("/")
    if len(cp_parts) < len(er_parts):
        return False
    for i, seg in enumerate(er_parts):
        if seg.startswith("{") and seg.endswith("}"):
            continue
        if i >= len(cp_parts) or cp_parts[i] != seg:
            return False
    return True


def _endpoint_key(method: str, route: str) -> str:
    return f"{str(method or '').upper()} {route}"


def build_test_plan(
    changed_files: Sequence[str],
    code_map: CodeMap,
    adapter: Mapping[str, Any],
    *,
    anchor_commit: str = "",
    transitive_depth: int = 2,
) -> TargetedTestPlan:
    """组装针对性测试清单：必跑 L3 关系 + L1 fuzz + 补缺建议。"""
    impacted = compute_impacted(changed_files, code_map, transitive_depth=transitive_depth)
    impacted_entry_ids = set(impacted.semantic_map_entry_ids)
    changed = set(impacted.changed_files)

    must_l2: List[Dict[str, Any]] = []
    l2_seen: Set[str] = set()
    for spec in _playwright_specs(adapter):
        entry_id = str(spec.get("semantic_map_entry_id") or "")
        spec_path = _norm(str(spec.get("spec_path") or ""))
        if entry_id not in impacted_entry_ids and spec_path not in changed:
            continue
        key = f"{spec.get('spec')}:{entry_id}"
        if key in l2_seen:
            continue
        l2_seen.add(key)
        reason = (
            "changed Playwright spec is whitelisted"
            if spec_path in changed and entry_id not in impacted_entry_ids
            else "impacted semantic entry has an existing L2 machine/spec check"
        )
        must_l2.append(
            {
                "spec": spec.get("spec", ""),
                "spec_path": spec_path,
                "semantic_map_entry_id": entry_id,
                "oracle_level": "L2",
                "selection_reason": reason,
            }
        )
    for spec in _playwright_spec_links(code_map):
        entry_id = str(spec.get("semantic_map_entry_id") or "")
        spec_path = _norm(str(spec.get("spec_path") or ""))
        if entry_id not in impacted_entry_ids and spec_path not in changed:
            continue
        key = f"{spec.get('spec')}:{entry_id}"
        if key in l2_seen:
            continue
        l2_seen.add(key)
        reason = (
            "changed Playwright spec has a code-map playwright_spec link"
            if spec_path in changed
            else "impacted semantic entry has a code-map playwright_spec L2 link"
        )
        must_l2.append(
            {
                "spec": spec.get("spec", ""),
                "spec_path": spec_path,
                "semantic_map_entry_id": entry_id,
                "oracle_level": "L2",
                "selection_reason": reason,
            }
        )

    # 必跑 L3：scenario 的 semantic_map_entry_id 命中受影响 entry
    must_l3: List[Dict[str, Any]] = []
    covered_entry_ids: Set[str] = set()
    for scenario in _l3_scenarios(adapter):
        sid_entry = str(scenario.get("semantic_map_entry_id", ""))
        if sid_entry and sid_entry in impacted_entry_ids:
            must_l3.append(
                {
                    "scenario_id": scenario.get("scenario_id", ""),
                    "strategy": scenario.get("strategy", ""),
                    "relation_id": scenario.get("relation_id", ""),
                    "semantic_map_entry_id": sid_entry,
                    "oracle_level": "L3",
                    "execution_gate": scenario.get("execution_gate", ""),
                    "selection_reason": "impacted semantic entry has an existing L3 relation",
                }
            )
            covered_entry_ids.add(sid_entry)

    # 必跑 L1 fuzz：case method + path 命中受影响 HTTP 端点；methodless WS endpoint 不被 HTTP fuzz 覆盖。
    impacted_endpoint_keys = [
        _endpoint_key(str(e.get("method") or ""), str(e.get("route") or ""))
        for e in impacted.endpoints
        if str(e.get("route") or "")
    ]
    must_l1: List[Dict[str, Any]] = []
    covered_endpoint_keys: Set[str] = set()
    for case in _l1_cases(adapter):
        case_path = str(case.get("path", ""))
        case_method = str(case.get("method") or "").upper()
        for endpoint in impacted.endpoints:
            route = str(endpoint.get("route", ""))
            endpoint_method = str(endpoint.get("method") or "").upper()
            endpoint_key = _endpoint_key(endpoint_method, route)
            if not endpoint_method or endpoint_method != case_method:
                continue
            if _route_matches_endpoint(case_path, route):
                must_l1.append(
                    {
                        "case_id": case.get("id", ""),
                        "method": case.get("method", ""),
                        "path": case_path,
                        "matched_endpoint": endpoint_key,
                        "semantic_map_entry_id": str(case.get("semantic_map_entry_id") or ""),
                        "oracle_level": "L1",
                        "selection_reason": "impacted endpoint is covered by bounded fuzz",
                    }
                )
                covered_endpoint_keys.add(endpoint_key)
                break

    # 补缺：受影响但无 L3 关系覆盖的 entry / 无 fuzz 覆盖的端点
    gaps: List[Dict[str, Any]] = []
    relation_catalog_entry_ids = _l3_relation_catalog_entry_ids(adapter)
    for eid in sorted(impacted_entry_ids - covered_entry_ids):
        if eid in relation_catalog_entry_ids:
            target_channel = "owner-gate"
            suggestion = "受影响 entry 仅有 owner-gated L3 relation catalog：需要 owner gate 和可执行 scenario 后才能运行。"
        else:
            target_channel = "L4-suggestion"
            suggestion = "受影响 entry 无现有 L3 关系覆盖：建议补差分/蜕变关系或喂 L4 选靶。"
        gaps.append(
            {
                "kind": "entry_without_l3_relation",
                "semantic_map_entry_id": eid,
                "target_channel": target_channel,
                "suggestion": suggestion,
            }
        )
    for endpoint in impacted.endpoints:
        route = str(endpoint.get("route", ""))
        endpoint_key = _endpoint_key(str(endpoint.get("method") or ""), route)
        if route and endpoint_key not in covered_endpoint_keys:
            gaps.append(
                {
                    "kind": "endpoint_without_l1_fuzz",
                    "endpoint": {
                        "kind": endpoint.get("kind"),
                        "method": endpoint.get("method"),
                        "route": route,
                    },
                    "target_channel": "L4-suggestion",
                    "suggestion": "受影响端点无 L1 fuzz case：建议补畸形输入 fuzz case。",
                }
            )

    return TargetedTestPlan(
        impacted=impacted,
        must_run_l2_checks=must_l2,
        must_run_l3_relations=must_l3,
        must_run_l1_fuzz=must_l1,
        coverage_gaps=gaps,
        anchor_commit=anchor_commit,
    )


def build_profile_seed_plan(
    adapter: Mapping[str, Any],
    *,
    anchor_commit: str = "",
    explicit_target_plan: Optional[Mapping[str, Any]] = None,
) -> TargetedTestPlan:
    """Build an explicit L2 seed plan from an adapter profile whitelist.

    This is intentionally opt-in. It lets a profile with already explained
    Playwright targets run in a no-diff situation without weakening the default
    empty-plan fail-closed behavior.
    """
    must_l2: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    semantic_ids: Set[str] = set()
    explicit_targets = _explicit_l2_targets(explicit_target_plan)
    for spec in _playwright_specs(adapter):
        entry_id = str(spec.get("semantic_map_entry_id") or "")
        spec_name = str(spec.get("spec") or "")
        if not entry_id or not spec_name:
            continue
        if explicit_targets is not None:
            target = explicit_targets.get((spec_name, entry_id))
            if target is None:
                continue
        else:
            target = {}
        key = f"{spec_name}:{entry_id}"
        if key in seen:
            continue
        seen.add(key)
        semantic_ids.add(entry_id)
        must_l2.append(
            {
                "spec": spec_name,
                "spec_path": _norm(str(target.get("spec_path") or spec.get("spec_path") or "")),
                "semantic_map_entry_id": entry_id,
                "oracle_level": "L2",
                "selection_reason": (
                    "profile seed target from explicit target plan"
                    if explicit_targets is not None
                    else "profile seed target from adapter whitelist"
                ),
            }
        )
    impacted = ImpactedItems(semantic_map_entry_ids=sorted(semantic_ids))
    return TargetedTestPlan(
        impacted=impacted,
        must_run_l2_checks=must_l2,
        anchor_commit=anchor_commit,
    )


def _explicit_l2_targets(
    explicit_target_plan: Optional[Mapping[str, Any]],
) -> Optional[Dict[tuple[str, str], Mapping[str, Any]]]:
    if explicit_target_plan is None:
        return None
    if explicit_target_plan.get("__pipeline_v2_missing_target_plan"):
        return {}
    has_any_target = any(
        isinstance(explicit_target_plan.get(key), list) and explicit_target_plan.get(key)
        for key in (
            "must_run_l2_checks",
            "must_run_l3_relations",
            "must_run_l1_fuzz",
            "coverage_gaps",
        )
    )
    if not has_any_target:
        return None
    items = explicit_target_plan.get("must_run_l2_checks")
    if not isinstance(items, list):
        return {}
    targets: Dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        spec = str(item.get("spec") or "").strip()
        entry_id = str(item.get("semantic_map_entry_id") or "").strip()
        if spec and entry_id:
            targets[(spec, entry_id)] = item
    return targets


def build_test_plan_from_dicts(
    changed_files: Sequence[str],
    code_map_data: Mapping[str, Any],
    adapter: Mapping[str, Any],
    *,
    anchor_commit: str = "",
    transitive_depth: int = 2,
) -> TargetedTestPlan:
    """便捷入口：直接喂 dict 形式的 code-map（离线测试 / CLI 用）。"""
    return build_test_plan(
        changed_files,
        code_map_from_dict(code_map_data),
        adapter,
        anchor_commit=anchor_commit,
        transitive_depth=transitive_depth,
    )


def render_plan_markdown(plan: TargetedTestPlan) -> str:
    """把测试清单渲染成人可读 Markdown（供 .pending-loop 待办附带）。"""
    lines: List[str] = []
    lines.append("## 针对性测试清单（变更驱动）")
    if plan.anchor_commit:
        lines.append(f"- 锚点 commit: `{plan.anchor_commit}`")
    imp = plan.impacted
    lines.append(f"- 改动文件: {len(imp.changed_files)}；受影响模块: {len(imp.modules)}；"
                 f"端点: {len(imp.endpoints)}；WS command: {len(imp.ws_commands)}；"
                 f"关联 entry: {len(imp.semantic_map_entry_ids)}")
    lines.append("")
    lines.append("### 必跑 — L2 machine/spec checks")
    if plan.must_run_l2_checks:
        for check in plan.must_run_l2_checks:
            lines.append(
                f"- `[L2]` `{check['spec']}` → {check['semantic_map_entry_id']}；"
                f"{check.get('selection_reason','')}"
            )
    else:
        lines.append("- （无受影响 entry 命中现有 L2 machine/spec check）")
    lines.append("")
    lines.append("### 必跑 — 现有 L3 关系")
    if plan.must_run_l3_relations:
        for rel in plan.must_run_l3_relations:
            lines.append(
                f"- `[L3]` `{rel['scenario_id']}` ({rel['strategy']}) → "
                f"{rel['semantic_map_entry_id']}；{rel.get('selection_reason','')}"
            )
    else:
        lines.append("- （无受影响 entry 命中现有 L3 关系）")
    lines.append("")
    lines.append("### 必跑 — L1 fuzz")
    if plan.must_run_l1_fuzz:
        for case in plan.must_run_l1_fuzz:
            lines.append(
                f"- `[L1]` `{case['case_id']}` {case['method']} {case['path']}"
                f"（命中 {case['matched_endpoint']}）"
                f" → {case.get('semantic_map_entry_id') or 'unmapped'}；"
                f"{case.get('selection_reason','')}"
            )
    else:
        lines.append("- （无受影响端点命中现有 L1 fuzz case）")
    lines.append("")
    lines.append("### 补缺建议")
    if plan.coverage_gaps:
        for gap in plan.coverage_gaps:
            if gap["kind"] == "entry_without_l3_relation":
                lines.append(
                    f"- [{gap.get('target_channel','L4-suggestion')}] [entry 无关系] "
                    f"{gap['semantic_map_entry_id']}：{gap['suggestion']}"
                )
            else:
                ep = gap["endpoint"]
                lines.append(
                    f"- [{gap.get('target_channel','L4-suggestion')}] [端点无 fuzz] "
                    f"{ep.get('method','')} {ep.get('route','')}：{gap['suggestion']}"
                )
    else:
        lines.append("- （无补缺项）")
    return "\n".join(lines) + "\n"


def _git_changed_files(product_repo: str, anchor: str, head: str = "HEAD") -> List[str]:
    import subprocess

    result = subprocess.run(
        ["git", "-C", product_repo, "diff", "--name-only", f"{anchor}..{head}"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _load_adapter_target_plan(
    *,
    adapter_path: "Path",
    adapter: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    import json

    audit = adapter.get("knowledge_audit")
    if not isinstance(audit, Mapping):
        return None
    value = str(audit.get("target_plan") or "").strip()
    if not value:
        return None
    raw_path = adapter_path.__class__(value)
    candidates = [raw_path] if raw_path.is_absolute() else [
        adapter_path.parent / raw_path,
        adapter_path.parent.parent.parent / raw_path,
        raw_path,
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                data = json.loads(candidate.read_text(encoding="utf-8"))
                return data if isinstance(data, Mapping) else {}
        except OSError:
            continue
    return {"__pipeline_v2_missing_target_plan": True}


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import json
    from pathlib import Path

    try:
        import yaml
    except Exception:  # pragma: no cover
        yaml = None

    parser = argparse.ArgumentParser(
        description="变更驱动挖掘：改动文件 → 受影响项 + 针对性测试清单（产品中立）。"
    )
    parser.add_argument("--code-map", required=True, help="code-map.yaml 路径")
    parser.add_argument("--adapter", required=True, help="adapter 配置 JSON（含 http_driver/l1_fuzz）")
    parser.add_argument("--product-repo", help="产品仓路径（与 --anchor 配合，用 git diff 取改动文件）")
    parser.add_argument("--anchor", help="基线 commit（默认取 code-map 的 based_on_product_commit）")
    parser.add_argument("--head", default="HEAD", help="对比终点（默认 HEAD）")
    parser.add_argument("--changed-file", action="append", default=[], help="直接指定改动文件（可多次；离线用）")
    parser.add_argument(
        "--seed-from-adapter-profile",
        action="store_true",
        help="无改动文件时，从 adapter playwright.spec_whitelist 生成显式 L2 seed target plan",
    )
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    args = parser.parse_args(argv)

    with Path(args.code_map).open("r", encoding="utf-8") as handle:
        code_map_data = yaml.safe_load(handle)
    with Path(args.adapter).open("r", encoding="utf-8") as handle:
        adapter = json.load(handle)

    anchor = args.anchor or str(code_map_data.get("based_on_product_commit", ""))

    changed = list(args.changed_file)
    if not changed and args.product_repo and anchor:
        changed = _git_changed_files(args.product_repo, anchor, args.head)

    if not changed and args.seed_from_adapter_profile:
        explicit_target_plan = _load_adapter_target_plan(
            adapter_path=Path(args.adapter),
            adapter=adapter,
        )
        plan = build_profile_seed_plan(
            adapter,
            anchor_commit=anchor,
            explicit_target_plan=explicit_target_plan,
        )
    else:
        plan = build_test_plan_from_dicts(changed, code_map_data, adapter, anchor_commit=anchor)
    if args.format == "markdown":
        print(render_plan_markdown(plan), end="")
    else:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
