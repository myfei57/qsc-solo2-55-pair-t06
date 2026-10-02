"""吸热器流量门控、通盐、受热与保护。"""

from __future__ import annotations

import pytest

from cspplant.config import default_config
from cspplant.errors import InterlockError
from cspplant.receiver.derate import DerateController
from cspplant.receiver.flow import (
    CONFIRMED,
    HALTED,
    IDLE,
    LOADED,
    RETRY,
    STARTUP,
    WAIT_OPERATOR,
    FlowGate,
)
from cspplant.receiver.header import SaltHeader
from cspplant.receiver.receiver import AT_POWER, CIRCULATING, COLD, DERATED, TRIPPED, Receiver
from cspplant.receiver.sensor import TemperatureSensor
from cspplant.receiver.thermal import ThermalModel
from cspplant.receiver.threshold import ThresholdModel
from cspplant.receiver.valve import InletValve


def build_receiver(limits=None) -> Receiver:
    config = default_config()
    receiver_limits = limits or config.receiver
    return Receiver(
        receiver_limits,
        InletValve(receiver_limits),
        FlowGate(receiver_limits),
        TemperatureSensor(receiver_limits.ambient_c),
        ThermalModel(receiver_limits),
        DerateController(receiver_limits),
        SaltHeader(config.salt),
        ThresholdModel(config.salt),
    )


def confirm_flow(receiver: Receiver, final_kg_s: float = 58.0, ramp_kg_s2: float = 1.4) -> None:
    """按泵真实爬坡逐秒喂点，直到确认链完成。"""

    flow = receiver.limits.flow_min_kg_s - 2.0
    while not receiver.flow_confirmed():
        flow = min(final_kg_s, flow + ramp_kg_s2)
        receiver.verify_flow(flow, 1.0)


def test_flow_gate_startup_uses_trend_loaded_uses_margin():
    receiver = build_receiver()
    limits = receiver.limits
    gate = receiver.gate

    # 泵起步：流量在联锁门限以下时停留在空闲。
    receiver.verify_flow(limits.flow_min_kg_s - 5.0, 1.0)
    assert gate.phase() == IDLE

    # 起步阶段逐秒爬升：窗口未满，即使瞬时越过裕度门限也不能跳到带负荷确认。
    flow = limits.flow_min_kg_s
    for _ in range(5):
        flow += 2.0
        status = receiver.verify_flow(flow, 1.0)
        assert gate.phase() == STARTUP
        assert status.confirmed is False

    # 窗口判满且净涨为正、末值够到裕度门限，才进入带负荷阶段（时序没跳）。
    for _ in range(limits.flow_startup_window_s.__int__()):
        flow += 2.0
        status = receiver.verify_flow(flow, 1.0)
        if gate.phase() == LOADED:
            break
    assert gate.phase() == LOADED
    assert gate.phases_seen() == (STARTUP, LOADED)

    # 带负荷阶段裕度保持不够时长不算确认。
    receiver.verify_flow(flow, limits.flow_confirm_s / 2.0)
    assert receiver.flow_confirmed() is False
    receiver.verify_flow(flow, limits.flow_confirm_s / 2.0)
    assert receiver.flow_confirmed() is True
    assert gate.phase() == CONFIRMED
    assert receiver.state() == CIRCULATING


def test_flow_gate_flat_or_falling_startup_resets_to_idle():
    receiver = build_receiver()
    limits = receiver.limits
    gate = receiver.gate
    # 窗口判满时流量全程没涨：趋势不成立 → 退回重来。
    for _ in range(int(limits.flow_startup_window_s)):
        receiver.verify_flow(limits.flow_min_kg_s, 1.0)
    assert gate.phase() == IDLE
    assert gate.resets() == 1
    status = receiver.verify_flow(limits.flow_min_kg_s + 1.0, 1.0)
    assert status.interrupted is False

    # 爬升中出现超过容差的明显回跌：起步阶段被打断，退回去重来并自动重试。
    receiver.verify_flow(limits.flow_min_kg_s + 4.0, 1.0)
    status = receiver.verify_flow(
        limits.flow_min_kg_s + 4.0 - limits.flow_drop_tol_kg_s - 0.5, 1.0
    )
    assert status.interrupted is True
    assert status.resolution == RETRY
    assert gate.phase() == IDLE
    assert gate.resets() == 2
    archived = gate.attempts()[-1]
    assert archived.result == "retried"
    assert archived.reason == "flow_reversed"


