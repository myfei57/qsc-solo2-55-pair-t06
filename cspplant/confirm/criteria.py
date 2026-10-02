"""判据：爬升趋势与固定裕度。

两种判据都是纯函数，输入是最近窗口里的有效样本（时间戳、流量），输出是否达标
和每一项指标。指标全部回传，供逐拍证据落审计——复盘时要能看到当时用的是哪个
模式、斜率/拟合度/裕度各是多少，而不是只有一个布尔值。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Sample:
    """一个有效流量样本。"""

    ts: float
    flow_kg_s: float


def linear_fit(samples: list[Sample]) -> tuple[float, float]:
    """对样本做最小二乘线性拟合，返回 ``(斜率 kg/s^2, 截距)``。

    时间以首个样本为零点，避免大时间戳参与运算放大误差。
    """

    if not samples:
        return 0.0, 0.0
    t0 = samples[0].ts
    count = len(samples)
    if count == 1:
        return 0.0, samples[0].flow_kg_s
    mean_t = sum(item.ts - t0 for item in samples) / count
    mean_f = sum(item.flow_kg_s for item in samples) / count
    var_t = sum((item.ts - t0 - mean_t) ** 2 for item in samples)
    if var_t <= 0.0:
        return 0.0, mean_f
    cov = sum((item.ts - t0 - mean_t) * (item.flow_kg_s - mean_f) for item in samples)
    slope = cov / var_t
    intercept = mean_f - slope * mean_t
    return slope, intercept


def r_squared(samples: list[Sample]) -> float:
    """返回线性拟合的决定系数 R²，刻画“是否真在沿一条直线爬”。"""

    count = len(samples)
    if count < 2:
        return 0.0
    slope, intercept = linear_fit(samples)
    t0 = samples[0].ts
    mean_f = sum(item.flow_kg_s for item in samples) / count
    ss_tot = sum((item.flow_kg_s - mean_f) ** 2 for item in samples)
    if ss_tot <= 0.0:
        # 流量完全不变：只在斜率要求为 0 时才算“完美拟合”，否则视为没在爬。
        return 1.0 if slope == 0.0 else 0.0
    ss_res = sum((item.flow_kg_s - (intercept + slope * (item.ts - t0))) ** 2 for item in samples)
    value = 1.0 - ss_res / ss_tot
    # 浮点误差可能略微越界，夹回 [0, 1]。
    return max(0.0, min(1.0, value))


def evaluate_ramp(samples: list[Sample], limits) -> dict:
    """起步阶段爬升趋势判据（``RAMP_TREND``）。

    窗口必须攒满 :attr:`FlowConfirmLimits.ramp_window_s` 秒，窗口内全部为有效
    样本，且：斜率不低于最小爬升斜率、拟合度不低于门槛、末端流量达到起步下限、
    流量没有掉头向下。任何一项不满足都只是“还没判”，累计作废与否由门策略决定。
    """

    metrics = {
        "criterion": "ramp_trend",
        "window_s": 0.0,
        "samples": len(samples),
        "slope_kg_s2": 0.0,
        "r2": 0.0,
        "end_flow_kg_s": 0.0,
        "reversed": False,
        "ok": False,
        "reason": "collecting",
    }
    if not samples:
        return metrics
    t0 = samples[0].ts
    metrics["window_s"] = round(samples[-1].ts - t0, 3)
    slope, _intercept = linear_fit(samples)
    metrics["slope_kg_s2"] = round(slope, 4)
    metrics["r2"] = round(r_squared(samples), 4)
    metrics["end_flow_kg_s"] = round(samples[-1].flow_kg_s, 3)
    reversed_flow = any(
        samples[i].flow_kg_s + 1e-9 < samples[i - 1].flow_kg_s for i in range(1, len(samples))
    )
    metrics["reversed"] = reversed_flow

    if metrics["window_s"] + 1e-9 < limits.ramp_window_s:
        metrics["reason"] = "window_short"
        return metrics
    if metrics["r2"] + 1e-9 < limits.ramp_min_r2:
        metrics["reason"] = "fit_poor"
        return metrics
    if slope + 1e-9 < limits.ramp_min_slope_kg_s2:
        metrics["reason"] = "slope_low"
        return metrics
    if metrics["end_flow_kg_s"] + 1e-9 < limits.ramp_min_end_kg_s:
        metrics["reason"] = "end_flow_low"
        return metrics
    if reversed_flow:
        metrics["reason"] = "flow_reversed"
        return metrics
    metrics["ok"] = True
    metrics["reason"] = "trend_healthy"
    return metrics


def evaluate_fixed_margin(samples: list[Sample], limits) -> dict:
    """带负荷阶段固定裕度判据（``FIXED_MARGIN``）。

    保持窗口必须攒满 :attr:`FlowConfirmLimits.loaded_hold_s` 秒，窗口内每个有效
    样本都在「联锁门限 + 固定裕度」之上，且整窗拟合斜率已经走平（绝对值不超过
    :attr:`FlowConfirmLimits.loaded_slope_bound_kg_s2`）。门限之上留的裕度是
    硬配置，确认链不允许临时放宽。
    """

    threshold = limits.loaded_threshold_kg_s()
    metrics = {
        "criterion": "fixed_margin",
        "window_s": 0.0,
        "samples": len(samples),
        "threshold_kg_s": round(threshold, 3),
        "margin_kg_s": round(limits.loaded_margin_kg_s, 3),
        "min_flow_kg_s": 0.0,
        "slope_kg_s2": 0.0,
        "min_headroom_kg_s": 0.0,
        "ok": False,
        "reason": "collecting",
    }
    if not samples:
        return metrics
    flows = [item.flow_kg_s for item in samples]
    minimum = min(flows)
    slope, _intercept = linear_fit(samples)
    metrics["window_s"] = round(samples[-1].ts - samples[0].ts, 3)
    metrics["min_flow_kg_s"] = round(minimum, 3)
    metrics["slope_kg_s2"] = round(slope, 4)
    metrics["min_headroom_kg_s"] = round(minimum - threshold, 3)

    if metrics["window_s"] + 1e-9 < limits.loaded_hold_s:
        metrics["reason"] = "window_short"
        return metrics
    if minimum + 1e-9 < threshold:
        metrics["reason"] = "below_margin"
        return metrics
    if abs(slope) > limits.loaded_slope_bound_kg_s2 + 1e-9:
        metrics["reason"] = "still_climbing"
        return metrics
    metrics["ok"] = True
    metrics["reason"] = "margin_held"
    return metrics
