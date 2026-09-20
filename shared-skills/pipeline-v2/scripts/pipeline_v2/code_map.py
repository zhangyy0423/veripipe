"""code_map.py — 产品中立的代码地图查询层。

读取由产品专属脚本（如 scripts/<product>/build-code-map.py）生成的 code-map.yaml，
提供按文件 / 符号 / 端点查询关联的 modules / endpoints / 调用链 / semantic entry 的能力。

铁律：本模块产品中立——不出现任何产品名、写死端点或产品路径；
所有产品具体信息均来自传入的 code-map 数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

try:  # pragma: no cover - PyYAML 总是可用，留兜底以防离线
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore


SURFACE_LINK_KINDS = {
    "playwright_spec",
    "ui_component",
    "source_path",
    "config_env",
    "testdata_fixture",
    "fixture",
    "doc",
}


@dataclass
class CodeMap:
    """代码地图的内存视图，提供反查能力。"""

    raw: Mapping[str, Any]

    @property
    def product(self) -> str:
        return str(self.raw.get("product", ""))

    @property
    def based_on_product_commit(self) -> str:
        return str(self.raw.get("based_on_product_commit", ""))

    @property
    def parser(self) -> str:
        return str(self.raw.get("parser", ""))

    @property
    def modules(self) -> List[Mapping[str, Any]]:
        mods = self.raw.get("modules", [])
        return list(mods) if isinstance(mods, list) else []

    @property
    def edges(self) -> List[Mapping[str, Any]]:
        edges = self.raw.get("edges", [])
        return list(edges) if isinstance(edges, list) else []

    @property
    def links(self) -> List[Mapping[str, Any]]:
        links = self.raw.get("links", [])
        return list(links) if isinstance(links, list) else []

    # ---------- 基础索引 ----------

    def module_for_file(self, file_path: str) -> Optional[Mapping[str, Any]]:
        norm = _norm(file_path)
        for module in self.modules:
            for f in module.get("files", []):
                if _norm(str(f)) == norm:
                    return module
        # 退而求其次：按目录前缀匹配 module id
        for module in self.modules:
            mod_id = _norm(str(module.get("id", "")))
            if mod_id and (norm == mod_id or norm.startswith(mod_id + "/")):
                return module
        return None

    def modules_for_files(self, file_paths: Iterable[str]) -> List[Mapping[str, Any]]:
        seen: Set[str] = set()
        result: List[Mapping[str, Any]] = []
        for path in file_paths:
            module = self.module_for_file(path)
            if module is None:
                continue
            mod_id = str(module.get("id", ""))
            if mod_id in seen:
                continue
            seen.add(mod_id)
            result.append(module)
        return result

    def exports_in_file(self, file_path: str) -> List[Mapping[str, Any]]:
        norm = _norm(file_path)
        out: List[Mapping[str, Any]] = []
        for module in self.modules:
            for exp in module.get("exports", []):
                if _norm(str(exp.get("file", ""))) == norm:
                    out.append(exp)
        return out

    def endpoints_in_file(self, file_path: str) -> List[Mapping[str, Any]]:
        norm = _norm(file_path)
        out: List[Mapping[str, Any]] = []
        for module in self.modules:
            for ep in module.get("endpoints", []):
                if _norm(str(ep.get("file", ""))) == norm:
                    out.append(ep)
        return out

    def endpoints_in_files(self, file_paths: Iterable[str]) -> List[Mapping[str, Any]]:
        norms = {_norm(p) for p in file_paths}
        out: List[Mapping[str, Any]] = []
        for module in self.modules:
            for ep in module.get("endpoints", []):
                if _norm(str(ep.get("file", ""))) in norms:
                    out.append(ep)
        return out

    # ---------- 调用链 ----------

    def callers_of(self, symbol: str) -> List[Mapping[str, Any]]:
        return [e for e in self.edges if str(e.get("callee", "")) == symbol]

    def callees_of(self, symbol: str) -> List[Mapping[str, Any]]:
        return [e for e in self.edges if str(e.get("caller", "")) == symbol]

    def transitive_callers(self, symbol: str, max_depth: int = 3) -> Set[str]:
        """反向调用链：哪些符号（间接）调用了 symbol。用于变更影响波及面。"""
        result: Set[str] = set()
        frontier = {symbol}
        depth = 0
        while frontier and depth < max_depth:
            next_frontier: Set[str] = set()
            for target in frontier:
                for edge in self.callers_of(target):
                    caller = str(edge.get("caller", ""))
                    if caller and caller not in result and caller != symbol:
                        result.add(caller)
                        next_frontier.add(caller)
            frontier = next_frontier
            depth += 1
        return result

    # ---------- links（symbol/endpoint ↔ semantic entry）----------

    def entries_for_symbol(self, symbol: str) -> List[str]:
        out: List[str] = []
        for link in self.links:
            if str(link.get("from", "")) == symbol:
                entry_id = str(link.get("semantic_map_entry_id", ""))
                if entry_id:
                    out.append(entry_id)
        return sorted(set(out))

    def entries_for_file(self, file_path: str) -> List[str]:
        norm = _norm(file_path)
        out: List[str] = []
        for link in self.links:
            if _norm(str(link.get("from_file", ""))) == norm:
                entry_id = str(link.get("semantic_map_entry_id", ""))
                if entry_id:
                    out.append(entry_id)
        return sorted(set(out))

    def entries_for_files(self, file_paths: Iterable[str]) -> List[str]:
        norms = {_norm(p) for p in file_paths}
        out: List[str] = []
        for link in self.links:
            if _norm(str(link.get("from_file", ""))) in norms:
                entry_id = str(link.get("semantic_map_entry_id", ""))
                if entry_id:
                    out.append(entry_id)
        return sorted(set(out))


def _norm(path: str) -> str:
    return str(path).strip().lstrip("./").rstrip("/")


def _is_safe_relative_path(path: str) -> bool:
    raw = str(path).strip()
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw:
        return False
    p = Path(raw)
    return not p.is_absolute() and ".." not in p.parts


def _is_surface_link_kind(kind: str) -> bool:
    return str(kind).strip() in SURFACE_LINK_KINDS


def validate_code_map(
    data: Mapping[str, Any],
    *,
    semantic_entry_ids: Optional[Iterable[str]] = None,
) -> List[str]:
    """Validate the generic code-map shape without assuming any product.

    Product-specific builders can stay permissive while this shared validator
    protects downstream change-impact and knowledge-export consumers from
    dangling links, unsafe paths, and malformed modules.
    """

    errors: List[str] = []
    if not isinstance(data, Mapping):
        return ["code_map root must be a mapping"]

    modules = data.get("modules")
    if not isinstance(modules, list):
        errors.append("modules must be a list")
        modules = []
    edges = data.get("edges", [])
    if not isinstance(edges, list):
        errors.append("edges must be a list")
        edges = []
    links = data.get("links", [])
    if not isinstance(links, list):
        errors.append("links must be a list")
        links = []

    known_files: Set[str] = set()
    known_symbols: Set[str] = set()
    known_ws_commands: Set[str] = set()

    for index, module in enumerate(modules):
        if not isinstance(module, Mapping):
            errors.append(f"modules[{index}] must be a mapping")
            continue
        mod_id = str(module.get("id") or "").strip()
        if not mod_id:
            errors.append(f"modules[{index}].id is required")
        elif not _is_safe_relative_path(mod_id):
            errors.append(f"modules[{index}].id must be a safe relative path")

        files = module.get("files", [])
        if not isinstance(files, list):
            errors.append(f"modules[{index}].files must be a list")
            files = []
        for file_index, raw_path in enumerate(files):
            path = str(raw_path or "")
            if not _is_safe_relative_path(path):
                errors.append(f"modules[{index}].files[{file_index}] must be a safe relative path")
            else:
                known_files.add(_norm(path))

        exports = module.get("exports", [])
        if not isinstance(exports, list):
            errors.append(f"modules[{index}].exports must be a list")
            exports = []
        for export_index, export in enumerate(exports):
            if not isinstance(export, Mapping):
                errors.append(f"modules[{index}].exports[{export_index}] must be a mapping")
                continue
            symbol = str(export.get("symbol") or "").strip()
            if not symbol:
                errors.append(f"modules[{index}].exports[{export_index}].symbol is required")
            else:
                known_symbols.add(symbol)
            file_path = str(export.get("file") or "").strip()
            if file_path and not _is_safe_relative_path(file_path):
                errors.append(f"modules[{index}].exports[{export_index}].file must be a safe relative path")

        endpoints = module.get("endpoints", [])
        if not isinstance(endpoints, list):
            errors.append(f"modules[{index}].endpoints must be a list")
            endpoints = []
        for endpoint_index, endpoint in enumerate(endpoints):
            if not isinstance(endpoint, Mapping):
                errors.append(f"modules[{index}].endpoints[{endpoint_index}] must be a mapping")
                continue
            file_path = str(endpoint.get("file") or "").strip()
            if file_path and not _is_safe_relative_path(file_path):
                errors.append(f"modules[{index}].endpoints[{endpoint_index}].file must be a safe relative path")
            if endpoint.get("kind") == "ws_command":
                command = str(endpoint.get("command") or "").strip()
                if not command:
                    errors.append(f"modules[{index}].endpoints[{endpoint_index}].command is required")
                else:
                    known_ws_commands.add(command)

    for index, edge in enumerate(edges):
        if not isinstance(edge, Mapping):
            errors.append(f"edges[{index}] must be a mapping")
            continue
        if not str(edge.get("caller") or "").strip():
            errors.append(f"edges[{index}].caller is required")
        if not str(edge.get("callee") or "").strip():
            errors.append(f"edges[{index}].callee is required")
        file_path = str(edge.get("caller_file") or "").strip()
        if file_path and not _is_safe_relative_path(file_path):
            errors.append(f"edges[{index}].caller_file must be a safe relative path")

    allowed_entries = {str(eid) for eid in semantic_entry_ids} if semantic_entry_ids is not None else None
    for index, link in enumerate(links):
        if not isinstance(link, Mapping):
            errors.append(f"links[{index}] must be a mapping")
            continue
        from_kind = str(link.get("from_kind") or "").strip()
        source = str(link.get("from") or "").strip()
        entry_id = str(link.get("semantic_map_entry_id") or "").strip()
        if not from_kind:
            errors.append(f"links[{index}].from_kind is required")
        if not source:
            errors.append(f"links[{index}].from is required")
        if not entry_id:
            errors.append(f"links[{index}].semantic_map_entry_id is required")
        elif allowed_entries is not None and entry_id not in allowed_entries:
            errors.append(f"links[{index}].semantic_map_entry_id not found in semantic-map: {entry_id}")
        from_file = str(link.get("from_file") or "").strip()
        if from_file:
            if not _is_safe_relative_path(from_file):
                errors.append(f"links[{index}].from_file must be a safe relative path")
            elif known_files and _norm(from_file) not in known_files and not _is_surface_link_kind(from_kind):
                errors.append(f"links[{index}].from_file not found in module files: {from_file}")
        if from_kind == "ws_command" and source and source not in known_ws_commands:
            errors.append(f"links[{index}].from does not match a known ws_command: {source}")
        if from_kind in {"symbol", "export"} and source and known_symbols and source not in known_symbols:
            errors.append(f"links[{index}].from does not match a known symbol: {source}")

    return errors


def load_code_map(path: str | Path) -> CodeMap:
    if yaml is None:  # pragma: no cover
        raise RuntimeError("PyYAML required to load code map")
    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"code map root must be a mapping: {path}")
    return CodeMap(raw=data)


def code_map_from_dict(data: Mapping[str, Any]) -> CodeMap:
    return CodeMap(raw=dict(data))


def _load_semantic_entry_ids(path: Optional[str | Path]) -> Optional[Set[str]]:
    if path is None:
        return None
    if yaml is None:  # pragma: no cover
        raise RuntimeError("PyYAML required to load semantic map")
    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, Mapping):
        raise ValueError(f"semantic map root must be a mapping: {path}")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError(f"semantic map entries must be a list: {path}")
    return {str(entry.get("id") or "") for entry in entries if isinstance(entry, Mapping) and entry.get("id")}


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Inspect and validate a product-neutral code-map.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate code-map structure and optional semantic links")
    validate.add_argument("--code-map", required=True)
    validate.add_argument("--semantic-map")

    args = parser.parse_args(argv)
    if args.command == "validate":
        code_map = load_code_map(args.code_map)
        entry_ids = _load_semantic_entry_ids(args.semantic_map) if args.semantic_map else None
        errors = validate_code_map(code_map.raw, semantic_entry_ids=entry_ids)
        print(
            json.dumps(
                {
                    "status": "ok" if not errors else "fail",
                    "error_count": len(errors),
                    "errors": errors,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if not errors else 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
