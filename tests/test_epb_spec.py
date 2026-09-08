from pathlib import Path

import numpy as np
import yaml

from canprobe.analyzer import analyze_functions


ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "samples" / "function_specs" / "functions_epb.yaml"


class TimelineStore:
    def __init__(self, rows):
        self.times = np.array([row["t"] for row in rows], dtype=float)
        self._rows = rows

    def value_at(self, signal, t):
        value = None
        for row in self._rows:
            if row["t"] > t:
                break
            if signal in row:
                value = row[signal]
        return value


def _function(function_id):
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    return {
        "functions": [
            function for function in spec["functions"]
            if function["id"] == function_id
        ]
    }


def _base_row(t, **overrides):
    row = {
        "t": t,
        "CVC_EPB_SwitchSta": "Not Pressed",
        "M2SApplyReleaseRequest": "No request",
        "EPB_Sts": "Released",
        "WCBS_ADCU_VAP_EPBState": "Released",
        "EPB_CDPActive": "Off",
        "WCBS_CDPFailSts": "No failure",
        "WCBS_EPB_Fault": "No Error",
        "EPB_Swch_Mafnc": "No Error",
        "CVC_ACTGear": "D",
        "WCBS_VehicleSpeed": 20.0,
        "WCBS_VehicleSpeedValid": "Valid",
        "WCBS_BrkPedalPct": 0.0,
        "WCBS_BrkPedalTravel": 0.0,
        "WCBS_BrkPedalValid": "Valid",
        "WCBS_MainCylinderPress": 0.0,
        "WCBS_MainCylinderPressValid": "Valid",
        "CVC_StsBrakePedalSwitch": "Not pressed()",
    }
    row.update(overrides)
    return row


def test_epb_functions_are_independent_rows():
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    ids = [function["id"] for function in spec["functions"]]

    assert ids == [
        "epb_apply",
        "epb_release",
        "epb_transition_integrity",
        "brake_pressure_response",
        "epb_health",
        "epb_avh_takeover_boundary",
    ]


def test_transition_integrity_detects_unrequested_apply_and_reversal():
    store = TimelineStore([
        _base_row(0.0),
        _base_row(0.5, EPB_Sts="Locking"),
        _base_row(0.6, EPB_Sts="Locking", M2SApplyReleaseRequest="Request"),
        _base_row(1.0, EPB_Sts="Releasing"),
        _base_row(1.5, EPB_Sts="Released"),
    ])

    result = analyze_functions(store, _function("epb_transition_integrity"))
    summaries = [event["summary"] for event in result["events"]]

    assert any("无前置可见请求" in summary for summary in summaries)
    assert any("未到 Locked 即反向进入 Releasing" in summary for summary in summaries)
    assert any("迁移完整性恢复" in summary for summary in summaries)


def test_transition_integrity_accepts_visible_request_before_apply():
    store = TimelineStore([
        _base_row(0.0),
        _base_row(0.3, M2SApplyReleaseRequest="Request"),
        _base_row(0.4, EPB_Sts="Locking", M2SApplyReleaseRequest="Request"),
        _base_row(0.8, EPB_Sts="Locked", M2SApplyReleaseRequest="No request"),
    ])

    result = analyze_functions(store, _function("epb_transition_integrity"))

    assert not any(
        "无前置可见请求" in event["summary"]
        for event in result["events"]
    )


def test_brake_pressure_response_requires_sustained_low_pressure():
    store = TimelineStore([
        _base_row(0.0),
        _base_row(0.1, WCBS_BrkPedalPct=95.0, WCBS_BrkPedalTravel=30.0,
                  WCBS_MainCylinderPress=10.0),
        _base_row(0.2, WCBS_BrkPedalPct=100.0, WCBS_BrkPedalTravel=35.0,
                  WCBS_MainCylinderPress=12.0),
        _base_row(0.4, WCBS_BrkPedalPct=100.0, WCBS_BrkPedalTravel=35.0,
                  WCBS_MainCylinderPress=15.0),
        _base_row(0.5, WCBS_BrkPedalPct=70.0, WCBS_BrkPedalTravel=20.0,
                  WCBS_MainCylinderPress=5.0),
    ])

    result = analyze_functions(store, _function("brake_pressure_response"))
    summaries = [event["summary"] for event in result["events"]]

    assert any("压力响应疑似偏低" in summary for summary in summaries)
    assert any("筛查条件解除" in summary for summary in summaries)


def test_brake_pressure_response_accepts_pressure_build_up():
    store = TimelineStore([
        _base_row(0.0),
        _base_row(0.1, WCBS_BrkPedalPct=100.0, WCBS_BrkPedalTravel=35.0,
                  WCBS_MainCylinderPress=10.0),
        _base_row(0.2, WCBS_BrkPedalPct=100.0, WCBS_BrkPedalTravel=35.0,
                  WCBS_MainCylinderPress=25.0),
        _base_row(0.5, WCBS_BrkPedalPct=100.0, WCBS_BrkPedalTravel=35.0,
                  WCBS_MainCylinderPress=25.0),
    ])

    result = analyze_functions(store, _function("brake_pressure_response"))

    assert not any(
        event["type"] == "enter" for event in result["events"]
    )


def _avh_hold_row(t, **overrides):
    values = {
        "WCBS_AVH_Status": "AVH Active",
        "ESC_SlopeGrade": 22.5,
        "ESC_VehicleStandstill": "VEHICLE_STANDSTILL",
        "WCBS_VehicleSpeed": 0.0,
        "WCBS_MainCylinderPress": 3.5,
    }
    values.update(overrides)
    return _base_row(t, **values)


def test_epb_avh_takeover_detects_stable_slope_below_25_percent():
    store = TimelineStore([
        _avh_hold_row(0.0),
        _avh_hold_row(0.5),
        _avh_hold_row(0.6, EPB_Sts="Locking"),
    ])

    result = analyze_functions(store, _function("epb_avh_takeover_boundary"))
    summaries = [event["summary"] for event in result["events"]]

    assert any("22.5% < 25.0%" in summary for summary in summaries)


def test_epb_avh_takeover_accepts_stable_slope_at_25_percent():
    store = TimelineStore([
        _avh_hold_row(0.0, ESC_SlopeGrade=-25.0),
        _avh_hold_row(0.5, ESC_SlopeGrade=-25.0),
        _avh_hold_row(0.6, ESC_SlopeGrade=-25.0, EPB_Sts="Locking"),
    ])

    result = analyze_functions(store, _function("epb_avh_takeover_boundary"))
    summaries = [event["summary"] for event in result["events"]]

    assert any("满足 >=25.0%" in summary for summary in summaries)
    assert not any("低于阈值" in summary for summary in summaries)


def test_epb_avh_takeover_does_not_classify_unstable_slope():
    store = TimelineStore([
        _avh_hold_row(0.0, ESC_SlopeGrade=22.0),
        _avh_hold_row(0.3, ESC_SlopeGrade=23.0),
        _avh_hold_row(0.6, ESC_SlopeGrade=22.0, EPB_Sts="Locking"),
    ])

    result = analyze_functions(store, _function("epb_avh_takeover_boundary"))
    summaries = [event["summary"] for event in result["events"]]
    attempts = [attempt["trigger"] for attempt in result["attempts"]]

    assert any("坡度未稳定满 0.5s" in attempt for attempt in attempts)
    assert any("接管边沿证据不足" in summary for summary in summaries)
    assert not any("低于阈值" in summary for summary in summaries)