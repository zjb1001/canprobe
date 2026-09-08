"""FastAPI application: serves the web UI and the analysis API."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analyzer import SpecError, evaluate_timeline, spec_functions
from .diag.api import router as diag_router
from .log_parser import ParseError
from .store import project

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
SAMPLES_DIR = BASE_DIR / "samples"

# /api/series 一次能回好几 MB（八条曲线 × 两万点），标准库 json 序列化是那一步的
# 大头。装了 orjson 就用，没装照常跑 —— 只是个加速件，不是依赖。
# 注意 fastapi.responses.ORJSONResponse **没装 orjson 也 import 得到**，
# 要到渲染时才 assert 失败，所以这里必须直接探 orjson 本身。
try:
    import orjson as _orjson  # noqa: F401

    from fastapi.responses import ORJSONResponse as _JSONResponse
except Exception:  # pragma: no cover - orjson 未安装
    _JSONResponse = None

app = FastAPI(title="CanProbe — CAN 总线回放与信号分析", version="0.2.0",
              **({"default_response_class": _JSONResponse} if _JSONResponse else {}))

# a private upload dir (kept out of git)
_UPLOAD_DIR = Path(tempfile.gettempdir()) / "can_replay_uploads"
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _save_upload(f: UploadFile) -> str:
    safe = os.path.basename(f.filename or "upload")
    path = _UPLOAD_DIR / safe
    # 分块拷贝：日志动辄上百 MB，f.file.read() 会把整份再复制进内存一次
    with open(path, "wb") as out:
        shutil.copyfileobj(f.file, out, 1 << 20)
    return str(path)


@app.get("/api/status")
def status():
    return project().summary()


@app.get("/api/messages")
def messages():
    p = project()
    if p.dbc is None:
        return []
    # 用"解得开"而不是"出现过"：报文录到了但按当前 DBC 解不开（典型是 CAN-FD
    # 长度不符），它下面的信号同样一个值都拿不到，置灰才是诚实的。
    present = p.store.decodable_message_ids() if p.store is not None else None
    out = []
    for mid in sorted(p.dbc.messages):
        m = p.dbc.messages[mid]
        # 该报文在当前日志里是否出现过。有无数据是报文级的事实——报文没录到，
        # 它下面的信号一个都不会有值，信号树据此整组置灰。
        has_data = present is None or mid in present
        out.append({
            **m.to_dict(),
            "has_data": has_data,
            "signals": [{**s.to_dict(), "has_data": has_data} for s in m.signals],
        })
    return out


@app.get("/api/signals")
def signals():
    p = project()
    if p.dbc is None:
        return []
    present = p.store.decodable_message_ids() if p.store is not None else None
    return [
        {
            "name": name,
            "message_id": p.dbc._signal_to_message.get(name),
            # 该信号所属报文在当前日志里是否出现过；前端据此把无数据信号置灰
            "has_data": (present is None
                         or p.dbc._signal_to_message.get(name) in present),
            **meta.to_dict(),
        }
        for name, meta in sorted(p.dbc.signals.items())
    ]


@app.get("/api/timeline")
def timeline():
    p = project()
    if p.store is None:
        return {"start": None, "end": None, "frame_count": 0}
    return {
        "start": p.store.start_time,
        "end": p.store.end_time,
        "frame_count": p.store.frame_count,
    }


@app.get("/api/series")
def series(signals: str, start: Optional[float] = None, end: Optional[float] = None,
           max_points: int = 4000):
    p = project()
    if p.store is None:
        raise HTTPException(400, "尚未加载报文日志")
    names = [s for s in signals.split(",") if s]
    t0 = start if start is not None else p.store.start_time
    t1 = end if end is not None else p.store.end_time
    out = {}
    for name in names:
        out[name] = p.store.window_series(name, t0, t1, max_points)
    return out


@app.get("/api/values")
def values(signals: str, t: float):
    """返回各信号在时间 t 的精确值（零阶保持），用于右侧信号值面板。"""
    p = project()
    if p.store is None:
        raise HTTPException(400, "尚未加载报文日志")
    names = [s for s in signals.split(",") if s]
    return {name: p.store.value_at(name, t) for name in names}


@app.get("/api/trace")
def trace(start: Optional[float] = None, end: Optional[float] = None, limit: int = 2000):
    p = project()
    if p.store is None:
        raise HTTPException(400, "尚未加载报文日志")
    t0 = start if start is not None else p.store.start_time
    t1 = end if end is not None else p.store.end_time
    return p.store.trace_window(t0, t1, limit)


@app.get("/api/analysis")
def analysis(fast: bool = False):
    p = project()
    if p.spec is None:
        raise HTTPException(400, "尚未加载功能分析规格")
    if p.store is None:
        raise HTTPException(400, "尚未加载报文日志")
    try:
        return p.analysis_for(fast)
    except SpecError as e:
        raise HTTPException(400, str(e))


@app.get("/api/events")
def events(types: Optional[str] = None, functions: Optional[str] = None,
           q: Optional[str] = None, limit: int = 3000, fast: bool = False):
    """关键事件列表 —— 下边框事件面板专用。

    只回面板要画的字段，**不含** ``evidence`` 证据树：一份几千条事件的规格
    （比如按 200 ms 翻转的开关信号）走 ``/api/analysis`` 会是几 MB 的 JSON，
    而面板一行只用得上 5 个字段。

    ``functions`` / ``types`` 的计数按**未过滤**的全集统计，这样筛选时
    chip 上的数字不会自己跳，用户能看出"筛掉了多少"。

    ``functions`` 按**规格里的书写顺序**返回，并且保留 count=0 的功能：
    按出现次数排序会把翻转几百次的开关请求排到最前、把只触发一次的根因判定
    压到最后，恰好和调查时的关注度相反；而"某功能一次都没触发"（比如 AVH
    全程没进过 Active）本身就是结论，不该从列表里消失。
    """
    p = project()
    if p.spec is None:
        raise HTTPException(400, "尚未加载功能分析规格")
    if p.store is None:
        raise HTTPException(400, "尚未加载报文日志")
    try:
        a = p.analysis_for(fast) or {}
    except SpecError as e:
        raise HTTPException(400, str(e))
    spec_funcs = a.get("functions", [])
    names = {f["id"]: (f.get("name") or f["id"]) for f in spec_funcs}
    all_events = a.get("events", [])

    per_func: dict[str, int] = {f["id"]: 0 for f in spec_funcs}
    per_type: dict[str, int] = {}
    for e in all_events:
        per_func[e["function"]] = per_func.get(e["function"], 0) + 1
        per_type[e["type"]] = per_type.get(e["type"], 0) + 1

    want_types = {x for x in (types or "").split(",") if x}
    want_funcs = {x for x in (functions or "").split(",") if x}
    needle = (q or "").strip().lower()

    rows = []
    for e in all_events:
        if want_types and e["type"] not in want_types:
            continue
        if want_funcs and e["function"] not in want_funcs:
            continue
        name = names.get(e["function"], e["function"])
        summary = e.get("summary", "") or ""
        if needle and needle not in f"{e['function']} {name} {summary}".lower():
            continue
        rows.append({
            "t": e["t"], "type": e["type"], "function": e["function"],
            "name": name, "summary": summary,
        })

    return {
        "total": len(rows),
        "truncated": len(rows) > limit,
        "events": rows[:limit],
        "functions": [
            {"id": fid, "name": names.get(fid, fid), "count": n}
            for fid, n in per_func.items()
        ],
        "types": per_type,
    }


class EvaluateBody(BaseModel):
    condition: dict


@app.post("/api/evaluate")
def evaluate(body: EvaluateBody):
    p = project()
    if p.store is None:
        raise HTTPException(400, "尚未加载报文日志")
    try:
        return evaluate_timeline(p.store, body.condition)
    except SpecError as e:
        raise HTTPException(400, str(e))


class SpecBody(BaseModel):
    text: str


@app.get("/api/spec")
def get_spec():
    p = project()
    return {"text": p.spec_text or "", "name": p.spec_name}


@app.get("/api/functions")
def list_functions():
    """功能清单 + 每个功能引用的信号，供侧栏「一键勾选」用。

    刻意做成轻量的：只解析规格做静态提取，不跑状态机、不要求已加载日志，
    所以规格里有一个功能写错也不会让整个列表拿不到。
    """
    p = project()
    if p.spec is None:
        return []
    try:
        funcs = spec_functions(p.spec)
    except SpecError as e:
        raise HTTPException(400, f"规格错误: {e}")

    known = set(p.dbc.signals) if p.dbc else set()
    # "DBC 里没有"和"DBC 里有但这份日志没录到"是两种完全不同的毛病，前者要换
    # DBC，后者要换日志。混成一个 missing 的话，点了按钮"没反应"依旧没法自查。
    with_data = set(p.store.signals_with_data()) if p.store is not None else None
    for f in funcs:
        sigs = f["signals"]
        if not known:
            f["available"], f["missing"], f["nodata"] = list(sigs), [], []
            continue
        f["missing"] = [s for s in sigs if s not in known]
        in_dbc = [s for s in sigs if s in known]
        if with_data is None:
            f["available"], f["nodata"] = in_dbc, []
        else:
            f["available"] = [s for s in in_dbc if s in with_data]
            f["nodata"] = [s for s in in_dbc if s not in with_data]
    return funcs


@app.post("/api/spec")
def set_spec(body: SpecBody):
    """Set the function-analysis spec from raw YAML/JSON text (inline editing)."""
    p = project()
    try:
        return p.load_spec("inline.yaml", body.text)
    except SpecError as e:
        raise HTTPException(400, f"规格错误: {e}")
    except Exception as e:
        raise HTTPException(400, f"规格解析失败: {e}")


@app.post("/api/upload/dbc")
def upload_dbc(file: UploadFile = File(...)):
    path = _save_upload(file)
    try:
        return project().load_dbc(path)
    except Exception as e:
        raise HTTPException(400, f"DBC 解析失败: {e}")


@app.post("/api/upload/log")
def upload_log(file: UploadFile = File(...), start: Optional[float] = None,
               end: Optional[float] = None, max_frames: Optional[int] = None):
    """上传日志。可指定时间窗口/帧数上限用于超大日志的增量加载。"""
    path = _save_upload(file)
    try:
        return project().load_log(path, t0=start, t1=end, max_frames=max_frames)
    except ParseError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"日志解析失败: {e}")


@app.post("/api/upload/spec")
def upload_spec(file: UploadFile = File(...)):
    path = _save_upload(file)
    try:
        # 从落盘的副本读，不要再碰 file.file —— _save_upload() 已经把流读空了，
        # 二次 read() 只会拿到 b""，规格会静默变成 None。
        text = Path(path).read_text(encoding="utf-8")
        return project().load_spec(path, text)
    except SpecError as e:
        raise HTTPException(400, f"规格错误: {e}")
    except Exception as e:
        raise HTTPException(400, f"规格解析失败: {e}")


@app.post("/api/load/sample")
def load_sample():
    p = project()
    dbc = SAMPLES_DIR / "cruise.dbc"
    log = SAMPLES_DIR / "cruise.csv"
    spec = SAMPLES_DIR / "function_specs" / "functions.yaml"
    if not (dbc.exists() and log.exists() and spec.exists()):
        raise HTTPException(404, "示例文件缺失，请先运行 samples/generate_samples.py")
    p.load_dbc(str(dbc))
    p.load_log(str(log))
    p.load_spec(str(spec), spec.read_text(encoding="utf-8"))
    return p.summary()


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
app.include_router(diag_router)

# --------------------------------------------------------------------------- #
# Static UI
# --------------------------------------------------------------------------- #
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))
