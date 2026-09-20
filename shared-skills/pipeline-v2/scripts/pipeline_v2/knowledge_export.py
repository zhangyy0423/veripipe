"""knowledge_export.py — 从真相源单向生成 Obsidian 兼容知识库视图。

设计依据：knowledge-map-plan.md §D。真相源（仓内 git 文件）= semantic-map + code-map +
台账 cases/hits + 已排除线索（mining-capability-plan §九）。本模块**只读真相源、只写
knowledge/**，生成：
  - skills/<skill>.md     每个 skill 一页（含其 flow/entry、关联代码符号、覆盖状态）
  - code/<module>.md      每个代码模块一页（端点 / 导出符号 / 关联 entry）
  - findings/<fp>.md       每条立案/排除线索一页（指纹、oracle level、结论）
  - index.md               覆盖率快照 + 全量索引

铁律：
- 单向生成、不反写真相源；幂等可重跑（同输入 → 同输出）。
- frontmatter（YAML）+ `[[wikilink]]` 互链，Obsidian 可跳。
- 本模块产品中立：不出现产品名/写死端点/产品路径，全部来自传入数据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .code_map import CodeMap, code_map_from_dict


CONFIRMED_CASE_ORACLE_LEVELS = {"L1", "L2", "L3"}
NON_CONFIRMED_CASE_STATUSES = {
    "candidate",
    "observing",
    "intermittent",
    "expired",
    "environment-event",
    "vetoed",
    "false-positive",
}
NON_CONFIRMED_TRIAGE_RESULTS = {"false-positive", "owner-review-required"}


def _slug(text: str) -> str:
    """文件名安全 slug：保留字母数字/下划线/连字符/点，其余转连字符。"""
    s = re.sub(r"[^\w.\-]+", "-", str(text).strip())
    return s.strip("-") or "unknown"


def _yaml_frontmatter(meta: Mapping[str, Any]) -> str:
    """生成稳定排序的 YAML frontmatter（不依赖 PyYAML，保证幂等可读）。"""
    lines = ["---"]
    for key in sorted(meta.keys()):
        value = meta[key]
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_scalar(item)}")
        else:
            lines.append(f"{key}: {_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    text = str(value)
    if text == "" or re.search(r"[:#\[\]{}\"']", text) or text != text.strip():
        return '"' + text.replace('"', '\\"') + '"'
    return text


def _wikilink(target: str, alias: Optional[str] = None) -> str:
    if alias and alias != target:
        return f"[[{target}|{alias}]]"
    return f"[[{target}]]"


def _render_link_reference(link: Mapping[str, Any]) -> str:
    """Render a code-map link without creating wikilinks for pages we do not emit."""

    kind = str(link.get("from_kind") or "").strip()
    source = str(link.get("from") or "").strip()
    if kind == "ws_command" and source:
        return _wikilink("ws_command-" + _slug(source), source)

    label = f"{kind}: {source}".strip(": ") or source or kind or "unknown"
    file_name = str(link.get("from_file") or "").strip()
    line = str(link.get("from_line") or "").strip()
    if file_name:
        suffix = f":{line}" if line else ""
        return f"`{label}` ({file_name}{suffix})"
    return f"`{label}`"


@dataclass
class TruthSources:
    """真相源集合（全部为已加载的内存数据，模块只读）。"""

    semantic_map: Mapping[str, Any]
    code_map: Mapping[str, Any]
    cases: Sequence[Mapping[str, Any]] = field(default_factory=list)
    excluded_leads: Sequence[Mapping[str, Any]] = field(default_factory=list)

    @property
    def entries(self) -> List[Mapping[str, Any]]:
        items = self.semantic_map.get("entries", [])
        return list(items) if isinstance(items, list) else []

    @property
    def based_on_product_commit(self) -> str:
        return str(
            self.semantic_map.get("based_on_product_commit")
            or self.code_map.get("based_on_product_commit")
            or ""
        )


def _entries_by_skill(entries: Iterable[Mapping[str, Any]]) -> Dict[str, List[Mapping[str, Any]]]:
    grouped: Dict[str, List[Mapping[str, Any]]] = {}
    for entry in entries:
        skill = (entry.get("skill") or {}).get("name", "") or "unknown"
        grouped.setdefault(skill, []).append(entry)
    return grouped


def _covered_entry_ids(cases: Iterable[Mapping[str, Any]], *, commit: str = "") -> set:
    covered = set()
    for case in cases:
        if not _is_confirmed_case_coverage_candidate(case):
            continue
        case_commit = str(case.get("based_on_product_commit") or "").strip()
        if commit and case_commit and case_commit != commit:
            continue
        if commit and not case_commit:
            continue
        eid = case.get("semantic_map_entry_id")
        if eid:
            covered.add(str(eid))
    return covered


def _is_confirmed_case_coverage_candidate(case: Mapping[str, Any]) -> bool:
    oracle_level = str(case.get("oracle_level") or "").strip()
    if oracle_level not in CONFIRMED_CASE_ORACLE_LEVELS:
        return False
    status = str(case.get("status") or "").strip().lower()
    if status in NON_CONFIRMED_CASE_STATUSES:
        return False
    triage_result = str(case.get("triage_result") or "").strip().lower()
    if triage_result in NON_CONFIRMED_TRIAGE_RESULTS:
        return False
    return True


# ---------- 各页生成 ----------


def _render_skill_page(
    skill: str,
    entries: List[Mapping[str, Any]],
    code_map: CodeMap,
    covered_ids: set,
    commit: str,
) -> str:
    entry_ids = [str(e.get("id", "")) for e in entries]
    covered = [eid for eid in entry_ids if eid in covered_ids]
    meta = {
        "type": "skill",
        "skill": skill,
        "based_on_product_commit": commit,
        "entry_count": len(entries),
        "covered_entry_count": len(covered),
        "tags": ["knowledge/skill"],
    }
    out = [_yaml_frontmatter(meta), "", f"# Skill: {skill}", ""]
    desc = ""
    if entries:
        desc = str((entries[0].get("skill") or {}).get("description", "")).strip()
    if desc:
        out.append(desc)
        out.append("")
    out.append("## Flows / 可断言终态")
    for entry in entries:
        eid = str(entry.get("id", ""))
        flow = entry.get("flow") or {}
        summary = str(flow.get("summary", "")).strip()
        ets = entry.get("expected_terminal_state") or {}
        assertion = str(ets.get("assertion", "")).strip()
        cov = "✅ 已覆盖" if eid in covered_ids else "⬜ 未覆盖"
        out.append(f"### {eid} — {cov}")
        if summary:
            out.append(f"- 流程：{summary}")
        if assertion:
            out.append(f"- 终态断言：`{assertion}`")
        linked = [
            link
            for link in code_map.links
            if str(link.get("semantic_map_entry_id", "")) == eid
        ]
        for rendered in sorted({_render_link_reference(link) for link in linked}):
            out.append(f"- 关联代码：{rendered}")
        out.append("")
    out.append("---")
    out.append(f"> 真相源：semantic-map.yaml（基于 commit `{commit}`）。本页单向生成，勿手改。")
    return "\n".join(out) + "\n"


def _render_code_page(module: Mapping[str, Any], commit: str) -> str:
    mod_id = str(module.get("id", ""))
    endpoints = module.get("endpoints", []) or []
    http_ws = [e for e in endpoints if e.get("kind") in ("http", "ws")]
    ws_cmds = [e for e in endpoints if e.get("kind") == "ws_command"]
    exports = module.get("exports", []) or []
    meta = {
        "type": "code-module",
        "module": mod_id,
        "package": str(module.get("package", "")),
        "based_on_product_commit": commit,
        "endpoint_count": len(http_ws),
        "ws_command_count": len(ws_cmds),
        "export_count": len(exports),
        "tags": ["knowledge/code"],
    }
    out = [_yaml_frontmatter(meta), "", f"# Module: {mod_id}", ""]
    if http_ws:
        out.append("## HTTP/WS 端点")
        for ep in http_ws:
            method = str(ep.get("method", "")).strip()
            route = str(ep.get("route", ""))
            out.append(f"- `{method} {route}` → {ep.get('handler','')}")
        out.append("")
    if ws_cmds:
        out.append("## WS Commands")
        for cmd in ws_cmds:
            name = str(cmd.get("command", ""))
            out.append(f"- {_wikilink('ws_command-' + _slug(name), name)}")
        out.append("")
    if exports:
        out.append("## 导出符号")
        for exp in exports[:200]:
            out.append(f"- `{exp.get('symbol','')}`（{exp.get('file','')}:{exp.get('line','')}）")
        out.append("")
    out.append("---")
    out.append(f"> 真相源：code-map.yaml（基于 commit `{commit}`）。本页单向生成，勿手改。")
    return "\n".join(out) + "\n"


def _render_ws_command_page(
    command: str,
    endpoint: Mapping[str, Any],
    module_id: str,
    links: Sequence[Mapping[str, Any]],
    commit: str,
) -> str:
    linked_entries = sorted(
        {
            str(link.get("semantic_map_entry_id") or "")
            for link in links
            if str(link.get("from_kind") or "") == "ws_command"
            and str(link.get("from") or "") == command
            and str(link.get("semantic_map_entry_id") or "")
        }
    )
    meta = {
        "type": "ws-command",
        "command": command,
        "module": module_id,
        "file": str(endpoint.get("file", "")),
        "line": str(endpoint.get("line", "")),
        "based_on_product_commit": commit,
        "semantic_entry_count": len(linked_entries),
        "tags": ["knowledge/code", "knowledge/ws-command"],
    }
    out = [_yaml_frontmatter(meta), "", f"# WS Command: {command}", ""]
    out.append(f"- 模块：{_wikilink('module-' + _slug(module_id), module_id)}")
    if endpoint.get("file"):
        line = str(endpoint.get("line") or "")
        suffix = f":{line}" if line else ""
        out.append(f"- 定义：`{endpoint.get('file')}{suffix}`")
    out.append("")
    out.append("## Semantic Entries")
    if linked_entries:
        for entry_id in linked_entries:
            out.append(f"- `{entry_id}`")
    else:
        out.append("- （暂无 semantic-map link）")
    out.append("")
    out.append("---")
    out.append(f"> 真相源：code-map.yaml（基于 commit `{commit}`）。本页单向生成，勿手改。")
    return "\n".join(out) + "\n"


def _render_finding_page(finding: Mapping[str, Any], commit: str) -> str:
    fp = str(finding.get("fingerprint", "") or finding.get("id", "unknown"))
    finding_commit = str(finding.get("based_on_product_commit") or commit)
    meta = {
        "type": "finding",
        "fingerprint": fp,
        "oracle_level": str(finding.get("oracle_level", "")),
        "status": str(finding.get("status", "")),
        "conclusion": str(finding.get("conclusion", "")),
        "based_on_product_commit": finding_commit,
        "tags": ["knowledge/finding"],
    }
    out = [_yaml_frontmatter(meta), "", f"# Finding: {fp}", ""]
    if finding.get("summary"):
        out.append(str(finding.get("summary")))
        out.append("")
    entry_id = finding.get("semantic_map_entry_id")
    if entry_id:
        out.append(f"- 关联 skill entry：`{entry_id}`")
    if finding.get("root_cause"):
        out.append(f"- 根因：{finding.get('root_cause')}")
    if finding.get("conclusion"):
        out.append(f"- 结论：{finding.get('conclusion')}")
    out.append("")
    out.append("---")
    out.append("> 真相源：台账 cases / mining-capability-plan §九。本页单向生成，勿手改。")
    return "\n".join(out) + "\n"


def _render_index_page(
    sources: TruthSources,
    skills: List[str],
    modules: List[str],
    commands: List[str],
    findings: List[str],
    coverage: Mapping[str, Any],
) -> str:
    commit = sources.based_on_product_commit
    meta = {
        "type": "index",
        "based_on_product_commit": commit,
        "skill_count": len(skills),
        "module_count": len(modules),
        "command_count": len(commands),
        "finding_count": len(findings),
        "entry_total": coverage.get("total", 0),
        "entry_covered": coverage.get("covered", 0),
        "tags": ["knowledge/index"],
    }
    out = [_yaml_frontmatter(meta), "", "# 知识库索引 / 覆盖率快照", ""]
    out.append(f"- 基于产品 commit：`{commit}`")
    total = coverage.get("total", 0)
    covered = coverage.get("covered", 0)
    rate = f"{(covered / total * 100):.0f}%" if total else "—"
    out.append(f"- 语义图 entry 覆盖：{covered}/{total}（{rate}）")
    out.append("")
    out.append("## Skills")
    for skill in skills:
        out.append(f"- {_wikilink('skill-' + _slug(skill), skill)}")
    out.append("")
    out.append("## Code Modules")
    for mod in modules:
        out.append(f"- {_wikilink('module-' + _slug(mod), mod)}")
    out.append("")
    out.append("## WS Commands")
    if commands:
        for command in commands:
            out.append(f"- {_wikilink('ws_command-' + _slug(command), command)}")
    else:
        out.append("- （暂无 WS command）")
    out.append("")
    out.append("## Findings")
    if findings:
        for fp in findings:
            out.append(f"- {_wikilink('finding-' + _slug(fp), fp)}")
    else:
        out.append("- （暂无立案/排除线索）")
    out.append("")
    out.append("---")
    out.append("> 本知识库由 knowledge_export.py 从仓内真相源单向生成，幂等可重跑，勿手改。")
    return "\n".join(out) + "\n"


def export_knowledge(sources: TruthSources, output_dir: str | Path) -> Dict[str, Any]:
    """生成知识库到 output_dir/knowledge 结构；返回生成报告。"""
    code_map = code_map_from_dict(sources.code_map)
    out_root = Path(output_dir)
    skills_dir = out_root / "skills"
    code_dir = out_root / "code"
    findings_dir = out_root / "findings"
    for d in (skills_dir, code_dir, findings_dir):
        d.mkdir(parents=True, exist_ok=True)

    commit = sources.based_on_product_commit
    entries = sources.entries
    covered_ids = _covered_entry_ids(sources.cases, commit=commit)
    grouped = _entries_by_skill(entries)

    written: List[str] = []

    # skills/
    skill_names: List[str] = sorted(grouped.keys())
    for skill in skill_names:
        page = _render_skill_page(skill, grouped[skill], code_map, covered_ids, commit)
        path = skills_dir / f"skill-{_slug(skill)}.md"
        _write_if_changed(path, page)
        written.append(str(path))

    # code/
    module_ids: List[str] = []
    command_records: List[tuple[str, Mapping[str, Any], str]] = []
    for module in code_map.modules:
        mod_id = str(module.get("id", ""))
        # 只为有内容（端点 / 导出符号）的模块出页，避免噪声
        if not (module.get("endpoints") or module.get("exports")):
            continue
        module_ids.append(mod_id)
        for endpoint in module.get("endpoints", []) or []:
            if not isinstance(endpoint, Mapping) or endpoint.get("kind") != "ws_command":
                continue
            command = str(endpoint.get("command") or "").strip()
            if command:
                command_records.append((command, endpoint, mod_id))
        page = _render_code_page(module, commit)
        path = code_dir / f"module-{_slug(mod_id)}.md"
        _write_if_changed(path, page)
        written.append(str(path))
    module_ids.sort()

    command_names: List[str] = []
    seen_commands = set()
    for command, endpoint, module_id in sorted(command_records, key=lambda item: item[0]):
        if command in seen_commands:
            continue
        seen_commands.add(command)
        command_names.append(command)
        page = _render_ws_command_page(command, endpoint, module_id, code_map.links, commit)
        path = code_dir / f"ws_command-{_slug(command)}.md"
        _write_if_changed(path, page)
        written.append(str(path))

    # findings/  —— 台账 cases + §九 排除线索
    finding_fps: List[str] = []
    findings_all: List[Mapping[str, Any]] = list(sources.cases) + list(sources.excluded_leads)
    for finding in findings_all:
        fp = str(finding.get("fingerprint", "") or finding.get("id", ""))
        if not fp:
            continue
        finding_fps.append(fp)
        page = _render_finding_page(finding, commit)
        path = findings_dir / f"finding-{_slug(fp)}.md"
        _write_if_changed(path, page)
        written.append(str(path))
    finding_fps = sorted(set(finding_fps))

    coverage = {"total": len(entries), "covered": len(covered_ids & {str(e.get("id")) for e in entries})}

    index_page = _render_index_page(sources, skill_names, module_ids, command_names, finding_fps, coverage)
    index_path = out_root / "index.md"
    _write_if_changed(index_path, index_page)
    written.append(str(index_path))

    return {
        "based_on_product_commit": commit,
        "skill_pages": len(skill_names),
        "code_pages": len(module_ids),
        "command_pages": len(command_names),
        "finding_pages": len(finding_fps),
        "coverage": coverage,
        "written": written,
    }


def _write_if_changed(path: Path, content: str) -> None:
    """幂等写：内容相同则不动文件（保证可重跑、diff 友好）。"""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")


def _load_json_list(path: Optional[str]) -> List[Mapping[str, Any]]:
    if not path:
        return []
    import json

    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return list(data) if isinstance(data, list) else []


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import json

    try:
        import yaml
    except Exception:  # pragma: no cover
        yaml = None

    parser = argparse.ArgumentParser(
        description="从真相源（semantic-map + code-map + 台账 + §九排除线索）单向生成 Obsidian 知识库。"
    )
    parser.add_argument("--semantic-map", required=True)
    parser.add_argument("--code-map", required=True)
    parser.add_argument("--cases", help="台账 cases 的 JSON 文件（list[dict]，可选）")
    parser.add_argument("--excluded-leads", help="§九 排除线索 JSON（list[dict]，可选）")
    parser.add_argument("--output-dir", required=True, help="knowledge/ 输出根目录")
    args = parser.parse_args(argv)

    with Path(args.semantic_map).open("r", encoding="utf-8") as handle:
        semantic_map = yaml.safe_load(handle)
    with Path(args.code_map).open("r", encoding="utf-8") as handle:
        code_map = yaml.safe_load(handle)

    sources = TruthSources(
        semantic_map=semantic_map,
        code_map=code_map,
        cases=_load_json_list(args.cases),
        excluded_leads=_load_json_list(args.excluded_leads),
    )
    report = export_knowledge(sources, args.output_dir)
    report_print = {k: v for k, v in report.items() if k != "written"}
    report_print["written_count"] = len(report["written"])
    print(json.dumps(report_print, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
