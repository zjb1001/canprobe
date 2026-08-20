"""向量化 BLF 读取器与 python-can 的逐帧等价性。

性能优化的前提是结果一模一样，所以这里不抽样：对 ``samples/`` 下的每个 BLF
把每一帧的时间戳、ID、通道、载荷都和 ``can.io.BLFReader`` 比对。
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from canprobe.blf_fast import read_blf_arrays

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
BLFS = sorted(f for f in os.listdir(SAMPLES) if f.lower().endswith(".blf")) if os.path.isdir(SAMPLES) else []

can_io = pytest.importorskip("can.io", reason="需要 python-can 作为参照实现")


@pytest.mark.skipif(not BLFS, reason="samples/ 下没有 BLF 文件")
@pytest.mark.parametrize("name", BLFS)
def test_matches_python_can(name):
    path = os.path.join(SAMPLES, name)
    arr = read_blf_arrays(path)

    ref_t, ref_id, ref_ch, ref_data = [], [], [], []
    with can_io.BLFReader(path) as reader:
        for msg in reader:
            if msg.is_error_frame:
                continue
            ref_t.append(msg.timestamp)
            ref_id.append(msg.arbitration_id)
            ref_ch.append(np.uint8(msg.channel or 0))
            ref_data.append(bytes(msg.data))

    assert len(arr.ts) == len(ref_t), "帧数不一致"
    # 时间戳必须逐位相同：python-can 走 Decimal 精确有理数再舍入，
    # blf_fast 用 ticks/1e9（两个操作数都可精确表示）得到同一个正确舍入结果
    assert np.array_equal(arr.ts, np.array(ref_t, dtype=np.float64))
    assert np.array_equal(arr.ids, np.array(ref_id, dtype=np.uint32))
    assert np.array_equal(arr.channels, np.array(ref_ch, dtype=np.uint8))

    for i, want in enumerate(ref_data):
        got = arr.data[i, :int(arr.dlc[i])].tobytes()
        assert got == want, f"第 {i} 帧载荷不一致: {got.hex()} != {want.hex()}"


@pytest.mark.skipif(not BLFS, reason="samples/ 下没有 BLF 文件")
def test_rejects_non_blf(tmp_path):
    from canprobe.blf_fast import BlfFastError

    p = tmp_path / "x.blf"
    p.write_bytes(b"not a blf file at all" * 20)
    with pytest.raises(BlfFastError):
        read_blf_arrays(str(p))
