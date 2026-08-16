"""Execute user-supplied Python "reference function code" against the timeline.

This is the escape hatch for logic that is awkward to express declaratively
(counters, timers, hysteresis, cross-signal computation). A function spec can
carry a ``code`` field instead of ``enter``/``exit``::

    functions:
      - id: cruise_py
        name: "定速巡航 (Python)"
        code: |
          ACTIVE = "ACTIVE"          # 可选：明确哪些状态算"激活"
          INITIAL = "IDLE"           # 可选：初始状态

          def update(t, s, dt):
              # s['VehSpd'] / s.VehSpd  信号当前值（零阶保持）
              # s.prev.VehSpd           上一采样值
              # s.rising('X') / s.falling('X') / s.changed('X')
              if s.rising("CruiseSetBtn") and s.VehSpd >= 30 and s.BrakePedal == 0:
                  reason(f"SET 按下且车速 {s.VehSpd:.1f} >= 30")
                  return "ACTIVE"
              if s.BrakePedal == 1 or s.CruiseCancelBtn == 1:
                  reason(f"刹车={s.BrakePedal} 取消={s.CruiseCancelBtn}")
                  return "IDLE"
              return None               # None = 保持状态

Helpers available inside the snippet:
  ``reason(msg)``   — annotate the current tick; used as the transition summary.
  ``attempt(msg)``  — mark a blocked attempt (fires when no transition occurs).
"""
from __future__ import annotations

from typing import Any, Optional

from .analyzer import SpecError


def _truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)) and v == 0:
        return False
    s = str(v).strip().lower()
    return s not in {"", "idle", "inactive", "off", "disabled", "standby",
                     "none", "null", "false", "0", "0.0"}


def _is_active(state: Any, active_set: Optional[set]) -> bool:
    if active_set is not None:
        return state in active_set
    return _truthy(state)


def _jsonable(v: Any) -> Any:
    try:
        import json
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


class _ValueAccessor:
    """Read-only signal value accessor for a fixed time."""
    def __init__(self, store, t: float, reads: set):
        self._store = store
        self._t = t
        self._reads = reads

    def __getitem__(self, name: str):
        self._reads.add(name)
        return self._store.value_at(name, self._t)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        self._reads.add(name)
        return self._store.value_at(name, self._t)


class Signals(_ValueAccessor):
    """Current values + edge helpers, plus ``.prev`` for the previous sample."""
    def __init__(self, store, t: float, prev_t: Optional[float], reads: set):
        super().__init__(store, t, reads)
        self._prev_t = prev_t
        self.prev = _ValueAccessor(store, prev_t, reads) if prev_t is not None else self

    def _edge(self, name: str) -> tuple:
        cur = self[name]
        pv = self._store.value_at(name, self._prev_t) if self._prev_t is not None else None
        return cur, pv

    def rising(self, name: str) -> bool:
        cur, pv = self._edge(name)
        return _truthy(cur) and not _truthy(pv)

    def falling(self, name: str) -> bool:
        cur, pv = self._edge(name)
        return _truthy(pv) and not _truthy(cur)

    def changed(self, name: str) -> bool:
        cur, pv = self._edge(name)
        return cur != pv


def run_code_function(store, func: dict) -> dict:
    """Run a Python-code function over the timeline.

    Returns a dict with ``functions``/``events``/``attempts``/``intervals``
    shaped exactly like :func:`analyzer.analyze_functions` output (for one fn).
    """
    code = func.get("code")
    if not code:
        raise SpecError(f"功能 {func.get('id')!r} 缺少 code 字段")
    fid = func["id"]
    name = func.get("name", fid)

    ns: dict = {"__name__": f"__fn_{fid}", "__builtins__": __builtins__}
    captured: dict = {"reason": None, "attempt": None}

    def reason(msg):
        captured["reason"] = str(msg)

    def attempt(msg):
        captured["attempt"] = str(msg)

    ns["reason"] = reason
    ns["attempt"] = attempt
    exec(compile(code, f"<function:{fid}>", "exec"), ns)

    update = ns.get("update")
    if not callable(update):
        raise SpecError(f"Python 功能 {fid} 需要定义 update(t, s, dt) 函数")

    active_val = ns.get("ACTIVE_STATES", ns.get("ACTIVE", None))
    if active_val is None:
        active_set = None
    elif isinstance(active_val, (list, tuple, set, frozenset)):
        active_set = set(active_val)
    else:
        active_set = {active_val}

    times = store.times
    state = ns.get("INITIAL", None)
    events: list[dict] = []
    attempts: list[dict] = []
    intervals: list[list] = []
    int_start: Optional[float] = None

    prev_active = _is_active(state, active_set)
    if prev_active and len(times):
        int_start = float(times[0])

    prev_t: Optional[float] = None
    for t in times:
        ft = float(t)
        reads: set = set()
        s = Signals(store, ft, prev_t, reads)
        dt = (ft - prev_t) if prev_t is not None else 0.0
        captured["reason"] = None
        captured["attempt"] = None

        try:
            new_state = update(ft, s, dt)
        except Exception as e:  # surface the user's bug with context
            raise SpecError(f"Python 功能 {fid} 在 t={ft:.3f} 执行出错: {e!r}")
        new_state = _jsonable(new_state)

        if new_state is not None and new_state != state:
            # 真正的状态跳变（state 为 None 表示尚未确定初始状态，不记事件）
            if state is not None:
                new_active = _is_active(new_state, active_set)
                if not prev_active and new_active:
                    etype = "enter"
                elif prev_active and not new_active:
                    etype = "exit"
                else:
                    etype = "change"
                summary = captured["reason"] or f"{state} → {new_state}"
                events.append({
                    "function": fid, "type": etype, "t": ft,
                    "summary": summary, "evidence": _snapshot_evidence(reads, store, ft),
                })
                if etype == "enter":
                    int_start = ft
                elif etype == "exit" and int_start is not None:
                    intervals.append([int_start, ft])
                    int_start = None
            state = new_state
            prev_active = _is_active(state, active_set)

        # 被阻止的尝试：调用了 attempt() 但本 tick 没有发生状态跳变
        if captured["attempt"] is not None and (new_state is None or new_state == state):
            attempts.append({
                "function": fid, "t": ft,
                "trigger": captured["attempt"],
                "trigger_evidence": None,
                "satisfied": [], "blocking": [],
            })

        prev_t = ft

    if prev_active and int_start is not None and len(times):
        intervals.append([int_start, float(times[-1])])

    return {
        "functions": [{
            "id": fid, "name": name, "description": func.get("description", ""),
            "engine": "python", "enter": None, "exit": None, "trigger": None,
            "signals": sorted(set()),
        }],
        "events": events,
        "attempts": attempts,
        "intervals": {fid: intervals},
    }


def _snapshot_evidence(reads: set, store, t: float) -> dict:
    children = []
    for sig in sorted(reads):
        v = store.value_at(sig, t)
        children.append({
            "kind": "cmp", "signal": sig, "op": "==", "value": v, "actual": v,
            "ok": True, "margin": None, "text": f"{sig} = {v}",
        })
    return {"kind": "all", "ok": True, "text": "信号快照", "children": children}
