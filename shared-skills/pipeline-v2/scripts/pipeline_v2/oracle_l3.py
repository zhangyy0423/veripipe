"""L3 参照系 oracle：差分（differential）与蜕变（metamorphic）。

设计依据：discuss/02 §1（L3 把"判断对错"转成"判断一致性/关系成立"）、
discuss/06 §2（L3 是 agent 深挖主引擎）。

分工铁律（权限非对称）：
- LLM 只负责"提出"关系/路径（生成性，可放开）；
- 机器只负责"验证"关系是否成立（确定性）；
- 关系破坏 → 产出 oracle_level=L3 的 FailureSignal，走弱通道复现验证。

本模块是纯函数 + 数据类，完全离线可单测，不依赖任何执行器或网络。
产品中立：不出现任何产品名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .schema import FailureSignal


# 蜕变关系类型（machine-verifiable relations）。
RELATION_EQUAL = "equal"            # 两终态应相等（加无关步骤 / 乱序后不变）
RELATION_RESTORED = "restored"      # 变换后应回到基线（hide -> unhide 恢复）
RELATION_SUBSET = "subset"          # 过滤后结果应是原结果的子集


@dataclass(frozen=True)
class L3Result:
    """L3 判定结果（三态）。failure_signal 仅在关系破坏时填充。"""

    status: str                                  # "pass" | "fail" | "inconclusive"
    strategy: str                                # "differential" | "metamorphic"
    reason: str
    detail: Dict[str, Any] = field(default_factory=dict)
    failure_signal: Optional[FailureSignal] = None


def _terminal(state: Mapping[str, Any]) -> Any:
    """取一次运行的可比较终态。

    约定优先取 http_driver 产出的 observed_state["terminal_state"]；
    没有则回退到整个 observed_state，保证对其它执行器也可用。"""

    if not isinstance(state, Mapping):
        return state
    if "terminal_state" in state:
        return state["terminal_state"]
    return {k: v for k, v in state.items() if k not in ("event_sequence",)}


def evaluate_differential(
    states: Sequence[Mapping[str, Any]],
    *,
    scenario_id: str,
    step_id: str,
    llm_involvement: str,
    semantic_map_entry_id: Optional[str] = None,
    path_labels: Optional[Sequence[str]] = None,
) -> L3Result:
    """差分：同一目标走多条路径，终态应一致；不一致 → L3 信号。"""

    terminals = [_terminal(s) for s in states]
    if len(terminals) < 2:
        return L3Result("inconclusive", "differential", "need_at_least_two_runs")

    baseline = terminals[0]
    diverged_index = next((i for i, t in enumerate(terminals) if t != baseline), None)
    detail = {
        "path_labels": list(path_labels or []),
        "terminals": terminals,
    }
    if diverged_index is None:
        return L3Result("pass", "differential", "all_paths_consistent", detail)

    signal = FailureSignal(
        scenario_id=scenario_id,
        step_id=step_id,
        failure_type="differential_inconsistency",
        oracle_level="L3",
        llm_involvement=llm_involvement,
        expected_state={"relation": "all_paths_equal", "baseline": baseline},
        actual_state={"diverged_index": diverged_index, "actual": terminals[diverged_index]},
        semantic_map_entry_id=semantic_map_entry_id,
        evidence={"oracle_l3": {"strategy": "differential", **detail}},
    )
    return L3Result("fail", "differential", "paths_inconsistent", detail, signal)


def _relation_holds(relation: str, base: Any, transformed: Any) -> Optional[bool]:
    """机器验证一条蜕变关系。返回 None 表示无法判定（inconclusive）。"""

    if relation in (RELATION_EQUAL, RELATION_RESTORED):
        return base == transformed
    if relation == RELATION_SUBSET:
        if isinstance(base, Mapping) and isinstance(transformed, Mapping):
            return set(transformed.items()) <= set(base.items())
        if isinstance(base, (list, tuple)) and isinstance(transformed, (list, tuple)):
            return set(map(repr, transformed)) <= set(map(repr, base))
        return None
    return None


def evaluate_metamorphic(
    *,
    relation: str,
    base_state: Mapping[str, Any],
    transformed_state: Mapping[str, Any],
    scenario_id: str,
    step_id: str,
    llm_involvement: str,
    relation_id: str = "",
    semantic_map_entry_id: Optional[str] = None,
) -> L3Result:
    """蜕变：对 base/transformed 两终态验证一条关系；破坏 → L3 信号。"""

    base = _terminal(base_state)
    transformed = _terminal(transformed_state)
    holds = _relation_holds(relation, base, transformed)
    detail = {
        "relation": relation,
        "relation_id": relation_id,
        "base": base,
        "transformed": transformed,
    }
    if holds is None:
        return L3Result("inconclusive", "metamorphic", f"unverifiable_relation:{relation}", detail)
    if holds:
        return L3Result("pass", "metamorphic", "relation_holds", detail)

    signal = FailureSignal(
        scenario_id=scenario_id,
        step_id=step_id,
        failure_type=f"metamorphic_break:{relation}",
        oracle_level="L3",
        llm_involvement=llm_involvement,
        expected_state={"relation": relation, "relation_id": relation_id, "base": base},
        actual_state={"transformed": transformed},
        semantic_map_entry_id=semantic_map_entry_id,
        evidence={"oracle_l3": {"strategy": "metamorphic", **detail}},
    )
    return L3Result("fail", "metamorphic", "relation_broken", detail, signal)


def evaluate_sibling_consistency(
    observations: Sequence[Mapping[str, Any]],
    *,
    scenario_id: str,
    step_id: str,
    llm_involvement: str,
    group_id: str = "",
    semantic_map_entry_id: Optional[str] = None,
) -> L3Result:
    """兄弟一致性：一组同类命令喂同样的非法输入，校验响应应一致（都拒绝 / 都接受）。

    参照系 = 组内多数行为（兄弟互为参照，绕开"标准答案"）。偏离多数者 = 校验缺口。
    只有当"该组校验应一致"是产品方在 adapter 里声明的隐式契约时才调用本判定
    （声明即背书 → L2 性质的显式规约），故 set_model 这类**设计上故意宽松**的命令
    不会被放进同一组，避免误报。

    每个 observation 形如：
      {"sibling": "set_mode", "rejected": true}   # rejected=是否拒绝了非法输入
    多数 rejected==True 而个别 False（或反之）→ 偏离者立案。
    """

    sibs = [o for o in observations if isinstance(o, Mapping)]
    if len(sibs) < 3:
        return L3Result("inconclusive", "sibling_consistency", "need_at_least_three_siblings",
                        {"group_id": group_id})

    def _rej(o: Mapping[str, Any]) -> Optional[bool]:
        v = o.get("rejected")
        return v if isinstance(v, bool) else None

    if any(_rej(o) is None for o in sibs):
        return L3Result("inconclusive", "sibling_consistency", "missing_rejected_field",
                        {"group_id": group_id, "observations": list(sibs)})

    rejected_count = sum(1 for o in sibs if _rej(o) is True)
    total = len(sibs)
    # 多数派 = 期望行为；少数派 = 偏离（校验不对称）。
    majority_rejects = rejected_count * 2 > total
    deviants = [
        str(o.get("sibling") or "?")
        for o in sibs
        if _rej(o) is not (True if majority_rejects else False)
    ]
    detail = {
        "group_id": group_id,
        "majority": "reject" if majority_rejects else "accept",
        "rejected_count": rejected_count,
        "total": total,
        "deviants": deviants,
        "observations": list(sibs),
    }
    if not deviants:
        return L3Result("pass", "sibling_consistency", "all_siblings_consistent", detail)
    if rejected_count == 0 or rejected_count == total:
        # 全体一致（虽有 deviants 计算误差）—— 理论上不会到这，兜底为 pass。
        return L3Result("pass", "sibling_consistency", "all_siblings_consistent", detail)

    signal = FailureSignal(
        scenario_id=scenario_id,
        step_id=step_id,
        failure_type="sibling_validation_asymmetry",
        oracle_level="L3",
        llm_involvement=llm_involvement,
        expected_state={"group_id": group_id, "expected": detail["majority"], "siblings": total},
        actual_state={"deviants": deviants, "rejected_count": rejected_count},
        semantic_map_entry_id=semantic_map_entry_id,
        evidence={"oracle_l3": {"strategy": "sibling_consistency", **detail}},
    )
    return L3Result("fail", "sibling_consistency", "validation_asymmetry", detail, signal)
