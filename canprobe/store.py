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
from .log_parser import ParseError, blf_arrays, iter_frames


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
        self._frames = None
        self._arrays = None
        self.analysis: Optional[dict] = None
        # 「快速求值」模式的结果单独缓存，与精确模式互不覆盖
        self.analysis_fast: Optional[dict] = None

    def load_dbc(self, path: str) -> dict:
        self.dbc = DbcDatabase.load(path)
        self.dbc_name = os.path.basename(path)
        self._rebuild_store()
        return self.summary()

    def load_log(self, path: str, t0: float | None = None, t1: float | None = None,
                 max_frames: int | None = None) -> dict:
        self._frames = None
        self._arrays = None
        if os.path.splitext(path)[1].lower() == ".blf":
            self._arrays = blf_arrays(path)      # 向量化快速通道，失败返回 None
        if self._arrays is not None:
            n = self._apply_array_window(t0, t1, max_frames)
            if not n:
                raise ParseError(f"文件 {os.path.basename(path)} 中未解析到任何 CAN 报文")
            self._truncated = bool(max_frames and n >= max_frames)
        else:
            frames = list(iter_frames(path, t0=t0, t1=t1, max_frames=max_frames))
            if not frames:
                raise ParseError(f"文件 {os.path.basename(path)} 中未解析到任何 CAN 报文")
            self._frames = frames
            self._truncated = bool(max_frames and len(frames) >= max_frames)
        self.log_name = os.path.basename(path)
        self._rebuild_store()
        return self.summary()

    def _apply_array_window(self, t0, t1, max_frames) -> int:
        """在数组上做时间窗 / 帧数截断，语义与 :func:`iter_frames` 的过滤一致。"""
        import numpy as np

        a = self._arrays
        keep = None
        if t0 is not None or t1 is not None:
            lo = t0 if t0 is not None else -np.inf
            hi = t1 if t1 is not None else np.inf
            keep = (a.ts >= lo) & (a.ts <= hi)
            a.ts, a.ids, a.dlc = a.ts[keep], a.ids[keep], a.dlc[keep]
            a.data, a.channels = a.data[keep], a.channels[keep]
            a.events = [e for e in a.events if lo <= e.t <= hi]
        if max_frames is not None and len(a.ts) + len(a.events) > max_frames:
            # iter_frames 是按产出顺序数到 max_frames 就停，数据帧和事件都算一个。
            # 数组通道里两者是分开的，所以要按时间合并着数：找到最大的 k，使得
            # 「前 k+1 个数据帧」加上「不晚于它们的事件」总数仍不超过 max_frames。
            ev_ts = np.array(sorted(e.t for e in a.events), dtype=np.float64)
            before = np.searchsorted(ev_ts, a.ts, side="right")
            total = np.arange(1, len(a.ts) + 1) + before      # 单调不减
            k = int(np.searchsorted(total, max_frames, side="right")) - 1
            if k < 0:
                cut = -np.inf
                a.ts, a.ids, a.dlc = a.ts[:0], a.ids[:0], a.dlc[:0]
                a.data, a.channels = a.data[:0], a.channels[:0]
                a.events = a.events[:max_frames]
            else:
                cut = float(a.ts[k])
                a.ts, a.ids, a.dlc = a.ts[:k + 1], a.ids[:k + 1], a.dlc[:k + 1]
                a.data, a.channels = a.data[:k + 1], a.channels[:k + 1]
                a.events = [e for e in a.events if e.t <= cut]
        return len(a.ts) + len(a.events)

    def _rebuild_store(self) -> None:
        arrays = getattr(self, "_arrays", None)
        frames = getattr(self, "_frames", None)
        if arrays is not None:
            self.store = SeriesStore.from_arrays(
                arrays.ts, arrays.ids, arrays.dlc, arrays.data,
                arrays.channels, arrays.events, self.dbc)
        else:
            self.store = SeriesStore(frames, self.dbc) if frames else None
        # 分析改为惰性：/api/analysis 与 /api/events 本来就有"没算过就算"的分支。
        # 500 万帧的日志跑一遍功能规格是分钟级的，挂在上传请求上会让加载看着像卡死。
        self.analysis = None
        self.analysis_fast = None

    def load_spec(self, path: str, text: str) -> dict:
        import json

        import yaml

        try:
            self.spec = yaml.safe_load(text)
        except Exception:
            self.spec = json.loads(text)
        self.spec_name = os.path.basename(path)
        self.spec_text = text
        # 与 _rebuild_store 一致：分析惰性化，装规格本身要立刻返回。
        # 规格里有语法/信号错误仍然会在这里暴露（analyze_functions 的 SpecError
        # 会在首次取事件时抛出，前端事件面板照常显示错误文本）。
        self.analysis = None
        self.analysis_fast = None
        return self.summary()

    def run_analysis(self, fast: bool = False) -> dict:
        if self.store is None:
            raise ValueError("尚未加载报文日志")
        if self.spec is None:
            raise ValueError("尚未加载功能分析规格")
        result = analyze_functions(self.store, self.spec, fast=fast)
        if fast:
            self.analysis_fast = result
        else:
            self.analysis = result
        return result

    def analysis_for(self, fast: bool = False) -> Optional[dict]:
        """取（必要时计算）对应模式的分析结果。"""
        cached = self.analysis_fast if fast else self.analysis
        return cached if cached is not None else self.run_analysis(fast=fast)

    def summary(self) -> dict:
        # DBC 定义了什么 vs 这份日志里录到了什么 —— 两个数分开报，装错日志时
        # 一眼能看出来（比如 DBC 1861 信号 / 日志只覆盖到 8 个）。
        with_data = self.store.signals_with_data() if self.store else []
        unknown = self.store.unknown_message_ids() if self.store else []
        present = self.store.present_message_ids() if self.store else set()
        s = {
            "dbc": self.dbc_name,
            "log": self.log_name,
            "spec": self.spec_name,
            "message_count": len(self.dbc.messages) if self.dbc else 0,
            "signal_count": len(self.dbc.signals) if self.dbc else 0,
            "frame_count": self.store.frame_count if self.store else 0,
            "start": self.store.start_time if self.store else None,
            "end": self.store.end_time if self.store else None,
            # 语义修正：以前这里回的是 DBC 全集，名字叫 decoded 却和日志无关
            "decoded_signals": with_data,
            "data_signal_count": len(with_data),
            "log_message_count": len(present),
            "log_unknown_ids": unknown,
            # 出现过但一帧都解不开的报文（CAN-FD 长度不符是最常见的一种）。
            # 不报出来的话，这些报文的信号会以"没录到"的面目消失，0 条事件
            # 就会被读成"查过了没问题"。
            "decode_failures": self.store.decode_failures() if self.store else [],
            "truncated": self._truncated,
        }
        return s


# A single global project guarded by a lock (the tool is single-user local).
_lock = threading.Lock()
_project = Project()


def project() -> Project:
    with _lock:
        return _project
