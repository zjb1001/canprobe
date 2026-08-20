"""Function / state-machine analysis over decoded signal timelines.

This is the "evidence engine" that turns a CANoe-like playback into a problem
investigation aid: given declarative *functions* (enter/exit conditions over
signals), it reports

* every ENTER / EXIT event with the exact signal values that caused it,
* "blocked attempts" — moments where a trigger fired but entry was denied, with
  a per-condition breakdown (为什么没有进入 / 为什么退出),
* the active intervals of each function (for shading on the plot).

Condition expression schema (JSON/YAML)::

    # numeric / enum comparison
    {"signal": "VehSpd", "op": ">=", "value": 30}
    # edge detection
    {"rising":  "CruiseSetBtn"}
    {"falling": "CruiseSetBtn"}
    {"changed": "Mode"}
    # boolean combinators
    {"all": [ ... ]}
    {"any": [ ... ]}
    {"not": { ... }}

A *function* spec::

    {"id": "cruise", "name": "巡航", "enter": {...}, "exit": {...},
     "trigger": {...}, "initial": false}
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

OPS = {">", "<", ">=", "<=", "==", "!="}
EDGE_KEYS = {"rising", "falling", "changed"}


class SpecError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Condition helpers
# --------------------------------------------------------------------------- #
def referenced_signals(cond: dict) -> set[str]:
    if not isinstance(cond, dict):
        return set()
    names: set[str] = set()
    for key in EDGE_KEYS:
        if key in cond and isinstance(cond[key], str):
            names.add(cond[key])
    if "signal" in cond:
        names.add(cond["signal"])
    for key in ("all", "any"):
        if key in cond:
            for child in cond[key]:
                names |= referenced_signals(child)
    if "not" in cond:
        names |= referenced_signals(cond["not"])
    return names


# Python `code` 片段里的信号引用：s.rising('X') / s['X'] / s.prev['X'] / s.X / s.prev.X
# 顺序有意义——边沿助手必须排在裸属性之前，否则 `s.rising` 会被当成信号名。
# `_B` 是左边界：没有它，`tracks.append(...)` 里的 `s.append` 也会匹配上，凭空多出
# 一个叫 append 的"信号"，然后在功能按钮上报一个查无此物的 ⚠。
_B = r"(?<![A-Za-z0-9_.])"
_CODE_SIG_RE = re.compile(
    rf"{_B}s\s*\.\s*(?:rising|falling|changed)\s*\(\s*['\"](?P<edge>[^'\"]+)['\"]"
    rf"|{_B}s(?:\s*\.\s*prev)?\s*\[\s*['\"](?P<idx>[^'\"]+)['\"]\s*\]"
    rf"|{_B}s\s*\.\s*(?:prev\s*\.\s*)?(?P<attr>[A-Za-z_]\w*)"
)
_CODE_SKIP = {"prev", "rising", "falling", "changed"}


def function_signals(raw: dict) -> list[str]:
    """静态提取一个 function 声明用到的信号名。

    不需要加载日志，也不执行 ``code``。三个来源取并集：

    * 显式 ``signals: [...]`` 列表（想额外挂几个上下文信号时用）
    * 声明式 ``enter`` / ``exit`` / ``trigger`` 条件树
    * Python ``code`` 里的 ``s.X`` / ``s['X']`` / ``s.prev.X`` / ``s.rising('X')``

    code 的提取是静态正则，只能看到字面量信号名；动态拼出来的名字（如
    ``s[f"Whl{i}"]``）抓不到，这类需要在 ``signals:`` 里显式补。

    返回顺序：显式 ``signals:`` **保持书写顺序**在前，其余按字母序追加。
    功能按钮是按这个顺序往 Graphics 里加曲线的，而"请求→反馈→上下文"这种
    排法比字母序好读得多——作者写下的顺序是有意义的信息，不该被 sort 掉。
    """
    if not isinstance(raw, dict):
        return []
    ordered: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)

    explicit = raw.get("signals")
    if isinstance(explicit, (list, tuple)):
        for x in explicit:
            if x:
                add(str(x))

    found: set[str] = set()
    for key in ("enter", "exit", "trigger"):
        cond = raw.get(key)
        if not cond:
            continue
        try:
            found |= referenced_signals(normalize_condition(cond))
        except SpecError:
            continue

    code = raw.get("code")
    if isinstance(code, str) and code.strip():
        for m in _CODE_SIG_RE.finditer(code):
            n = m.group("edge") or m.group("idx") or m.group("attr")
            if n and n not in _CODE_SKIP:
                found.add(n)

    for n in sorted(found):
        add(n)
    return ordered


def spec_functions(spec: dict) -> list[dict]:
    """列出规格里的功能及其引用信号（轻量，不跑状态机、不需要日志）。"""
    if not isinstance(spec, dict):
        raise SpecError("分析规格必须是 dict")
    funcs = spec.get("functions", spec.get("function", []))
    out: list[dict] = []
    for raw in funcs or []:
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        fid = raw["id"]
        out.append({
            "id": fid,
            "name": raw.get("name", fid),
            "description": raw.get("description", ""),
            "engine": "python" if raw.get("code") else "declarative",
            "signals": function_signals(raw),
        })
    return out


def edge_leaves(cond: dict) -> list[dict]:
    """Collect all edge-predicate leaves in a condition tree."""
    if not isinstance(cond, dict):
        return []
    if any(k in cond for k in EDGE_KEYS):
        return [cond]
    out = []
    for key in ("all", "any"):
        if key in cond:
            for child in cond[key]:
                out.extend(edge_leaves(child))
    if "not" in cond:
        out.extend(edge_leaves(cond["not"]))
    return out


def normalize_condition(node: Any) -> dict:
    """Validate and normalise a condition tree."""
    if not isinstance(node, dict) or not node:
        raise SpecError(f"条件必须是 dict: {node!r}")
    if any(k in node for k in EDGE_KEYS):
        key = next(k for k in EDGE_KEYS if k in node)
        if not isinstance(node[key], str):
            raise SpecError(f"{key} 需要一个信号名: {node!r}")
        return {key: node[key]}
    if "signal" in node:
        sig = node.get("signal")
        op = node.get("op", "==")
        if op not in OPS:
            raise SpecError(f"未知操作符 {op!r}（允许 {sorted(OPS)}）")
        if "value" not in node:
            raise SpecError(f"比较条件缺少 value: {node!r}")
        return {"signal": sig, "op": op, "value": node["value"]}
    for key in ("all", "any"):
        if key in node:
            if not isinstance(node[key], list) or not node[key]:
                raise SpecError(f"{key} 需要一个非空列表: {node!r}")
            return {key: [normalize_condition(c) for c in node[key]]}
    if "not" in node:
        return {"not": normalize_condition(node["not"])}
    raise SpecError(f"无法识别的条件: {node!r}")


def _truthy(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s not in {"", "0", "0.0", "false", "off", "no", "none", "null",
                     "idle", "inactive", "disabled", "standby", "0x0", "0x00"}


def _coerce(v: Any) -> Any:
    """Normalise a value/string for comparison."""
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s, 0) if s.lower().startswith("0x") else int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return s
    return v


def _cmp(a: Any, op: str, b: Any) -> bool:
    a, b = _coerce(a), _coerce(b)
    # numeric vs numeric
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return {
            ">": lambda: a > b, "<": lambda: a < b, ">=": lambda: a >= b,
            "<=": lambda: a <= b, "==": lambda: a == b, "!=": lambda: a != b,
        }[op]()
    # mixed numeric / string: compare string forms, plus numeric fallback
    if op in ("==", "!="):
        eq = str(a) == str(b)
        if not eq:
            try:
                eq = float(a) == float(b)
            except (TypeError, ValueError):
                pass
        return eq if op == "==" else not eq
    # ordering on non-numeric: compare strings
    try:
        return {"<": str(a) < str(b), ">": str(a) > str(b),
                "<=": str(a) <= str(b), ">=": str(a) >= str(b)}[op]()
    except Exception:
        return False


def _margin(a: Any, op: str, b: Any) -> Optional[float]:
    if op in ("==", "!="):
        return None
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if op in (">", ">="):
        return fa - fb
    if op in ("<", "<="):
        return fb - fa
    return None


# --------------------------------------------------------------------------- #
# Evaluator
# --------------------------------------------------------------------------- #
def evaluate(cond: dict, cur: Callable[[str], Any], prev: Callable[[str], Any]):
    """Evaluate a condition, returning ``(ok, evidence_node)``."""
    if not isinstance(cond, dict):
        return False, {"ok": False, "kind": "invalid", "text": str(cond)}

    for key in EDGE_KEYS:
        if key in cond:
            sig = cond[key]
            c, p = cur(sig), prev(sig)
            if key == "rising":
                ok = _truthy(c) and not _truthy(p)
                detail = f"{p!r} → {c!r}"
            elif key == "falling":
                ok = _truthy(p) and not _truthy(c)
                detail = f"{p!r} → {c!r}"
            else:  # changed
                ok = (c is not None or p is not None) and c != p
                detail = f"{p!r} → {c!r}"
            label = {"rising": "上升沿", "falling": "下降沿", "changed": "变化"}[key]
            return ok, {"ok": ok, "kind": key, "signal": sig,
                        "text": f"{sig} {label}", "detail": detail}

    if "signal" in cond:
        sig, op, thr = cond["signal"], cond["op"], cond["value"]
        actual = cur(sig)
        ok = _cmp(actual, op, thr)
        margin = _margin(actual, op, thr)
        if actual is None:
            text = f"{sig} {op} {thr} (无数据)"
        else:
            text = f"{sig} {op} {thr} (实际 {actual})"
        return ok, {"ok": ok, "kind": "cmp", "signal": sig, "op": op,
                    "value": thr, "actual": actual, "margin": margin, "text": text}

    if "all" in cond:
        nodes = [evaluate(c, cur, prev) for c in cond["all"]]
        ok = all(n[0] for n in nodes)
        return ok, {"ok": ok, "kind": "all", "text": "全部满足" if ok else "未全部满足",
                    "children": [n[1] for n in nodes]}

    if "any" in cond:
        nodes = [evaluate(c, cur, prev) for c in cond["any"]]
        ok = any(n[0] for n in nodes)
        return ok, {"ok": ok, "kind": "any", "text": "任一满足" if ok else "无一满足",
                    "children": [n[1] for n in nodes]}

    if "not" in cond:
        inner_ok, inner = evaluate(cond["not"], cur, prev)
        ok = not inner_ok
        return ok, {"ok": ok, "kind": "not", "text": "取反",
                    "children": [inner]}

    return False, {"ok": False, "kind": "invalid", "text": str(cond)}


def flatten_leaves(node: dict) -> list[dict]:
    """Flatten an evidence tree to its leaf nodes."""
    if node.get("kind") in ("cmp", "rising", "falling", "changed", "invalid"):
        return [node]
    return [leaf for c in node.get("children", []) for leaf in flatten_leaves(c)]


def _derive_trigger(enter: dict) -> Optional[dict]:
    edges = edge_leaves(enter)
    if not edges:
        return None
    return {"any": edges} if len(edges) > 1 else edges[0]


# --------------------------------------------------------------------------- #
# 时间轴分段
#
# 条件求值的输入只有「被引用信号的当前值 + 上一拍值」。信号是零阶保持的，所以
# 在两个采样点之间取值恒定 —— 一条 10 ms 周期的报文，在 500 万帧的日志里只有
# 几万个真正的变化点，其余几百万拍的输入和上一拍**一模一样**。
#
# 逐拍 evaluate() 因此是纯粹的重复劳动：把时间轴切成「取值恒定」的分段后，
# 每段只求值一次，段内其余拍用缓存结果重放状态机（见 analyze_functions）。
# 重放保证事件与 attempts 的条数、时刻与逐拍求值完全一致。
# --------------------------------------------------------------------------- #
def change_times(store, sigs) -> "Any":
    """被引用信号真正有新采样的时刻（升序去重）。"""
    import numpy as np

    marks = []
    for s in sigs:
        ax = store.series(s)[0]
        if len(ax):
            marks.append(np.asarray(ax))
    if not marks:
        return np.array([], dtype=float)
    return np.unique(np.concatenate(marks))


def constant_runs(store, sigs):
    """把时间轴切成取值恒定的分段。

    产出 ``(t, cur, rest)``：``t`` 是分段起点，``cur`` 是该段内恒定的
    ``{信号: 值}``，``rest`` 是段内其余时间戳。等价于逐拍
    ``{s: store.value_at(s, t) for s in sigs}``，但不做百万次 bisect ——
    每个采样点只在它所属分段的起点更新一次。
    """
    import numpy as np

    times = store.times
    n = len(times)
    if not n:
        return
    sigs = sorted(sigs)
    axes = {s: store.series(s) for s in sigs}

    marks = [np.asarray(ax) for ax, _ in axes.values() if len(ax)]
    if marks:
        cp = np.unique(np.concatenate(marks))
        bidx = np.searchsorted(times, cp, side="left")
        bounds = np.unique(np.concatenate(([0], bidx[bidx < n])))
    else:
        bounds = np.array([0])

    bt = times[bounds]
    updates: list[list] = [[] for _ in range(len(bounds))]
    for s in sigs:
        ax, vals = axes[s]
        if not len(ax):
            continue
        # 每个采样点落在它自己那个分段的起点上；同一时刻的重复采样后者覆盖前者，
        # 与 value_at 的 bisect_right-1 语义一致
        bi = np.searchsorted(bt, np.asarray(ax), side="right") - 1
        for k, b in enumerate(bi.tolist()):
            if b >= 0:
                updates[b].append((s, vals[k]))

    cur = {s: None for s in sigs}
    blist = bounds.tolist()
    for i, b in enumerate(blist):
        for s, v in updates[i]:
            cur[s] = v
        end = blist[i + 1] if i + 1 < len(blist) else n
        yield float(times[b]), dict(cur), times[b + 1:end]


# --------------------------------------------------------------------------- #
# Function analysis
# --------------------------------------------------------------------------- #
def _summarize(node: dict) -> str:
    if node.get("kind") == "cmp":
        return f"{node['signal']} {node['op']} {node['value']} → {node.get('actual')}"
    if node.get("kind") in EDGE_KEYS:
        return f"{node['signal']} {node['text']}"
    if node.get("kind") in ("all", "any"):
        return "、".join(_summarize(c) for c in node.get("children", []))
    if node.get("kind") == "not":
        return "NOT(" + _summarize(node.get("children", [{}])[0]) + ")"
    return str(node.get("text", ""))


def analyze_functions(store, spec: dict, fast: bool = False) -> dict:
    """Run function analysis over a :class:`SeriesStore`.

    ``spec`` looks like ``{"functions": [ {id,name,enter,exit?,trigger?,initial?} ]}``.

    ``fast=True`` 时只在被引用信号真正变化的时刻求值（可选的"快速求值"模式）。
    默认 ``False`` 逐拍求值，输出与逐拍遍历时间轴完全一致。
    """
    if not isinstance(spec, dict):
        raise SpecError("分析规格必须是 dict")
    funcs = spec.get("functions", spec.get("function", []))
    if not funcs:
        raise SpecError("规格中没有 functions 列表")

    result: dict[str, list] = {"functions": [], "events": [], "attempts": [], "intervals": {}}
    times = store.times

    for raw in funcs:
        if not isinstance(raw, dict) or "id" not in raw:
            raise SpecError(f"每个 function 需要 id 字段: {raw!r}")
        fid = raw["id"]

        # 带 code 字段 → 直接执行用户 Python 参考功能代码
        if raw.get("code"):
            from .executor import run_code_function  # 惰性导入避免循环依赖
            r = run_code_function(store, raw, fast=fast)
            result["functions"].extend(r["functions"])
            result["events"].extend(r["events"])
            result["attempts"].extend(r["attempts"])
            result["intervals"].update(r["intervals"])
            continue

        enter = normalize_condition(raw["enter"])
        exit_cond = normalize_condition(raw["exit"]) if raw.get("exit") else None
        trigger = normalize_condition(raw["trigger"]) if raw.get("trigger") else _derive_trigger(enter)

        sigs = referenced_signals(enter) | referenced_signals(exit_cond or {}) | referenced_signals(trigger or {})

        prev: dict[str, Any] = {}
        active = bool(raw.get("initial", False))
        intervals: list[list] = []
        interval_start: Optional[float] = None
        if active:
            interval_start = float(times[0]) if len(times) else None

        events, attempts = result["events"], result["attempts"]

        def step(t: float, enter_ok, enter_ev, exit_ok, exit_ev, trig) -> None:
            """状态机的一拍。``trig`` 是延迟求值的 (ok, evidence) 取值函数。"""
            nonlocal active, interval_start
            if active and exit_ok:
                active = False
                if interval_start is not None:
                    intervals.append([interval_start, t])
                events.append({
                    "function": fid, "type": "exit", "t": t,
                    "summary": _summarize(exit_ev), "evidence": exit_ev,
                })
            elif not active and enter_ok:
                active = True
                interval_start = t
                events.append({
                    "function": fid, "type": "enter", "t": t,
                    "summary": _summarize(enter_ev), "evidence": enter_ev,
                })

            # blocked attempts: trigger fired but entry not granted
            if not active and not enter_ok and trigger is not None:
                trig_ok, trig_ev = trig()
                if trig_ok:
                    leaves = flatten_leaves(enter_ev)
                    attempts.append({
                        "function": fid, "t": t,
                        "trigger": _summarize(trig_ev),
                        "trigger_evidence": trig_ev,
                        "satisfied": [l for l in leaves if l.get("ok")],
                        "blocking": [l for l in leaves if not l.get("ok")],
                    })

        for t0, cur, rest in constant_runs(store, sigs):
            get_cur = lambda s, _cur=cur: _cur.get(s)
            get_prev = lambda s, _prev=prev: _prev.get(s)

            enter_ok, enter_ev = evaluate(enter, get_cur, get_prev)
            exit_ok, exit_ev = (evaluate(exit_cond, get_cur, get_prev)
                                if exit_cond is not None else (False, None))
            step(t0, enter_ok, enter_ev, exit_ok, exit_ev,
                 lambda: evaluate(trigger, get_cur, get_prev))
            prev = cur

            if fast or not len(rest):
                continue

            # 段内其余拍：cur 与 prev 相同，输入完全一致，只需求值一次后重放。
            # 一旦出现「状态没变且没产出任何行」的不动点，后面每一拍都会一样，
            # 直接跳到段尾 —— 这是把百万次 evaluate() 压成几万次的地方。
            e_ok, e_ev = evaluate(enter, get_cur, get_cur)
            x_ok, x_ev = (evaluate(exit_cond, get_cur, get_cur)
                          if exit_cond is not None else (False, None))
            cached_trig: list = []

            def same_trig(_c=get_cur):
                if not cached_trig:
                    cached_trig.append(evaluate(trigger, _c, _c))
                return cached_trig[0]

            for t in rest.tolist():
                mark = (active, len(events), len(attempts))
                step(float(t), e_ok, e_ev, x_ok, x_ev, same_trig)
                if mark == (active, len(events), len(attempts)):
                    break

        if active and interval_start is not None and len(times):
            intervals.append([interval_start, float(times[-1])])

        result["functions"].append({
            "id": fid,
            "name": raw.get("name", fid),
            "description": raw.get("description", ""),
            "enter": enter,
            "exit": exit_cond,
            "trigger": trigger,
            "signals": sorted(sigs),
        })
        result["intervals"][fid] = intervals

    result["events"].sort(key=lambda e: e["t"])
    result["attempts"].sort(key=lambda a: a["t"])
    return result


# --------------------------------------------------------------------------- #
# Generic condition timeline (watches / arbitrary expressions)
# --------------------------------------------------------------------------- #
def evaluate_timeline(store, cond: dict) -> dict:
    """Evaluate an arbitrary expression over the timeline.

    Returns true/false transitions plus the active intervals.
    """
    cond = normalize_condition(cond)
    sigs = referenced_signals(cond)
    times = store.times
    prev: dict[str, Any] = {}
    transitions: list[dict] = []
    intervals: list[list] = []
    state: Optional[bool] = None
    start: Optional[float] = None

    # 与 analyze_functions 同理：取值恒定的分段内只求值一次。这里的状态机更简单
    # ——段内第二拍起输入完全相同，最多再翻转一次，之后必然稳定。
    for t0, cur, rest in constant_runs(store, sigs):
        get_cur = lambda s, _cur=cur: _cur.get(s)
        get_prev = lambda s, _prev=prev: _prev.get(s)
        ok, ev = evaluate(cond, get_cur, get_prev)
        if state is None:
            state = ok
            start = t0
        elif ok != state:
            transitions.append({"t": t0, "to": ok, "summary": _summarize(ev), "evidence": ev})
            if state:
                intervals.append([start, t0])
            state = ok
            start = t0
        prev = cur

        if not len(rest):
            continue
        ok2, ev2 = evaluate(cond, get_cur, get_cur)
        if ok2 != state:
            t = float(rest[0])
            transitions.append({"t": t, "to": ok2, "summary": _summarize(ev2), "evidence": ev2})
            if state:
                intervals.append([start, t])
            state = ok2
            start = t

    if state and start is not None and len(times):
        intervals.append([start, float(times[-1])])
    return {"transitions": transitions, "intervals": intervals, "signals": sorted(sigs)}
