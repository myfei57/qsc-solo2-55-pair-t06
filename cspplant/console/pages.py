"""控制台页面。

四个页面各自只读一份状态：镜场页看辐照与对焦、吸热器页看温度带与降额、熔盐页
看罐位与联锁门限、告警页看闩锁与升级等级。页面不做任何写操作，动作一律走
``/api/...`` 接口，避免浏览器刷新就把现场设备带动一次。
"""

from __future__ import annotations

from html import escape

from ..app import Platform

PAGE_PATHS: tuple[str, ...] = ("/", "/receiver", "/salt", "/confirm", "/alarms")

_STATUS_TEXT = {
    "idle": "空闲",
    "running": "确认中",
    "held": "保持等人",
    "aborted": "已终止",
    "confirmed": "已确认放行",
}
_GATE_TEXT = {
    "g0_precheck": "G0 前置条件",
    "g1_startup_ramp": "G1 起步爬升趋势",
    "g2_arm_loaded": "G2 工况锁定待命",
    "g3_loaded_margin": "G3 带负荷固定裕度",
    "g4_salt_permit": "G4 通盐许可",
}
_GATE_STATE_TEXT = {"waiting": "未进入", "active": "判定中", "passed": "已通过", "blocked": "被打断"}

_STYLE = """
body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 0; background: #0f1720; color: #e6edf3; }
header { padding: 14px 20px; background: #16202b; border-bottom: 1px solid #223041; }
nav a { color: #8ecdf7; margin-right: 14px; text-decoration: none; }
main { padding: 18px 20px 40px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 18px; }
th, td { border: 1px solid #223041; padding: 6px 10px; text-align: left; font-size: 14px; }
th { background: #17222e; }
.alarm { color: #ff8080; font-weight: 600; }
.ok { color: #7ee787; }
.warn { color: #f0c674; }
"""


def _table(title: str, rows: list[tuple[str, object]]) -> str:
    """渲染一张两列数据表。"""

    body = "".join(
        f"<tr><th>{escape(str(key))}</th><td>{escape(str(value))}</td></tr>" for key, value in rows
    )
    return f"<h2>{escape(title)}</h2><table>{body}</table>"


def _shell(title: str, body: str) -> str:
    """套上页头与导航。"""

    nav = "".join(
        f'<a href="{path}">{label}</a>'
        for path, label in (
            ("/", "镜场"),
            ("/receiver", "吸热器"),
            ("/salt", "熔盐"),
            ("/confirm", "通盐确认"),
            ("/alarms", "告警"),
        )
    )
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><header><strong>{escape(title)}</strong><nav style=\"margin-top:8px\">{nav}</nav>"
        f"</header><main>{body}</main></body></html>"
    )


def _mirror_page(platform: Platform) -> str:
    """镜场页：辐照、策略、对焦与调度。"""

    status = platform.status()["field"]
    rows = [
        ("运行模式", status["mode"]),
        ("判定依据", status["reason"]),
        ("当前辐照 (W/m²)", status["dni_w_m2"]),
        ("已落盘辐照 (W/m²)", status["durable_dni_w_m2"]),
        ("采样 / 拒绝 / 落盘次数", f"{status['samples']} / {status['rejected']} / {status['persists']}"),
        ("对焦镜数", f"{status['focusing']} / {status['mirrors']['count']}"),
        ("避让镜数", status["excluded"]),
        ("手动接管镜数", status["manual"]),
        ("执行负荷 (%)", status["dispatch_pct"]),
        ("调度目标 (%)", status["dispatch_target_pct"]),
        ("避让区挡下次数", status["zone_vetoes"]),
        ("清洗窗口数", status["windows"]),
        ("热功率 (kW)", round(platform.field.thermal_power_kw(), 1)),
    ]
    return _shell("镜场控制台", _table("镜场状态", rows))


def _receiver_page(platform: Platform) -> str:
    """吸热器页：温度带、流量门控与降额。"""

    snapshot = platform.status()["receiver"]
    report = platform.temperature_report()
    rows = [
        ("状态", snapshot["state"]),
        ("出口温度 (℃)", snapshot["temperature_c"]),
        ("峰值 (℃)", snapshot["peak_c"]),
        ("目标 / 降额 / 上限 (℃)", f"{report['target_c']} / {report['derate_c']} / {report['max_c']}"),
        ("距降额余量 (℃)", report["margin_to_derate_c"]),
        ("距上限余量 (℃)", report["margin_to_max_c"]),
        ("是否降额", "是" if snapshot["derated"] else "否"),
        ("出力上限 (%)", snapshot["ceiling_pct"]),
        ("进口阀开度 (%)", snapshot["valve_pct"]),
        ("流量 (kg/s)", snapshot["flow_kg_s"]),
        ("流量确认", "已确认" if snapshot["flow_confirmed"] else "未确认"),
        ("确认累计 (s)", snapshot["hold_s"]),
        ("跳闸原因", snapshot["trip_reason"] or "-"),
    ]
    return _shell("吸热器控制台", _table("吸热器状态", rows))


