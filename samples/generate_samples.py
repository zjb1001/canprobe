"""Generate the bundled demo dataset.

Produces:
* ``cruise.dbc``     — a small vehicle DBC (cruise control + motor status)
* ``cruise.csv``     — a ~30 s CAN trace exercising enter / exit / blocked entry
* ``functions.yaml`` — the function analysis spec

Scenario (times in seconds):

* 0–8 s   vehicle accelerates 0 → 32 km/h
* 5.0 s   driver presses SET while speed is only ~20 km/h  → *blocked entry*
* 8.0 s   driver presses SET at 32 km/h                    → *enter cruise*
* 15.0 s  driver brakes                                     → *exit cruise*
* ~16.6 s motor temp crosses 120 °C                         → *enter over-temp*
* ~26.6 s motor temp cools below 105 °C                     → *exit over-temp*
"""
from __future__ import annotations

import csv
from collections import OrderedDict
from pathlib import Path

import cantools
from cantools.database.can import Database, Message, Signal
from cantools.database.conversion import LinearConversion, NamedSignalConversion

HERE = Path(__file__).resolve().parent

DT = 0.02  # 50 Hz sampling
T_END = 30.0


def build_dbc() -> Database:
    db = Database(version="1.0")

    veh = Message(frame_id=0x100, name="VehStatus", length=8, signals=[], senders=["BCM"], cycle_time=20)
    veh.signals.append(Signal("VehSpd", start=0, length=12,
                              conversion=LinearConversion(0.1, 0.0, False),
                              minimum=0, maximum=300, unit="km/h"))
    veh.signals.append(Signal("BrakePedal", start=12, length=1, minimum=0, maximum=1))
    veh.signals.append(Signal("AccelPedal", start=16, length=8, minimum=0, maximum=100, unit="%"))
    db.messages.append(veh)

    drv = Message(frame_id=0x101, name="DriverInput", length=8, signals=[], senders=["BCM"], cycle_time=20)
    drv.signals.append(Signal("CruiseSetBtn", start=0, length=1, minimum=0, maximum=1))
    drv.signals.append(Signal("CruiseCancelBtn", start=1, length=1, minimum=0, maximum=1))
    drv.signals.append(Signal("CruiseResumeBtn", start=2, length=1, minimum=0, maximum=1))
    db.messages.append(drv)

    ccm = Message(frame_id=0x102, name="CCM_Status", length=8, signals=[], senders=["CCM"], cycle_time=20)
    ccm.signals.append(Signal("CruiseActive", start=0, length=1, minimum=0, maximum=1))
    ccm.signals.append(Signal("CruiseSetSpeed", start=8, length=12,
                              conversion=LinearConversion(0.1, 0.0, False),
                              minimum=0, maximum=300, unit="km/h"))
    ccm.signals.append(Signal("CruiseState", start=20, length=3, minimum=0, maximum=3,
                              conversion=NamedSignalConversion(1, 0, OrderedDict(
                                  [(0, "IDLE"), (1, "ARMED"), (2, "ACTIVE"), (3, "CANCEL")]), False)))
    db.messages.append(ccm)

    mot = Message(frame_id=0x200, name="MotorStatus", length=8, signals=[], senders=["MCU"], cycle_time=20)
    mot.signals.append(Signal("MotorTemp", start=0, length=8,
                              conversion=LinearConversion(1.0, -40.0, False),
                              minimum=-40, maximum=215, unit="degC"))
    mot.signals.append(Signal("MotorSpeed", start=8, length=16, minimum=0, maximum=20000, unit="rpm"))
    db.messages.append(mot)

    db.refresh()
    return db


# --- scenario signal generators ------------------------------------------ #
def veh_spd(t: float) -> float:
    if t < 8.0:
        return t / 8.0 * 32.0
    if t < 15.0:
        return 32.0
    if t < 17.0:
        return 32.0 - (t - 15.0) / 2.0 * 12.0
    return 20.0 + (t - 17.0) / 13.0 * 15.0


def brake(t: float) -> int:
    return 1 if 15.0 <= t < 17.0 else 0


def accel(t: float) -> int:
    if t < 8.0:
        return 30
    if 17.0 <= t:
        return 40
    return 0


def set_btn(t: float) -> int:
    return 1 if (5.0 <= t < 6.0) or (8.0 <= t < 9.0) else 0


def motor_temp(t: float) -> float:
    if t < 18.0:
        return 60.0 + (125.0 - 60.0) / 18.0 * t
    if t < 22.0:
        return 125.0
    return 125.0 - (125.0 - 90.0) / 8.0 * (t - 22.0)


def motor_speed(t: float) -> float:
    return 1000.0 + veh_spd(t) * 50.0


def cruise_active(t: float) -> int:
    return 1 if 8.0 <= t < 15.0 else 0


def main() -> None:
    db = build_dbc()
    cantools.database.dump_file(db, str(HERE / "cruise.dbc"))

    rows = []
    t = 0.0
    while t <= T_END:
        ts = f"{t:.6f}"

        rows.append((ts, "0x100", db.encode_message("VehStatus", {
            "VehSpd": veh_spd(t), "BrakePedal": brake(t), "AccelPedal": accel(t)})))
        rows.append((ts, "0x101", db.encode_message("DriverInput", {
            "CruiseSetBtn": set_btn(t), "CruiseCancelBtn": 0, "CruiseResumeBtn": 0})))
        rows.append((ts, "0x102", db.encode_message("CCM_Status", {
            "CruiseActive": cruise_active(t),
            "CruiseSetSpeed": 32.0 if cruise_active(t) else 0.0,
            "CruiseState": "ACTIVE" if cruise_active(t) else "IDLE"})))
        rows.append((ts, "0x200", db.encode_message("MotorStatus", {
            "MotorTemp": motor_temp(t), "MotorSpeed": motor_speed(t)})))

        t = round(t + DT, 6)

    with open(HERE / "cruise.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "id", "data"])
        for ts, fid, data in rows:
            w.writerow([ts, fid, " ".join(f"{b:02X}" for b in data)])

    print(f"wrote cruise.dbc, cruise.csv ({len(rows)} sample rows)")


if __name__ == "__main__":
    main()
