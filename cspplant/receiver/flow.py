"""流量确认链（通盐前的硬门控）。

确认链把"流量够不够"按工况拆成两段判据，判据定死、不允许运行时切换：

* 起步阶段（泵刚起步、流量还在爬）按爬升趋势判断：在固定窗口
  ``flow_startup_window_s`` 内，流量必须总体在涨（末值相对窗口起点为净涨，
  且不允许超过 ``flow_drop_tol_kg_s`` 的明显回跌），窗口判满且末值已越过联锁
  门限才算趋势成立。泵起步时本来就够不到固定门限，只按门限+计时会在流量
  爬坡中途误确认。
* 带负荷阶段按固定裕度判断：流量必须在联锁门限之上再留
  ``flow_loaded_margin_kg_s`` 裕度，并连续保持 ``flow_confirm_s`` 秒，留了
  裕度才算数，贴着线飘的不算。

阶段时序固定为 ``idle → startup → loaded → confirmed``，不能跳步：没有
起步趋势成立，就不允许进入带负荷裕度确认。

打断与传感器掉点按阶段分别处置：

* 起步阶段趋势被破坏（流量回跌、跌破联锁门限、传感器掉点）属于部分失败，
  处置是退回去重来：本步直接回到 ``idle`` 重新爬升确认。
* 带负荷阶段裕度被破坏（流量跌破裕度门限、传感器掉点）属于部分失败，处置
  是停下来等人：进入 ``halted``，不自动重试也绝不确认，必须由人工
  :meth:`FlowGate.resume` 解除后再从 ``idle`` 重来。
* 已确认之后再掉点或跌破裕度不撤销确认（确认是通盐的一次性前提，撤销由
  运行期低流量联锁负责），但仍按 ``sensor_drop`` / ``flow_fallback`` 留痕。

每一步的采样（含传感器掉点）都留在确认链台账里并通过 ``sink`` 对外发布，
控制台展示与审计落盘读的是同一份结构；每次尝试结束都归档一条尝试记录，
事后可以按步骤序号复盘当时的全部数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..config import ReceiverLimits

# 确认阶段：严格线性时序，不允许跳步。
IDLE = "idle"
STARTUP = "startup"
LOADED = "loaded"
CONFIRMED = "confirmed"
HALTED = "halted"

PHASE_ORDER = (IDLE, STARTUP, LOADED, CONFIRMED)

# 起步阶段被打断 → 退回重来；带负荷阶段被打断 → 停下来等人。
RETRY = "retry"
WAIT_OPERATOR = "wait_operator"

# 事件类型（同时作为审计 action）。
SAMPLE = "flow_sample"
PHASE_ENTERED = "flow_phase_entered"
TREND_FAILED = "flow_trend_failed"
SENSOR_DROP = "flow_sensor_drop"
FLOW_FALLBACK = "flow_flow_fallback"
CONFIRM_HALTED = "flow_confirm_halted"
CONFIRM_DONE = "flow_confirmed"
CONFIRM_RESUME = "flow_confirm_resume"
CONFIRM_RESET = "flow_confirm_reset"

Sink = Callable[[str, dict], None]


@dataclass(frozen=True)
class ChainSample:
    """确认链上的一个采样步（传感器掉点也要留痕）。"""

    seq: int
    ts: float
    phase: str
    flow_kg_s: float | None
    valid: bool
    sufficient: bool
    above_margin: bool
    window_s: float
    net_gain_kg_s: float
    hold_s: float
    event: str

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": round(self.ts, 3),
            "phase": self.phase,
            "flow_kg_s": None if self.flow_kg_s is None else round(self.flow_kg_s, 3),
            "valid": self.valid,
            "sufficient": self.sufficient,
            "above_margin": self.above_margin,
            "window_s": round(self.window_s, 3),
            "net_gain_kg_s": round(self.net_gain_kg_s, 3),
            "hold_s": round(self.hold_s, 3),
            "event": self.event,
        }


@dataclass
class AttemptRecord:
    """一次确认尝试的归档记录。"""

    attempt: int
    started_seq: int
    ended_seq: int
    result: str
    reason: str
    phases: tuple[str, ...] = ()
    samples: list[ChainSample] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "attempt": self.attempt,
            "started_seq": self.started_seq,
            "ended_seq": self.ended_seq,
            "result": self.result,
            "reason": self.reason,
            "phases": list(self.phases),
            "samples": [sample.as_dict() for sample in self.samples],
        }


@dataclass(frozen=True)
class FlowStatus:
    """一次流量判定结果。"""

    flow_kg_s: float | None
    valid: bool
    sufficient: bool
    above_margin: bool
    confirmed: bool
    phase: str
    hold_s: float
    window_s: float
    net_gain_kg_s: float
    interrupted: bool
    resolution: str
    reason: str
    threshold_kg_s: float
    loaded_threshold_kg_s: float
    attempt: int

    def as_dict(self) -> dict:
        return {
            "flow_kg_s": None if self.flow_kg_s is None else round(self.flow_kg_s, 3),
            "valid": self.valid,
            "sufficient": self.sufficient,
            "above_margin": self.above_margin,
            "confirmed": self.confirmed,
            "phase": self.phase,
            "hold_s": round(self.hold_s, 3),
            "window_s": round(self.window_s, 3),
            "net_gain_kg_s": round(self.net_gain_kg_s, 3),
            "interrupted": self.interrupted,
            "resolution": self.resolution,
            "reason": self.reason,
            "threshold_kg_s": round(self.threshold_kg_s, 3),
            "loaded_threshold_kg_s": round(self.loaded_threshold_kg_s, 3),
            "attempt": self.attempt,
        }


class FlowGate:
    """分阶段流量确认链。"""

    def __init__(self, limits: ReceiverLimits, sink: Sink | None = None) -> None:
        self._limits = limits
        self._sink = sink
        self._phase = IDLE
        self._samples: list[ChainSample] = []
        self._attempts: list[AttemptRecord] = []
        self._phases_seen: list[str] = []
        self._attempt = 0
        self._attempt_started_seq = 1
        self._seq = 0
        self._hold_s = 0.0
        self._window_s = 0.0
        self._net_gain = 0.0
        self._last_flow: float | None = None
        self._window_base_flow: float | None = None
        self._resets = 0
        self._halts = 0
        self._confirmations = 0
        self._drops = 0

    @property
    def limits(self) -> ReceiverLimits:
        return self._limits

    def attach_sink(self, sink: Sink) -> None:
        """挂上事件出口（装配期接事件总线，使审计与确认链同源）。"""

        self._sink = sink

    # ---- 推进 ---------------------------------------------------------

    def update(
        self,
        flow_kg_s: float | None,
        dt: float,
        *,
        valid: bool | None = None,
        ts: float | None = None,
    ) -> FlowStatus:
        """推进一个采样步并返回判定结果。

        ``flow_kg_s`` 为 ``None`` 或非有限数，或 ``valid`` 显式为假，都按
        传感器掉点处理：部分失败，绝不计入达标时间。
        """

        if dt <= 0:
            raise ValueError("步长必须为正")
        moment = float(self._seq if ts is None else ts)
        reading = self._read_sensor(flow_kg_s, valid)

        outcome: tuple[bool, str, str] = (False, "", "")

        if self._phase == HALTED:
            # 停下来等人：新来的数据不参与判定，但照样留档，不许自动恢复。
            self._record_sample(reading, moment, SAMPLE)
        elif self._phase == CONFIRMED:
            # 确认一经做出即锁定；之后掉点或跌破裕度只留痕，不撤销确认，
            # 运行期保护由低流量联锁负责。
            if not reading.valid:
                self._drops += 1
                self._record_sample(reading, moment, SENSOR_DROP)
            elif not reading.above_margin:
                self._record_sample(reading, moment, FLOW_FALLBACK)
            else:
                self._record_sample(reading, moment, SAMPLE)
        elif not reading.valid:
            self._drops += 1
            outcome = self._fail(reading, moment, "sensor_drop", SENSOR_DROP)
        elif self._phase == IDLE:
            if reading.sufficient:
                self._begin_attempt()
                self._enter(STARTUP, reading, moment)
                outcome = self._evaluate_startup(reading, dt, moment)
            else:
                # 还没够到联锁门限：留档但不开尝试，窗口计数不许提前累计。
                self._record_sample(reading, moment, SAMPLE)
        elif self._phase == STARTUP:
            outcome = self._evaluate_startup(reading, dt, moment)
        elif self._phase == LOADED:
            outcome = self._evaluate_loaded(reading, dt, moment)

        interrupted, resolution, reason = outcome
        status = self._status(reading, interrupted, resolution, reason, moment)
        self._last_flow = reading.flow
        return status

    def _read_sensor(self, raw: object, valid: bool | None) -> "Reading":
        is_valid = valid is not False
        flow: float | None = None
        if is_valid:
            try:
                value = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                is_valid = False
            else:
                if value != value or value in (float("inf"), float("-inf")):
                    is_valid = False
                else:
                    flow = value
        sufficient = is_valid and self._limits.flow_sufficient(flow)
        above_margin = is_valid and flow >= self._limits.flow_loaded_threshold()
        return Reading(flow, is_valid, sufficient, above_margin)

    def _begin_attempt(self) -> None:
        self._attempt += 1
        self._attempt_started_seq = self._seq + 1
        self._phases_seen = []
        self._window_s = 0.0
        self._window_base_flow = None
        self._net_gain = 0.0

    def _evaluate_startup(self, reading: "Reading", dt: float, moment: float) -> tuple[bool, str, str]:
        """起步阶段：只按固定窗口内的爬升趋势判，不按固定裕度判。

        窗口判满且净涨为正才算趋势成立；趋势成立后继续在起步阶段等到流量
        够到裕度门限才进入带负荷阶段（阶段时序不能跳），此时还在爬坡、够不
        到裕度不算打断。趋势判据按窗口走，不靠"连续多少秒过门限"。
        """

        # 跌破联锁门限：起步阶段被打断 → 退回重来。
        if not reading.sufficient:
            return self._fail(reading, moment, "below_interlock", FLOW_FALLBACK)
        # 超过容差的明显回跌：爬升趋势被破坏 → 退回重来。
        if (
            self._last_flow is not None
            and reading.flow is not None
            and reading.flow < self._last_flow - self._limits.flow_drop_tol_kg_s
        ):
            return self._fail(reading, moment, "flow_reversed", TREND_FAILED)

        if self._window_base_flow is None:
            self._window_base_flow = reading.flow
        self._window_s += dt
        self._net_gain = reading.flow - self._window_base_flow

        if self._window_s >= self._limits.flow_startup_window_s:
            if self._net_gain <= 0.0:
                # 窗口判满但没有净涨 → 趋势不成立，退回重来。
                return self._fail(reading, moment, "flat_window", TREND_FAILED)
            if reading.above_margin:
                # 趋势成立且流量已够到裕度门限，才允许进入带负荷阶段。
                self._record_sample(reading, moment, SAMPLE)
                self._window_s = 0.0
                self._window_base_flow = None
                self._net_gain = 0.0
                self._hold_s = 0.0
                self._enter(LOADED, reading, moment)
                return (False, "", "")
        self._record_sample(reading, moment, SAMPLE)
        return (False, "", "")

    def _evaluate_loaded(self, reading: "Reading", dt: float, moment: float) -> tuple[bool, str, str]:
        """带负荷阶段：必须在裕度门限之上连续保持固定时长。"""

        if not reading.above_margin:
            # 裕度没留住 → 部分失败，停下来等人。
            return self._fail(reading, moment, "below_loaded_margin", FLOW_FALLBACK)
        self._hold_s += dt
        self._record_sample(reading, moment, SAMPLE)
        if self._hold_s >= self._limits.flow_confirm_s:
            self._finish(reading, moment)
        return (False, "", "")

    # ---- 打断与恢复 ---------------------------------------------------

    def _fail(self, reading: "Reading", moment: float, reason: str, event: str) -> tuple[bool, str, str]:
        """记录打断点并按阶段决定进退：起步退回重来，带负荷停下来等人。"""

        self._record_sample(reading, moment, event)
        if self._phase in (STARTUP, IDLE):
            resolution = RETRY
            self._archive("retried", reason)
            self._phase = IDLE
            self._hold_s = 0.0
            self._window_s = 0.0
            self._window_base_flow = None
            self._net_gain = 0.0
            self._resets += 1
        else:  # LOADED
            resolution = WAIT_OPERATOR
            self._archive("halted", reason)
            self._phase = HALTED
            self._hold_s = 0.0
            self._halts += 1
            self._emit(
                CONFIRM_HALTED,
                {
                    "reason": reason,
                    "resolution": resolution,
                    "attempt": self._attempt,
                    "loaded_threshold_kg_s": round(self._limits.flow_loaded_threshold(), 3),
                },
            )
        return True, resolution, reason

    def resume(self, operator: str = "operator") -> str:
        """人工解除"停下来等人"，从空闲重新开始一条确认链。"""

        if self._phase != HALTED:
            raise ValueError("只有停下来等人的确认链才需要人工解除")
        self._phase = IDLE
        self._hold_s = 0.0
        self._window_s = 0.0
        self._window_base_flow = None
        self._net_gain = 0.0
        self._emit(CONFIRM_RESUME, {"operator": str(operator)})
        return self._phase

    def reset(self) -> None:
        """复位确认链（跳闸复位时调用）；台账与归档保留供复盘。"""

        self._phase = IDLE
        self._hold_s = 0.0
        self._window_s = 0.0
        self._window_base_flow = None
        self._net_gain = 0.0
        self._phases_seen = []
        self._attempt = 0
        self._emit(CONFIRM_RESET, {})

    # ---- 内部记录 -----------------------------------------------------

    def _enter(self, phase: str, reading: "Reading", moment: float) -> None:
        self._phase = phase
        if not self._phases_seen or self._phases_seen[-1] != phase:
            self._phases_seen.append(phase)
        self._emit(
            PHASE_ENTERED,
            {
                "phase": phase,
                "from_phase": PHASE_ORDER[max(0, PHASE_ORDER.index(phase) - 1)],
                "attempt": self._attempt,
                "flow_kg_s": None if reading.flow is None else round(reading.flow, 3),
            },
        )

    def _finish(self, reading: "Reading", moment: float) -> None:
        self._enter(CONFIRMED, reading, moment)
        self._archive("confirmed", "trend_and_margin_held")
        self._confirmations += 1
        self._emit(
            CONFIRM_DONE,
            {
                "attempt": self._attempt,
                "flow_kg_s": round(reading.flow, 3) if reading.flow is not None else None,
                "loaded_threshold_kg_s": round(self._limits.flow_loaded_threshold(), 3),
                "hold_s": round(self._hold_s, 3),
            },
        )

    def _archive(self, result: str, reason: str) -> None:
        if not self._attempt:
            return
        started = self._attempt_started_seq
        samples = [s for s in self._samples if s.seq >= started]
        self._attempts.append(
            AttemptRecord(
                attempt=self._attempt,
                started_seq=started,
                ended_seq=self._seq,
                result=result,
                reason=reason,
                phases=tuple(self._phases_seen),
                samples=samples,
            )
        )

    def _record_sample(self, reading: "Reading", moment: float, event: str) -> None:
        self._seq += 1
        sample = ChainSample(
            seq=self._seq,
            ts=moment,
            phase=self._phase,
            flow_kg_s=reading.flow,
            valid=reading.valid,
            sufficient=reading.sufficient,
            above_margin=reading.above_margin,
            window_s=self._window_s,
            net_gain_kg_s=self._net_gain,
            hold_s=self._hold_s,
            event=event,
        )
        self._samples.append(sample)
        self._emit(event, sample.as_dict())

    def _emit(self, kind: str, payload: dict) -> None:
        if self._sink is not None:
            self._sink(kind, payload)

    def _status(
        self,
        reading: "Reading",
        interrupted: bool,
        resolution: str,
        reason: str,
        moment: float,
    ) -> FlowStatus:
        return FlowStatus(
            flow_kg_s=reading.flow,
            valid=reading.valid,
            sufficient=reading.sufficient,
            above_margin=reading.above_margin,
            confirmed=self._phase == CONFIRMED,
            phase=self._phase,
            hold_s=self._hold_s,
            window_s=self._window_s,
            net_gain_kg_s=self._net_gain,
            interrupted=interrupted,
            resolution=resolution,
            reason=reason,
            threshold_kg_s=self._limits.flow_min_kg_s,
            loaded_threshold_kg_s=self._limits.flow_loaded_threshold(),
            attempt=self._attempt,
        )

    # ---- 查询 ---------------------------------------------------------

    def confirmed(self) -> bool:
        """流量是否已经确认。"""

        return self._phase == CONFIRMED

    def phase(self) -> str:
        """返回当前确认阶段。"""

        return self._phase

    def halted(self) -> bool:
        """是否停在"等人处理"。"""

        return self._phase == HALTED

    def hold_s(self) -> float:
        """带负荷裕度累计保持时间。"""

        return self._hold_s

    def window_s(self) -> float:
        """起步趋势窗口已累计时长。"""

        return self._window_s

    def net_gain_kg_s(self) -> float:
        """起步窗口内的净涨量。"""

        return self._net_gain

    def resets(self) -> int:
        """起步阶段被打断、退回重来的次数。"""

        return self._resets

    def halts(self) -> int:
        """带负荷阶段被打断、停下来等人的次数。"""

        return self._halts

    def drops(self) -> int:
        """传感器掉点次数（确认前后都计）。"""

        return self._drops

    def confirmations(self) -> int:
        """确认链完成确认的次数。"""

        return self._confirmations

    def attempt(self) -> int:
        """当前尝试序号。"""

        return self._attempt

    def samples(self) -> list[ChainSample]:
        """全部采样步台账（含掉点与打断点）。"""

        return list(self._samples)

    def attempts(self) -> list[AttemptRecord]:
        """全部已结束尝试的归档记录。"""

        return list(self._attempts)

    def phases_seen(self) -> tuple[str, ...]:
        """当前尝试已进入过的阶段（按进入顺序，未跳步才与规定时序一致）。"""

        return tuple(self._phases_seen)

    def chain_snapshot(self, tail: int | None = None) -> dict:
        """确认链快照：控制台展示与审计取数共用这一个出口。"""

        selected = self._samples if tail is None else self._samples[-tail:]
        return {
            "phase": self._phase,
            "confirmed": self._phase == CONFIRMED,
            "halted": self._phase == HALTED,
            "attempt": self._attempt,
            "phases_seen": list(self._phases_seen),
            "hold_s": round(self._hold_s, 3),
            "window_s": round(self._window_s, 3),
            "net_gain_kg_s": round(self._net_gain, 3),
            "threshold_kg_s": round(self._limits.flow_min_kg_s, 3),
            "loaded_threshold_kg_s": round(self._limits.flow_loaded_threshold(), 3),
            "resets": self._resets,
            "halts": self._halts,
            "drops": self._drops,
            "confirmations": self._confirmations,
            "samples": [sample.as_dict() for sample in selected],
            "attempts": [record.as_dict() for record in self._attempts],
        }


@dataclass(frozen=True)
class Reading:
    """一次传感器读数的规范化结果。"""

    flow: float | None
    valid: bool
    sufficient: bool
    above_margin: bool
