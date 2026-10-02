"""确认链状态机。

五道门严格按编号向前：前置条件 → 起步爬升趋势 → 工况锁定待命 → 带负荷固定裕度 →
通盐许可。判据模式由门静态绑定（见 :mod:`cspplant.confirm` 的 ``GATE_CRITERIA``），
一次运行内定死；门只能顺序进入，任何跳步请求都记告警并拒绝。被打断时按
:mod:`~cspplant.confirm.policy` 冻结的策略表判进退，传感器掉点是部分失败，绝不
算通过。状态机每一拍把原始输入与判据指标都写进 :class:`~cspplant.confirm.ledger.ConfirmLedger`，
控制台视图由 :mod:`~cspplant.confirm.projection` 从流水还原，现场不留第二份口径。
"""

from __future__ import annotations

import dataclasses
import hashlib

from ..errors import InterlockError
from . import (
    ABORT,
    ABORTED,
    ACTIVE,
    ALARM,
    BACKTRACK,
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
    EVT_REGIME_LOCKED,
    EVT_RUN_STARTED,
    EVT_SKIP_DENIED,
    EVT_TICK_EVALUATED,
    FIXED_MARGIN,
    G0_PRECHECK,
    G1_RAMP,
    G2_ARM,
    G3_MARGIN,
    G4_PERMIT,
    GATE_CRITERIA,
    GATE_ORDER,
    HELD,
    IDLE,
    INTERLOCK_LOST,
    LOADED,
    NOTICE,
    OPERATOR_ABORT,
    PUMP_TRIP,
    RAMP_TREND,
    REGIME_BACK,
    RETRY,
    RETRY_EXHAUSTED,
    RAMP_STALLED,
    RUNNING,
    SAMPLE_GAP,
    SENSOR_DROPOUT,
    SENSOR_FAULT,
    WARNING,
)
from .criteria import Sample, evaluate_fixed_margin, evaluate_ramp
from .ledger import ConfirmLedger, canonical
from .policy import action_for, describe_policy
from .projection import build_view
from .regime import RegimeDetector

_SEVERITY = {
    EVT_RUN_STARTED: NOTICE,
    EVT_GATE_ENTERED: NOTICE,
    EVT_TICK_EVALUATED: NOTICE,
    EVT_REGIME_LOCKED: NOTICE,
    EVT_GATE_PASSED: NOTICE,
    EVT_FLOW_PERMITTED: NOTICE,
    EVT_CHAIN_RESUMED: NOTICE,
    EVT_GATE_RETRY: WARNING,
    EVT_GATE_BACKTRACK: WARNING,
    EVT_OPERATOR_PAUSE: WARNING,
    EVT_CHAIN_HELD: WARNING,
    EVT_CHAIN_ABORTED: ALARM,
    EVT_SKIP_DENIED: ALARM,
}


def limits_hash(limits) -> str:
    """对确认链配置取摘要，随运行开始落审计，运行期改配置立刻可辨。"""

    return hashlib.sha256(canonical(dataclasses.asdict(limits)).encode("utf-8")).hexdigest()[:16]


