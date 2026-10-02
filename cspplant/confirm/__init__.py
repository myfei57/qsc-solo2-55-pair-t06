"""通盐前流量确认链。

确认链回答一个问题：熔盐进吸热器之前，流量到底算不算数。旧门控只有一条
「连续够量 ``flow_confirm_s`` 秒」，泵刚起步、流量还在往上爬时会被误判通过，
真出问题又拿不出当时每一步的依据。这里把确认拆成有严格时序的几道门，判据按
工况定死并全程留痕：

- 起步阶段（``RAMP_TREND``）：不看绝对够不够量，只看爬升趋势是否健康——
  最近窗口的线性拟合斜率、拟合度与末端流量都要达标；
- 带负荷阶段（``FIXED_MARGIN``）：联锁门限之上再留固定裕度，且流量已经走平，
  连续保持 ``loaded_hold_s`` 秒才算数；
- 工况切换带迟滞停留，避免在边界来回翻门；
- 传感器单点掉点是部分失败，绝不算通过：起步阶段允许自动重试本门，带负荷阶段
  保持窗口作废并退回待命；连续掉点升级为传感器故障，停下来等人；
- 每一步都写进只追加的事件流水（哈希链），控制台与审计读同一份事件投影，
  跨重启/复盘时按事件重放，重放结果必须与现场一致；
- 任何越门、跳步请求都被拒绝，门只能按编号顺序向前。
"""

from __future__ import annotations

# 工况：起步爬升 / 已带负荷。判据按工况绑定，不允许在门内临时切换。
STARTUP = "startup"
LOADED = "loaded"

# 判据模式：起步按爬升趋势，带负荷按固定裕度。模式由门静态绑定，定死。
RAMP_TREND = "ramp_trend"
FIXED_MARGIN = "fixed_margin"

# 门编号（严格时序，不可跳）。
G0_PRECHECK = "g0_precheck"
G1_RAMP = "g1_startup_ramp"
G2_ARM = "g2_arm_loaded"
G3_MARGIN = "g3_loaded_margin"
G4_PERMIT = "g4_salt_permit"

GATE_ORDER: tuple[str, ...] = (
    G0_PRECHECK,
    G1_RAMP,
    G2_ARM,
    G3_MARGIN,
    G4_PERMIT,
)

# 每道门静态绑定的判据模式。确认链开始时随 RUN_STARTED 落审计，一次运行内不变。
GATE_CRITERIA: dict[str, str] = {
    G0_PRECHECK: "preconditions",
    G1_RAMP: RAMP_TREND,
    G2_ARM: "regime_lock",
    G3_MARGIN: FIXED_MARGIN,
    G4_PERMIT: "permit",
}

# 链状态。
IDLE = "idle"
RUNNING = "running"
HELD = "held"          # 部分失败但可恢复，停下来等人，确认链不前进
ABORTED = "aborted"    # 硬失败/人工中止，本轮作废
CONFIRMED = "confirmed"

# 门状态。
WAITING = "waiting"    # 尚未进入
ACTIVE = "active"      # 正在判定
PASSED = "passed"      # 已通过（终态，不可回退到更前面的门）
BLOCKED = "blocked"    # 本门被打断并等待重试/处置（部分失败）

# 部分失败原因码。
SENSOR_DROPOUT = "sensor_dropout"
SENSOR_FAULT = "sensor_fault"
SAMPLE_GAP = "sample_gap"
RAMP_STALLED = "ramp_stalled"
MARGIN_LOST = "margin_lost"
REGIME_BACK = "regime_back"
INTERLOCK_LOST = "interlock_lost"
PUMP_TRIP = "pump_trip"
RETRY_EXHAUSTED = "retry_exhausted"
OPERATOR_ABORT = "operator_abort"

# 被打断时的处置。
RETRY = "retry"            # 起步阶段：作废本门累计，原地重试
BACKTRACK = "backtrack"    # 带负荷阶段：保持窗口作废，退回待命门重来
HOLD = "hold"              # 停下来等人（可恢复）
ABORT = "abort"            # 终止本轮，不可自行恢复

# 事件动作名（也是审计 action）。
EVT_RUN_STARTED = "confirm_run_started"
EVT_GATE_ENTERED = "confirm_gate_entered"
EVT_TICK_EVALUATED = "confirm_tick_evaluated"
EVT_REGIME_LOCKED = "confirm_regime_locked"
EVT_GATE_PASSED = "confirm_gate_passed"
EVT_GATE_RETRY = "confirm_gate_retry"
EVT_GATE_BACKTRACK = "confirm_gate_backtrack"
EVT_CHAIN_HELD = "confirm_chain_held"
EVT_CHAIN_ABORTED = "confirm_chain_aborted"
EVT_FLOW_PERMITTED = "confirm_salt_permitted"
EVT_OPERATOR_PAUSE = "confirm_operator_pause"
EVT_CHAIN_RESUMED = "confirm_chain_resumed"
EVT_SKIP_DENIED = "confirm_skip_denied"

# 严重度。
NOTICE = "notice"
WARNING = "warning"
ALARM = "alarm"

COMPONENT = "confirm"
