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

from . import fast_decode
from .dbc_loader import DbcDatabase
from .log_parser import Frame


_EMPTY_SERIES = (np.array([], dtype=np.float64), np.array([], dtype=object))


class SeriesStore:
    def __init__(self, frames: list[Frame], dbc: Optional[DbcDatabase],
                 t0: Optional[float] = None, t1: Optional[float] = None):
        # 时间窗口过滤（用于"超大日志分段加载"）
        if (t0 is not None or t1 is not None) and frames:
            lo = t0 if t0 is not None else -np.inf
            hi = t1 if t1 is not None else np.inf
            frames = [f for f in frames if lo <= f.t <= hi]

        # 分区：数据帧 → 紧凑数组；通信层事件（错误帧/状态）→ 单独列表
        data_frames = sorted((f for f in frames if not f.is_error), key=lambda f: f.t)
        events = sorted((f for f in frames if f.is_error), key=lambda f: f.t)

        if data_frames:
            ts = np.array([f.t for f in data_frames], dtype=np.float64)
            ids = np.array([f.frame_id for f in data_frames], dtype=np.uint32)
            dlc = np.array([len(f.data) for f in data_frames], dtype=np.uint8)
            max_dlc = int(dlc.max()) if len(dlc) else 0
            data = np.zeros((len(data_frames), max_dlc), dtype=np.uint8)
            for i, f in enumerate(data_frames):
                data[i, :len(f.data)] = np.frombuffer(f.data, dtype=np.uint8)
            channels = np.array([f.channel for f in data_frames], dtype=np.uint8)
        else:
            ts = np.array([], dtype=np.float64)
            ids = np.array([], dtype=np.uint32)
            dlc = np.array([], dtype=np.uint8)
            data = np.zeros((0, 0), dtype=np.uint8)
            channels = np.array([], dtype=np.uint8)

        self._adopt(ts, ids, dlc, data, channels, events, dbc)

    @classmethod
    def from_arrays(cls, ts, ids, dlc, data, channels, events,
                    dbc: Optional[DbcDatabase],
                    t0: Optional[float] = None, t1: Optional[float] = None) -> "SeriesStore":
        """从紧凑数组直接建仓，不经过逐帧 :class:`Frame` 对象。

        498 万帧的 BLF 走 ``__init__`` 要先造 498 万个 dataclass（约 1.5 GB）再用
        Python 循环填 NumPy；这条路直接接管 :mod:`canprobe.blf_fast` 产出的数组。
        排序与窗口过滤都是向量化的，语义与 ``__init__`` 完全一致（稳定排序，
        窗口为闭区间）。
        """
        obj = cls.__new__(cls)
        ts = np.asarray(ts, dtype=np.float64)

        if (t0 is not None or t1 is not None) and len(ts):
            lo = t0 if t0 is not None else -np.inf
            hi = t1 if t1 is not None else np.inf
            keep = (ts >= lo) & (ts <= hi)
            if not keep.all():
                ts, ids, dlc = ts[keep], ids[keep], dlc[keep]
                data, channels = data[keep], channels[keep]
            events = [e for e in events if lo <= e.t <= hi]

        # BLF 本来就是按时间录的，先检查再排，省掉 500 万元素的 argsort
        if len(ts) and not np.all(np.diff(ts) >= 0):
            order = np.argsort(ts, kind="stable")
            ts, ids, dlc = ts[order], ids[order], dlc[order]
            data, channels = data[order], channels[order]

        obj._adopt(ts, np.asarray(ids, np.uint32), np.asarray(dlc, np.uint8),
                   np.ascontiguousarray(data, np.uint8),
                   np.asarray(channels, np.uint8),
                   sorted(events, key=lambda f: f.t), dbc)
        return obj

    def _adopt(self, ts, ids, dlc, data, channels, events, dbc) -> None:
        self.dbc = dbc
        self._ts = ts
        self._ids = ids
        self._dlc = dlc
        self._data = data
        self._channels = channels
        self._events = events

        self._times_cache: Optional[np.ndarray] = None
        self._present_ids: Optional[set[int]] = None
        self._decode_probe: Optional[dict[int, dict]] = None
        self._by_id: dict[int, np.ndarray] = {}
        self._id_groups: Optional[dict[int, np.ndarray]] = None
        self._signal_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        # 与 _signal_cache 并行的一份 Python list 时间轴，专供 bisect：
        # 对 NumPy 数组做 bisect 每次比较都要新建一个 np.float64 对象，
        # 而 value_at() 在分析引擎里是几百万次量级的热点。
        self._bisect_times: dict[str, list] = {}
        self._decoded_messages: set[int] = set()
        # frame_id -> MessageDecoder / None（None = 已判定不适用，别再试）
        self._vec_decoders: dict[int, object] = {}
        self._vec_validated: dict[int, bool] = {}

    # -- properties --------------------------------------------------------- #
    @property
    def times(self) -> np.ndarray:
        """全部**唯一**时间戳，升序。

        惰性计算：500 万帧做一次 ``np.unique`` 约 0.5 s，而加载日志、画曲线、
        跑诊断都用不到它 —— 只有分析引擎按时间戳逐拍求值时才需要。
        """
        if self._times_cache is None:
            self._times_cache = np.unique(self._ts) if len(self._ts) else np.array([], dtype=np.float64)
        return self._times_cache

    # _ts 已按时间升序，首尾即为最小/最大，不必先物化 times 的唯一值集合
    @property
    def start_time(self) -> Optional[float]:
        return float(self._ts[0]) if len(self._ts) else None

    @property
    def end_time(self) -> Optional[float]:
        return float(self._ts[-1]) if len(self._ts) else None

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
        """DBC 里定义的全部信号名（与本日志是否录到无关）。"""
        if self.dbc is None:
            return []
        return sorted(self.dbc.signals.keys())

    def present_message_ids(self) -> set[int]:
        """本日志里**真实出现过**的报文 ID。

        "DBC 里定义了"和"这份日志里录到了"是两回事：装错日志时 DBC 照样能给出
        1861 个信号、坐标轴照样画得出来，只是每条曲线都是空的。要把这种情况说
        清楚，就得先知道日志里到底有哪些 ID。
        """
        if self._present_ids is None:
            self._present_ids = {int(i) for i in np.unique(self._ids)} if len(self._ids) else set()
        return self._present_ids

    # 每个报文抽验多少帧。判据是"这个 ID 的帧能不能按 DBC 解开"，是报文级的
    # 系统性事实（长度不符、DBC 装错），不需要全量解码——抽几帧就够，代价从
    # "解完整份日志"降到几十次 decode 调用。取多帧而非一帧是为了容忍个别坏帧。
    _PROBE_FRAMES = 8

    def message_decode_status(self) -> dict[int, dict]:
        """逐报文抽验解码结果: {mid: {frames, probed, ok, error}}。

        报文在日志里出现过 ≠ 它的信号解得出来。最典型的是 CAN-FD 长度不符
        （日志 12 字节 / DBC 24 字节），cantools 会整条拒绝，而
        :meth:`present_message_ids` 只看 ID 出现过——两者叠加就成了
        "界面显示有数据、曲线却全空、事件 0 条"的静默失败。这里把失败原因
        留下来，让上层能说清是"没录到"还是"没解开"。
        """
        if self._decode_probe is not None:
            return self._decode_probe
        out: dict[int, dict] = {}
        if self.dbc is None:
            self._decode_probe = out
            return out
        for mid in self.present_message_ids():
            idx = self._indices_for_id(mid)
            info = {"frames": int(len(idx)), "probed": 0, "ok": 0, "error": None}
            if mid not in self.dbc.messages:
                info["error"] = "DBC 中无此报文"
                out[mid] = info
                continue
            # 均匀抽样，避免只看开头几帧（有些日志开头是残缺帧）
            step = max(1, len(idx) // self._PROBE_FRAMES)
            for i in idx[::step][: self._PROBE_FRAMES]:
                info["probed"] += 1
                if self.dbc.decode(mid, self._frame_bytes(int(i))):
                    info["ok"] += 1
            if not info["ok"]:
                info["error"] = self._decode_error(mid, int(idx[0]))
            out[mid] = info
        self._decode_probe = out
        return out

    def _decode_error(self, mid: int, frame_idx: int) -> str:
        """抽一帧真解一次，把 cantools 的异常文本取出来当失败原因。"""
        try:
            self.dbc._db.decode_message(mid, self._frame_bytes(frame_idx))
        except Exception as e:
            return str(e)
        return "解码返回空"

    def decodable_message_ids(self) -> set[int]:
        """日志里出现过**且**能按当前 DBC 解开的报文 ID。"""
        return {mid for mid, s in self.message_decode_status().items() if s["ok"]}

    def decode_failures(self) -> list[dict]:
        """DBC 认识、日志里有、却一帧都解不开的报文，按帧数降序——用于顶栏告警。

        DBC 里根本没有的 ID 不算在内：那是"装错 DBC"，已由
        :meth:`unknown_message_ids` 单独报。这里只留可操作的那一类——
        两边都认这条报文，但长度/布局对不上。
        """
        out = []
        for mid, s in self.message_decode_status().items():
            if s["ok"] or not self.dbc or mid not in self.dbc.messages:
                continue
            m = self.dbc.messages.get(mid)
            out.append({
                "id": mid, "name": m.name if m else None,
                "frames": s["frames"], "error": s["error"],
                "log_bytes": int(self._dlc[self._indices_for_id(mid)[0]]),
                "dbc_bytes": m.length if m else None,
            })
        out.sort(key=lambda d: -d["frames"])
        return out

    def signals_with_data(self) -> list[str]:
        """本日志里真的有数据的信号（所属报文出现过**且**解得开）。"""
        if self.dbc is None:
            return []
        out: set[str] = set()
        for mid in self.decodable_message_ids():
            m = self.dbc.messages.get(mid)
            if m:
                out.update(s.name for s in m.signals)
        return sorted(out)

    def unknown_message_ids(self) -> list[int]:
        """日志里有、但当前 DBC 不认识的报文 ID —— DBC 装错时这个数会很大。"""
        known = set(self.dbc.messages) if self.dbc else set()
        return sorted(i for i in self.present_message_ids() if i not in known)

    # -- frame access ------------------------------------------------------- #
    def _frame_bytes(self, i: int) -> bytes:
        return self._data[i, :int(self._dlc[i])].tobytes()

    def _indices_for_id(self, frame_id: int) -> np.ndarray:
        if frame_id not in self._by_id:
            if self._id_groups is not None:
                self._by_id[frame_id] = self._id_groups.get(
                    frame_id, np.empty(0, dtype=np.intp))
            else:
                self._by_id[frame_id] = np.where(self._ids == frame_id)[0]
        return self._by_id[frame_id]

    def group_indices_by_id(self) -> dict[int, np.ndarray]:
        """一次性把帧下标按报文 ID 分组: {id: 升序下标数组}（缓存）。

        逐个 ID 做 ``np.where`` 在"扫描全部报文"的场景下是 O(报文数 × 帧数)：
        500 个 ID × 500 万帧要跑 25 亿次比较。这里一次 stable argsort 把同 ID
        的下标聚到一起再切片，总代价是一次排序。
        """
        if self._id_groups is None:
            groups: dict[int, np.ndarray] = {}
            if len(self._ids):
                order = np.argsort(self._ids, kind="stable")
                sorted_ids = self._ids[order]
                uniq, starts = np.unique(sorted_ids, return_index=True)
                bounds = list(starts) + [len(order)]
                for k, mid in enumerate(uniq):
                    groups[int(mid)] = order[bounds[k]:bounds[k + 1]]
            self._id_groups = groups
            # 已经算出来的分组顺带填进单 ID 缓存，省掉后续重复切片
            self._by_id.update(groups)
        return self._id_groups

    def message_frames(self, frame_id: int):
        """单条报文的紧凑数组切片 ``(ts, dlc, data, channels)``，按时间升序。

        :meth:`frame_arrays` 不含载荷；报文级体检要看 DLC 变化与载荷是否冻结，
        所以单独开这个访问器，避免诊断模块去摸 ``_data`` 这类私有字段。
        """
        idx = self._indices_for_id(int(frame_id))
        return (self._ts[idx], self._dlc[idx], self._data[idx], self._channels[idx])

    def _ensure_message_decoded(self, frame_id: int) -> None:
        """解开某个报文下所有信号的时间序列（首次访问时一次性完成）。

        这是"点一个功能按钮到曲线出来"的主延迟（十几万帧的报文逐帧调 cantools
        要 1.6 s），走两条快路：

        1. :mod:`canprobe.fast_decode` 把信号位置预编译成掩码/移位，用 NumPy
           整列抽位。上线前会和 cantools 逐字段对拍，不一致就整条降级。
        2. 降级路径按**去重后的载荷**调 cantools —— 静态报文里几千帧往往只有
           十几种不同载荷。

        两条路的数值与逐帧解码完全一致。
        """
        if frame_id in self._decoded_messages:
            return
        if self.dbc is None or frame_id not in self.dbc.messages:
            self._decoded_messages.add(frame_id)
            return
        idx = self._indices_for_id(frame_id)
        msg_signals = [s.name for s in self.dbc.messages[frame_id].signals]

        if len(idx) and self._decode_vectorized(frame_id, idx, msg_signals):
            self._decoded_messages.add(frame_id)
            return

        if len(idx):
            rows = self._data[idx]
            lens = self._dlc[idx]
            # 长度也要参与去重：同样的字节前缀在不同 DLC 下可能解不开 / 解出别的值
            keys = np.concatenate([lens[:, None], rows], axis=1)
            uniq, inverse = np.unique(keys, axis=0, return_inverse=True)
            inverse = np.asarray(inverse).ravel()
            decoded = [self.dbc.decode(frame_id, bytes(k[1:1 + int(k[0])])) for k in uniq]
        else:
            inverse = np.empty(0, dtype=np.intp)
            decoded = []

        # 逐信号收集：同一个 unique 载荷可能有的信号解得出、有的解不出
        ts_all = self._ts[idx]
        for s in msg_signals:
            if s in self._signal_cache:
                continue
            if not len(decoded):
                self._signal_cache[s] = _EMPTY_SERIES
                continue
            have = np.array([bool(d) and s in d for d in decoded], dtype=bool)
            if have.all():
                t = ts_all
                lut = [d[s] for d in decoded]
                v = np.empty(len(inverse), dtype=object)
                v[:] = [lut[j] for j in inverse.tolist()]
            elif not have.any():
                t = np.array([], dtype=np.float64)
                v = np.array([], dtype=object)
            else:
                keep = have[inverse]
                t = ts_all[keep]
                sub = inverse[keep]
                lut = [d[s] if (d and s in d) else None for d in decoded]
                v = np.empty(len(sub), dtype=object)
                v[:] = [lut[j] for j in sub.tolist()]
            self._signal_cache[s] = (t, v)
        self._decoded_messages.add(frame_id)

    def _decode_vectorized(self, frame_id: int, idx: np.ndarray,
                           msg_signals: list[str]) -> bool:
        """尝试用向量化抽位解开整条报文；不适用或对拍失败返回 False。"""
        dec = self._vec_decoders.get(frame_id, False)
        if dec is False:
            dec = fast_decode.build(self.dbc, frame_id)
            self._vec_decoders[frame_id] = dec
        if dec is None:
            return False

        # cantools 的 decode_message 默认 allow_excess=True / allow_truncated=False：
        # 短于 DBC 定义的帧整条解不开，长的则截断后照解。这里用同一条规则筛帧，
        # 保证"哪些帧有值"与逐帧解码时一致。
        lens = self._dlc[idx]
        keep = lens >= dec.length
        if not keep.any():
            return False
        rows = self._data[np.asarray(idx)[keep] if not keep.all() else idx, :dec.length]
        if rows.shape[1] < dec.length:
            return False

        if not self._vec_validated.get(frame_id, False):
            if not fast_decode.validate(dec, self.dbc, frame_id, rows):
                self._vec_decoders[frame_id] = None
                return False
            self._vec_validated[frame_id] = True

        t = self._ts[idx][keep] if not keep.all() else self._ts[idx]
        for p in dec.plans:
            if p.name in self._signal_cache:
                continue
            v = np.empty(len(t), dtype=object)
            v[:] = dec.signal_values(rows, p)
            self._signal_cache[p.name] = (t, v)
        # DBC 里定义了但 plan 没覆盖的信号不该悄悄消失（正常不会发生，
        # build() 要求所有信号都编译得出来）
        for s in msg_signals:
            self._signal_cache.setdefault(s, _EMPTY_SERIES)
        return True

    def _series(self, signal: str) -> tuple[np.ndarray, np.ndarray]:
        cached = self._signal_cache.get(signal)
        if cached is not None:
            return cached
        if self.dbc is None:
            return _EMPTY_SERIES
        msg_id = self.dbc._signal_to_message.get(signal)
        if msg_id is None:
            return _EMPTY_SERIES
        self._ensure_message_decoded(msg_id)
        return self._signal_cache.get(signal, _EMPTY_SERIES)

    # -- lookup ------------------------------------------------------------- #
    def _bisect_axis(self, signal: str) -> list:
        """供 ``bisect`` 用的纯 Python 时间轴。

        对 NumPy 数组做 ``bisect`` 时每次比较都要生成一个 ``np.float64`` 对象；
        换成 list 之后是纯 C 浮点比较。``value_at`` 在分析引擎里是百万次量级的
        热点，这一层缓存值得。
        """
        axis = self._bisect_times.get(signal)
        if axis is None:
            axis = self._series(signal)[0].tolist()
            self._bisect_times[signal] = axis
        return axis

    def value_at(self, signal: str, t: float):
        axis = self._bisect_times.get(signal)
        if axis is None:
            axis = self._bisect_axis(signal)
        if not axis:
            return None
        idx = bisect.bisect_right(axis, t) - 1
        if idx < 0:
            return None
        return self._signal_cache[signal][1][idx]

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
            # 绝大多数非枚举信号整条序列都是数值，直接向量化转换。
            # dbc_loader._plain() 保证每个值只可能是 str/bool/int/float/None：
            # str 会被 astype 悄悄转成数字（与逐值分支的 nan 不一致），先排掉；
            # None 会让 astype 抛 TypeError，落回逐值分支。
            if not any(v.__class__ is str for v in values):
                try:
                    return times, values.astype(np.float64)
                except (TypeError, ValueError):
                    pass
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
