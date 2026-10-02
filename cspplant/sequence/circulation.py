"""熔盐循环编排：启泵 → 流量确认 → 通盐 → 带负荷。

盐泵不能在流量确认之前提转速：流量没建立，低流量联锁就看不到干烧。这里按
"启泵 → 逐步升流 → 确认链放行 → 通盐 → 开进口阀"推进，任何一步没成立就停下并
把违约项报出来。

通盐前的流量确认不再由“连续够量 20 秒”判定，而是交给
:class:`~cspplant.confirm.chain.ConfirmChain`：起步阶段按爬升趋势、带负荷阶段按
固定裕度，打断按阶段策略判进退，每一步都进只追加哈希流水供控制台展示与审计复盘。
编排用递增的逻辑时间驱动确认链（与控制循环的挂钟驱动互斥，一轮确认只有一个驱动
源）；确认链没放行之前，绝不调用通盐。
"""

from __future__ import annotations

from ..confirm.chain import ConfirmChain
from ..errors import PlantError
from ..receiver.receiver import Receiver
from ..salt.loop import SaltLoop

MAX_STEPS = 1800


def circulate(
    salt: SaltLoop,
    receiver: Receiver,
    dt: float = 1.0,
    target_flow_kg_s: float | None = None,
    confirm: ConfirmChain | None = None,
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

    chain = confirm
    if chain is not None:
        # 一轮确认使用独立的逻辑时间轴：第 k 个步长记为 k*dt，间隔恒为 dt，
        # 与循环编排的逐拍推进一一对应。
        chain.start(0.0)

    clock = 0.0
    status = None
    for _ in range(MAX_STEPS):
        clock += dt
        salt.step(dt)
        flow_kg_s = salt.flow_kg_s()
        if chain is not None:
            chain.tick(clock, flow_kg_s, True, salt.pumps_running() > 0, True)
            if chain.status in ("aborted", "held"):
                break
            if chain.permitted():
                break
        else:
            status = receiver.verify_flow(flow_kg_s, dt)
            if status.confirmed:
                break

    if chain is not None:
        confirmed = chain.permitted()
        hold_s = 0.0
        confirm_view = chain.view()
    else:
        confirmed = receiver.flow_confirmed()
        hold_s = receiver.gate.hold_s()
        confirm_view = None
    steps.append(
        {
            "component": "receiver",
            "action": "confirm_flow",
            "detail": {
                "flow_kg_s": round(salt.flow_kg_s(), 3),
                "hold_s": round(hold_s, 3),
                "confirmed": receiver.flow_confirmed() if chain is None else confirmed,
                "confirm_status": chain.status if chain is not None else "legacy",
                "confirm_gate": chain.current_gate if chain is not None else "",
            },
        }
    )
    if chain is not None and chain.status == "held":
        violations.append("flow_confirm_held")
        return {"ok": False, "steps": steps, "violations": violations, "flow_kg_s": salt.flow_kg_s(),
                "confirm": confirm_view}
    if chain is not None and chain.status == "aborted":
        violations.append("flow_confirm_aborted")
        return {"ok": False, "steps": steps, "violations": violations, "flow_kg_s": salt.flow_kg_s(),
                "confirm": confirm_view}
    if not confirmed:
        violations.append("flow_not_confirmed")
        return {"ok": False, "steps": steps, "violations": violations, "flow_kg_s": salt.flow_kg_s(),
                "confirm": confirm_view}

    # 确认链放行后仍要过吸热器本地的低流量联锁门（设备级最后一道）。确认链已证明
    # 当前流量在联锁门限加固定裕度之上且已走平，这里把已确认的稳定流量喂满设备
    # 门自身的保持时长，让设备级门与确认链口径一致，而不是带半截累计去通盐。
    if chain is not None and not receiver.flow_confirmed():
        flow_kg_s = salt.flow_kg_s()
        for _ in range(int(receiver.limits.flow_confirm_s / dt) + 1):
            receiver.verify_flow(flow_kg_s, dt)
    try:
        receiver.admit()
    except PlantError as error:
        violations.append(error.message)
        return {"ok": False, "steps": steps, "violations": violations, "flow_kg_s": salt.flow_kg_s(),
                "confirm": confirm_view}
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
        "confirm": confirm_view,
    }