class ConfirmChain:
    """通盐前流量确认链。"""

    def __init__(self, limits, sink=None) -> None:
        self._limits = limits
        self._sink = sink
        self._ledger = ConfirmLedger()
        self._detector = RegimeDetector(limits)
        self._status = IDLE
        self._current: str | None = None
        self._attempts: dict[str, int] = {gate: 0 for gate in GATE_ORDER}
        self._gate_state: dict[str, str] = {gate: "waiting" for gate in GATE_ORDER}
        self._ramp: list[Sample] = []
        self._hold: list[Sample] = []
        self._invalid_streak = 0
        self._last_ts: float | None = None
        self._nominal: float | None = None
        self._tick_seq = 0
        self._config_hash = limits_hash(limits)

    # ---- 只读出口 -----------------------------------------------------

    @property
    def status(self) -> str:
        return self._status

    @property
    def current_gate(self) -> str | None:
        return self._current

    def permitted(self) -> bool:
        return self._status == CONFIRMED

    def events(self) -> list[dict]:
        return self._ledger.events()

    def verify(self) -> dict:
        return self._ledger.verify()

    def view(self) -> dict:
        """从事件流水还原控制台视图（控制台与审计共用此口径）。"""

        return build_view(self._ledger.events())

    # ---- 生命周期 -----------------------------------------------------

    def start(self, ts: float) -> dict:
        """开始一轮确认。只有空闲或已终态（作废/已确认）可以开新一轮。"""

        if self._status == RUNNING or self._status == HELD:
            raise InterlockError("确认链正在执行，不能重复开始", component="confirm")
        self._ledger = ConfirmLedger()
        self._detector = RegimeDetector(self._limits)
        self._status = RUNNING
        self._current = G0_PRECHECK
        self._attempts = {gate: 0 for gate in GATE_ORDER}
        self._gate_state = {gate: "waiting" for gate in GATE_ORDER}
        self._gate_state[G0_PRECHECK] = ACTIVE
        self._ramp = []
        self._hold = []
        self._invalid_streak = 0
        self._last_ts = None
        self._nominal = None
        self._tick_seq = 0
        self._emit(
            EVT_RUN_STARTED,
            ts,
            {
                "config_hash": self._config_hash,
                "criteria": dict(GATE_CRITERIA),
                "policy": describe_policy(),
                "limits": dataclasses.asdict(self._limits),
            },
        )
        self._enter_gate(G0_PRECHECK, ts)
        return self.view()

    def pause(self, ts: float, operator: str = "console", reason: str = "operator_pause") -> dict:
        """人工暂停：部分失败语义，链保持在当前门等人，不前进也不作废。"""

        if self._status != RUNNING or self._current is None:
            raise InterlockError("确认链不在执行中，无法暂停", component="confirm")
        gate = self._current
        self._gate_state[gate] = BLOCKED
        self._status = HELD
        self._emit(
            EVT_OPERATOR_PAUSE,
            ts,
            {"gate": gate, "operator": str(operator), "reason": str(reason)},
        )
        return self.view()

    def abort(self, ts: float, operator: str = "console", reason: str = "operator_abort") -> dict:
        """人工终止本轮确认：不可自行恢复，必须重新开始。"""

        if self._status not in (RUNNING, HELD) or self._current is None:
            raise InterlockError("确认链不在执行中，无法终止", component="confirm")
        self._status = ABORTED
        self._emit(
            EVT_CHAIN_ABORTED,
            ts,
            {"gate": self._current, "reason": OPERATOR_ABORT, "source": "operator",
             "operator": str(operator), "detail": str(reason)},
        )
        return self.view()

    def resume(self, ts: float, operator: str = "console") -> dict:
        """人工恢复保持：重试计数清零、本门累计作废，从被保持的门重新起步。"""

        if self._status != HELD or self._current is None:
            raise InterlockError("确认链不在保持状态，无法恢复", component="confirm")
        gate = self._current
        self._attempts[gate] = 0
        self._gate_state[gate] = ACTIVE
        self._reset_gate_evidence(gate)
        self._invalid_streak = 0
        self._last_ts = None
        self._status = RUNNING
        self._emit(EVT_CHAIN_RESUMED, ts, {"gate": gate, "operator": str(operator)})
        self._enter_gate(gate, ts)
        return self.view()

    def request_skip(self, gate: str | None, ts: float, operator: str = "console") -> dict:
        """请求跳过一道门：确认步骤有时序，任何跳步都拒绝并落告警。"""

        target = gate or self._current
        self._emit(
            EVT_SKIP_DENIED,
            ts,
            {"gate": target, "operator": str(operator), "reason": "gate_order_locked"},
        )
        return {"denied": True, "gate": target, "reason": "gate_order_locked"}

    # ---- 主循环 -------------------------------------------------------

    def tick(
        self,
        ts: float,
        flow_kg_s: float | None,
        valid: bool = True,
        pump_running: bool = True,
        interlock_ok: bool = True,
    ) -> dict:
        """喂入一拍现场样本并推进确认链，返回还原后的视图。

        保持/终态时这一拍不参与判定也不留事件（现场应停止确认，而不是带病计时）。
        """

        if self._last_ts is not None and ts + 1e-9 < self._last_ts:
            raise ValueError("确认链时间戳必须单调不减")

        interval = None if self._last_ts is None else ts - self._last_ts
        if interval is not None and interval > 0.0:
            self._nominal = interval if self._nominal is None else min(self._nominal, interval)
        self._tick_seq += 1

        regime_view = self._detector.update(
            ts, bool(valid), bool(pump_running), 0.0 if flow_kg_s is None else float(flow_kg_s)
        )
        if not valid:
            self._invalid_streak += 1
        else:
            self._invalid_streak = 0

        return self._feed(
            ts, flow_kg_s, valid, pump_running, interlock_ok, interval, regime_view, self._invalid_streak
        )

    def _feed(
        self, ts, flow_kg_s, valid, pump_running, interlock_ok, interval, regime_view, invalid_streak
    ) -> dict:
        """按已确定的工况快照推进一拍。现场与重放走同一段逻辑，保证结果一致。"""

        if self._status in (IDLE, ABORTED, CONFIRMED, HELD):
            return self.view()
        tick_seq = self._tick_seq
        guard = {
            "ts": round(ts, 3),
            "last_ts": None if self._last_ts is None else round(self._last_ts, 3),
            "tick_seq": tick_seq,
            "flow_kg_s": None if flow_kg_s is None else round(float(flow_kg_s), 3),
            "valid": bool(valid),
            "pump_running": bool(pump_running),
            "interlock_ok": bool(interlock_ok),
            "interval_s": None if interval is None else round(interval, 3),
            "nominal_s": None if self._nominal is None else round(self._nominal, 3),
            "regime": regime_view["regime"],
            "regime_dwell_s": regime_view["dwell_s"],
            "invalid_streak": invalid_streak,
        }
        self._last_ts = ts

        # 一拍内最多把每道门各判一次，防止回退后形成死循环。
        evaluated: set[str] = set()
        while self._status == RUNNING and self._current is not None and self._current not in evaluated:
            gate = self._current
            evaluated.add(gate)
            outcome = self._evaluate_gate(gate, ts, flow_kg_s, valid, pump_running, interlock_ok, regime_view, interval)
            data = {**guard, "gate": gate, "criterion": GATE_CRITERIA[gate], **outcome}
            self._emit(EVT_TICK_EVALUATED, ts, data)

            verdict = outcome["verdict"]
            if verdict == "passed":
                self._pass_gate(gate, ts, outcome.get("evidence"))
            elif verdict == "interrupted":
                self._handle_interrupt(gate, ts, outcome["cause"])
                break
            else:
                break
        return self.view()

    # ---- 各门判据 -----------------------------------------------------

    def _evaluate_gate(
        self, gate: str, ts: float, flow, valid: bool, pump_running: bool, interlock_ok: bool,
        regime_view: dict, interval: float | None,
    ) -> dict:
        cause = self._common_cause(gate, valid, pump_running, interlock_ok, interval)
        if cause is not None:
            return {"verdict": "interrupted", "cause": cause, "reason": cause, "evidence": None}

        if gate == G0_PRECHECK:
            return {"verdict": "passed", "reason": "preconditions_ok",
                    "evidence": {"flow_kg_s": round(float(flow), 3), "regime": regime_view["regime"]}}

        if gate == G1_RAMP:
            self._ramp.append(Sample(ts, float(flow)))
            cutoff = ts - self._limits.ramp_window_s
            self._ramp = [sample for sample in self._ramp if sample.ts >= cutoff - 1e-9]
            metrics = evaluate_ramp(self._ramp, self._limits)
            if metrics["ok"]:
                return {"verdict": "passed", "reason": "ramp_trend_confirmed", "evidence": metrics}
            # 起步门只认爬升趋势。窗口攒满后斜率仍达不到起步要求（爬不动/在门限
            # 附近走平/掉头）就是一次失败的起步，按起步阶段策略作废本窗重试，
            # 绝不允许“流量恰好够量”把趋势门混过去——那正是旧门控误判的路径。
            if metrics["window_s"] + 1e-9 >= self._limits.ramp_window_s and metrics["slope_kg_s2"] + 1e-9 < (
                self._limits.ramp_min_slope_kg_s2
            ):
                return {"verdict": "interrupted", "cause": RAMP_STALLED,
                        "reason": "ramp_stalled", "evidence": metrics}
            return {"verdict": "waiting", "reason": metrics["reason"], "evidence": metrics}

        if gate == G2_ARM:
            if regime_view["regime"] != LOADED:
                return {"verdict": "waiting", "reason": "awaiting_loaded_regime",
                        "evidence": {"regime": regime_view["regime"], "dwell_s": regime_view["dwell_s"]}}
            return {"verdict": "passed", "reason": "loaded_regime_locked",
                    "evidence": {"regime": LOADED, "next_criterion": FIXED_MARGIN,
                                 "threshold_kg_s": round(self._limits.loaded_threshold_kg_s(), 3),
                                 "hold_s": self._limits.loaded_hold_s}}

        if gate == G3_MARGIN:
            if regime_view["regime"] != LOADED:
                # 工况退回起步（迟滞已确认）：已经进入带负荷门又掉出工况，
                # 保持窗口立即作废，退回待命门。
                return {"verdict": "interrupted", "cause": REGIME_BACK,
                        "reason": REGIME_BACK, "evidence": {"regime": regime_view["regime"]}}
            threshold = self._limits.loaded_threshold_kg_s()
            if float(flow) + 1e-9 < threshold:
                # 掉出门限：只有此前已经攒过一段合格保持，才算“带负荷裕度被打断”；
                # 刚进带负荷门、还在往裕度上爬时掉下来属于尚未稳住，原地等。
                established = bool(self._hold)
                self._hold = []
                metrics = evaluate_fixed_margin([Sample(ts, float(flow))], self._limits)
                metrics["reason"] = "below_margin"
                if established:
                    return {"verdict": "interrupted", "cause": "margin_lost",
                            "reason": "margin_lost", "evidence": metrics}
                return {"verdict": "waiting", "reason": "below_margin", "evidence": metrics}
            self._hold.append(Sample(ts, float(flow)))
            cutoff = ts - self._limits.loaded_hold_s
            self._hold = [sample for sample in self._hold if sample.ts >= cutoff - 1e-9]
            metrics = evaluate_fixed_margin(self._hold, self._limits)
            if metrics["ok"]:
                return {"verdict": "passed", "reason": "fixed_margin_confirmed", "evidence": metrics}
            return {"verdict": "waiting", "reason": metrics["reason"], "evidence": metrics}

        # G4 许可门：能到这里说明前四道门全部按序通过，本拍即放行并写许可事件。
        return {"verdict": "passed", "reason": "permit_issued",
                "evidence": {"flow_min_kg_s": self._limits.flow_min_kg_s,
                             "loaded_threshold_kg_s": round(self._limits.loaded_threshold_kg_s(), 3)}}

    def _common_cause(self, gate, valid, pump_running, interlock_ok, interval: float | None) -> str | None:
        """硬失败与传感器类部分失败的统一识别，门的业务判据之前先过这里。"""

        if not pump_running:
            return PUMP_TRIP
        if not interlock_ok:
            return INTERLOCK_LOST
        if not valid:
            if self._invalid_streak >= self._limits.sensor_fault_streak:
                return SENSOR_FAULT
            return SENSOR_DROPOUT
        # 缺拍：相邻样本间隔超过名义周期的 gap_multiplier 倍（名义周期取实测最小值）。
        if interval is not None and self._nominal is not None:
            if interval > self._nominal * self._limits.tick_gap_multiplier + 1e-9:
                return SAMPLE_GAP
        return None

    # ---- 通过 / 打断 --------------------------------------------------

    def _pass_gate(self, gate: str, ts: float, evidence) -> None:
        self._gate_state[gate] = "passed"
        self._emit(EVT_GATE_PASSED, ts, {"gate": gate, "attempt": self._attempts[gate], "evidence": evidence})
        if gate == G2_ARM:
            self._emit(
                EVT_REGIME_LOCKED,
                ts,
                {"regime": LOADED, "criterion": FIXED_MARGIN,
                 "threshold_kg_s": round(self._limits.loaded_threshold_kg_s(), 3)},
            )
        if gate == G4_PERMIT:
            self._status = CONFIRMED
            self._emit(
                EVT_FLOW_PERMITTED,
                ts,
                {"flow_min_kg_s": self._limits.flow_min_kg_s,
                 "loaded_threshold_kg_s": round(self._limits.loaded_threshold_kg_s(), 3),
                 "criteria": {G1_RAMP: RAMP_TREND, G3_MARGIN: FIXED_MARGIN}},
            )
            return
        nxt = GATE_ORDER[GATE_ORDER.index(gate) + 1]
        self._current = nxt
        self._enter_gate(nxt, ts)

    def _handle_interrupt(self, gate: str, ts: float, cause: str) -> None:
        attempts_before = self._attempts[gate]
        action = action_for(gate, cause)
        # 重试次数上限只约束“反复重判”，不约束传感器故障升级：连续坏点达到故障
        # 条数时无论还剩几次重试都按传感器故障停下，避免把硬件故障误记成重试耗尽。
        if cause == SENSOR_DROPOUT and self._invalid_streak >= self._limits.sensor_fault_streak:
            cause = SENSOR_FAULT
            action = action_for(gate, cause)

        # 一次“孤立掉点”（坏点串的第一个）算作一次重试尝试；但同一串连续坏点的
        # 后续坏点不逐拍累加尝试次数——它们是同一个故障的延续，到故障条数就升级
        # 为传感器故障，而不是先把重试名额耗光。
        continuing_dropout = cause == SENSOR_DROPOUT and self._invalid_streak > 1
        counts_as_attempt = not continuing_dropout

        if action == RETRY:
            if counts_as_attempt and attempts_before >= self._limits.gate_max_retries:
                self._escalate_exhausted(gate, ts, cause, attempts_before)
                return
            self._attempts[gate] = attempts_before + (1 if counts_as_attempt else 0)
            self._emit(
                EVT_GATE_RETRY,
                ts,
                {"gate": gate, "reason": cause, "action": RETRY,
                 "attempt": attempts_before, "next_attempt": self._attempts[gate]},
            )
            self._reset_gate_evidence(gate)
            # 连续掉点计数不在重试时清零：几次孤立重试不能把“传感器已经连续坏点”
            # 的证据抹掉；只有有效样本到来或人工恢复才清零。
            self._enter_gate(gate, ts)
            return

        if action == BACKTRACK:
            if counts_as_attempt and attempts_before >= self._limits.gate_max_retries:
                self._escalate_exhausted(gate, ts, cause, attempts_before)
                return
            self._attempts[gate] = attempts_before + (1 if counts_as_attempt else 0)
            self._gate_state[gate] = "waiting"
            self._reset_gate_evidence(gate)
            target = G2_ARM
            self._emit(
                EVT_GATE_BACKTRACK,
                ts,
                {"gate": gate, "to_gate": target, "reason": cause, "action": BACKTRACK,
                 "attempt": attempts_before, "next_attempt": self._attempts[gate],
                 "target_attempt": self._attempts[target]},
            )
            self._current = target
            self._invalid_streak = 0
            self._enter_gate(target, ts)
            return

        if action == ABORT:
            self._status = ABORTED
            self._emit(
                EVT_CHAIN_ABORTED,
                ts,
                {"gate": gate, "reason": cause, "source": "field", "action": ABORT},
            )
            return

        # 默认最保守：停下来等人。
        self._gate_state[gate] = BLOCKED
        self._status = HELD
        self._emit(
            EVT_CHAIN_HELD,
            ts,
            {"gate": gate, "reason": cause, "action": action_for(gate, cause)},
        )

    def _escalate_exhausted(self, gate: str, ts: float, cause: str, attempts: int) -> None:
        """重试名额用尽：停下等人，由人工决定恢复（部分失败，不终止本轮）。"""

        self._gate_state[gate] = BLOCKED
        self._status = HELD
        self._emit(
            EVT_CHAIN_HELD,
            ts,
            {"gate": gate, "reason": RETRY_EXHAUSTED, "action": "hold",
             "underlying": cause, "attempts": attempts},
        )

    def _reset_gate_evidence(self, gate: str) -> None:
        if gate == G1_RAMP:
            self._ramp = []
        elif gate == G3_MARGIN:
            self._hold = []

    def _enter_gate(self, gate: str, ts: float) -> None:
        self._current = gate
        self._gate_state[gate] = ACTIVE
        # 进入一道门时其判据窗口必须是空的：回退/重试/级联进入都不允许带着
        # 前一道门或上一次尝试的旧样本计时，否则保持窗口会凭空“继承”达标秒数。
        self._reset_gate_evidence(gate)
        self._emit(
            EVT_GATE_ENTERED,
            ts,
            {"gate": gate, "attempt": self._attempts[gate], "criterion": GATE_CRITERIA[gate],
             "index": GATE_ORDER.index(gate)},
        )

    # ---- 事件出口 -----------------------------------------------------

    def _emit(self, action: str, ts: float, data: dict) -> dict:
        event = self._ledger.append(action, ts, data)
        if self._sink is not None:
            self._sink(event)
        return event


