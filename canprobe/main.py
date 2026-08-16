"""FastAPI application: serves the web UI and the analysis API."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analyzer import SpecError, evaluate_timeline
from .log_parser import ParseError
from .store import project

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
SAMPLES_DIR = BASE_DIR / "samples"

app = FastAPI(title="CanProbe — CAN 总线回放与信号分析", version="0.1.0")

# a private upload dir (kept out of git)
_UPLOAD_DIR = Path(tempfile.gettempdir()) / "can_replay_uploads"
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _save_upload(f: UploadFile) -> str:
    safe = os.path.basename(f.filename or "upload")
    path = _UPLOAD_DIR / safe
    with open(path, "wb") as out:
        out.write(f.file.read())
    return str(path)


@app.get("/api/status")
def status():
    return project().summary()


@app.get("/api/messages")
def messages():
    p = project()
    if p.dbc is None:
        return []
    out = []
    for mid in sorted(p.dbc.messages):
        m = p.dbc.messages[mid]
        out.append({
            **m.to_dict(),
            "signals": [s.to_dict() for s in m.signals],
        })
    return out


@app.get("/api/signals")
def signals():
    p = project()
    if p.dbc is None:
        return []
    return [
        {
            "name": name,
            "message_id": p.dbc._signal_to_message.get(name),
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
def analysis():
    p = project()
    if p.analysis is None:
        if p.spec is None:
            raise HTTPException(400, "尚未加载功能分析规格")
        try:
            p.analysis = p.run_analysis()
        except SpecError as e:
            raise HTTPException(400, str(e))
    return p.analysis


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
        text = file.file.read().decode("utf-8")
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
    spec = SAMPLES_DIR / "functions.yaml"
    if not (dbc.exists() and log.exists() and spec.exists()):
        raise HTTPException(404, "示例文件缺失，请先运行 samples/generate_samples.py")
    p.load_dbc(str(dbc))
    p.load_log(str(log))
    p.load_spec(str(spec), spec.read_text(encoding="utf-8"))
    return p.summary()


# --------------------------------------------------------------------------- #
# Static UI
# --------------------------------------------------------------------------- #
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))
