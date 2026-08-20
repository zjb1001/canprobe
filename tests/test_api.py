from pathlib import Path

import pytest

from canprobe.main import app

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(TestClient is None, reason="httpx not available")


@pytest.fixture()
def client():
    c = TestClient(app)
    # load bundled sample project
    r = c.post("/api/load/sample")
    assert r.status_code == 200, r.text
    return c


def test_status(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert data["dbc"] == "cruise.dbc"
    assert data["frame_count"] == 6004
    assert data["start"] == 0.0
    assert data["end"] == 30.0


def test_signals(client):
    r = client.get("/api/signals")
    names = {s["name"] for s in r.json()}
    assert {"VehSpd", "CruiseState", "MotorTemp"} <= names


def test_series(client):
    r = client.get("/api/series", params={"signals": "VehSpd,CruiseState", "start": 0, "end": 10})
    assert r.status_code == 200
    data = r.json()
    assert "VehSpd" in data and "CruiseState" in data
    assert data["VehSpd"]["v"][0] == 0.0
    assert data["CruiseState"]["enum"] is True
    assert data["CruiseState"]["choices"] == {"0": "IDLE", "1": "ARMED", "2": "ACTIVE", "3": "CANCEL"}


def test_trace(client):
    r = client.get("/api/trace", params={"start": 7.9, "end": 8.1, "limit": 100})
    assert r.status_code == 200
    rows = r.json()
    assert rows
    assert any(row["id"] == 0x102 for row in rows)


def test_values(client):
    r = client.get("/api/values", params={"signals": "VehSpd,CruiseState,MotorTemp", "t": 10.0})
    assert r.status_code == 200
    data = r.json()
    assert data["VehSpd"] == 32.0
    assert data["CruiseState"] == "ACTIVE"
    # MotorTemp 量化到整数（scale=1, offset=-40）
    assert data["MotorTemp"] == 96.0


def test_analysis(client):
    r = client.get("/api/analysis")
    assert r.status_code == 200
    data = r.json()
    types = {(e["function"], e["type"]) for e in data["events"]}
    assert ("cruise", "enter") in types
    assert ("cruise", "exit") in types
    attempts = [a for a in data["attempts"] if a["function"] == "cruise"]
    assert attempts and attempts[0]["t"] == 5.0


def test_status_separates_dbc_from_log_coverage(client):
    """DBC 定义了什么 ≠ 这份日志录到了什么。"""
    s = client.get("/api/status").json()
    assert s["log_message_count"] == 4          # cruise.csv 里 4 个 ID
    assert s["log_unknown_ids"] == []           # 配套 DBC，全都认识
    # decoded_signals 现在是"真的有数据的信号"，不再是 DBC 全集
    assert 0 < s["data_signal_count"] <= s["signal_count"]
    assert set(s["decoded_signals"]) <= set(x["name"] for x in client.get("/api/signals").json())


def test_signals_flag_missing_data(client):
    sigs = {s["name"]: s for s in client.get("/api/signals").json()}
    assert all(s["has_data"] for s in sigs.values())   # 示例工程完全配套
    msgs = client.get("/api/messages").json()
    assert all(m["has_data"] for m in msgs)
    assert all(s["has_data"] for m in msgs for s in m["signals"])


def test_functions_distinguish_nodata_from_missing(tmp_path):
    """装错日志时，功能按钮必须能区分「DBC 里没有」和「日志里没录到」。

    复现用户现场：DBC + 规格换成了 EP35 的，日志却还是示例 cruise.csv。
    """
    c = TestClient(app)
    c.post("/api/load/sample")
    dbc = ROOT / "samples" / "03_CCAN_EP_v2.1.0_20260417-MOD.dbc"
    spec = ROOT / "samples" / "functions_ep35_switch.yaml"
    if not (dbc.exists() and spec.exists()):
        pytest.skip("EP35 samples not present")
    with open(dbc, "rb") as f:
        assert c.post("/api/upload/dbc", files={"file": (dbc.name, f.read())}).status_code == 200
    with open(spec, "rb") as f:
        assert c.post("/api/upload/spec", files={"file": (spec.name, f.read())}).status_code == 200

    s = c.get("/api/status").json()
    # cruise.csv 的 4 个 ID 里只有 0x102 恰好撞上 EP35 DBC，其余 3 个不认识
    assert len(s["log_unknown_ids"]) == 3
    assert s["data_signal_count"] < s["signal_count"]

    funcs = c.get("/api/functions").json()
    assert funcs
    for f in funcs:
        # 信号都在 EP35 DBC 里（不是 missing），但日志里没有数据（是 nodata）
        assert f["missing"] == []
        assert f["available"] == []
        assert f["nodata"], f["id"]

    # 因此一个事件都算不出来
    assert c.get("/api/events").json()["total"] == 0

    # 信号树的置灰依据是「解得开」而不是「ID 出现过」。cruise.csv 的 0x102
    # 恰好和 EP35 的 CVC_LOGICAL_CLAMP 撞了 ID，但它只有 8 字节、DBC 要 24
    # 字节，一帧也解不开 —— 标成 has_data 会让这组信号在树里保持高亮，点开
    # 却条条是空曲线。撞 ID 不等于有数据。
    msgs = c.get("/api/messages").json()
    assert [m for m in msgs if m["has_data"]] == []
    assert all(s["has_data"] is False for m in msgs if not m["has_data"] for s in m["signals"])

    # 而且要说得出为什么：出现过却解不开的报文单独报，附实际长度 vs DBC 长度
    fails = c.get("/api/status").json()["decode_failures"]
    assert [f["id"] for f in fails] == [0x102]
    assert fails[0]["name"] == "CVC_LOGICAL_CLAMP"
    assert (fails[0]["log_bytes"], fails[0]["dbc_bytes"]) == (8, 24)


def test_upload_spec_reads_saved_copy(client):
    """_save_upload() 会把上传流读空，upload_spec 必须从落盘副本再读一次。

    回归用：曾经二次 file.file.read() 拿到 b""，规格静默变成 None。
    """
    spec = (ROOT / "samples" / "functions.yaml").read_text(encoding="utf-8")
    r = client.post("/api/upload/spec",
                    files={"file": ("functions.yaml", spec.encode("utf-8"), "text/yaml")})
    assert r.status_code == 200, r.text
    assert r.json()["spec"] == "functions.yaml"
    assert {f["id"] for f in client.get("/api/functions").json()} == {"cruise", "overtemp"}


def test_events_list(client):
    r = client.get("/api/events")
    assert r.status_code == 200
    data = r.json()
    # 事件按时间升序，且不带 evidence 证据树（面板不需要，几千条会撑爆载荷）
    ts = [e["t"] for e in data["events"]]
    assert ts == sorted(ts)
    assert all("evidence" not in e for e in data["events"])
    assert {(e["function"], e["type"]) for e in data["events"]} >= {
        ("cruise", "enter"), ("cruise", "exit"), ("overtemp", "enter")}
    # 未筛选时，按功能计数与按类型计数都应等于事件总数
    assert sum(f["count"] for f in data["functions"]) == data["total"]
    assert sum(data["types"].values()) == data["total"]


def test_events_filters(client):
    only_enter = client.get("/api/events", params={"types": "enter"}).json()
    assert only_enter["events"] and all(e["type"] == "enter" for e in only_enter["events"])
    # chip 计数仍是全集，不随筛选变化
    assert sum(f["count"] for f in only_enter["functions"]) > only_enter["total"]

    only_cruise = client.get("/api/events", params={"functions": "cruise"}).json()
    assert only_cruise["events"] and all(e["function"] == "cruise" for e in only_cruise["events"])

    hit = client.get("/api/events", params={"q": "MotorTemp"}).json()
    assert hit["events"] and all("motortemp" in e["summary"].lower() for e in hit["events"])
    assert client.get("/api/events", params={"q": "__no_such_thing__"}).json()["total"] == 0


def test_events_limit_marks_truncated(client):
    r = client.get("/api/events", params={"limit": 1}).json()
    assert len(r["events"]) == 1
    assert r["truncated"] is True
    assert r["total"] > 1


def test_evaluate(client):
    r = client.post("/api/evaluate", json={"condition": {"signal": "MotorTemp", "op": ">", "value": 120}})
    assert r.status_code == 200
    data = r.json()
    assert data["transitions"][0]["to"] is True


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
