"""In-memory project store.

Keeps the currently loaded DBC + trace + analysis spec. The tool is designed
for interactive investigation of a single loaded project at a time; this module
holds that state and the (cheap) derived artifacts.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from .analyzer import analyze_functions
from .dbc_loader import DbcDatabase
from .decoder import SeriesStore
from .log_parser import ParseError, iter_frames


class Project:
    def __init__(self):
        self.dbc: Optional[DbcDatabase] = None
        self.dbc_name: Optional[str] = None
        self.log_name: Optional[str] = None
        self.store: Optional[SeriesStore] = None
        self.spec: Optional[dict] = None
        self.spec_name: Optional[str] = None
        self.spec_text: Optional[str] = None
        self._truncated: bool = False
        self.analysis: Optional[dict] = None

    def load_dbc(self, path: str) -> dict:
        self.dbc = DbcDatabase.load(path)
        self.dbc_name = os.path.basename(path)
        self._rebuild_store()
        return self.summary()

    def load_log(self, path: str, t0: float | None = None, t1: float | None = None,
                 max_frames: int | None = None) -> dict:
        frames = list(iter_frames(path, t0=t0, t1=t1, max_frames=max_frames))
        if not frames:
            raise ParseError(f"文件 {os.path.basename(path)} 中未解析到任何 CAN 报文")
        self.log_name = os.path.basename(path)
        self._frames = frames
        self._truncated = bool(max_frames and len(frames) >= max_frames)
        self._rebuild_store()
        return self.summary()

    def _rebuild_store(self) -> None:
        frames = getattr(self, "_frames", [])
        self.store = SeriesStore(frames, self.dbc) if frames else None
        self.analysis = None
        if self.store is not None and self.spec is not None:
            try:
                self.analysis = analyze_functions(self.store, self.spec)
            except Exception:
                self.analysis = None

    def load_spec(self, path: str, text: str) -> dict:
        import json

        import yaml

        try:
            self.spec = yaml.safe_load(text)
        except Exception:
            self.spec = json.loads(text)
        self.spec_name = os.path.basename(path)
        self.spec_text = text
        self.analysis = None
        if self.store is not None:
            self.analysis = analyze_functions(self.store, self.spec)
        return self.summary()

    def run_analysis(self) -> dict:
        if self.store is None:
            raise ValueError("尚未加载报文日志")
        if self.spec is None:
            raise ValueError("尚未加载功能分析规格")
        self.analysis = analyze_functions(self.store, self.spec)
        return self.analysis

    def summary(self) -> dict:
        s = {
            "dbc": self.dbc_name,
            "log": self.log_name,
            "spec": self.spec_name,
            "message_count": len(self.dbc.messages) if self.dbc else 0,
            "signal_count": len(self.dbc.signals) if self.dbc else 0,
            "frame_count": self.store.frame_count if self.store else 0,
            "start": self.store.start_time if self.store else None,
            "end": self.store.end_time if self.store else None,
            "decoded_signals": self.store.signal_names() if self.store else [],
            "truncated": self._truncated,
        }
        return s


# A single global project guarded by a lock (the tool is single-user local).
_lock = threading.Lock()
_project = Project()


def project() -> Project:
    with _lock:
        return _project