def _salt_page(platform: Platform) -> str:
    """熔盐页：罐位、泵与联锁门限。"""

    salt = platform.status()["salt"]
    rows = [
        ("冷盐罐液位 (%)", f"{salt['cold_tank']['level_pct']} ({salt['cold_tank']['level_kg']} kg)"),
        ("热盐罐液位 (%)", f"{salt['hot_tank']['level_pct']} ({salt['hot_tank']['level_kg']} kg)"),
        ("冷泵流量 (kg/s)", salt["cold_pump"]["flow_kg_s"]),
        ("热泵流量 (kg/s)", salt["hot_pump"]["flow_kg_s"]),
        ("母管净流量 (kg/s)", salt["net_flow_kg_s"]),
        ("母管压力 (MPa)", salt["header"]["pressure_mpa"]),
        ("在运泵数", salt["cold_pump"]["running"] + salt["hot_pump"]["running"]),
        ("两泵对顶步数", salt["opposed_steps"]),
        ("流量基线 (kg/s)", salt["baseline_kg_s"]),
        ("联锁门限 (kg/s)", salt["threshold_kg_s"]),
        ("标定代际", salt["epoch"]),
        ("成功切换次数", salt["changeovers"]),
    ]
    return _shell("熔盐控制台", _table("熔盐回路", rows))


def _alarm_page(platform: Platform) -> str:
    """告警页：闩锁、历史与升级等级。"""

    status = platform.status()
    alarm = status["alarm"]
    rows = [
        ("闩锁状态", "已置位" if alarm["latched"] else "已复位"),
        ("跳闸来源", alarm["source"] or "-"),
        ("跳闸原因", alarm["reason"] or "-"),
        ("累计置位", alarm["trips"]),
        ("告警条数", alarm["history"]),
        ("按规则计数", alarm["by_rule"] or "-"),
    ]
    body = _table("闩锁与统计", rows)
    level_rows = [(item["rule"], f"{item['name']}（{item['occurrences']} 次）") for item in status["levels"]]
    body += _table("升级等级", level_rows)
    recent = platform.history.recent(12)
    recent_rows = [
        (f"{entry['ts']}", f"{entry['rule']} {entry['message']} = {entry['value']}") for entry in recent
    ] or [("暂无", "-")]
    body += _table("最近告警", recent_rows)
    body += _table("规则表", [(item["name"], f"{item['signal']} {'≥' if item['above'] else '≤'} {item['threshold']}") for item in platform.rules.describe()])
    return _shell("告警控制台", body)


def _confirm_page(platform: Platform) -> str:
    """通盐确认页：确认链状态、各门判据、最近一拍证据与打断记录。"""

    view = platform.status()["flow_confirm"]
    status_text = _STATUS_TEXT.get(view["status"], view["status"])
    permit = "已放行" if view["permitted"] else "未放行"
    rows = [
        ("链状态", status_text),
        ("当前门", _GATE_TEXT.get(view["current_gate"] or "-", view["current_gate"] or "-")),
        ("工况", "带负荷" if view["regime"] == "loaded" else ("起步" if view["regime"] == "startup" else "-")),
        ("通盐许可", permit),
        ("配置摘要", view["config_hash"] or "-"),
        ("已处理拍数", view["ticks"]),
    ]
    body = _table("确认链", rows)

    gate_rows: list[tuple[str, object]] = []
    for gate_id in (
        "g0_precheck",
        "g1_startup_ramp",
        "g2_arm_loaded",
        "g3_loaded_margin",
        "g4_salt_permit",
    ):
        gate = view["gates"][gate_id]
        gate_rows.append(
            (
                _GATE_TEXT[gate_id],
                f"{_GATE_STATE_TEXT.get(gate['state'], gate['state'])}｜判据 {gate['criterion'] or '-'}｜第 "
                f"{gate['attempt']} 次尝试",
            )
        )
    body += _table("各门（严格时序，不可跳）", gate_rows)

    last = view["last_tick"]
    if last is not None:
        evidence = last.get("evidence") or {}
        tick_rows: list[tuple[str, object]] = [
            ("门", _GATE_TEXT.get(last.get("gate") or "-", last.get("gate") or "-")),
            ("流量 (kg/s)", last.get("flow_kg_s")),
            ("采样有效", "是" if last.get("valid") else "否（掉点，不算通过）"),
            ("泵在运", "是" if last.get("pump_running") else "否"),
            ("联锁正常", "是" if last.get("interlock_ok") else "否"),
            ("判定", last.get("verdict")),
            ("原因", last.get("reason")),
        ]
        for key, label in (
            ("slope_kg_s2", "拟合斜率 (kg/s²)"),
            ("r2", "拟合度 R²"),
            ("threshold_kg_s", "固定裕度门限 (kg/s)"),
            ("min_headroom_kg_s", "门限上最小余量 (kg/s)"),
            ("window_s", "保持窗口 (s)"),
        ):
            if isinstance(evidence, dict) and key in evidence:
                tick_rows.append((label, evidence[key]))
        body += _table("最近一拍证据", tick_rows)

    interrupts = view["interrupts"][-8:]
    int_rows = [
        (
            f"{item['ts']} {_GATE_TEXT.get(item['gate'], item['gate'])}",
            f"{item['reason']} → {item['action']}（{item['resolved'] or '待处置'}）",
        )
        for item in reversed(interrupts)
    ] or [("暂无", "-")]
    body += _table("打断记录（退/停按阶段策略表）", int_rows)
    return _shell("通盐确认控制台", body)


_RENDERERS = {
    "/": _mirror_page,
    "/receiver": _receiver_page,
    "/salt": _salt_page,
    "/confirm": _confirm_page,
    "/alarms": _alarm_page,
}


def render_page(path: str, platform: Platform) -> str | None:
    """渲染一个页面；路径不是页面时返回 ``None``。"""

    renderer = _RENDERERS.get(path)
    if renderer is None:
        return None
    return renderer(platform)