def test_flow_gate_loaded_interrupt_halts_and_waits_for_operator():
    receiver = build_receiver()
    limits = receiver.limits
    gate = receiver.gate
    confirm_flow(receiver)
    gate.reset()

    # 重新爬进带负荷阶段，裕度保持一半时跌破裕度门限 → 停下来等人。
    flow = limits.flow_min_kg_s - 2.0
    while gate.phase() != LOADED:
        flow = min(limits.flow_loaded_threshold() + 3.0, flow + 1.4)
        receiver.verify_flow(flow, 1.0)
    receiver.verify_flow(flow, limits.flow_confirm_s / 2.0)
    status = receiver.verify_flow(limits.flow_min_kg_s + 1.0, 1.0)
    assert status.interrupted is True
    assert status.resolution == WAIT_OPERATOR
    assert gate.phase() == HALTED
    assert gate.halts() == 1
    # 停下后新来的数据不允许自动恢复确认。
    receiver.verify_flow(flow + 10.0, 1.0)
    assert gate.phase() == HALTED
    with pytest.raises(InterlockError):
        receiver.admit()
    # 人工解除后退回空闲，再正常爬升才能确认。
    assert gate.resume("shift-lead") == IDLE
    confirm_flow(receiver)
    assert receiver.flow_confirmed() is True


def test_sensor_drop_is_partial_failure_never_confirmed():
    receiver = build_receiver()
    limits = receiver.limits
    gate = receiver.gate

    # 起步阶段掉点：退回重来。
    receiver.verify_flow(limits.flow_min_kg_s + 3.0, 1.0)
    status = receiver.verify_flow(None, 1.0)
    assert status.valid is False
    assert status.confirmed is False
    assert status.interrupted is True
    assert status.resolution == RETRY
    assert gate.phase() == IDLE
    assert gate.drops() == 1

    # 带负荷阶段掉点：停下来等人。
    confirm_flow(receiver)
    gate.reset()
    flow = limits.flow_min_kg_s - 2.0
    while gate.phase() != LOADED:
        flow = min(limits.flow_loaded_threshold() + 3.0, flow + 1.4)
        receiver.verify_flow(flow, 1.0)
    status = receiver.verify_flow(flow, 1.0, valid=False)
    assert status.resolution == WAIT_OPERATOR
    assert gate.phase() == HALTED
    assert gate.drops() == 2
    gate.resume()

    # 完整确认后再掉点不撤销确认，但必须留痕。
    confirm_flow(receiver)
    assert receiver.flow_confirmed() is True
    receiver.verify_flow(None, 1.0)
    assert receiver.flow_confirmed() is True
    assert gate.phase() == CONFIRMED
    assert gate.samples()[-1].event == "flow_sensor_drop"


def test_phase_sequence_cannot_skip_startup():
    gate = FlowGate(default_config().receiver)
    limits = gate.limits
    # 一上来就给满足裕度的大流量：仍要先走起步趋势，不能直接进入带负荷确认。
    status = gate.update(limits.flow_loaded_threshold() + 6.0, 1.0)
    assert status.phase == STARTUP
    for _ in range(int(limits.flow_startup_window_s) - 1):
        status = gate.update(limits.flow_loaded_threshold() + 6.0, 1.0)
    # 窗口内流量完全没涨，趋势判据不允许通过：第一次尝试退回重来（不是计时通过）。
    assert status.net_gain_kg_s == 0.0
    assert gate.resets() == 1
    # 第二次尝试：从联锁门限起步，窗口内逐点爬升（净涨为正）才通过趋势判据。
    flow = limits.flow_min_kg_s
    status = None
    for _ in range(20):
        flow = min(limits.flow_loaded_threshold() + 6.0, flow + 2.0)
        status = gate.update(flow, 1.0)
        if gate.phase() == LOADED:
            break
        assert gate.phase() in (IDLE, STARTUP)
    assert status.phase == LOADED
    assert gate.phases_seen() == (STARTUP, LOADED)


def test_every_sample_and_attempt_is_kept_for_replay():
    events: list[tuple[str, dict]] = []
    gate = FlowGate(default_config().receiver, sink=lambda kind, payload: events.append((kind, payload)))
    limits = gate.limits

    gate.update(limits.flow_min_kg_s + 2.0, 1.0)
    gate.update(None, 1.0)
    # 正常跑完一条确认链。
    flow = limits.flow_min_kg_s - 2.0
    while not gate.confirmed():
        flow = min(limits.flow_loaded_threshold() + 6.0, flow + 1.4)
        gate.update(flow, 1.0)

    snapshot = gate.chain_snapshot()
    # 台账每一步都在（含掉点），步骤序号连续。
    seqs = [item["seq"] for item in snapshot["samples"]]
    assert seqs == list(range(1, len(seqs) + 1))
    assert any(item["valid"] is False for item in snapshot["samples"])
    # 打断尝试与成功尝试都归档，归档里能复盘当时的全部采样。
    results = [item["result"] for item in snapshot["attempts"]]
    assert "retried" in results and "confirmed" in results
    assert snapshot["attempts"][0]["samples"][0]["flow_kg_s"] is not None
    # 出口事件与台账同源：掉点打断一次起步尝试，阶段进入顺序仍与时序一致。
    phases = [payload["phase"] for kind, payload in events if kind == "flow_phase_entered"]
    assert phases == [STARTUP, STARTUP, LOADED, CONFIRMED]


