"""向量化 BLF 读取器：直接把 Vector BLF 解成 NumPy 数组。

python-can 的 :class:`can.io.BLFReader` 每帧要构造一个 ``Message``、一个
``Decimal`` 时间戳，上层再包一层 ``Frame`` dataclass，最后 ``SeriesStore``
用 Python 循环把它们填进 NumPy —— 一份 132 MB / 498 万帧的日志要 18 s，
中途还驻留约 1.5 GB 的临时对象。真正的 IO 只占其中 1 s（zlib 解压 456 MB）。

这里绕开逐帧对象：把日志容器解压后拼成大块字节，**向量化扫描 ``LOBJ`` 对象签名**，
再按固定偏移量批量 gather 各字段。对象大小是可变的（CAN_FD_MESSAGE_64 在真实日志
里是 82~144 字节），所以不能用定长 stride 的结构化 dtype；改成"扫签名 + 校验链"：
候选偏移之间的间距必须落在 ``[obj_size, obj_size+7]``（python-can 也是在
``next_pos`` 起 8 字节窗口内找下一个 ``LOBJ``），不满足就退回串行修正。

字段偏移量与 ``can/io/blf.py`` 里的 struct 定义一一对应，输出与 ``BLFReader``
逐字节一致（见 ``tests/test_blf_fast.py``）。任何解析不了的情况都抛
:class:`BlfFastError`，由 :mod:`canprobe.log_parser` 回退到 python-can。
"""
from __future__ import annotations

import datetime
import struct
import zlib
from dataclasses import dataclass

import numpy as np

# --- 与 can/io/blf.py 对齐的常量 ------------------------------------------- #
_FILE_HEADER = struct.Struct("<4sLBBBBBBBBQQLL8H8H")
_OBJ_HEADER_BASE = struct.Struct("<4sHHLL")
_LOG_CONTAINER = struct.Struct("<H6xL4x")

CAN_MESSAGE = 1
LOG_CONTAINER = 10
CAN_ERROR_EXT = 73
CAN_MESSAGE2 = 86
CAN_FD_MESSAGE = 100
CAN_FD_MESSAGE_64 = 101

NO_COMPRESSION = 0
ZLIB_DEFLATE = 2

CAN_MSG_EXT = 0x80000000
TIME_TEN_MICS = 0x00000001

_OBJ_HEADER_SIZE = _OBJ_HEADER_BASE.size          # 16
_CAN_FD_MSG_64_SIZE = 40                          # CAN_FD_MSG_64_STRUCT.size

# 解压后按这个粒度成批做向量化解析，避免整份日志（可达数百 MB）同时驻留
_CHUNK_BYTES = 32 << 20

# python-can 在 next_pos 起 8 字节窗口内寻找下一个 LOBJ（对象后的对齐填充）
_MAX_PAD = 7


class BlfFastError(Exception):
    """快速通道解析不了，调用方应回退到 python-can。"""


@dataclass
class BlfArrays:
    """紧凑的帧数组，可直接交给 :meth:`SeriesStore.from_arrays`。

    ``data`` 是 (N, W) 的 uint8 矩阵，每行有效长度由 ``dlc`` 给出（右侧补零）。
    ``events`` 是通信层事件（错误帧），数量很少，仍用 ``Frame`` 对象表示。
    """
    ts: np.ndarray
    ids: np.ndarray
    dlc: np.ndarray
    data: np.ndarray
    channels: np.ndarray
    events: list


# --------------------------------------------------------------------------- #
# 小工具：从 uint8 视图里按偏移量批量取整数
# --------------------------------------------------------------------------- #
def _u8(b: np.ndarray, p: np.ndarray) -> np.ndarray:
    return b[p]


def _u16(b: np.ndarray, p: np.ndarray) -> np.ndarray:
    return b[p].astype(np.uint32) | (b[p + 1].astype(np.uint32) << 8)