def replay_chain(limits, events: list[dict]) -> ConfirmChain:
    """从落盘事件重放一轮确认，返回重放链（其流水应与原流水逐条一致）。

    只有四类“输入事件”会驱动状态机：现场采样、人工暂停、人工恢复、跳步请求与
    人工终止；其余事件全是这些输入的确定性派生物，重放时不应再喂一次。
    """

    chain = ConfirmChain(limits)
    fed_ticks: set = set()
    for event in events:
        action = event["action"]
        ts = event["ts"]
        data = event.get("data", {})
        if action == EVT_RUN_STARTED:
            chain.start(ts)
        elif action == EVT_TICK_EVALUATED:
            # 一拍现场输入在级联时会产生多条门判定事件，只取该拍第一道门的守卫
            # 记录（它含本拍真实的工况与停留计时），按落盘证据还原识别器后喂入。
            # 这样重放不依赖识别器内部候选态能否被逐步重新推出，级联门也不会
            # 重复更新识别器，保证逐事件一致。
            seq = data.get("tick_seq")
            if seq is None:
                continue
            if seq in fed_ticks:
                continue
            fed_ticks.add(seq)
            chain._tick_seq = int(seq)
            chain._nominal = data.get("nominal_s")
            chain._invalid_streak = int(data.get("invalid_streak", 0))
            chain._detector.restore(str(data.get("regime", "startup")))
            if data.get("last_ts") is not None:
                chain._last_ts = float(data["last_ts"])
            regime_view = {
                "regime": data.get("regime"),
                "dwell_s": float(data.get("regime_dwell_s", 0.0)),
                "valid": bool(data.get("valid", True)),
                "pump_running": bool(data.get("pump_running", True)),
                "flow_kg_s": data.get("flow_kg_s"),
                "changed": False,
            }
            chain._feed(
                ts,
                data.get("flow_kg_s"),
                valid=bool(data.get("valid", True)),
                pump_running=bool(data.get("pump_running", True)),
                interlock_ok=bool(data.get("interlock_ok", True)),
                interval=data.get("interval_s"),
                regime_view=regime_view,
                invalid_streak=int(data.get("invalid_streak", 0)),
            )
        elif action == EVT_OPERATOR_PAUSE:
            chain.pause(ts, data.get("operator", "console"), data.get("reason", "operator_pause"))
        elif action == EVT_CHAIN_RESUMED:
            chain.resume(ts, data.get("operator", "console"))
        elif action == EVT_SKIP_DENIED:
            chain.request_skip(data.get("gate"), ts, data.get("operator", "console"))
        elif action == EVT_CHAIN_ABORTED and data.get("source") == "operator":
            chain.abort(ts, data.get("operator", "console"), data.get("detail", "operator_abort"))
    return chain
