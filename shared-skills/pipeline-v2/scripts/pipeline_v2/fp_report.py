# -*- coding: utf-8 -*-
"""Summarize the anti-false-positive funnel from orchestrator results.

Turns one or more ``OrchestratorResult`` dicts (the JSON printed by
``pipeline_v2.orchestrator``) into a small, auditable summary: how many candidate
checks ran, how many were filed as machine-verified findings, and how many were
withheld and why. Standard library only; product-neutral.

    python3 -m pipeline_v2.fp_report --result run1.json --result run2.json
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence


@dataclass
class FunnelSummary:
    batches: int
    executed: int
    filed: int
    withheld: int
    withheld_breakdown: Dict[str, int] = field(default_factory=dict)
    filed_rate: float = 0.0
    withheld_rate: float = 0.0


def _int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def summarize(results: Sequence[Mapping[str, object]]) -> FunnelSummary:
    executed = filed = passed = no_signal = observed = 0
    for result in results:
        executed += _int(result, "executed_count")
        filed += _int(result, "reported_count")
        passed += _int(result, "pass_count")
        no_signal += _int(result, "no_signal_count")
        observed += _int(result, "observed_count")
    withheld = max(executed - filed, 0)
    filed_rate = (filed / executed) if executed else 0.0
    return FunnelSummary(
        batches=len(results),
        executed=executed,
        filed=filed,
        withheld=withheld,
        withheld_breakdown={
            "passed": passed,
            "inconclusive_or_no_signal": no_signal,
            "observation_only": observed,
        },
        filed_rate=round(filed_rate, 4),
        withheld_rate=round(1 - filed_rate, 4) if executed else 0.0,
    )


def render(summary: FunnelSummary) -> str:
    b = summary.withheld_breakdown
    return "\n".join([
        "# Anti-false-positive funnel",
        f"- candidate checks executed: {summary.executed} (across {summary.batches} batch(es))",
        f"- filed as machine-verified findings: {summary.filed}",
        f"- withheld (not filed): {summary.withheld} ({summary.withheld_rate * 100:.0f}%)",
        f"    passed, no defect: {b.get('passed', 0)}",
        f"    inconclusive / no-signal: {b.get('inconclusive_or_no_signal', 0)}",
        f"    observation-only: {b.get('observation_only', 0)}",
        f"- filed rate: {summary.filed_rate * 100:.0f}%",
    ])


def trend(results: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    """One row per result, in order, for a filed/withheld series over batches."""
    rows: List[Dict[str, object]] = []
    for index, result in enumerate(results):
        s = summarize([result])
        label = str(result.get("batch_id") or f"batch-{index + 1}")
        rows.append({"batch": label, "executed": s.executed, "filed": s.filed,
                     "withheld": s.withheld, "filed_rate": s.filed_rate})
    return rows


def render_trend(rows: Sequence[Mapping[str, object]]) -> str:
    lines = ["# Funnel trend",
             "| batch | executed | filed | withheld | filed rate |",
             "|---|---:|---:|---:|---:|"]
    total_e = total_f = 0
    for r in rows:
        e = int(r["executed"]); f = int(r["filed"]); w = int(r["withheld"])
        total_e += e; total_f += f
        lines.append(f"| {r['batch']} | {e} | {f} | {w} | {float(r['filed_rate']) * 100:.0f}% |")
    agg_rate = (total_f / total_e * 100) if total_e else 0.0
    lines.append(f"| **all** | {total_e} | {total_f} | {total_e - total_f} | {agg_rate:.0f}% |")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize the anti-false-positive funnel from orchestrator result JSON(s).")
    parser.add_argument("--result", type=Path, action="append", required=True,
                        help="an orchestrator result JSON file (repeatable)")
    parser.add_argument("--trend", action="store_true",
                        help="show a per-batch series instead of a single aggregate")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)
    results: List[Mapping[str, object]] = []
    for path in args.result:
        results.append(json.loads(Path(path).read_text(encoding="utf-8")))
    if args.trend:
        rows = trend(results)
        print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) if args.json
              else render_trend(rows))
        return 0
    summary = summarize(results)
    if args.json:
        print(json.dumps(asdict(summary), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