def test_admit_requires_confirmed_flow():
    receiver = build_receiver()
    with pytest.raises(InterlockError):
        receiver.admit()
    confirm_flow(receiver)
    assert receiver.admit() == "admitted"
    assert receiver.valve.target_pct() == pytest.approx(100.0)


def test_heat_requires_admitted_header():
    receiver = build_receiver()
    with pytest.raises(InterlockError):
        receiver.heat(1000.0, 60.0, 1.0)
    assert receiver.state() == COLD


def test_heat_raises_temperature_toward_steady_state():
    receiver = build_receiver()
    limits = receiver.limits
    confirm_flow(receiver, limits.flow_min_kg_s + 16.0)
    receiver.admit()
    for _ in range(600):
        report = receiver.heat(25_000.0, limits.flow_min_kg_s + 16.0, 1.0)
    assert report["ok"] is True
    assert receiver.state() == AT_POWER
    assert 520.0 < receiver.output_temperature_c() < 600.0
    assert receiver.thermal.dry_steps() == 0


def test_low_flow_puts_ceiling_to_zero():
    receiver = build_receiver()
    limits = receiver.limits
    confirm_flow(receiver, limits.flow_min_kg_s + 16.0)
    receiver.admit()
    receiver.threshold.calibrate(58.0)
    assert receiver.ceiling_pct(58.0) == pytest.approx(100.0)
    assert receiver.ceiling_pct(20.0) == pytest.approx(0.0)


def test_derate_ceiling_interpolates_between_band_and_limit():
    receiver = build_receiver()
    limits = receiver.limits
    assert receiver.derate.ceiling_pct(limits.temp_derate_c - 1.0) == pytest.approx(100.0)
    mid = (limits.temp_derate_c + limits.temp_max_c) / 2.0
    assert receiver.derate.ceiling_pct(mid) == pytest.approx(50.0)
    assert receiver.derate.ceiling_pct(limits.temp_max_c) == pytest.approx(0.0)


def test_derate_controller_has_hysteresis():
    receiver = build_receiver()
    limits = receiver.limits
    assert receiver.derate.evaluate(limits.temp_derate_c).active is True
    receiver.derate.evaluate(limits.temp_derate_c - 1.0)
    assert receiver.derate.active() is True
    receiver.derate.evaluate(limits.temp_derate_c - 6.0)
    assert receiver.derate.active() is False
    assert receiver.derate.entries() == 1
    assert receiver.derate.clears() == 1


def test_overtemperature_trips_and_reset_requires_latch_cleared():
    receiver = build_receiver()
    limits = receiver.limits
    confirm_flow(receiver, limits.flow_min_kg_s + 16.0)
    receiver.admit()
    for _ in range(200):
        receiver.heat(120_000.0, 1.0, 1.0)
        if receiver.state() == TRIPPED:
            break
    assert receiver.state() == TRIPPED
    assert receiver.trip_reason() == "overtemperature"
    with pytest.raises(InterlockError):
        receiver.reset(latch_cleared=False)
    assert receiver.reset(latch_cleared=True) == COLD


def test_thermal_model_carries_flow_away():
    receiver = build_receiver()
    limits = receiver.limits
    confirm_flow(receiver, limits.flow_min_kg_s + 16.0)
    receiver.admit()
    dry = build_receiver()
    confirm_flow(dry, limits.flow_min_kg_s + 16.0)
    dry.admit()
    for _ in range(200):
        receiver.heat(25_000.0, 58.0, 1.0)
        dry.heat(25_000.0, 0.0, 1.0)
    assert dry.output_temperature_c() > receiver.output_temperature_c() + 100.0
    assert dry.thermal.dry_steps() == 200


def test_header_admit_and_overpressure():
    config = default_config()
    header = SaltHeader(config.salt)
    with pytest.raises(InterlockError):
        header.admit(False)
    assert header.admit(True) == "admitted"
    for _ in range(60):
        header.update(config.salt.flow_baseline_kg_s, 1, 1.0)
    assert header.overpressure() is False
    for _ in range(60):
        header.update(config.salt.flow_baseline_kg_s, 2, 1.0)
    assert header.overpressure() is True
    assert header.depressurise(60.0) < config.salt.header_pressure_max_mpa


def test_threshold_tracks_calibration_epoch():
    model = ThresholdModel(default_config().salt)
    before = model.threshold_kg_s()
    model.calibrate(70.0)
    assert model.epoch == 1
    assert model.threshold_kg_s() > before
    assert model.evaluate(70.0)["ok"] is True
    assert model.evaluate(30.0)["ok"] is False
