"""Decode raw frames into per-signal time series.

The :class:`SeriesStore` holds frames in compact NumPy arrays (not a list of
Python objects) so multi-million-frame logs stay memory-bounded, and decodes
signals *lazily*: a signal is only decoded the first time it is read, one whole
message at a time. Plotting and analysis therefore touch only the signals they
actually need, and opening a large log stays fast.
"""
from __future__ import annotations

import bisect
from typing import Optional

import numpy as np

from .dbc_loader import DbcDatabase
from .log_parser import Frame


class SeriesStore:
    def __init__(self, frames: list[Frame], dbc: Optional[DbcDatabase],
                 t0: Optional[float] = None, t1: Optional[float] = None):
        self.dbc = dbc

        # 时间窗口过滤（用于"超大日志分段加载"）
        if (t0 is not None or t1 is not None) and frames:
            lo = t0 if t0 is not None else -np.inf
            hi = t1 if t1 is not None else np.inf
            frames = [f for f in frames if lo <= f.t <= hi]

        # 分区：数据帧 → 紧凑数组；通信层事件（错误帧/状态）→ 单独列表
        data_frames = sorted((f for f in frames if not f.is_error), key=lambda f: f.t)
        self._events = sorted((f for f in frames if f.is_error), key=lambda f: f.t)

        if data_frames:
            self._ts = np.array([f.t for f in data_frames], dtype=np.float64)
            self._ids = np.array([f.frame_id for f in data_frames], dtype=np.uint32)
            self._dlc = np.array([len(f.data) for f in data_frames], dtype=np.uint8)
            max_dlc = int(self._dlc.max()) if len(self._dlc) else 0
            data = np.zeros((len(data_frames), max_dlc), dtype=np.uint8)
            for i, f in enumerate(data_frames):
                data[i, :len(f.data)] = np.frombuffer(f.data, dtype=np.uint8)
            self._data = data
            self._channels = np.array([f.channel for f in data_frames], dtype=np.uint8)
            self._times = np.unique(self._ts)
        else:
            self._ts = np.array([], dtype=np.float64)
            self._ids = np.array([], dtype=np.uint32)
            self._dlc = np.array([], dtype=np.uint8)
            self._data = np.zeros((0, 0), dtype=np.uint8)
            self._channels = np.array([], dtype=np.uint8)
            self._times = np.array([], dtype=np.float64)

        self._by_id: dict[int, np.ndarray] = {}
        self._signal_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._decoded_messages: set[int] = set()

    # -- properties --------------------------------------------------------- #
    @property
    def times(self) -> np.ndarray:
        return self._times

    @property
    def start_time(self) -> Optional[float]:
        return float(self._times[0]) if len(self._times) else None

    @property
    def end_time(self) -> Optional[float]:
        return float(self._times[-1]) if len(self._times) else None

    @property
    def frame_count(self) -> int:
        return len(self._ts)

    def frame_arrays(self):
        """返回紧凑帧数组 (ts, ids, dlc, channels)，供外部分析（如诊断引擎）。

        均为按时间升序的 NumPy 数组；is_extended 可由 ids > 0x7FF 推导。
        仅含数据帧；错误帧/状态事件见 :meth:`events`。
        """
        return self._ts, self._ids, self._dlc, self._channels

    def events(self) -> list:
        """返回通信层事件（错误帧 / 总线状态），按时间升序，供 Tier 2/3 诊断。"""
        return self._events

    def signal_names(self) -> list[str]:
        if self.dbc is None:
            return []
        return sorted(self.dbc.signals.keys())

    # -- frame access ------------------------------------------------------- #
    def _frame_bytes(self, i: int) -> bytes:
        return self._data[i, :int(self._dlc[i])].tobytes()

    def _indices_for_id(self, frame_id: int) -> np.ndarray:
        if frame_id not in self._by_id:
            self._by_id[frame_id] = np.where(self._ids == frame_id)[0]
        return self._by_id[frame_id]

    def _ensure_message_decoded(self, frame_id: int) -> None:
        if frame_id in self._decoded_messages:
            return
        if self.dbc is None or frame_id not in self.dbc.messages:
            self._decoded_messages.add(frame_id)
            return
        idx = self._indices_for_id(frame_id)
        msg_signals = [s.name for s in self.dbc.messages[frame_id].signals]
        buckets: dict[str, list] = {s: [] for s in msg_signals}
        for i in idx:
            d = self.dbc.decode(frame_id, self._frame_bytes(int(i)))
            if not d:
                continue
            for s in msg_signals:
                if s in d:
                    buckets[s].append((self._ts[i], d[s]))
        for s in msg_signals:
            if s in self._signal_cache:
                continue
            t = np.array([x[0] for x in buckets[s]], dtype=np.float64)
            v = np.array([x[1] for x in buckets[s]], dtype=object)
            self._signal_cache[s] = (t, v)
        self._decoded_messages.add(frame_id)

    def _series(self, signal: str) -> tuple[np.ndarray, np.ndarray]:
        empty = (np.array([], dtype=np.float64), np.array([], dtype=object))
        if signal in self._signal_cache:
            return self._signal_cache[signal]
        if self.dbc is None:
            return empty
        msg_id = self.dbc._signal_to_message.get(signal)
        if msg_id is None:
            return empty
        self._ensure_message_decoded(msg_id)
        return self._signal_cache.get(signal, empty)

    # -- lookup ------------------------------------------------------------- #
    def value_at(self, signal: str, t: float):
        times, values = self._series(signal)
        if len(times) == 0:
            return None
        idx = bisect.bisect_right(times, t) - 1
        if idx < 0:
            return None
        return values[idx]

    def series(self, signal: str) -> tuple[np.ndarray, np.ndarray]:
        return self._series(signal)

    def is_enum(self, signal: str) -> bool:
        if self.dbc is None:
            return False
        meta = self.dbc.signals.get(signal)
        return bool(meta and meta.choices)

    def _choices_dict(self, signal: str):
        if self.dbc is None:
            return None
        meta = self.dbc.signals.get(signal)
        if not meta or not meta.choices:
            return None
        return {str(k): v for k, v in meta.choices.items()}

    # -- numeric projection for plotting ------------------------------------ #
    def numeric_series(self, signal: str) -> tuple[np.ndarray, np.ndarray]:
        times, values = self._series(signal)
        if len(times) == 0:
            return times, np.array([], dtype=np.float64)

        if not self.is_enum(signal):
            out = []
            for v in values:
                out.append(float(v) if isinstance(v, (int, float, np.integer, np.floating)) else np.nan)
            return times, np.array(out, dtype=np.float64)

        choices = self.dbc.signals[signal].choices
        order = sorted(choices.keys(), key=lambda c: int(c) if str(c).lstrip("-").isdigit() else 0)
        code = {choices[k]: i for i, k in enumerate(order)}
        out = [float(code.get(v, np.nan)) if isinstance(v, str) else float(v) for v in values]
        return times, np.array(out, dtype=np.float64)

    # -- downsampling for plotting ----------------------------------------- #
    def window_series(self, signal: str, t0: float, t1: float, max_points: int = 4000) -> dict:
        times, values = self.numeric_series(signal)
        if len(times) == 0:
            return {"t": [], "v": [], "enum": self.is_enum(signal),
                    "choices": self._choices_dict(signal), "min": None, "max": None}
        lo = bisect.bisect_left(times, t0)
        hi = bisect.bisect_right(times, t1)
        times = times[lo:hi]
        values = values[lo:hi]

        if self.is_enum(signal):
            vmin, vmax = None, None
        else:
            finite = values[~np.isnan(values)]
            vmin = float(finite.min()) if finite.size else None
            vmax = float(finite.max()) if finite.size else None

        if len(times) > max_points:
            step = int(np.ceil(len(times) / max_points))
            times = times[::step]
            values = values[::step]
        return {
            "t": times.tolist(),
            "v": [None if np.isnan(x) else x for x in values.tolist()],
            "enum": self.is_enum(signal),
            "choices": self._choices_dict(signal),
            "min": vmin,
            "max": vmax,
        }

    # -- raw trace window --------------------------------------------------- #
    def frames_in_window(self, t0: float, t1: float, limit: int = 5000) -> list[dict]:
        out = []
        lo = bisect.bisect_left(self._ts, t0)
        hi = bisect.bisect_right(self._ts, t1)
        for i in range(lo, hi):
            out.append(self._frame_dict(int(i)))
            if len(out) >= limit:
                break
        return out

    def trace_window(self, t0: float, t1: float, limit: int = 2000) -> list[dict]:
        return self.frames_in_window(t0, t1, limit)

    def _frame_dict(self, i: int) -> dict:
        frame_id = int(self._ids[i])
        item = {
            "t": float(self._ts[i]),
            "id": frame_id,
            "data": self._frame_bytes(i).hex(" "),
            "channel": int(self._channels[i]),
            "is_extended": frame_id > 0x7FF,
        }
        item["signals"] = []
        if self.dbc is not None:
            msg = self.dbc.message_for_id(frame_id)
            item["name"] = msg.name if msg else ""
            decoded = self.dbc.decode(frame_id, self._frame_bytes(i))
            if decoded:
                item["signals"] = [{"name": k, "value": v} for k, v in decoded.items()]
        return item
