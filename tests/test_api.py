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


def test_evaluate(client):
    r = client.post("/api/evaluate", json={"condition": {"signal": "MotorTemp", "op": ">", "value": 120}})
    assert r.status_code == 200
    data = r.json()
    assert data["transitions"][0]["to"] is True


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
