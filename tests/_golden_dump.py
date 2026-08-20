"""对拍工具：把「解析 → 解码 → 分析」的结果 dump 成一份稳定的 JSON。

性能优化只允许改实现，不允许改结果。用法::

    python tests/_golden_dump.py before.json      # 优化前
    python tests/_golden_dump.py after.json       # 优化后
    python tests/_golden_dump.py --diff before.json after.json

Dump 内容覆盖每一层的输出：帧数组（哈希）、逐信号序列、window_series/value_at、
以及 analyze_functions 的 events / attempts / intervals 全文。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import yaml  # noqa: E402

from canprobe.analyzer import analyze_functions  # noqa: E402
from canprobe.store import Project  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")

CASES = [
    ("ep35_signal", "03_CCAN_EP_v2.1.0_20260417-MOD.dbc",
     "20260818_EP35_signal.blf", "functions_hba.yaml"),
    ("wild_hba", "03_CCAN_EP_v2.1.0_20260417-MOD.dbc",
     "Wild-HBA.blf", "functions_hba.yaml"),
    ("cruise", "cruise.dbc", "cruise.csv", "functions.yaml"),
    ("cruise_py", "cruise.dbc", "cruise.csv", "functions_py.yaml"),
]


def _round(x, nd=9):
    if isinstance(x, float):
        return round(x, nd)
    if isinstance(x, list):
        return [_round(v, nd) for v in x]
    if isinstance(x, dict):
        return {k: _round(v, nd) for k, v in x.items()}
    return x


def dump_case(name: str, dbc: str, log: str, spec: str) -> dict:
    p = Project()
    p.load_dbc(os.path.join(SAMPLES, dbc))
    p.load_log(os.path.join(SAMPLES, log))
    st = p.store
    out: dict = {"summary": _round(p.summary())}

    ts, ids, dlc, ch = st.frame_arrays()
    out["frames"] = {
        "n": int(len(ts)),
        "ts_sha": hashlib.sha1(np.ascontiguousarray(ts, np.float64).tobytes()).hexdigest(),
        "ids_sha": hashlib.sha1(np.ascontiguousarray(ids, np.uint32).tobytes()).hexdigest(),
        "dlc_sha": hashlib.sha1(np.ascontiguousarray(dlc, np.uint8).tobytes()).hexdigest(),
        "ch_sha": hashlib.sha1(np.ascontiguousarray(ch, np.uint8).tobytes()).hexdigest(),
        "data_sha": hashlib.sha1(np.ascontiguousarray(st._data, np.uint8).tobytes()).hexdigest(),
        "events": [e.to_dict() for e in st.events()[:200]],
        "n_events": len(st.events()),
        "times_n": int(len(st.times)),
    }

    # 逐信号序列（取前 40 个有数据的信号，全量比对取值）
    sigs = st.signals_with_data()[:40]
    t0, t1 = st.start_time, st.end_time
    per_sig = {}
    for s in sigs:
        times, values = st.series(s)
        per_sig[s] = {
            "n": int(len(times)),
            "t_sha": hashlib.sha1(np.ascontiguousarray(times, np.float64).tobytes()).hexdigest(),
            "v_sha": hashlib.sha1(json.dumps(
                [_round(v) if isinstance(v, float) else v for v in values.tolist()],
                default=str).encode()).hexdigest(),
            "window": _round(st.window_series(s, t0, t1, 500)),
            "at": [_round(st.value_at(s, t0 + (t1 - t0) * f)) if not isinstance(
                st.value_at(s, t0 + (t1 - t0) * f), (str, type(None))) else st.value_at(
                s, t0 + (t1 - t0) * f) for f in (0.0, 0.25, 0.5, 0.75, 1.0)],
        }
    out["signals"] = per_sig
    out["trace"] = _round(st.trace_window(t0, t0 + (t1 - t0) * 0.01, 50))
    out["decode_status"] = {str(k): v for k, v in sorted(st.message_decode_status().items())}

    spec_obj = yaml.safe_load(open(os.path.join(SAMPLES, spec), encoding="utf-8").read())
    out["analysis"] = _round(analyze_functions(st, spec_obj))
    return out


def main() -> int:
    if sys.argv[1:2] == ["--diff"]:
        a = json.load(open(sys.argv[2], encoding="utf-8"))
        b = json.load(open(sys.argv[3], encoding="utf-8"))
        bad = 0
        for case in sorted(set(a) | set(b)):
            for key in sorted(set(a.get(case, {})) | set(b.get(case, {}))):
                x = json.dumps(a.get(case, {}).get(key), sort_keys=True, default=str)
                y = json.dumps(b.get(case, {}).get(key), sort_keys=True, default=str)
                if x != y:
                    bad += 1
                    print(f"DIFF {case}.{key}  ({len(x)} vs {len(y)} chars)")
                    for i in range(min(len(x), len(y))):
                        if x[i] != y[i]:
                            print(f"      @{i}: ...{x[max(0,i-90):i+90]}")
                            print(f"      @{i}: ...{y[max(0,i-90):i+90]}")
                            break
        print("IDENTICAL" if not bad else f"{bad} sections differ")
        return 1 if bad else 0

    result = {}
    for name, dbc, log, spec in CASES:
        if not all(os.path.exists(os.path.join(SAMPLES, f)) for f in (dbc, log, spec)):
            print(f"skip {name} (missing sample)")
            continue
        print(f"dumping {name} …", flush=True)
        result[name] = dump_case(name, dbc, log, spec)
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        json.dump(result, f, sort_keys=True, default=str)
    print(f"wrote {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
