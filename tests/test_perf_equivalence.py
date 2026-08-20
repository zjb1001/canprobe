"""性能优化的等价性回归。

每一条快路都必须和它替换掉的朴素实现给出**完全一样**的结果，否则就不是优化
而是改行为。这里把三条快路各自钉住：

* ``SeriesStore.from_arrays`` vs 逐 ``Frame`` 构造
* 向量化抽位解码 vs 逐帧 ``cantools.decode_message``
* 分段重放的 ``analyze_functions`` vs 逐时间戳朴素求值
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from canprobe import fast_decode
from canprobe.analyzer import (
    _derive_trigger,
    _summarize,
    analyze_functions,
    evaluate,
    flatten_leaves,
    normalize_condition,
    referenced_signals,
)
from canprobe.dbc_loader import DbcDatabase
from canprobe.decoder import SeriesStore
from canprobe.log_parser import Frame

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
BIG_DBC = os.path.join(SAMPLES, "03_CCAN_EP_v2.1.0_20260417-MOD.dbc")
BIG_LOG = os.path.join(SAMPLES, "20260818_EP35_signal.blf")


# --------------------------------------------------------------------------- #
# 朴素参考实现：优化前 analyze_functions 声明式分支的原样搬运
# --------------------------------------------------------------------------- #
def naive_analyze(store, funcs: list[dict]) -> dict:
    result: dict = {"events": [], "attempts": [], "intervals": {}}
    times = store.times
    for raw in funcs:
        fid = raw["id"]
        enter = normalize_condition(raw["enter"])
        exit_cond = normalize_condition(raw["exit"]) if raw.get("exit") else None
        trigger = (normalize_condition(raw["trigger"]) if raw.get("trigger")
                   else _derive_trigger(enter))
        sigs = (referenced_signals(enter) | referenced_signals(exit_cond or {})
                | referenced_signals(trigger or {}))

        prev: dict = {}
        active = bool(raw.get("initial", False))
        intervals: list = []
        interval_start = float(times[0]) if (active and len(times)) else None

        for t in times:
            cur = {s: store.value_at(s, float(t)) for s in sigs}
            get_cur = lambda s, _c=cur: _c.get(s)
            get_prev = lambda s, _p=prev: _p.get(s)
            enter_ok, enter_ev = evaluate(enter, get_cur, get_prev)
            exit_ok, exit_ev = (evaluate(exit_cond, get_cur, get_prev)
                                if exit_cond is not None else (False, None))
            if active and exit_ok:
                active = False
                if interval_start is not None:
                    intervals.append([interval_start, float(t)])
                result["events"].append({"function": fid, "type": "exit", "t": float(t),
                                         "summary": _summarize(exit_ev), "evidence": exit_ev})
            elif not active and enter_ok:
                active = True
                interval_start = float(t)
                result["events"].append({"function": fid, "type": "enter", "t": float(t),
                                         "summary": _summarize(enter_ev), "evidence": enter_ev})
            if not active and not enter_ok and trigger is not None:
                trig_ok, trig_ev = evaluate(trigger, get_cur, get_prev)
                if trig_ok:
                    leaves = flatten_leaves(enter_ev)
                    result["attempts"].append({
                        "function": fid, "t": float(t),
                        "trigger": _summarize(trig_ev), "trigger_evidence": trig_ev,
                        "satisfied": [x for x in leaves if x.get("ok")],
                        "blocking": [x for x in leaves if not x.get("ok")],
                    })
            prev = cur

        if active and interval_start is not None and len(times):
            intervals.append([interval_start, float(times[-1])])
        result["intervals"][fid] = intervals
    result["events"].sort(key=lambda e: e["t"])
    result["attempts"].sort(key=lambda a: a["t"])
    return result


def _blob(x) -> str:
    return json.dumps(x, sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# 合成日志：把各种边界情形都塞进来（枚举、连续量、长时间不变、同刻多帧）
# --------------------------------------------------------------------------- #
DBC_TEXT = """VERSION ""

NS_ :

BS_:

BU_: ECU

BO_ 100 Ctl: 2 ECU
 SG_ Btn : 0|1@1+ (1,0) [0|1] "" ECU
 SG_ Mode : 8|4@1+ (1,0) [0|15] "" ECU

BO_ 200 Meas: 2 ECU
 SG_ Speed : 0|16@1+ (0.1,-50) [0|100] "km/h" ECU

