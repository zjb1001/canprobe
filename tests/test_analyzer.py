from pathlib import Path

import pytest
import yaml

from canprobe.analyzer import (
    SpecError,
    analyze_functions,
    evaluate,
    evaluate_timeline,
    referenced_signals,
)
from canprobe.dbc_loader import DbcDatabase
from canprobe.decoder import SeriesStore
from canprobe.log_parser import sniff_and_parse

ROOT = Path(__file__).resolve().parent.parent


def _store():
    dbc = DbcDatabase.load(str(ROOT / "samples" / "cruise.dbc"))
    frames = sniff_and_parse(str(ROOT / "samples" / "cruise.csv"))
    return SeriesStore(frames, dbc)


def _cur(env):
    return lambda s: env.get(s)


def _prev(env):
    return lambda s: env.get(s)


# --- condition evaluator -------------------------------------------------- #
def test_evaluate_comparison():
    ok, node = evaluate({"signal": "VehSpd", "op": ">=", "value": 30},
                        _cur({"VehSpd": 32.0}), _prev({}))
    assert ok is True
    assert node["actual"] == 32.0
    assert node["margin"] == 2.0


def test_evaluate_rising_edge():
    ok, node = evaluate({"rising": "Btn"}, _cur({"Btn": 1}), _prev({"Btn": 0}))
    assert ok is True
    ok2, _ = evaluate({"rising": "Btn"}, _cur({"Btn": 1}), _prev({"Btn": 1}))
    assert ok2 is False


def test_evaluate_falling_edge():
    ok, _ = evaluate({"falling": "Btn"}, _cur({"Btn": 0}), _prev({"Btn": 1}))
    assert ok is True


def test_evaluate_all_any_not():
    cond = {"all": [{"signal": "a", "op": ">", "value": 0},
                    {"any": [{"signal": "b", "op": "==", "value": 1},
                             {"signal": "c", "op": "==", "value": 2}]}]}
    ok, _ = evaluate(cond, _cur({"a": 1, "b": 0, "c": 2}), _prev({}))
    assert ok is True


def test_referenced_signals():
    cond = {"all": [{"rising": "Btn"}, {"signal": "VehSpd", "op": ">", "value": 0}]}
    assert referenced_signals(cond) == {"Btn", "VehSpd"}


def test_function_signals_from_code_ignores_method_calls():
    """静态提取只能认 `s.` 开头的引用，不能把 `tracks.append` 里的 s. 也算上。

    回归用：缺左边界时 `tracks.append(...)` 会凭空提取出一个叫 append 的信号，
    进而在功能按钮上报一个"当前 DBC 中不存在"的假 ⚠。
    """
    from canprobe.analyzer import function_signals
    code = (
        "def update(t, s, dt):\n"
        "    tracks.append([1])\n"
        "    items.sort()\n"
        "    obj.s.bogus\n"
        "    if s.rising('Btn') and s['MotorTemp'] > 3 and s.prev.BrakePedal:\n"
        "        return s.VehSpd\n"
    )
    assert function_signals({"id": "x", "code": code}) == [
        "BrakePedal", "Btn", "MotorTemp", "VehSpd"]


def test_invalid_condition():
    from canprobe.analyzer import normalize_condition

    try:
        normalize_condition({"signal": "x", "op": "??", "value": 1})
        assert False
    except SpecError:
        pass


# --- function analysis over the sample scenario --------------------------- #
def test_analyze_cruise_events():
    spec = yaml.safe_load((ROOT / "samples" / "function_specs" / "functions.yaml").read_text(encoding="utf-8"))
    res = analyze_functions(_store(), spec)

    events = {(e["function"], e["type"]): e for e in res["events"]}
    assert ("cruise", "enter") in events
    assert ("cruise", "exit") in events
    assert ("overtemp", "enter") in events
    assert ("overtemp", "exit") in events

    enter = events[("cruise", "enter")]
    assert enter["t"] == 8.0
    exit_ = events[("cruise", "exit")]
    assert exit_["t"] == 15.0

    # over-temp enters when temp first exceeds 120 (between 16.6 and 17)
    ot = events[("overtemp", "enter")]
    assert 16.5 < ot["t"] < 17.0


def test_analyze_blocked_attempt():
    spec = yaml.safe_load((ROOT / "samples" / "function_specs" / "functions.yaml").read_text(encoding="utf-8"))
    res = analyze_functions(_store(), spec)

    attempts = [a for a in res["attempts"] if a["function"] == "cruise"]
    assert len(attempts) == 1
    a = attempts[0]
    assert a["t"] == 5.0
    blocking = [l["signal"] for l in a["blocking"]]
    assert "VehSpd" in blocking
    # the blocked condition should show the actual speed (20 km/h)
    veh = next(l for l in a["blocking"] if l["signal"] == "VehSpd")
    assert veh["actual"] == 20.0


def test_analyze_intervals():
    spec = yaml.safe_load((ROOT / "samples" / "function_specs" / "functions.yaml").read_text(encoding="utf-8"))
    res = analyze_functions(_store(), spec)
    assert res["intervals"]["cruise"] == [[8.0, 15.0]]


def test_evaluate_timeline_watch():
    store = _store()
    res = evaluate_timeline(store, {"signal": "MotorTemp", "op": ">", "value": 120})
    assert res["transitions"]
    # first transition should be false -> true
    assert res["transitions"][0]["to"] is True
    assert 16.5 < res["transitions"][0]["t"] < 17.0


def test_enum_signal_decoded():
    store = _store()
    assert store.value_at("CruiseState", 10.0) == "ACTIVE"
    assert store.value_at("CruiseState", 2.0) == "IDLE"


def test_python_code_function():
    store = _store()
    spec = {"functions": [{
        "id": "cruise_py", "name": "巡航(Python)",
        "code": '''
ACTIVE = "ACTIVE"
INITIAL = "IDLE"
def update(t, s, dt):
    if s.rising("CruiseSetBtn"):
        if s.VehSpd >= 30 and s.BrakePedal == 0:
            reason("SET 按下且车速>=30")
            return "ACTIVE"
        else:
            attempt(f"SET 按下但车速 {s.VehSpd:.1f} < 30")
    if s.BrakePedal == 1:
        reason("刹车")
        return "IDLE"
    return None
''',
    }]}
    res = analyze_functions(store, spec)

    events = [(e["type"], round(e["t"], 3)) for e in res["events"]]
    assert ("enter", 8.0) in events
    assert ("exit", 15.0) in events
    assert res["intervals"]["cruise_py"] == [[8.0, 15.0]]

    attempts = res["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["t"] == 5.0
    assert "20.0" in attempts[0]["trigger"]


def test_python_code_function_edge_helpers():
    from canprobe.executor import Signals

    store = _store()
    reads = set()
    s = Signals(store, 5.0, 4.98, reads)
    assert s.rising("CruiseSetBtn") is True
    assert s.VehSpd == 20.0
    assert s.prev.VehSpd == pytest.approx(19.9, abs=1e-6)  # 0.1 km/h 量化
