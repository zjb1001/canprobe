"""CAN trace/log parsers.

Supported formats (auto-detected by extension, with content sniffing as a
fallback):

* ``.asc``  — Vector CANalyzer/CANoe ASCII logging format
* ``.csv``  — flexible CSV: timestamp, id, (dlc), data (hex text or ints)
* ``.json`` — ``[{"t": 0.0, "id": 291, "data": "00 11 22"}]``
* ``.trc``  — PEAK Trace format
* ``.blf``  — Vector BLF (optional, needs ``python-can``)
* ``.mf4``  — ASAM MDF4 (optional, needs ``asammdf``)

Every parser returns a list of :class:`Frame` sorted by time.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Frame:
    t: float
    frame_id: int
    data: bytes
    channel: int = 0
    is_extended: bool = False
    # 通信层事件字段（非数据帧使用；数据帧保持默认值）
    kind: str = "data"  # data | error | status | remote | overload
    error_type: str = ""  # stuff | form | crc | ack | bit | other | ""
    tec: Optional[int] = None  # 发送错误计数
    rec: Optional[int] = None  # 接收错误计数
    state: str = ""  # active | passive | bus_off | ""
    direction: str = ""  # rx | tx | ""

    @property
    def is_error(self) -> bool:
        """是否为通信层事件（非普通数据帧）。"""
        return self.kind != "data"

    def to_dict(self) -> dict:
        d = {
            "t": self.t,
            "id": self.frame_id,
            "data": self.data.hex(" "),
            "channel": self.channel,
            "is_extended": self.is_extended,
        }
        if self.kind != "data":
            d["kind"] = self.kind
            if self.error_type:
                d["error_type"] = self.error_type
            if self.tec is not None:
                d["tec"] = self.tec
            if self.rec is not None:
                d["rec"] = self.rec
            if self.state:
                d["state"] = self.state
            if self.direction:
                d["direction"] = self.direction
        return d


_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _parse_hex_bytes(text: str) -> Optional[bytes]:
    """Parse '00 11 aa', '0011aa', or '0x00 0x11' style hex into bytes."""
    text = text.strip().replace(",", " ")
    if text.startswith("0x") or text.startswith("0X"):
        parts = text.split()
        try:
            return bytes(int(p, 16) for p in parts)
        except ValueError:
            return None
    parts = text.split()
    if parts and _HEX_RE.match("".join(parts)) and len(parts) > 1:
        try:
            return bytes(int(p, 16) for p in parts)
        except ValueError:
            return None
    if _HEX_RE.match(text) and len(text) % 2 == 0:
        try:
            return bytes.fromhex(text)
        except ValueError:
            return None
    # last resort: space separated single bytes
    try:
        return bytes(int(p, 16) for p in parts if p)
    except ValueError:
        return None


def _parse_int(x: str) -> Optional[int]:
    x = x.strip()
    if not x:
        return None
    try:
        if x.lower().startswith("0x"):
            return int(x, 16)
        if re.match(r"^[0-9a-fA-F]{1,8}$", x) and any(c in "abcdefABCDEF" for c in x):
            return int(x, 16)
        return int(x)
    except ValueError:
        return None


class ParseError(Exception):
    pass


# --------------------------------------------------------------------------- #
# ASC
# --------------------------------------------------------------------------- #
_ASC_BASE_RE = re.compile(r"^\s*base\s+(\w+)\s+timestamps\s+(\w+)", re.IGNORECASE)
_ASC_DATA_RE = re.compile(
    r"^\s*(?P<t>\d+(?:\.\d+)?)\s+(?P<ch>\d+)\s+(?P<id>[0-9a-fA-Fx]+)\s+(?P<dir>Rx|Tx)\s+d\s+(?P<dlc>\d+)\s+(?P<data>[0-9a-fA-F ]+)\s*$"
)
_ASC_EVENT_TS_RE = re.compile(r"^\s*(?P<t>\d+(?:\.\d+)?)\s+(?P<ch>\d+)\s+(?P<rest>.*)$")


def _parse_asc_event(line: str) -> Optional[Frame]:
    """Best-effort 解析 Vector ASC 的错误帧 / 总线状态事件行。

    ASC 各版本错误帧语法不一（Vector 版本演化），此处做启发式关键词匹配，
    能力由诊断引擎的 Capabilities 显式声明，缺失时降级而非静默。
    """
    m = _ASC_EVENT_TS_RE.match(line)
    if not m:
        return None
    rl = m.group("rest").lower()
    if "errorframe" in rl or "error frame" in rl:
        et = "other"
        for kw, name in (("stuff", "stuff"), ("form", "form"), ("crc", "crc"),
                         ("ack", "ack"), ("bit", "bit")):
            if kw in rl:
                et = name
                break
        return Frame(t=float(m.group("t")), frame_id=0, data=b"",
                     channel=int(m.group("ch")), kind="error", error_type=et)
    if "busoff" in rl or "bus off" in rl:
        return Frame(t=float(m.group("t")), frame_id=0, data=b"",
                     channel=int(m.group("ch")), kind="status", state="bus_off")
    if "error passive" in rl or "errorpassive" in rl:
        return Frame(t=float(m.group("t")), frame_id=0, data=b"",
                     channel=int(m.group("ch")), kind="status", state="passive")
    if "error active" in rl or "erroractive" in rl:
        return Frame(t=float(m.group("t")), frame_id=0, data=b"",
                     channel=int(m.group("ch")), kind="status", state="active")
    return None


def parse_asc(text: str) -> list[Frame]:
    base = 16
    is_absolute = False
    start_offset: Optional[float] = None
    frames: list[Frame] = []
    raw_offsets: list[tuple[float, int, bytes]] = []

    for line in text.splitlines():
        line = line.rstrip()
        m = _ASC_BASE_RE.match(line)
        if m:
            base_str = m.group(1).lower()
            base = 16 if base_str in ("hex", "hexadecimal") else 10
            is_absolute = m.group(2).lower() in ("absolute", "abs")
            continue
        if "Begin Triggerblock" in line or "Start of measurement" in line:
            continue
        m = _ASC_DATA_RE.match(line)
        if not m:
            ev = _parse_asc_event(line)
            if ev is not None:
                frames.append(ev)  # 错误帧/状态事件（相对时间戳）
            continue
        t = float(m.group("t"))
        frame_id = int(m.group("id"), base)
        data = _parse_hex_bytes(m.group("data"))
        if data is None:
            continue
        if is_absolute:
            # absolute timestamps are seconds-since-epoch; keep raw and re-base later
            raw_offsets.append((t, frame_id, data))
        else:
            frames.append(Frame(t=t, frame_id=frame_id, data=data))

    if raw_offsets and frames:
        # mix of absolute and relative is unlikely; if we only have absolute,
        # re-base to zero.
        pass
    if raw_offsets:
        if start_offset is None:
            start_offset = raw_offsets[0][0]
        for t, fid, data in raw_offsets:
            frames.append(Frame(t=t - start_offset, frame_id=fid, data=data))

    frames.sort(key=lambda f: f.t)
    return frames


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
_TS_ALIASES = {"timestamp", "time", "t", "t[ms]", "time[ms]", "t[us]", "time[us]", "ts"}
_ID_ALIASES = {"id", "canid", "can_id", "identifier", "arbitration_id", "frame_id", "msgid"}
_DATA_ALIASES = {"data", "payload", "bytes", "data_bytes", "d", "raw"}


def _col_index(header: list[str], aliases: set[str]) -> Optional[int]:
    for i, h in enumerate(header):
        if h.strip().lower() in aliases:
            return i
    return None


def _timestamp_scale(header: list[str]) -> float:
    """Return multiplier to convert the timestamp column to seconds."""
    for h in header:
        hl = h.strip().lower()
        if hl in ("t[ms]", "time[ms]", "timestamp[ms]", "time_ms", "tms"):
            return 1e-3
        if hl in ("t[us]", "time[us]", "timestamp[us]", "time_us", "tus"):
            return 1e-6
        if hl in ("t[ns]", "time[ns]"):
            return 1e-9
    return 1.0


def parse_csv(text: str) -> list[Frame]:
    # Drop leading empty lines to locate header more reliably.
    sample = text.lstrip("﻿\r\n")
    dialect = csv.Sniffer().sniff(sample[:4096], delimiters=",;\t ")
    reader = csv.reader(io.StringIO(sample), dialect)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return []

    header = rows[0]
    # detect header row: contains text column names
    is_header = any(
        h.strip().lower() in (_TS_ALIASES | _ID_ALIASES | _DATA_ALIASES)
        or h.strip().lower() in {"channel", "dir", "direction", "dlc"}
        for h in header
    )

    t_idx = _col_index(header, _TS_ALIASES) if is_header else 0
    id_idx = _col_index(header, _ID_ALIASES) if is_header else 1
    data_idx = _col_index(header, _DATA_ALIASES) if is_header else 2
    scale = _timestamp_scale(header) if is_header else 1.0

    data_rows = rows[1:] if is_header else rows
    frames: list[Frame] = []
    for r in data_rows:
        if len(r) <= max(x for x in (t_idx, id_idx, data_idx) if x is not None):
            continue
        try:
            t = float(r[t_idx]) * scale
        except (ValueError, IndexError):
            continue
        fid = _parse_int(r[id_idx])
        if fid is None:
            continue
        raw = r[data_idx].strip()
        if raw and re.match(r"^\d+$", raw):
            # single decimal number -> treat as a data byte list of one? skip
            data = None
        else:
            data = _parse_hex_bytes(raw)
        if data is None:
            continue
        frames.append(Frame(t=t, frame_id=fid, data=data))

    frames.sort(key=lambda f: f.t)
    return frames


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #
def parse_json(text: str) -> list[Frame]:
    obj = json.loads(text)
    if isinstance(obj, dict):
        obj = obj.get("frames", obj.get("data", []))
    frames: list[Frame] = []
    for item in obj:
        try:
            t = float(item.get("t", item.get("timestamp", item.get("time", 0.0))))
            fid = int(item.get("id", item.get("frame_id", item.get("can_id"))))
        except (TypeError, ValueError):
            continue
        d = item.get("data", item.get("payload", item.get("bytes")))
        if isinstance(d, list):
            data = bytes(int(x) & 0xFF for x in d)
        elif isinstance(d, str):
            data = _parse_hex_bytes(d)
        else:
            continue
        if data is None:
            continue
        frames.append(Frame(t=t, frame_id=fid, data=data, channel=int(item.get("channel", 0) or 0)))
    frames.sort(key=lambda f: f.t)
    return frames


# --------------------------------------------------------------------------- #
# TRC (PEAK)
# --------------------------------------------------------------------------- #
def parse_trc(text: str) -> list[Frame]:
    frames: list[Frame] = []
    for line in text.splitlines():
        m = re.match(
            r"^\s*(?P<t>\d+\.?\d*)\s+(?P<id>[0-9a-fA-F]+)\s+(?P<dir>Rx|Tx)\s+d\s+(?P<dlc>\d+)\s+(?P<data>[0-9a-fA-F ]+)\s*$",
            line,
        )
        if not m:
            # alternate TRC layout: time  id  Rx  length  data
            m2 = re.match(
                r"^\s*(?P<t>\d+\.?\d*)\s+(?P<id>[0-9a-fA-F]+)\s+(?P<dir>Rx|Tx)\s+(?P<dlc>\d+)\s+(?P<data>[0-9a-fA-F ]+)\s*$",
                line,
            )
            m = m2
        if not m:
            continue
        t = float(m.group("t"))
        fid = int(m.group("id"), 16)
        data = _parse_hex_bytes(m.group("data"))
        if data is None:
            continue
        frames.append(Frame(t=t, frame_id=fid, data=data))
    frames.sort(key=lambda f: f.t)
    return frames


# --------------------------------------------------------------------------- #
# BLF / MF4 (optional heavy formats)
# --------------------------------------------------------------------------- #
# python-can 错误帧的 arbitration_id 高位编码错误类型（SocketCAN 惯例），best-effort 映射
_BLF_ERR_FLAGS = {
    0x00000001: "stuff", 0x00000002: "form", 0x00000004: "ack",
    0x00000008: "bit", 0x00000010: "bit", 0x00000020: "crc",
}


def _blf_frame(msg) -> Frame:
    """把 python-can 的 Message 转成 Frame，识别错误帧。"""
    arb = int(getattr(msg, "arbitration_id", 0))
    is_err = bool(getattr(msg, "is_error_frame", False))
    if is_err:
        et = "other"
        for flag, name in _BLF_ERR_FLAGS.items():
            if arb & flag:
                et = name
                break
        return Frame(
            t=float(msg.timestamp), frame_id=arb & 0x1FFFFFFF, data=b"",
            channel=int(getattr(msg, "channel", 0) or 0),
            is_extended=bool(getattr(msg, "is_extended_id", False)),
            kind="error", error_type=et,
        )
    return Frame(
        t=float(msg.timestamp),
        frame_id=arb,
        data=bytes(msg.data),
        channel=int(getattr(msg, "channel", 0) or 0),
        is_extended=bool(msg.is_extended_id),
    )


def parse_blf(path: str) -> list[Frame]:
    try:
        import can
        from can.io import BLFReader
    except Exception as e:  # pragma: no cover - optional dep
        raise ParseError(f"BLF 需要 python-can，未安装或不可用: {e}")

    frames: list[Frame] = []
    with BLFReader(path) as reader:
        for msg in reader:
            frames.append(_blf_frame(msg))
    frames.sort(key=lambda f: f.t)
    return frames


def blf_arrays(path: str):
    """BLF → 紧凑 NumPy 数组（快速通道）。

    走 :mod:`canprobe.blf_fast`，跳过逐帧 ``Message`` / :class:`Frame` 对象。
    快速通道解析不了时返回 ``None``，由调用方回退到 :func:`parse_blf`。
    """
    from .blf_fast import BlfFastError, read_blf_arrays

    try:
        return read_blf_arrays(path)
    except BlfFastError:
        return None
    except Exception:
        # 快速通道任何意外都不该让日志打不开——回退到 python-can
        return None


# CAN-FD DLC code → payload length in bytes. For 0..8 the code *is* the length;
# 9..15 are the FD escape codes. MF4 stores the raw 4-bit code in
# ``CAN_DataFrame.DLC``, so using it directly as a byte count silently truncates
# every FD frame — a 24-byte WCBS_Info arrives as 12 bytes and cantools then
# rejects the whole message ("Wrong data size: 12 instead of 24"), which reads
# downstream as "this signal was never recorded" rather than "decode failed".
_FD_DLC_TO_LEN = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


def parse_mf4(path: str) -> list[Frame]:
    """Extract raw CAN frames from a Vector bus-logging MF4 (via asammdf).

    Mirrors asammdf's own CAN_DataFrame extraction: bus-event channel groups
    expose a composite ``CAN_DataFrame`` whose sub-channels hold ID / DLC /
    DataBytes / BusChannel, aligned on the master timestamps.

    Payload length comes from ``CAN_DataFrame.DataLength`` when the file has it
    (Vector writes it for FD logs); otherwise the DLC code is expanded through
    :data:`_FD_DLC_TO_LEN`. Both are capped by the actual DataBytes row width,
    which MF4 pads to a fixed 64 bytes.
    """
    try:
        import numpy as _np
        from asammdf import MDF
    except Exception as e:  # pragma: no cover - optional dep
        raise ParseError(f"MF4 需要 asammdf，未安装或不可用: {e}")

    mdf = MDF(path)
    frames: list[Frame] = []
    for gidx, group in enumerate(mdf.groups):
        names = {ch.name for ch in group.channels}
        if "CAN_DataFrame" not in names and "CAN_RemoteFrame" not in names:
            continue
        try:
            data = mdf.get("CAN_DataFrame", gidx)
        except Exception:
            continue
        ts = _np.asarray(data.timestamps, dtype=_np.float64)
        ids = _np.asarray(data["CAN_DataFrame.ID"]).astype(_np.uint32) & 0x1FFFFFFF
        dlc = _np.asarray(data["CAN_DataFrame.DLC"]).astype(_np.uint8)
        db = data["CAN_DataFrame.DataBytes"]
        if "CAN_DataFrame.DataLength" in names:
            dlen = _np.asarray(data["CAN_DataFrame.DataLength"]).astype(_np.uint16)
        else:
            dlen = None
        if "CAN_DataFrame.BusChannel" in names:
            bus = _np.asarray(data["CAN_DataFrame.BusChannel"]).astype(_np.uint8)
        else:
            bus = _np.zeros(len(ts), dtype=_np.uint8)
        for i in range(len(ts)):
            raw = bytes(db[i])
            if dlen is not None:
                n = int(dlen[i])
            else:
                n = _FD_DLC_TO_LEN[int(dlc[i]) & 0x0F]
            frames.append(Frame(
                t=float(ts[i]), frame_id=int(ids[i]),
                data=raw[: min(n, len(raw))], channel=int(bus[i]),
                is_extended=bool(int(ids[i]) > 0x7FF),
            ))
    if not frames:
        raise ParseError(f"MF4 文件 {os.path.basename(path)} 中未找到 CAN_DataFrame 通道")
    frames.sort(key=lambda f: f.t)
    return frames


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
EXT_PARSERS = {
    ".asc": lambda p: parse_asc(_read(p)),
    ".csv": lambda p: parse_csv(_read(p)),
    ".log": lambda p: parse_asc(_read(p)),
    ".trc": lambda p: parse_trc(_read(p)),
    ".json": lambda p: parse_json(_read(p)),
    ".blf": parse_blf,
    ".mf4": parse_mf4,
}


def _read(path: str) -> str:
    for enc in ("utf-8", "latin-1", "gbk", "utf-16"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def sniff_and_parse(path: str) -> list[Frame]:
    """Parse a trace file by extension, with a couple of content fallbacks."""
    import os

    ext = os.path.splitext(path)[1].lower()
    parser = EXT_PARSERS.get(ext)
    if parser is None:
        raise ParseError(f"不支持的文件类型: {ext or '(无扩展名)'}，支持 asc/csv/json/trc/blf/mf4")

    frames = parser(path)
    if not frames:
        raise ParseError(f"文件 {os.path.basename(path)} 中未解析到任何 CAN 报文")
    return frames


# --------------------------------------------------------------------------- #
# 流式解析（增量加载超大日志，避免一次性读入整段文本）
# --------------------------------------------------------------------------- #
def _stream_lines(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            yield line.rstrip("\n")


def _iter_csv(path: str):
    import csv as _csv

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        sample = f.read(4096).lstrip("﻿\r\n")
        f.seek(0)
        try:
            dialect = _csv.Sniffer().sniff(sample, delimiters=",;\t ")
        except _csv.Error:
            dialect = _csv.excel
        reader = _csv.reader(f, dialect)
        rows = (r for r in reader if any(c.strip() for c in r))
        header = next(rows, None)
        if header is None:
            return
        is_header = any(
            h.strip().lower() in (_TS_ALIASES | _ID_ALIASES | _DATA_ALIASES)
            or h.strip().lower() in {"channel", "dir", "direction", "dlc"}
            for h in header
        )
        t_idx = _col_index(header, _TS_ALIASES) if is_header else 0
        id_idx = _col_index(header, _ID_ALIASES) if is_header else 1
        data_idx = _col_index(header, _DATA_ALIASES) if is_header else 2
        scale = _timestamp_scale(header) if is_header else 1.0
        for r in rows:
            try:
                t = float(r[t_idx]) * scale
                fid = _parse_int(r[id_idx])
                data = _parse_hex_bytes(r[data_idx].strip()) if len(r) > data_idx else None
            except (ValueError, IndexError):
                continue
            if fid is None or data is None:
                continue
            yield Frame(t=t, frame_id=fid, data=data)


def _iter_asc(path: str):
    base = 16
    is_absolute = False
    offset = None
    for line in _stream_lines(path):
        m = _ASC_BASE_RE.match(line)
        if m:
            base = 16 if m.group(1).lower() in ("hex", "hexadecimal") else 10
            is_absolute = m.group(2).lower() in ("absolute", "abs")
            continue
        m = _ASC_DATA_RE.match(line)
        if not m:
            ev = _parse_asc_event(line)
            if ev is not None:
                yield ev
            continue
        t = float(m.group("t"))
        if is_absolute:
            if offset is None:
                offset = t
            t = t - offset
        fid = int(m.group("id"), base)
        data = _parse_hex_bytes(m.group("data"))
        if data is None:
            continue
        yield Frame(t=t, frame_id=fid, data=data)


def _iter_trc(path: str):
    for line in _stream_lines(path):
        m = re.match(
            r"^\s*(?P<t>\d+\.?\d*)\s+(?P<id>[0-9a-fA-F]+)\s+(?P<dir>Rx|Tx)\s+(?P<dlc>\d+)\s+(?P<data>[0-9a-fA-F ]+)\s*$",
            line,
        )
        if not m:
            continue
        data = _parse_hex_bytes(m.group("data"))
        if data is None:
            continue
        yield Frame(t=float(m.group("t")), frame_id=int(m.group("id"), 16), data=data)


def _iter_blf(path: str):
    try:
        from can.io import BLFReader
    except Exception as e:
        raise ParseError(f"BLF 需要 python-can: {e}")
    with BLFReader(path) as reader:
        for msg in reader:
            yield _blf_frame(msg)


def iter_frames(path: str, t0=None, t1=None, max_frames=None):
    """Stream a trace file, yielding :class:`Frame` (optionally filtered).

    Used for incremental loading of very large logs: frames are produced
    line-by-line and can be restricted to a time window / count.
    """
    import os

    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        gen = _iter_csv(path)
    elif ext in (".asc", ".log"):
        gen = _iter_asc(path)
    elif ext == ".trc":
        gen = _iter_trc(path)
    elif ext == ".blf":
        gen = _iter_blf(path)
    elif ext == ".json":
        gen = iter(parse_json(_read(path)))
    elif ext == ".mf4":
        gen = iter(parse_mf4(path))
    else:
        raise ParseError(f"不支持的文件类型: {ext or '(无扩展名)'}，支持 asc/csv/json/trc/blf/mf4")

    count = 0
    for f in gen:
        if t0 is not None and f.t < t0:
            continue
        if t1 is not None and f.t > t1:
            continue
        yield f
        count += 1
        if max_frames is not None and count >= max_frames:
            return