VAL_ 100 Mode 0 "OFF" 1 "READY" 2 "RUN" ;
"""


@pytest.fixture(scope="module")
def synth_store(tmp_path_factory):
    d = tmp_path_factory.mktemp("dbc")
    p = d / "s.dbc"
    p.write_text(DBC_TEXT, encoding="utf-8")
    dbc = DbcDatabase.load(str(p))

    frames = []
    t = 0.0
    for i in range(400):
        btn = 1 if (i // 7) % 3 == 0 else 0
        mode = (i // 11) % 3
        frames.append(Frame(t=t, frame_id=100, data=bytes([btn, mode])))
        if i % 2 == 0:                      # 采样率不同 → 制造大量"取值不变"的拍
            spd = 500 + (i % 60) * 7
            frames.append(Frame(t=t + 0.001, frame_id=200,
                                data=bytes([spd & 0xFF, spd >> 8])))
        t += 0.01
    # 同一时刻的重复帧：value_at 取后者，分段起点也必须取后者
    frames.append(Frame(t=t, frame_id=100, data=bytes([1, 2])))
    frames.append(Frame(t=t, frame_id=100, data=bytes([0, 1])))
    return SeriesStore(frames, dbc)


SPECS = [
    {"id": "f_edge", "enter": {"rising": "Btn"}, "exit": {"falling": "Btn"}},
    {"id": "f_level", "enter": {"signal": "Speed", "op": ">=", "value": 5},
     "exit": {"signal": "Speed", "op": "<", "value": 2}},
    {"id": "f_enum", "enter": {"signal": "Mode", "op": "==", "value": "RUN"},
     "exit": {"signal": "Mode", "op": "==", "value": "OFF"}},
    {"id": "f_mixed", "initial": True,
     "enter": {"all": [{"rising": "Btn"}, {"signal": "Speed", "op": ">", "value": 3}]},
     "exit": {"any": [{"changed": "Mode"}, {"not": {"signal": "Speed", "op": ">", "value": 0}}]},
     # 电平型 trigger：会让每一拍都产出一条 attempts，专门压测重放不能提前收敛
     "trigger": {"signal": "Speed", "op": ">=", "value": 0}},
    {"id": "f_notrig", "enter": {"signal": "Mode", "op": "!=", "value": "OFF"}},
]


@pytest.mark.parametrize("spec", SPECS, ids=[s["id"] for s in SPECS])
def test_declarative_matches_naive(synth_store, spec):
    fast = analyze_functions(synth_store, {"functions": [spec]})
    ref = naive_analyze(synth_store, [spec])
    assert _blob(fast["events"]) == _blob(ref["events"])
    assert _blob(fast["attempts"]) == _blob(ref["attempts"])
    assert _blob(fast["intervals"]) == _blob(ref["intervals"])


def test_declarative_matches_naive_all_at_once(synth_store):
    fast = analyze_functions(synth_store, {"functions": SPECS})
    ref = naive_analyze(synth_store, SPECS)
    for key in ("events", "attempts", "intervals"):
        assert _blob(fast[key]) == _blob(ref[key]), key


def test_from_arrays_matches_frame_path(synth_store):
    a = synth_store
    b = SeriesStore.from_arrays(a._ts, a._ids, a._dlc, a._data, a._channels,
                                list(a.events()), a.dbc)
    assert np.array_equal(a._ts, b._ts)
    assert np.array_equal(a._ids, b._ids)
    assert np.array_equal(a._data, b._data)
    assert a.start_time == b.start_time and a.end_time == b.end_time
    assert np.array_equal(a.times, b.times)
    for s in a.signal_names():
        ta, va = a.series(s)
        tb, vb = b.series(s)
        assert np.array_equal(ta, tb) and list(va) == list(vb), s


def test_from_arrays_window_and_sort():
    """乱序输入 + 时间窗：两条路都要给出同一份稳定排序的结果。"""
    frames = [Frame(t=t, frame_id=1, data=bytes([i]))
              for i, t in enumerate([0.5, 0.1, 0.3, 0.1, 0.9, 0.7])]
    ref = SeriesStore(frames, None, t0=0.2, t1=0.8)
    ts = np.array([f.t for f in frames], np.float64)
    ids = np.ones(len(frames), np.uint32)
    dlc = np.ones(len(frames), np.uint8)
    data = np.array([[i] for i in range(len(frames))], np.uint8)
    ch = np.zeros(len(frames), np.uint8)
    got = SeriesStore.from_arrays(ts, ids, dlc, data, ch, [], None, t0=0.2, t1=0.8)
    assert np.array_equal(ref._ts, got._ts)
    assert np.array_equal(ref._data, got._data)


# --------------------------------------------------------------------------- #
# BLF 数组直通 vs iter_frames：时间窗 / 帧数上限必须裁出一样多的帧
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not os.path.exists(BIG_LOG), reason="需要样例 BLF")
@pytest.mark.parametrize("max_frames", [1, 7, 1000, 50_000, None])
def test_blf_array_path_max_frames_matches_iter(max_frames):
    from canprobe.log_parser import iter_frames
    from canprobe.store import Project

    p = Project()
    got = p.load_log(BIG_LOG, max_frames=max_frames)
    assert p._arrays is not None, "应当走向量化数组通道"
    ref = list(iter_frames(BIG_LOG, max_frames=max_frames))
    assert got["frame_count"] == len(ref)


@pytest.mark.skipif(not os.path.exists(BIG_LOG), reason="需要样例 BLF")
def test_blf_array_path_time_window_matches_iter():
    from canprobe.log_parser import iter_frames
    from canprobe.store import Project

    probe = Project()
    probe.load_log(BIG_LOG)
    t0, t1 = probe.store.start_time, probe.store.end_time
    a, b = t0 + (t1 - t0) * 0.2, t0 + (t1 - t0) * 0.35

    p = Project()
    got = p.load_log(BIG_LOG, t0=a, t1=b)
    ref = list(iter_frames(BIG_LOG, t0=a, t1=b))
    assert got["frame_count"] == len(ref)
    assert got["start"] == min(f.t for f in ref)
    assert got["end"] == max(f.t for f in ref)


# --------------------------------------------------------------------------- #
# 向量化抽位 vs cantools：拿真实 DBC 的每一条报文对拍
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not os.path.exists(BIG_DBC), reason="需要样例 DBC")
def test_vector_decode_matches_cantools():
    dbc = DbcDatabase.load(BIG_DBC)
    rng = np.random.default_rng(20260820)
    checked = skipped = 0
    for mid, meta in sorted(dbc.messages.items()):
        dec = fast_decode.build(dbc, mid)
        if dec is None:
            skipped += 1
            continue
        rows = rng.integers(0, 256, size=(24, dec.length), dtype=np.uint8)
        got = {p.name: dec.signal_values(rows, p) for p in dec.plans}
        for i in range(len(rows)):
            want = dbc.decode(mid, rows[i].tobytes())
            assert want, f"{meta.name} 参照解码失败"
            for name, values in got.items():
                v, w = values[i], want.get(name)
                assert type(v) is type(w), f"{meta.name}.{name} 类型不符: {type(v)} != {type(w)}"
                assert v == w, f"{meta.name}.{name} 取值不符: {v!r} != {w!r}"
        checked += 1
    assert checked > 50, f"只对拍了 {checked} 条报文（跳过 {skipped}）"


@pytest.mark.skipif(not (os.path.exists(BIG_DBC) and os.path.exists(BIG_LOG)),
                    reason="需要样例 DBC + BLF")
def test_store_series_matches_per_frame_decode():
    """整仓层面再验一次：每个信号的序列必须和逐帧解码逐点相同。"""
    from canprobe.store import Project

    p = Project()
    p.load_dbc(BIG_DBC)
    p.load_log(BIG_LOG)
    st = p.store
    ids = sorted(st.decodable_message_ids())[:12]
    assert ids
    for mid in ids:
        idx = st._indices_for_id(mid)[:150]
        ref: dict[str, list] = {}
        ref_t: dict[str, list] = {}
        for i in idx:
            d = st.dbc.decode(mid, st._frame_bytes(int(i)))
            if not d:
                continue
            for k, v in d.items():
                ref.setdefault(k, []).append(v)
                ref_t.setdefault(k, []).append(float(st._ts[i]))
        for name, want in ref.items():
            t, v = st.series(name)
            assert list(v[:len(want)]) == want, f"0x{mid:x}.{name}"
            assert list(t[:len(want)]) == ref_t[name], f"0x{mid:x}.{name} 时间轴"
