"""按报文批量解信号：用 NumPy 一次性抽位，替掉逐帧调 cantools。

``cantools`` 的 ``decode_message`` 每帧要走一遍 bitstruct、再逐信号做一次
``raw_to_scaled``。一条 10 ms 周期的报文在 10 分钟的日志里有十几万帧，画一条曲线
就要 1.6 s —— 这是"点一个功能按钮到曲线出来"的主延迟。真实车载日志里载荷几乎
帧帧不同（滚动计数 + CRC），所以靠载荷去重救不了，只能把抽位本身向量化。

做法是把每个信号在报文里的位置预编译成 (起始字节, 字节数, 右移量, 掩码)，
然后对整列帧做一次 uint64 拼装 + 移位 + 掩码，最后按 cantools 的转换规则
（``database/conversion.py``）还原成完全相同的 Python 值类型。

**自校验**：编译出来的解码器上线前会拿一批真实帧和 cantools 逐字段对拍
（值和类型都比），任何不一致就整条报文退回 cantools。所以这里的"快"不以
"可能不对"为代价 —— 遇到多路复用、浮点、超长信号等本模块没实现的情形，
对拍会失败，自动降级。
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

# 位抽取用 uint64 拼装，所以一个信号最多跨 8 个字节
_MAX_SPAN_BYTES = 8

# 上线前和 cantools 对拍的帧数。取这么多是为了覆盖不同载荷分布，
# 代价只有几十次 decode，相对于省下的十几万次可以忽略。
_VALIDATE_FRAMES = 96


class _Plan:
    """一个信号的抽位 + 转换方案。"""
    __slots__ = ("name", "byte_start", "nbytes", "shift", "mask", "big_endian",
                 "signed", "half", "full", "kind", "scale", "offset", "choices")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _plan_for(sig, msg_length: int) -> Optional[_Plan]:
    """把一个 cantools Signal 编译成抽位方案；不支持的形态返回 None。"""
    if sig.is_float:
        return None                      # 浮点原始类型没实现，退回 cantools
    length = int(sig.length)
    if length <= 0 or length > 57:       # 57 = 8 字节 - 最多 7 位错位
        return None

    if sig.byte_order == "big_endian":
        # cantools 的 sawtooth → 网络位序，见 database/utils.py:decode_data
        start = 8 * (int(sig.start) // 8) + (7 - int(sig.start) % 8)
        big = True
    else:
        start = int(sig.start)
        big = False

    if big:
        byte_start, bit_off = divmod(start, 8)
        nbytes = (bit_off + length + 7) // 8
        shift = nbytes * 8 - bit_off - length
    else:
        byte_start, bit_off = divmod(start, 8)
        nbytes = (bit_off + length + 7) // 8
        shift = bit_off
    if nbytes > _MAX_SPAN_BYTES:
        return None
    if byte_start < 0 or byte_start + nbytes > msg_length:
        return None

    conv = sig.conversion
    choices = None
    scale, offset = conv.scale, conv.offset
    if getattr(conv, "choices", None):
        choices = {int(k): str(v) for k, v in conv.choices.items()}
        inner = getattr(conv, "_conversion", None)
        if inner is not None:
            scale, offset = inner.scale, inner.offset

    # 与 BaseConversion.factory 同样的分支：恒等 / 整数线性 / 浮点线性
    if scale == 1 and offset == 0:
        kind = "id"
    elif _is_int(scale) and _is_int(offset):
        kind = "int"
        scale, offset = int(scale), int(offset)
    else:
        kind = "float"
        scale, offset = float(scale), float(offset)

    return _Plan(name=sig.name, byte_start=byte_start, nbytes=nbytes, shift=shift,
                 mask=(1 << length) - 1, big_endian=big,
                 signed=bool(sig.is_signed), half=1 << (length - 1), full=1 << length,
                 kind=kind, scale=scale, offset=offset, choices=choices)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) or (hasattr(v, "is_integer") and v.is_integer())


class MessageDecoder:
    """一条报文的向量化解码器。"""

    def __init__(self, length: int, plans: list[_Plan]):
        self.length = length
        self.plans = plans

    def signal_values(self, rows: np.ndarray, plan: _Plan) -> list:
        """抽出一个信号在所有帧上的值，类型与 cantools 逐帧解码完全一致。"""
        acc = np.zeros(len(rows), dtype=np.uint64)
        if plan.big_endian:
            for k in range(plan.nbytes):
                acc |= rows[:, plan.byte_start + k].astype(np.uint64) << np.uint64(8 * (plan.nbytes - 1 - k))
        else:
            for k in range(plan.nbytes):
                acc |= rows[:, plan.byte_start + k].astype(np.uint64) << np.uint64(8 * k)
        raw = ((acc >> np.uint64(plan.shift)) & np.uint64(plan.mask)).astype(np.int64)
        if plan.signed:
            raw = np.where(raw >= plan.half, raw - plan.full, raw)

        if plan.kind == "id":
            num = raw
        elif plan.kind == "int":
            num = raw * plan.scale + plan.offset
        else:
            num = raw.astype(np.float64) * plan.scale + plan.offset

        if plan.choices is None:
            return num.tolist()
        # 枚举查表用的是**原始值**（见 NamedSignalConversion.raw_to_scaled）
        lut = plan.choices
        return [lut.get(r, n) for r, n in zip(raw.tolist(), num.tolist())]


def build(dbc, frame_id: int) -> Optional[MessageDecoder]:
    """为一条报文编译向量化解码器；任一信号不支持就整条放弃。"""
    db = getattr(dbc, "_db", None)
    if db is None:
        return None
    try:
        msg = db.get_message_by_frame_id(frame_id)
    except Exception:
        return None
    if msg.is_multiplexed() or getattr(msg, "is_container", False):
        return None
    plans = []
    for sig in msg.signals:
        p = _plan_for(sig, int(msg.length))
        if p is None:
            return None
        plans.append(p)
    if not plans:
        return None
    return MessageDecoder(int(msg.length), plans)


def validate(decoder: MessageDecoder, dbc, frame_id: int, rows: np.ndarray) -> bool:
    """拿真实帧和 cantools 对拍：值相等**且**类型相同才算通过。"""
    if not len(rows):
        return True
    step = max(1, len(rows) // _VALIDATE_FRAMES)
    sample = rows[::step][:_VALIDATE_FRAMES]
    got = {p.name: decoder.signal_values(sample, p) for p in decoder.plans}
    for i in range(len(sample)):
        want = dbc.decode(frame_id, sample[i, :decoder.length].tobytes())
        if not want:
            return False
        for name, values in got.items():
            v = values[i]
            w = want.get(name)
            if type(v) is not type(w) or v != w:
                return False
    return True
