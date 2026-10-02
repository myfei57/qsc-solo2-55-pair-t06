"""工况识别：起步爬升 / 带负荷。

判据要跟着工况走，工况本身必须稳定、不能在门限附近来回翻。这里用带迟滞停留的
两态识别器：泵在运转、流量越过联锁门限并保持 ``loaded_dwell_s`` 秒才进入带负荷；
回到门限以下并保持 ``startup_dwell_s`` 秒才退回起步。识别器每拍只看一个有效
样本，无效样本（传感器掉点）不推进任何停留计时。
"""

from __future__ import annotations

from . import LOADED, STARTUP


class RegimeDetector:
    """带迟滞停留的工况识别器。"""

    def __init__(self, limits) -> None:
        self._limits = limits
        self._regime = STARTUP
        self._candidate: str | None = None
        self._candidate_since: float | None = None
        self._last_ts: float | None = None

    @property
    def regime(self) -> str:
        return self._regime

    def restore(self, regime: str, dwell_s: float = 0.0) -> None:
        """按落盘证据恢复识别器内部状态（重放用，候选态已折算成停留计时）。"""

        self._regime = regime
        self._candidate = None
        self._candidate_since = None

    def update(self, ts: float, valid: bool, pump_running: bool, flow_kg_s: float) -> dict:
        """喂入一拍样本，返回本次识别结果（含停留计时，供证据展示）。"""

        if not valid or not pump_running:
            # 掉点或泵已停：工况判定证据不足，停留计时冻结，工况不翻。
            self._candidate = None
            self._candidate_since = None
            self._last_ts = ts
            return {
                "regime": self._regime,
                "valid": False,
                "pump_running": pump_running,
                "flow_kg_s": round(flow_kg_s, 3),
                "dwell_s": 0.0,
                "changed": False,
            }

        target = LOADED if flow_kg_s >= self._limits.flow_min_kg_s else STARTUP
        dwell_need = self._limits.loaded_dwell_s if target == LOADED else self._limits.startup_dwell_s
        changed = False
        dwell = 0.0
        if target == self._regime:
            self._candidate = None
            self._candidate_since = None
        else:
            if self._candidate != target:
                # 进入候选工况的这一拍算作停留的第一秒：停留要求为 N 秒时，
                # 需要连续 N 拍（而不是 N+1 拍），口径直观、可复盘。
                self._candidate = target
                self._candidate_since = ts - 1.0
            assert self._candidate_since is not None
            dwell = ts - self._candidate_since
            if dwell + 1e-9 >= dwell_need:
                self._regime = target
                changed = True
                self._candidate = None
                self._candidate_since = None
                dwell = 0.0
        self._last_ts = ts
        return {
            "regime": self._regime,
            "valid": True,
            "pump_running": pump_running,
            "flow_kg_s": round(flow_kg_s, 3),
            "dwell_s": round(dwell, 3),
            "changed": changed,
        }
