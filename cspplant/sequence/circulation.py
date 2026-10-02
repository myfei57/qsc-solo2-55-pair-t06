"""熔盐循环编排：启泵 → 流量确认 → 通盐 → 带负荷。

盐泵不能在流量确认之前提转速：流量没建立，低流量联锁就看不到干烧。确认链
按阶段推进：起步阶段按爬升趋势判，带负荷阶段按固定裕度判，阶段时序不能
跳。起步阶段被打断会自己退回重来，编排继续跟进；带负荷阶段被打断（含
传感器掉点）会停下来等人，编排随即停在原地报 ``flow_confirm_halted``，
不自动重试也绝不带着半截确认通盐。
"""

from __future__ import annotations

from ..errors import PlantError
from ..receiver.flow import CONFIRMED
from ..receiver.receiver import Receiver
from ..salt.loop import SaltLoop

MAX_STEPS = 1800


def circulate(
    salt: SaltLoop,
    receiver: Receiver,
    dt: float = 1.0,
    target_flow_kg_s: float | None = None,
    *,
    sensor_valid: bool = True,
) -> dict:
    """建立熔盐循环并把熔盐引进吸热器。"""

    steps: list[dict] = []
    violations: list[str] = []
    target = salt.limits.flow_baseline_kg_s if target_flow_kg_s is None else float(target_flow_kg_s)

    try:
        salt.start_cold()
    except PlantError as error:
        violations.append(error.message)
        return {"ok": False, "steps": steps, "violations": violations, "flow_kg_s": salt.flow_kg_s()}
    steps.append(
        {
            "component": "salt",
            "action": "start_cold_pump",
            "detail": {"target_flow_kg_s": round(target, 3)},
        }
    )

    status = None
    halted = False
    for _ in range(MAX_STEPS):
        salt.step(dt)
        status = receiver.verify_flow(salt.flow_kg_s(), dt, valid=sensor_valid)
        if status.confirmed:
            break
        if receiver.flow_chain_halted():
            halted = True
            break
    steps.append(
        {
            "component": "receiver",
            "action": "confirm_flow",
            "detail": {
                "flow_kg_s": round(salt.flow_kg_s(), 3),
                "phase": receiver.gate.phase(),
                "window_s": round(receiver.gate.window_s(), 3),
                "net_gain_kg_s": round(receiver.gate.net_gain_kg_s(), 3),
                "hold_s": round(receiver.gate.hold_s(), 3),
                "resets": receiver.gate.resets(),
                "halts": receiver.gate.halts(),
                "drops": receiver.gate.drops(),
                "confirmed": receiver.flow_confirmed(),
            },
        }
    )
    if halted:
        violations.append("flow_confirm_halted")
        return {"ok": False, "steps": steps, "violations": violations, "flow_kg_s": salt.flow_kg_s()}
    if not receiver.flow_confirmed() or status is None or status.phase != CONFIRMED:
        violations.append("flow_not_confirmed")
        return {"ok": False, "steps": steps, "violations": violations, "flow_kg_s": salt.flow_kg_s()}

    try:
        receiver.admit()
    except PlantError as error:
        violations.append(error.message)
        return {"ok": False, "steps": steps, "violations": violations, "flow_kg_s": salt.flow_kg_s()}
    steps.append(
        {
            "component": "receiver",
            "action": "admit_salt",
            "detail": {"header": receiver.header.state(), "valve_target_pct": receiver.valve.target_pct()},
        }
    )

    for _ in range(MAX_STEPS):
        receiver.step_valve(dt)
        if receiver.valve.at_target():
            break
    steps.append(
        {
            "component": "receiver",
            "action": "open_inlet_valve",
            "detail": {"position_pct": round(receiver.valve.position_pct(), 2), "at_target": receiver.valve.at_target()},
        }
    )
    if not receiver.valve.at_target():
        violations.append("valve_not_settled")

    return {
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "flow_kg_s": round(salt.flow_kg_s(), 3),
    }
