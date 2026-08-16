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


def analyze_functions(store, spec: dict) -> dict:
    """Run function analysis over a :class:`SeriesStore`.

    ``spec`` looks like ``{"functions": [ {id,name,enter,exit?,trigger?,initial?} ]}``.
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
            r = run_code_function(store, raw)
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

        for t in times:
            cur = {s: store.value_at(s, float(t)) for s in sigs}
            get_cur = lambda s, _cur=cur: _cur.get(s)
            get_prev = lambda s, _prev=prev: _prev.get(s)

            enter_ok, enter_ev = evaluate(enter, get_cur, get_prev)
            exit_ok = False
            exit_ev = None
            if exit_cond is not None:
                exit_ok, exit_ev = evaluate(exit_cond, get_cur, get_prev)

            if active and exit_ok:
                active = False
                if interval_start is not None:
                    intervals.append([interval_start, float(t)])
                result["events"].append({
                    "function": fid, "type": "exit", "t": float(t),
                    "summary": _summarize(exit_ev), "evidence": exit_ev,
                })
            elif not active and enter_ok:
                active = True
                interval_start = float(t)
                result["events"].append({
                    "function": fid, "type": "enter", "t": float(t),
                    "summary": _summarize(enter_ev), "evidence": enter_ev,
                })

            # blocked attempts: trigger fired but entry not granted
            if not active and not enter_ok and trigger is not None:
                trig_ok, trig_ev = evaluate(trigger, get_cur, get_prev)
                if trig_ok:
                    leaves = flatten_leaves(enter_ev)
                    result["attempts"].append({
                        "function": fid, "t": float(t),
                        "trigger": _summarize(trig_ev),
                        "trigger_evidence": trig_ev,
                        "satisfied": [l for l in leaves if l.get("ok")],
                        "blocking": [l for l in leaves if not l.get("ok")],
                    })

            prev = cur

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

    for t in times:
        cur = {s: store.value_at(s, float(t)) for s in sigs}
        get_cur = lambda s, _cur=cur: _cur.get(s)
        get_prev = lambda s, _prev=prev: _prev.get(s)
        ok, ev = evaluate(cond, get_cur, get_prev)
        if state is None:
            state = ok
            start = float(t)
        elif ok != state:
            transitions.append({"t": float(t), "to": ok, "summary": _summarize(ev), "evidence": ev})
            if state:
                intervals.append([start, float(t)])
            state = ok
            start = float(t)
        prev = cur

    if state and start is not None and len(times):
        intervals.append([start, float(times[-1])])
    return {"transitions": transitions, "intervals": intervals, "signals": sorted(sigs)}
