"""打断处置策略表（按阶段定死）。

确认链被打断时“退回去重来还是停下来等人”不允许临场发挥：起步阶段（G1）和
带负荷阶段（G3）对同一个现象的处置不同，因为带负荷阶段任何一次自动重试都可能
让已经进吸热器的盐失去保护。策略表是冻结的常量映射，随运行开始事件一起落
审计；表里没有列出的组合一律按最保守的 :data:`~cspplant.confirm.HOLD` 处理。
"""

from __future__ import annotations

from . import (
    ABORT,
    BACKTRACK,
    G1_RAMP,
    G3_MARGIN,
    HOLD,
    INTERLOCK_LOST,
    MARGIN_LOST,
    OPERATOR_ABORT,
    PUMP_TRIP,
    RAMP_STALLED,
    REGIME_BACK,
    RETRY,
    RETRY_EXHAUSTED,
    SAMPLE_GAP,
    SENSOR_DROPOUT,
    SENSOR_FAULT,
)

# (门, 打断原因) -> 处置动作。
POLICY: dict[tuple[str, str], str] = {
    # 起步爬升阶段：现场扰动频繁，掉点/缺拍/爬升停滞允许作废本窗、原地重试；
    # 连续掉点说明传感器坏了，重试无意义，停下来等人。
    (G1_RAMP, SENSOR_DROPOUT): RETRY,
    (G1_RAMP, SAMPLE_GAP): RETRY,
    (G1_RAMP, RAMP_STALLED): RETRY,
    (G1_RAMP, SENSOR_FAULT): HOLD,
    (G1_RAMP, RETRY_EXHAUSTED): HOLD,
    (G1_RAMP, REGIME_BACK): RETRY,
    # 带负荷阶段：保持窗口作废，但绝不自动重判，退回待命门等现场重新稳住；
    # 传感器层面的问题直接停下等人。
    (G3_MARGIN, MARGIN_LOST): BACKTRACK,
    (G3_MARGIN, REGIME_BACK): BACKTRACK,
    (G3_MARGIN, SENSOR_DROPOUT): BACKTRACK,
    (G3_MARGIN, SAMPLE_GAP): BACKTRACK,
    (G3_MARGIN, SENSOR_FAULT): HOLD,
    (G3_MARGIN, RETRY_EXHAUSTED): HOLD,
}

# 任何门遇到这些原因都立即终止本轮，不可自行恢复。
FATAL_ALWAYS: dict[str, str] = {
    PUMP_TRIP: ABORT,
    INTERLOCK_LOST: ABORT,
    OPERATOR_ABORT: ABORT,
}

# 处置是否允许人工恢复（决定 HELD 后 resume 的语义）。
RESUMABLE: dict[str, bool] = {
    HOLD: True,
    ABORT: False,
    RETRY: True,
    BACKTRACK: True,
}


def action_for(gate: str, reason: str) -> str:
    """按门与打断原因查处置动作；硬失败优先，未列出的组合保守停下。"""

    if reason in FATAL_ALWAYS:
        return FATAL_ALWAYS[reason]
    return POLICY.get((gate, reason), HOLD)


def describe_policy() -> list[dict]:
    """把策略表展开成可落审计/可展示的清单（顺序固定，便于对账）。"""

    rows = [
        {"gate": gate, "reason": reason, "action": action_for(gate, reason)}
        for gate, reason in sorted(set(POLICY) | set((g1, reason) for g1 in (G1_RAMP, G3_MARGIN) for reason in FATAL_ALWAYS))
    ]
    return sorted(rows, key=lambda item: (item["gate"], item["reason"]))
