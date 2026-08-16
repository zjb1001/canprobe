"""DBC loading and signal metadata extraction.

Wraps `cantools` for decoding but exposes a lightweight, JSON-serialisable
signal/message model so the rest of the app never has to talk to cantools
directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import cantools


def _plain(value: Any) -> Any:
    """Normalise a decoded value (incl. NamedSignalValue) to a JSON-safe type."""
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    name = getattr(value, "name", None)
    if name is not None:
        return str(name)
    return str(value)


@dataclass
class SignalMeta:
    name: str
    start_bit: int
    length: int
    is_signed: bool
    is_float: bool
    scale: float
    offset: float
    minimum: Optional[float]
    maximum: Optional[float]
    unit: str
    choices: Optional[dict] = None
    comment: str = ""
    receivers: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "start_bit": self.start_bit,
            "length": self.length,
            "is_signed": self.is_signed,
            "is_float": self.is_float,
            "scale": self.scale,
            "offset": self.offset,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "unit": self.unit,
            "choices": self.choices,
            "comment": self.comment,
        }


@dataclass
class MessageMeta:
    name: str
    frame_id: int
    length: int
    signals: list[SignalMeta] = field(default_factory=list)
    comment: str = ""
    senders: list = field(default_factory=list)
    is_extended: bool = False
    cycle_time: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "frame_id": self.frame_id,
            "length": self.length,
            "signal_count": len(self.signals),
            "comment": self.comment,
            "cycle_time": self.cycle_time,
        }


class DbcDatabase:
    """Thin wrapper around a cantools database.

    Holds the decoded metadata and the underlying cantools db (for fast
    frame -> signal decoding).
    """

    def __init__(self, db: Any, messages: dict[int, MessageMeta], signals: dict[str, SignalMeta]):
        self._db = db
        self.messages = messages            # frame_id -> MessageMeta
        self.signals = signals              # signal name -> SignalMeta
        self._signal_to_message = {}        # signal name -> frame_id
        for mid, msg in messages.items():
            for sig in msg.signals:
                self._signal_to_message[sig.name] = mid

    @classmethod
    def load(cls, path: str) -> "DbcDatabase":
        db = cantools.database.load_file(path)
        messages: dict[int, MessageMeta] = {}
        signals: dict[str, SignalMeta] = {}

        for m in db.messages:
            sigs = []
            for s in m.signals:
                sm = SignalMeta(
                    name=s.name,
                    start_bit=s.start,
                    length=s.length,
                    is_signed=bool(s.is_signed),
                    is_float=bool(s.is_float),
                    scale=float(s.scale or 1.0),
                    offset=float(s.offset or 0.0),
                    minimum=None if s.minimum is None else float(s.minimum),
                    maximum=None if s.maximum is None else float(s.maximum),
                    unit=s.unit or "",
                    choices={int(k): str(v) for k, v in s.choices.items()} if s.choices else None,
                    comment=(s.comment or "").strip(),
                    receivers=list(s.receivers or []),
                )
                sigs.append(sm)
                signals[sm.name] = sm
            mm = MessageMeta(
                name=m.name,
                frame_id=int(m.frame_id),
                length=int(m.length),
                signals=sigs,
                comment=(m.comment or "").strip(),
                senders=list(m.senders or []),
                is_extended=bool(m.is_extended_frame),
                cycle_time=int(m.cycle_time) if getattr(m, "cycle_time", None) else None,
            )
            messages[mm.frame_id] = mm

        return cls(db, messages, signals)

    def message_for_id(self, frame_id: int) -> Optional[MessageMeta]:
        return self.messages.get(frame_id)

    def decode(self, frame_id: int, data: bytes) -> Optional[dict]:
        """Decode a raw frame into {signal_name: physical_value}.

        Enum values are normalised to plain ``str``. Returns None when the
        frame id is not in the DBC.
        """
        try:
            decoded = self._db.decode_message(frame_id, data, decode_choices=True, scaling=True)
            return {k: _plain(v) for k, v in decoded.items()}
        except Exception:
            return None

    def decode_raw(self, frame_id: int, data: bytes) -> Optional[dict]:
        """Decode without scaling/choices (raw integer values)."""
        try:
            decoded = self._db.decode_message(frame_id, data, decode_choices=False, scaling=False)
            return {k: _plain(v) for k, v in decoded.items()}
        except Exception:
            return None

    def signal_names(self) -> list[str]:
        return sorted(self.signals.keys())
