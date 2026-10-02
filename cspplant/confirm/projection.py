"""确认链只读投影：从事件流水还原控制台视图。

控制台展示、审计对账、复盘比对都走这一个纯函数：给它一串确认链事件，它还原出
链状态、每道门的状态与尝试次数、最近一拍证据、工况、打断/保持记录和通盐许可。
现场链不另存一份“给控制台看”的状态，保证两边口径天然一致；把落盘事件重放一遍
得到的视图与现场视图逐字段相等，即“确认链与审计一致”。
"""

from __future__ import annotations

from ..confirm import (
    ABORTED,
    ACTIVE,
    BLOCKED,
    CONFIRMED,
    EVT_CHAIN_ABORTED,
    EVT_CHAIN_HELD,
    EVT_CHAIN_RESUMED,
    EVT_FLOW_PERMITTED,
    EVT_GATE_BACKTRACK,
    EVT_GATE_ENTERED,
    EVT_GATE_PASSED,
    EVT_GATE_RETRY,
    EVT_OPERATOR_PAUSE,
    EVT_RUN_STARTED,
    EVT_TICK_EVALUATED,
    GATE_ORDER,
    G4_PERMIT,
    HELD,
    IDLE,
    PASSED,
    RUNNING,
    WAITING,
)


def _blank_gates() -> dict[str, dict]:
    return {
        gate: {"state": WAITING, "attempt": 0, "criterion": "", "last_evidence": None}
        for gate in GATE_ORDER
    }


def build_view(events: list[dict]) -> dict:
    """从事件流水构造确认链视图。事件之外不读取任何运行态。"""

    gates = _blank_gates()
    view: dict = {
        "status": IDLE,
        "current_gate": None,
        "gates": gates,
        "regime": None,
        "last_tick": None,
        "criteria": {},
        "config_hash": None,
        "interrupts": [],
        "holds": [],
        "policy": [],
        "started_ts": None,
        "permitted": False,
        "permitted_ts": None,
        "aborted_reason": None,
        "ticks": 0,
        "last_ts": None,
        "event_count": len(events),
    }

    def _open_interrupt(ts: float, gate: str, reason: str, action: str) -> None:
        view["interrupts"].append(
            {"ts": ts, "gate": gate, "reason": reason, "action": action, "resolved": None}
        )

    for event in events:
        action = event["action"]
        ts = event["ts"]
        data = event.get("data", {})
        view["last_ts"] = ts

        if action == EVT_RUN_STARTED:
            view["status"] = RUNNING
            view["started_ts"] = ts
            view["config_hash"] = data.get("config_hash")
            view["criteria"] = dict(data.get("criteria", {}))
            view["policy"] = list(data.get("policy", []))
            for gate, criterion in view["criteria"].items():
                if gate in gates:
                    gates[gate]["criterion"] = criterion
            continue

        if action == EVT_GATE_ENTERED:
            gate = data["gate"]
            gates[gate]["state"] = ACTIVE
            gates[gate]["attempt"] = int(data.get("attempt", gates[gate]["attempt"]))
            gates[gate]["last_evidence"] = None
            view["current_gate"] = gate
            view["status"] = RUNNING
            continue

        if action == EVT_TICK_EVALUATED:
            view["ticks"] += 1
            view["regime"] = data.get("regime", view["regime"])
            gate = data.get("gate")
            tick_view = {key: data.get(key) for key in (
                "ts", "tick_seq", "gate", "flow_kg_s", "valid", "pump_running",
                "interlock_ok", "dt", "regime", "regime_dwell_s", "verdict", "reason", "evidence",
            )}
            view["last_tick"] = tick_view
            if gate in gates:
                gates[gate]["last_evidence"] = data.get("evidence")
            continue

        if action == EVT_GATE_PASSED:
            gate = data["gate"]
            gates[gate]["state"] = PASSED
            gates[gate]["last_evidence"] = data.get("evidence", gates[gate]["last_evidence"])
            view["current_gate"] = gate
            continue

        if action == EVT_GATE_RETRY:
            gate = data["gate"]
            gates[gate]["attempt"] = int(data.get("next_attempt", gates[gate]["attempt"]))
            _open_interrupt(ts, gate, data["reason"], "retry")
            continue

        if action == EVT_GATE_BACKTRACK:
            gate = data["gate"]
            target = data["to_gate"]
            gates[gate]["state"] = WAITING
            gates[gate]["last_evidence"] = None
            if target in gates:
                gates[target]["attempt"] = int(data.get("target_attempt", gates[target]["attempt"]))
            _open_interrupt(ts, gate, data["reason"], "backtrack")
            continue

        if action == EVT_CHAIN_HELD:
            gate = data["gate"]
            gates[gate]["state"] = BLOCKED
            view["status"] = HELD
            view["holds"].append(
                {"ts": ts, "gate": gate, "reason": data["reason"], "resumed_ts": None}
            )
            _open_interrupt(ts, gate, data["reason"], data.get("action", "hold"))
            continue

        if action == EVT_OPERATOR_PAUSE:
            # 人工暂停与现场部分失败同为“保持等人”：门不前进、不作废，状态置 HELD。
            gate = data["gate"]
            gates[gate]["state"] = BLOCKED
            view["status"] = HELD
            view["holds"].append(
                {"ts": ts, "gate": gate, "reason": data.get("reason", "operator_pause"),
                 "resumed_ts": None, "operator": data.get("operator")}
            )
            _open_interrupt(ts, gate, data.get("reason", "operator_pause"), "operator_pause")
            continue

        if action == EVT_CHAIN_RESUMED:
            if view["holds"] and view["holds"][-1]["resumed_ts"] is None:
                view["holds"][-1]["resumed_ts"] = ts
            if view["interrupts"] and view["interrupts"][-1]["resolved"] is None:
                view["interrupts"][-1]["resolved"] = "operator_resume"
            view["status"] = RUNNING
            continue

        if action == EVT_CHAIN_ABORTED:
            view["status"] = ABORTED
            view["aborted_reason"] = data.get("reason")
            if view["interrupts"] and view["interrupts"][-1]["resolved"] is None:
                view["interrupts"][-1]["resolved"] = "abort"
            continue

        if action == EVT_FLOW_PERMITTED:
            view["permitted"] = True
            view["permitted_ts"] = ts
            view["status"] = CONFIRMED
            view["current_gate"] = G4_PERMIT
            continue

    return view
