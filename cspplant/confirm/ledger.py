"""确认链事件流水（只追加 + 哈希链）。

控制台展示、落审计、跨重启复盘读的是同一条流水：每个事件带有序号、动作与数据，
并以前一条事件的 SHA-256 摘要为父哈希。任何一行被删改，后续哈希全部对不上，
复盘时 :meth:`verify` 会直接报出来。流水本身只追加，不提供修改/删除入口。
"""

from __future__ import annotations

import copy
import hashlib
import json

GENESIS = "0" * 64


def canonical(payload: dict) -> str:
    """把事件数据规范化成稳定字符串，保证同一输入在任何机器上哈希一致。"""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ConfirmLedger:
    """确认链事件流水。"""

    def __init__(self) -> None:
        self._events: list[dict] = []

    def append(self, action: str, ts: float, data: dict) -> dict:
        """追加一个事件，计算并写入哈希链，返回该事件的可落盘字典。"""

        if not isinstance(data, dict):
            raise ValueError("事件数据必须是映射")
        seq = len(self._events)
        body = {"seq": seq, "action": action, "ts": round(float(ts), 3), "data": copy.deepcopy(data)}
        parent = GENESIS if seq == 0 else self._events[-1]["hash"]
        digest = hashlib.sha256((canonical(body) + "|" + parent).encode("utf-8")).hexdigest()
        event = {**body, "hash": digest}
        self._events.append(event)
        return copy.deepcopy(event)

    def events(self) -> list[dict]:
        """返回全部事件（按序号升序）。"""

        return copy.deepcopy(self._events)

    def latest(self, action: str | None = None) -> dict | None:
        """返回最近一个事件，可按动作过滤。"""

        for event in reversed(self._events):
            if action is None or event["action"] == action:
                return copy.deepcopy(event)
        return None

    def verify(self) -> dict:
        """从头复算哈希链，返回完整性校验结果。"""

        parent = GENESIS
        for index, event in enumerate(self._events):
            body = {key: event[key] for key in ("seq", "action", "ts", "data")}
            expect = hashlib.sha256((canonical(body) + "|" + parent).encode("utf-8")).hexdigest()
            if event["seq"] != index:
                return {"ok": False, "at_seq": index, "reason": "seq_gap"}
            if event["hash"] != expect:
                return {"ok": False, "at_seq": index, "reason": "hash_mismatch"}
            parent = event["hash"]
        return {"ok": True, "events": len(self._events)}
