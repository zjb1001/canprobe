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


_PLAIN_TYPES = (str, bool, int, float, type(None))


def _jsonable(v: Any) -> Any:
    # update() 的返回值几乎总是状态名（str）或 None —— 对这些直接放行，
    # 别为每一拍都跑一次 json.dumps（500 万拍时这一项就是分钟级的开销）
    if type(v) in _PLAIN_TYPES:
        return v
    try:
        import json
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


class _ValueAccessor:
    """Read-only signal value accessor for a fixed time."""
    __slots__ = ("_store", "_t", "_reads", "_memo")

    def __init__(self, store, t: Optional[float], reads: set):
        self._store = store
        self._t = t
        self._reads = reads
        self._memo: dict = {}

    def _reset(self, t: Optional[float], reads: set) -> None:
        """换到下一拍：复用同一个对象，省掉每拍的对象构造。"""
        self._t = t
        self._reads = reads
        self._memo.clear()

    def _get(self, name: str):
        self._reads.add(name)
        memo = self._memo
        if name in memo:                    # 同一拍里 s.X 常被读好几次
            return memo[name]
        v = self._store.value_at(name, self._t)
        memo[name] = v
        return v

    def __getitem__(self, name: str):
        return self._get(name)

    def __getattr__(self, name: str):
        # __slots__ 里的属性走不到这里，所以只可能是信号名或私有名
        if name[0] == "_":
            raise AttributeError(name)
        return self._get(name)


class Signals(_ValueAccessor):
    """Current values + edge helpers, plus ``.prev`` for the previous sample."""
    __slots__ = ("_prev_t", "prev", "_prev_acc")

    def __init__(self, store, t: float, prev_t: Optional[float], reads: set):
        super().__init__(store, t, reads)
        self._prev_t = prev_t
        self._prev_acc = _ValueAccessor(store, prev_t, reads)
        self.prev = self._prev_acc if prev_t is not None else self

    def _reset(self, t: float, prev_t: Optional[float], reads: set) -> None:
        super()._reset(t, reads)
        self._prev_t = prev_t
        self._prev_acc._reset(prev_t, reads)
        self.prev = self._prev_acc if prev_t is not None else self

    def _edge(self, name: str) -> tuple:
        cur = self._get(name)
        pv = self._prev_acc._get(name) if self._prev_t is not None else None
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


def run_code_function(store, func: dict, fast: bool = False) -> dict:
    """Run a Python-code function over the timeline.

    Returns a dict with ``functions``/``events``/``attempts``/``intervals``
    shaped exactly like :func:`analyzer.analyze_functions` output (for one fn).

    ``fast=True``（可选的"快速求值"模式）只在该功能静态引用到的信号真正变化的
    时刻调 ``update()``。用户代码是黑盒，跳拍对纯电平/边沿逻辑等价，但对"按拍
    计数"或依赖固定步长的写法会改变结果 —— 所以默认是 ``False``。
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
    if fast:
        from .analyzer import change_times, function_signals
        ticks = change_times(store, function_signals(func))
        if len(ticks):
            times = ticks
    state = ns.get("INITIAL", None)
    events: list[dict] = []
    attempts: list[dict] = []
    intervals: list[list] = []
    int_start: Optional[float] = None

    prev_active = _is_active(state, active_set)
    if prev_active and len(times):
        int_start = float(times[0])

    prev_t: Optional[float] = None
    all_reads: set = set()          # 全程访问过的信号，供前端「功能→信号」使用
    # 每拍的信号访问器复用同一个对象：几百万拍时，光是构造 Signals + 两个
    # 访问器就要占掉可观的时间，而它们的状态只有 (t, prev_t, reads) 三项
    reads: set = set()
    s = Signals(store, 0.0, None, reads)
    for t in times.tolist():
        ft = t
        reads.clear()
        s._reset(ft, prev_t, reads)
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

        all_reads |= reads
        prev_t = ft

    if prev_active and int_start is not None and len(times):
        intervals.append([int_start, float(times[-1])])

    return {
        "functions": [{
            "id": fid, "name": name, "description": func.get("description", ""),
            "engine": "python", "enter": None, "exit": None, "trigger": None,
            # 运行期实际读到的信号（分支没走到的抓不着，静态提取见 analyzer.function_signals）
            "signals": sorted(all_reads | set(func.get("signals") or [])),
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
