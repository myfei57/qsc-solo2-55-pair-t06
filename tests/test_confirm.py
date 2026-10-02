"""通盐前流量确认链：判据、工况、时序、部分失败进退、留痕与重放。

覆盖需求逐条对应：
- 起步按爬升趋势、带负荷按固定裕度，模式由门静态定死，不随单拍切换；
- 旧门控「连续够量 20 秒」在泵起步走平时会误判，新链必须拦住；
- 门严格按序，跳步请求一律拒绝；
- 传感器掉点是部分失败不算过：起步自动重试，带负荷退回待命，连续掉点停下等人；
- 打断的退/停按阶段策略表，重试耗尽停下等人，泵停/联锁丢失终止；
- 每一步都进只追加哈希流水，篡改可验，落盘事件重放与现场逐事件一致；
- 平台层确认事件同时供控制台展示并落审计，重放对账一致；HTTP 接口可用。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from cspplant.config import default_config, load_config
from cspplant.confirm import (
    ABORTED,
    CONFIRMED,
    FIXED_MARGIN,
    G0_PRECHECK,
    G1_RAMP,
    G2_ARM,
    G3_MARGIN,
    G4_PERMIT,
    HELD,
    RAMP_TREND,
    RUNNING,
)
from cspplant.confirm.chain import ConfirmChain, limits_hash, replay_chain
from cspplant.confirm.criteria import Sample, evaluate_fixed_margin, evaluate_ramp, linear_fit, r_squared
from cspplant.confirm.ledger import ConfirmLedger
from cspplant.confirm.policy import action_for
from cspplant.confirm.regime import RegimeDetector
from cspplant.config import ConfigError
from cspplant.errors import InterlockError

from cspplant.console.api import Api
from cspplant.console.pages import render_page


LIMITS = default_config().flow_confirm


def _healthy_chain(ramp: float = 1.4, plateau: float = 58.0, until: str = CONFIRMED):
    """泵按固定斜率爬升到平台流量，喂到指定状态为止。"""

    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    flow = 0.0
    t = 0
    while True:
        t += 1
        flow = min(plateau, flow + ramp)
        chain.tick(float(t), flow)
        if chain.status == until or t > 500:
            break
    return chain, t


# ---- 判据数学 -------------------------------------------------------------


def test_ramp_requires_window_slope_fit_and_end_flow():
    samples = [Sample(float(t), 30.0 + 1.2 * t) for t in range(11)]
    metrics = evaluate_ramp(samples, LIMITS)
    assert metrics["criterion"] == RAMP_TREND
    assert metrics["ok"] is True
    assert metrics["slope_kg_s2"] == pytest.approx(1.2, abs=1e-6)
    assert metrics["r2"] == pytest.approx(1.0, abs=1e-6)
    assert metrics["end_flow_kg_s"] == pytest.approx(42.0, abs=1e-6)


def test_ramp_rejects_short_window_and_flat_flow():
    short = [Sample(float(t), 10.0 + 1.0 * t) for t in range(5)]
    assert evaluate_ramp(short, LIMITS)["ok"] is False
    assert evaluate_ramp(short, LIMITS)["reason"] == "window_short"

    flat = [Sample(float(t), 43.0) for t in range(11)]
    metrics = evaluate_ramp(flat, LIMITS)
    assert metrics["ok"] is False
    assert metrics["slope_kg_s2"] == pytest.approx(0.0)
    assert metrics["reason"] == "slope_low"


def test_ramp_rejects_poor_fit_and_reversal():
    # 围绕爬升线大幅抖动：拟合度远低于门槛（拟合差比斜率更早被拦下）。
    noisy = [Sample(0.0, 30.0), Sample(1.0, 33.5), Sample(2.0, 30.2), Sample(3.0, 33.8),
             Sample(4.0, 30.5), Sample(5.0, 34.1), Sample(6.0, 30.8), Sample(7.0, 34.4),
             Sample(8.0, 31.1), Sample(9.0, 34.7), Sample(10.0, 31.4)]
    metrics = evaluate_ramp(noisy, LIMITS)
    assert metrics["ok"] is False
    assert metrics["reason"] == "fit_poor"

    reversed_flow = [Sample(float(t), 30.0 - 0.1 * t) for t in range(11)]
    assert evaluate_ramp(reversed_flow, LIMITS)["reason"] == "slope_low"


def test_linear_fit_and_r_squared_basics():
    assert linear_fit([]) == (0.0, 0.0)
    assert r_squared([Sample(0.0, 5.0)]) == 0.0
    assert r_squared([Sample(0.0, 5.0), Sample(1.0, 5.0), Sample(2.0, 5.0)]) == 1.0


def test_fixed_margin_requires_threshold_margin_hold_and_flatness():
    threshold = LIMITS.loaded_threshold_kg_s()
    assert threshold == LIMITS.flow_min_kg_s + LIMITS.loaded_margin_kg_s
    enough = [Sample(float(t), threshold + 1.0) for t in range(21)]
    metrics = evaluate_fixed_margin(enough, LIMITS)
    assert metrics["criterion"] == FIXED_MARGIN
    assert metrics["ok"] is True
    assert metrics["min_headroom_kg_s"] == pytest.approx(1.0)

    # 门限之上但仍在爬：斜率超出走平约束，不算数。
    climbing = [Sample(float(t), threshold + 0.5 * t) for t in range(21)]
    assert evaluate_fixed_margin(climbing, LIMITS)["reason"] == "still_climbing"

    # 低于固定裕度（哪怕高于联锁门限）。
    low = [Sample(float(t), LIMITS.flow_min_kg_s + 1.0) for t in range(21)]
    assert evaluate_fixed_margin(low, LIMITS)["reason"] == "below_margin"


# ---- 工况迟滞 -------------------------------------------------------------


def test_regime_detector_needs_dwell_and_freezes_on_dropout():
    detector = RegimeDetector(LIMITS)
    # 进入候选工况的第一拍算停留第一秒，但要连续 loaded_dwell_s 拍才真翻。
    first = detector.update(1.0, True, True, 50.0)
    assert first["regime"] == "startup" and first["dwell_s"] == pytest.approx(1.0)
    second = detector.update(2.0, True, True, 50.0)
    assert second["regime"] == "loaded" and second["changed"] is True
    # 回落到门限以下需要 startup_dwell_s 拍才退回。
    assert detector.update(3.0, True, True, 40.0)["regime"] == "loaded"
    assert detector.update(4.0, True, True, 40.0)["regime"] == "startup"

    detector.update(5.0, True, True, 50.0)
    before = detector.update(6.0, False, True, 50.0)
    assert before["valid"] is False and before["regime"] == "startup"
    # 掉点不推进停留计时，工况不翻。
    assert detector.update(7.0, False, True, 99.0)["regime"] == "startup"


def test_regime_pump_stopped_does_not_flip():
    detector = RegimeDetector(LIMITS)
    detector.update(1.0, True, True, 50.0)
    detector.update(2.0, True, True, 50.0)
    assert detector.regime == "loaded"
    assert detector.update(3.0, True, False, 0.0)["regime"] == "loaded"


# ---- 流水哈希 -------------------------------------------------------------


def test_ledger_is_append_only_and_detects_tampering():
    ledger = ConfirmLedger()
    ledger.append("a", 1.0, {"x": 1})
    ledger.append("b", 2.0, {"x": 2})
    assert ledger.verify()["ok"] is True
    events = ledger.events()
    import copy

    tampered = copy.deepcopy(events)
    tampered[1]["data"]["x"] = 999
    rebuilt = ConfirmLedger()
    for event in tampered:
        rebuilt._events.append(event)
    result = rebuilt.verify()
    assert result["ok"] is False
    assert result["at_seq"] == 1


# ---- 全链：判据定死、旧误判被拦住 -----------------------------------------


def test_healthy_ramps_through_all_gates_and_permits():
    chain, t = _healthy_chain()
    assert chain.status == CONFIRMED
    assert chain.permitted() is True
    view = chain.view()
    assert [gate for gate, st in view["gates"].items() if st["state"] == "passed"] == [
        G0_PRECHECK, G1_RAMP, G2_ARM, G3_MARGIN, G4_PERMIT,
    ]
    # 判据模式由门静态绑定，并随运行开始落审计。
    started = chain.events()[0]
    assert started["data"]["criteria"][G1_RAMP] == RAMP_TREND
    assert started["data"]["criteria"][G3_MARGIN] == FIXED_MARGIN
    assert started["data"]["config_hash"] == limits_hash(LIMITS)


def test_flat_flow_above_interlock_does_not_falsely_confirm():
    """旧门控：连续 20 秒 >= flow_min 就算过。新链起步斜率为 0，永远过不了起步门。"""

    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    for t in range(1, 41):
        chain.tick(float(t), LIMITS.flow_min_kg_s + 1.0)
    assert chain.status == RUNNING
    assert chain.current_gate == G1_RAMP
    assert chain.permitted() is False
    # 起步门被反复判为失败并重试，直到重试上限停下等人。
    assert chain.view()["gates"][G1_RAMP]["attempt"] >= 1
    assert any(i["reason"] == "ramp_stalled" for i in chain.view()["interrupts"])


def test_ramp_stalled_policy_retries_in_startup():
    assert action_for(G1_RAMP, "ramp_stalled") == "retry"
    assert action_for(G3_MARGIN, "ramp_stalled") == "hold"


def test_criterion_mode_is_locked_per_gate_not_switched_by_samples():
    chain, _ = _healthy_chain()
    ticks = [e for e in chain.events() if e["action"] == "confirm_tick_evaluated"]
    by_gate = {}
    for event in ticks:
        by_gate.setdefault(event["data"]["gate"], set()).add(event["data"]["criterion"])
    assert by_gate[G1_RAMP] == {RAMP_TREND}
    assert by_gate[G3_MARGIN] == {FIXED_MARGIN}


# ---- 门时序：不可跳 -------------------------------------------------------


def test_gates_only_advance_in_order_and_skip_is_denied():
    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    assert chain.current_gate == G0_PRECHECK
    result = chain.request_skip(G3_MARGIN, 1.0, "lead")
    assert result["denied"] is True
    assert chain.current_gate == G0_PRECHECK
    # 跳步拒绝进了审计流水且是告警档动作。
    assert chain.events()[-1]["action"] == "confirm_skip_denied"


def test_cannot_start_twice_or_resume_without_hold():
    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    with pytest.raises(InterlockError):
        chain.start(1.0)
    with pytest.raises(InterlockError):
        chain.resume(1.0)


# ---- 部分失败：掉点/缺拍/连续故障 -----------------------------------------


def test_startup_single_dropout_retries_without_passing():
    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    flow = 0.0
    for t in range(1, 16):
        flow = min(58.0, flow + 1.4)
        chain.tick(float(t), flow, valid=(t != 12))
    assert chain.status == RUNNING
    gate = chain.view()["gates"][G1_RAMP]
    assert gate["attempt"] == 1
    interrupt = chain.view()["interrupts"][-1]
    assert interrupt["reason"] == "sensor_dropout"
    assert interrupt["action"] == "retry"


def test_loaded_single_dropout_backtracks_to_arming():
    chain, t = _run_to_g3()
    flow = min(58.0, 1.4 * t)
    chain.tick(float(t + 1), flow, valid=False)
    assert chain.current_gate == G2_ARM
    assert chain.view()["interrupts"][-1]["reason"] == "sensor_dropout"
    assert chain.view()["interrupts"][-1]["action"] == "backtrack"


def test_consecutive_dropouts_become_sensor_fault_and_hold():
    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    flow = 0.0
    for t in range(1, 20):
        flow = min(58.0, flow + 1.4)
        # t=13 起连续掉点，中间不插有效样本，连续坏点计数应升级为传感器故障。
        chain.tick(float(t), flow, valid=(t < 13))
    assert chain.status == HELD
    assert chain.view()["holds"][-1]["reason"] == "sensor_fault"
    assert chain.permitted() is False


def test_sample_gap_is_partial_failure_and_retries_in_startup():
    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    flow = 0.0
    for t in range(1, 12):
        flow = min(58.0, flow + 1.4)
        chain.tick(float(t), flow)
    # 缺拍：间隔 5 秒，超过名义周期 1s 的 gap_multiplier。
    chain.tick(16.0, flow)
    interrupt = chain.view()["interrupts"][-1]
    assert interrupt["reason"] == "sample_gap"
    assert interrupt["action"] == "retry"


# ---- 带负荷阶段打断：裕度丢失/工况退回 -------------------------------------


def test_loaded_margin_lost_after_hold_backtracks_to_arming():
    chain, t = _run_to_g3()
    for k in range(1, 4):
        chain.tick(float(t + k), 55.0)  # 55 >= 48，攒合格保持
    chain.tick(float(t + 4), 47.0)      # 47 < 48 但 >= 42，工况仍 loaded
    assert chain.current_gate == G2_ARM
    assert chain.status == RUNNING
    interrupt = chain.view()["interrupts"][-1]
    assert interrupt["reason"] == "margin_lost"
    assert interrupt["action"] == "backtrack"


def test_regime_back_in_loaded_gate_backtracks():
    chain, t = _run_to_g3()
    # 流量跌破门限并持续超过 startup_dwell，工况按迟滞退回起步。
    tt = t
    for _ in range(3):
        tt += 1
        chain.tick(float(tt), 40.0)
    assert chain.current_gate in (G2_ARM, G1_RAMP)
    assert any(item["reason"] == "regime_back" for item in chain.view()["interrupts"])


def _run_to_g3():
    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    flow = 0.0
    t = 0
    while chain.current_gate != G3_MARGIN:
        t += 1
        flow = min(58.0, flow + 1.4)
        chain.tick(float(t), flow)
    return chain, t


# ---- 硬失败与人工处置 ------------------------------------------------------


def test_pump_trip_aborts_and_cannot_self_recover():
    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    for t in range(1, 10):
        chain.tick(float(t), 10.0)
    chain.tick(10.0, 10.0, pump_running=False)
    assert chain.status == ABORTED
    assert chain.view()["aborted_reason"] == "pump_trip"
    # 终态后再喂样本不产生任何新事件，也不能 resume。
    before = len(chain.events())
    chain.tick(11.0, 50.0)
    assert len(chain.events()) == before
    with pytest.raises(InterlockError):
        chain.resume(12.0)


def test_interlock_lost_aborts():
    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    chain.tick(1.0, 10.0, interlock_ok=False)
    assert chain.status == ABORTED
    assert chain.view()["aborted_reason"] == "interlock_lost"


def test_operator_pause_resume_and_abort():
    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    chain.pause(5.0, "lead", "inspect")
    assert chain.status == HELD
    held_ticks = len(chain.events())
    chain.tick(6.0, 50.0)  # 保持期间喂样本不推进
    assert len(chain.events()) == held_ticks
    chain.resume(7.0, "lead")
    assert chain.status == RUNNING
    chain.abort(8.0, "lead")
    assert chain.status == ABORTED
    assert chain.view()["aborted_reason"] == "operator_abort"


def test_retry_exhaustion_holds_for_operator():
    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    flow = 0.0
    drop_at = {8, 14, 20, 26}
    for t in range(1, 30):
        flow = min(58.0, flow + 1.4)
        chain.tick(float(t), flow, valid=(t not in drop_at))
    assert chain.status == HELD
    assert chain.view()["holds"][-1]["reason"] == "retry_exhausted"
    # 人工确认现场后恢复：重试计数清零，可以继续走到放行。
    chain.resume(float(30), "lead")
    flow = 30.0
    t = 30
    while chain.status != CONFIRMED and t < 120:
        t += 1
        flow = min(58.0, flow + 1.4)
        chain.tick(float(t), flow)
    assert chain.status == CONFIRMED


# ---- 策略表冻结 -----------------------------------------------------------


def test_policy_table_is_stage_specific_and_conservative_default():
    assert action_for(G1_RAMP, "sensor_dropout") == "retry"
    assert action_for(G3_MARGIN, "sensor_dropout") == "backtrack"
    assert action_for(G1_RAMP, "sensor_fault") == "hold"
    assert action_for(G3_MARGIN, "margin_lost") == "backtrack"
    assert action_for(G1_RAMP, "pump_trip") == "abort"
    assert action_for(G3_MARGIN, "pump_trip") == "abort"
    # 没列出的组合一律最保守：停下来等人。
    assert action_for(G2_ARM, "mystery_cause") == "hold"


# ---- 留痕重放：逐事件一致 + 篡改可验 ---------------------------------------


def test_replay_reproduces_every_event_for_healthy_run():
    chain, _ = _healthy_chain()
    replayed = replay_chain(LIMITS, chain.events())
    assert replayed.verify()["ok"] is True
    assert replayed.events() == chain.events()
    assert replayed.view() == chain.view()


def test_replay_reproduces_interrupted_run_with_holds_and_resume():
    chain = ConfirmChain(LIMITS)
    chain.start(0.0)
    flow = 0.0
    for t in range(1, 20):
        flow = min(58.0, flow + 1.4)
        chain.tick(float(t), flow, valid=(t != 12))
    chain, t2 = chain, 19
    flow = min(58.0, flow + 1.4 * 0)  # 保持当前
    chain.pause(20.0, "lead", "check")
    chain.resume(21.0, "lead")
    while chain.status != CONFIRMED and t2 < 120:
        t2 += 1
        flow = min(58.0, flow + 1.4)
        chain.tick(float(t2), flow)
    replayed = replay_chain(LIMITS, chain.events())
    assert replayed.events() == chain.events()
    assert replayed.view() == chain.view()


# ---- 配置 -----------------------------------------------------------------


def test_loaded_config_file_contains_flow_confirm_section():
    cfg = load_config(str(ROOT / "config" / "csp.json"))
    assert cfg.flow_confirm.ramp_min_slope_kg_s2 == pytest.approx(0.8)
    assert cfg.flow_confirm.loaded_hold_s == 20.0
    assert cfg.flow_confirm.ramp_min_end_kg_s < cfg.receiver.flow_min_kg_s


def test_config_rejects_ramp_end_above_interlock():
    mapping = dict(default_config().__dict__)
    # 直接构造一个末端门限高于联锁门限的非法配置。
    from cspplant.config import FlowConfirmLimits

    with pytest.raises(ConfigError):
        FlowConfirmLimits.from_mapping(
            {
                "ramp_window_s": 10.0, "ramp_min_slope_kg_s2": 0.8, "ramp_min_r2": 0.9,
                "ramp_min_end_kg_s": 50.0, "loaded_margin_kg_s": 6.0, "loaded_hold_s": 20.0,
                "loaded_slope_bound_kg_s2": 0.25, "startup_dwell_s": 2.0, "loaded_dwell_s": 2.0,
                "sensor_fault_streak": 5, "tick_gap_multiplier": 2.0, "gate_max_retries": 3,
            },
            flow_min_kg_s=42.0,
        )


# ---- 平台：确认链供控制台并落审计、重放对账一致 -----------------------------


def test_platform_publishes_confirm_events_to_audit_and_console(platform, clock):
    platform.confirm_start()
    flow = 0.0
    for _ in range(60):
        clock.advance(1.0)
        flow = min(58.0, flow + 1.4)
        platform.confirm_tick(flow)
        if platform.confirm.status == CONFIRMED:
            break
    assert platform.confirm.permitted() is True
    # 控制台视图来自同一事件流水。
    view = platform.confirm_view()
    assert view["status"] == CONFIRMED
    assert view["current_gate"] == G4_PERMIT
    # 审计流水里确实落了确认事件，且带哈希。
    records = [e for e in platform.audit.records(limit=1000) if e.component == "confirm"]
    assert records
    assert all("hash" in e.payload for e in records)
    assert any(e.action == "confirm_salt_permitted" for e in records)
    # 从落盘审计重放，与现场逐字段一致。
    reconciliation = platform.confirm_replay()
    assert reconciliation["same"] is True
    assert reconciliation["ledger_ok"] is True


def test_stored_ledger_detects_body_tamper_and_row_deletion():
    """篡改已落盘流水的正文或删掉一行，哈希链校验必须立即报错。"""

    import copy

    chain, _ = _healthy_chain()
    body = copy.deepcopy(chain.events())

    tampered = ConfirmLedger()
    tampered._events = copy.deepcopy(body)
    tampered._events[len(body) // 2]["data"]["flow_kg_s"] = 999.0
    result = tampered.verify()
    assert result["ok"] is False and result["reason"] == "hash_mismatch"

    deleted = ConfirmLedger()
    deleted._events = copy.deepcopy(body)
    del deleted._events[len(body) // 2]
    result = deleted.verify()
    assert result["ok"] is False and result["reason"] == "seq_gap"


def test_platform_pause_resume_skip_api(platform, clock):
    api = Api(platform)
    status, payload = api.handle("POST", "/api/confirm/start", {}, {})
    assert status == 200 and payload["status"] == RUNNING
    status, payload = api.handle("POST", "/api/confirm/pause", {}, {"operator": "lead"})
    assert payload["status"] == HELD
    status, payload = api.handle("POST", "/api/confirm/skip", {}, {"gate": G3_MARGIN})
    assert payload["denied"] is True
    status, payload = api.handle("POST", "/api/confirm/resume", {}, {"operator": "lead"})
    assert payload["status"] == RUNNING
    status, payload = api.handle("GET", "/api/confirm", {}, {})
    assert status == 200 and payload["status"] == RUNNING
    status, events = api.handle("GET", "/api/confirm/events", {}, {})
    assert status == 200 and len(events["events"]) > 0
    status, replay = api.handle("GET", "/api/confirm/replay", {}, {})
    assert status == 200 and replay["same"] is True


def test_confirm_console_page_renders(platform):
    platform.confirm_start()
    html = render_page("/confirm", platform)
    assert html is not None
    assert "通盐确认控制台" in html
    assert "G1 起步爬升趋势" in html