def _u32(b: np.ndarray, p: np.ndarray) -> np.ndarray:
    return (b[p].astype(np.uint32)
            | (b[p + 1].astype(np.uint32) << 8)
            | (b[p + 2].astype(np.uint32) << 16)
            | (b[p + 3].astype(np.uint32) << 24))


def _u64(b: np.ndarray, p: np.ndarray) -> np.ndarray:
    out = np.zeros(len(p), dtype=np.uint64)
    for k in range(8):
        out |= b[p + k].astype(np.uint64) << np.uint64(8 * k)
    return out


def _systemtime_to_timestamp(st) -> float:
    """SYSTEMTIME → epoch 秒。与 can.io.blf.systemtime_to_timestamp 同语义。"""
    try:
        t = datetime.datetime(st[0], st[1], st[3], st[4], st[5], st[6],
                              st[7] * 1000, tzinfo=datetime.timezone.utc)
        return t.timestamp()
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# 对象定位：向量化扫签名 + 校验链
# --------------------------------------------------------------------------- #
def _scan_objects(b: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """返回 (offsets, obj_size, obj_type, header_size)，只含完整对象。

    先向量化找出所有 ``LOBJ`` 出现位置，再用"下一个对象必须紧跟在
    ``off + obj_size`` 之后 0~7 字节内"校验整条链。真实日志里这条链一次都不会断
    （132 MB 样本 498 万个对象零失配），断了才走串行修正剔除伪候选
    —— 载荷里凑巧出现 ``LOBJ`` 四字节是可能的。
    """
    n = len(b)
    if n < _OBJ_HEADER_SIZE:
        return (np.empty(0, np.int64),) * 4
    hit = ((b[:-3] == 0x4C) & (b[1:-2] == 0x4F)
           & (b[2:-1] == 0x42) & (b[3:] == 0x4A))
    cand = np.flatnonzero(hit).astype(np.int64)
    # 头部要完整才能读出 obj_size
    cand = cand[cand + _OBJ_HEADER_SIZE <= n]
    if not len(cand):
        return (np.empty(0, np.int64),) * 4

    osz = _u32(b, cand + 8).astype(np.int64)
    otype = _u16(b, cand + 12).astype(np.int64)
    hsize = _u16(b, cand + 4).astype(np.int64)

    complete = cand + osz <= n
    if not complete.all():
        # 只保留链上第一个不完整对象之前的部分；其余留给下一块
        cut = int(np.argmin(complete))
        cand, osz, otype, hsize = cand[:cut], osz[:cut], otype[:cut], hsize[:cut]
    if not len(cand):
        return (np.empty(0, np.int64),) * 4

    if cand[0] > _MAX_PAD:
        raise BlfFastError(f"容器起始处 {cand[0]} 字节内没有 LOBJ 对象头")

    gap = np.diff(cand)
    ok = (gap >= osz[:-1]) & (gap <= osz[:-1] + _MAX_PAD)
    if ok.all():
        return cand, osz, otype, hsize
    return _repair_chain(cand, osz, otype, hsize)


def _repair_chain(cand, osz, otype, hsize):
    """串行走一遍对象链，剔除载荷里凑巧出现的伪 ``LOBJ`` 候选。"""
    keep = [0]
    i = 0
    n = len(cand)
    while True:
        want = cand[i] + osz[i]
        j = int(np.searchsorted(cand, want, side="left"))
        if j >= n:
            break
        if j <= i:
            # obj_size 为 0 或指回自身：文件坏了，别在这儿转圈
            raise BlfFastError(f"偏移 {int(cand[i])} 处的对象大小无效（{int(osz[i])}）")
        if cand[j] > want + _MAX_PAD:
            raise BlfFastError(
                f"对象链在偏移 {int(cand[i])} 处断开（期望下一个对象在 "
                f"{int(want)}，实际 {int(cand[j])}）")
        keep.append(j)
        i = j
    idx = np.array(keep, dtype=np.int64)
    return cand[idx], osz[idx], otype[idx], hsize[idx]


# --------------------------------------------------------------------------- #
# 时间戳
# --------------------------------------------------------------------------- #
def _timestamps(b, off, start_ts: float) -> np.ndarray:
    """对象头里的 tick 计数 → epoch 秒。

    python-can 走 ``float(Decimal(ticks) * Decimal("1e-9"))``，是精确有理数再
    舍入一次。这里用 ``ticks / 1e9``：ticks 与 1e9 都能被 float64 精确表示，
    IEEE 除法同样是"精确商正确舍入"，两者逐位相同。乘 1e-9 则不然（1e-9 本身
    有表示误差），所以这里必须写成除法。
    """
    flags = _u32(b, off + 16)
    ticks = _u64(b, off + 24).astype(np.float64)
    ten_mics = flags == TIME_TEN_MICS
    out = np.where(ten_mics, ticks / 1e5, ticks / 1e9)
    return out + start_ts


# --------------------------------------------------------------------------- #
# 各报文类型的向量化字段提取
# --------------------------------------------------------------------------- #
def _gather_payload(b: np.ndarray, base: np.ndarray, length: np.ndarray,
                    cap: int) -> tuple[np.ndarray, np.ndarray]:
    """按 ``base`` 起始、``length`` 长度取出载荷矩阵（右侧补零）。"""
    width = int(length.max()) if len(length) else 0
    width = min(width, cap)
    if width <= 0:
        return np.zeros((len(base), 0), np.uint8), np.zeros(len(base), np.uint8)
    # 末尾补零，避免最后一帧的 gather 越界
    padded = np.concatenate([b, np.zeros(width + 8, np.uint8)])
    cols = np.arange(width)
    out = padded[base[:, None] + cols]
    out[cols[None, :] >= length[:, None]] = 0
    return out, length.astype(np.uint8)


def _extract_classic(b, off, osz, hsize, start_ts):
    """CAN_MESSAGE(1) / CAN_MESSAGE2(86)：payload 固定 8 字节。"""
    ts = _timestamps(b, off, start_ts)
    ch = _u16(b, off + 32).astype(np.int32) - 1
    dlc = _u8(b, off + 35).astype(np.int64)
    cid = _u32(b, off + 36)
    n = np.minimum(dlc, 8)
    data, dlc_out = _gather_payload(b, off + 40, n, 8)
    return ts, cid, dlc_out, data, ch


def _extract_fd(b, off, osz, hsize, start_ts):
    """CAN_FD_MESSAGE(100)：payload 固定 64 字节，有效长度是 validDataBytes。"""
    ts = _timestamps(b, off, start_ts)
    ch = _u16(b, off + 32).astype(np.int32) - 1
    cid = _u32(b, off + 36)
    valid = _u8(b, off + 46).astype(np.int64)
    n = np.minimum(valid, 64)
    data, dlc_out = _gather_payload(b, off + 52, n, 64)
    return ts, cid, dlc_out, data, ch


def _extract_fd64(b, off, osz, hsize, start_ts):
    """CAN_FD_MESSAGE_64(101)：payload 长度可变。

    ``validDataBytes`` 可能大于对象里实际带的字节数（python-can issue #1905），
    所以取 ``min(valid, (extDataOffset or obj_size) - header_size - 40)``，
    再按 ``valid`` 右补零 —— 与 CANoe / binlog.dll 行为一致。
    """
    ts = _timestamps(b, off, start_ts)
    ch = _u8(b, off + 32).astype(np.int32) - 1
    valid = _u8(b, off + 34).astype(np.int64)
    cid = _u32(b, off + 36)
    edo = _u8(b, off + 67).astype(np.int64)
    limit = np.where(edo > 0, edo, osz) - hsize - _CAN_FD_MSG_64_SIZE
    avail = np.clip(np.minimum(valid, limit), 0, None)
    # 有效长度取 valid（不足部分补零），但只从对象里读 avail 个字节
    width = int(valid.max()) if len(valid) else 0
    width = min(width, 64)
    if width <= 0:
        data = np.zeros((len(off), 0), np.uint8)
    else:
        padded = np.concatenate([b, np.zeros(width + 8, np.uint8)])
        cols = np.arange(width)
        data = padded[(off + 72)[:, None] + cols]
        data[cols[None, :] >= avail[:, None]] = 0
    return ts, cid, np.minimum(valid, 64).astype(np.uint8), data, ch


_EXTRACTORS = {
    CAN_MESSAGE: _extract_classic,
    CAN_MESSAGE2: _extract_classic,
    CAN_FD_MESSAGE: _extract_fd,
    CAN_FD_MESSAGE_64: _extract_fd64,
}
_EXTRACTOR_TYPES = np.array(sorted(_EXTRACTORS), dtype=np.int64)

# python-can 错误帧的 arbitration_id 高位编码错误类型（SocketCAN 惯例）
_ERR_FLAGS = {
    0x00000001: "stuff", 0x00000002: "form", 0x00000004: "ack",
    0x00000008: "bit", 0x00000010: "bit", 0x00000020: "crc",
}


def _extract_errors(b, off, osz, hsize, start_ts) -> list:
    """CAN_ERROR_EXT(73) → Frame 事件。数量很少，走标量路径即可。"""
    from .log_parser import Frame

    ts = _timestamps(b, off, start_ts)
    ch = _u16(b, off + 32).astype(np.int32) - 1
    cid = _u32(b, off + 48)
    out = []
    for i in range(len(off)):
        arb = int(cid[i]) & 0x1FFFFFFF
        et = "other"
        for flag, name in _ERR_FLAGS.items():
            if arb & flag:
                et = name
                break
        out.append(Frame(t=float(ts[i]), frame_id=arb, data=b"",
                         channel=int(np.uint8(ch[i])),
                         is_extended=bool(int(cid[i]) & CAN_MSG_EXT),
                         kind="error", error_type=et))
    return out


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _iter_container_data(path: str):
    """顺序读顶层对象，产出每个 LOG_CONTAINER 解压后的字节，外加起始时间戳。"""
    with open(path, "rb") as f:
        head = f.read(_FILE_HEADER.size)
        if len(head) < _FILE_HEADER.size:
            raise BlfFastError("文件太短，不是 BLF")
        header = _FILE_HEADER.unpack(head)
        if header[0] != b"LOGG":
            raise BlfFastError("文件头不是 LOGG，不是 BLF")
        start_ts = _systemtime_to_timestamp(header[14:22])
        f.read(header[1] - _FILE_HEADER.size)
        yield start_ts, None

        while True:
            raw = f.read(_OBJ_HEADER_SIZE)
            if not raw:
                return
            if len(raw) < _OBJ_HEADER_SIZE:
                raise BlfFastError("文件尾部对象头不完整")
            sig, _hs, _hv, osz, otype = _OBJ_HEADER_BASE.unpack(raw)
            if sig != b"LOBJ":
                raise BlfFastError("顶层对象签名不是 LOBJ")
            body = f.read(osz - _OBJ_HEADER_SIZE)
            f.read(osz % 4)                       # python-can 同款对齐填充
            if otype != LOG_CONTAINER:
                continue
            method, _usz = _LOG_CONTAINER.unpack_from(body)
            payload = body[_LOG_CONTAINER.size:]
            if method == NO_COMPRESSION:
                yield None, payload
            elif method == ZLIB_DEFLATE:
                yield None, zlib.decompress(payload)
            else:
                raise BlfFastError(f"未知的容器压缩方式 {method}")


def read_blf_arrays(path: str) -> BlfArrays:
    """读取 BLF，返回紧凑的 :class:`BlfArrays`。失败抛 :class:`BlfFastError`。"""
    gen = _iter_container_data(path)
    start_ts, _ = next(gen)

    parts: list[tuple] = []       # (ts, ids, dlc, data, channels)
    events: list = []
    buffered: list[bytes] = []
    buffered_len = 0
    tail = b""

    def flush() -> None:
        """解析一批容器数据，产出**保持原始顺序**的一组数组。

        按类型分开做向量化提取后要散射回原顺序：同一个块里可能既有经典帧又有
        FD 帧，直接按类型拼会把时间顺序打乱，而下游 ``SeriesStore`` 的稳定排序
        依赖"同一时刻的帧保持录制顺序"。
        """
        nonlocal buffered, buffered_len, tail
        if not buffered:
            return
        buf = tail + b"".join(buffered) if tail else b"".join(buffered)
        buffered, buffered_len = [], 0
        b = np.frombuffer(buf, np.uint8)
        off, osz, otype, hsize = _scan_objects(b)
        if not len(off):
            tail = buf
            return
        tail = buf[int(off[-1] + osz[-1]):]

        is_can = np.isin(otype, _EXTRACTOR_TYPES)
        n_can = int(is_can.sum())
        if n_can:
            slot_of = np.cumsum(is_can) - 1        # 每个对象在本块 CAN 帧中的序号
            found = []
            for t, fn in _EXTRACTORS.items():
                sel = otype == t
                if not sel.any():
                    continue
                found.append((slot_of[sel], fn(b, off[sel], osz[sel], hsize[sel], start_ts)))
            if len(found) == 1:
                # 常见情况：整块只有一种报文类型，提取结果本来就是原顺序
                ts_, ids_, dlc_, data_, ch_ = found[0][1]
                parts.append((ts_, ids_.astype(np.uint32), dlc_.astype(np.uint8),
                              data_, ch_.astype(np.uint8)))
            else:
                width = max(c[1][3].shape[1] for c in found)
                ts_c = np.empty(n_can, np.float64)
                ids_c = np.empty(n_can, np.uint32)
                dlc_c = np.empty(n_can, np.uint8)
                ch_c = np.empty(n_can, np.uint8)
                data_c = np.zeros((n_can, width), np.uint8)
                for dest, (ts_, ids_, dlc_, data_, ch_) in found:
                    ts_c[dest] = ts_
                    ids_c[dest] = ids_
                    dlc_c[dest] = dlc_
                    ch_c[dest] = ch_.astype(np.uint8)
                    data_c[dest[:, None], np.arange(data_.shape[1])] = data_
                parts.append((ts_c, ids_c, dlc_c, data_c, ch_c))

        sel = otype == CAN_ERROR_EXT
        if sel.any():
            events.extend(_extract_errors(b, off[sel], osz[sel], hsize[sel], start_ts))

    for _st, data in gen:
        buffered.append(data)
        buffered_len += len(data)
        if buffered_len >= _CHUNK_BYTES:
            flush()
    flush()

    if not parts:
        if not events:
            # 一帧都没认出来：与其让上层报"文件里没有 CAN 报文"，不如让它回退到
            # python-can —— 也许只是我们没实现那种对象类型
            raise BlfFastError("快速通道没有解析出任何 CAN 帧")
        return BlfArrays(
            ts=np.zeros(0, np.float64), ids=np.zeros(0, np.uint32),
            dlc=np.zeros(0, np.uint8), data=np.zeros((0, 0), np.uint8),
            channels=np.zeros(0, np.uint8), events=sorted(events, key=lambda f: f.t))

    # 各块之间天然按录制顺序衔接，直接拼接即可（时间排序交给 SeriesStore）
    ts = np.concatenate([p[0] for p in parts])
    ids = np.concatenate([p[1] for p in parts]).astype(np.uint32) & 0x1FFFFFFF
    dlc = np.concatenate([p[2] for p in parts]).astype(np.uint8)
    channels = np.concatenate([p[4] for p in parts]).astype(np.uint8)
    width = max(p[3].shape[1] for p in parts)
    if len(parts) == 1:
        data = parts[0][3]
    else:
        data = np.zeros((len(ts), width), np.uint8)
        pos = 0
        for p in parts:
            d = p[3]
            data[pos:pos + len(d), :d.shape[1]] = d
            pos += len(d)

    events.sort(key=lambda f: f.t)
    return BlfArrays(ts=ts, ids=ids, dlc=dlc, data=data,
                     channels=channels, events=events)
