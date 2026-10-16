"""Typed views of the JSON slskd returns, and helpers for its peer paths.

slskd speaks camelCase and omits fields freely: ``bitRate`` only on lossy
files, ``bitDepth``/``sampleRate`` only on lossless ones, ``length`` when the
peer's client didn't index it, and ``extension`` is often ``""``. Every
``from_json`` here is therefore tolerant: a missing or null field becomes
``None`` (or an empty default), never a KeyError. Parsing never raises on a
dict; anything that is not a dict parses to an empty value.

Transfer states come from the slskd *server* as a flags string -
``"Completed, Succeeded"``, ``"Completed, Errored"``, ``"Queued, Remotely"``,
``"InProgress"``... - not from the slskd-api library, so a server upgrade
is what can change them. ``Transfer.outcome`` classifies them.
"""
from __future__ import annotations

import dataclasses
import enum
import os
import re
from dataclasses import dataclass, field
from typing import Any


def remote_basename(path: str | None) -> str:
    """Basename of a peer path. slskd paths use backslashes, which
    os.path.basename does not split on Linux — so the whole share path,
    folder names included, used to be scored and blacklisted."""
    return re.split(r"[\\/]", path or "")[-1]


def _num(value: Any) -> Any:
    """A numeric field as sent (int, or float if slskd ever sends one), or
    None when absent/null/not a number. bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class PeerFile:
    """One file in a search response (``SearchResponse.files``)."""

    filename: str
    size: int | None = None
    extension: str = ""
    bit_rate: int | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    length: int | None = None
    is_variable_bit_rate: bool | None = None
    is_locked: bool = False

    @classmethod
    def from_json(cls, data: Any) -> PeerFile:
        d = _dict(data)
        return cls(
            filename=d.get("filename") or "",
            size=_num(d.get("size")),
            extension=d.get("extension") or "",
            bit_rate=_num(d.get("bitRate")),
            bit_depth=_num(d.get("bitDepth")),
            sample_rate=_num(d.get("sampleRate")),
            length=_num(d.get("length")),
            is_variable_bit_rate=_bool(d.get("isVariableBitRate")),
            is_locked=bool(d.get("isLocked")),
        )

    @property
    def basename(self) -> str:
        return remote_basename(self.filename)

    @property
    def ext(self) -> str:
        """Lowercased extension taken from the *filename*, dot included.
        slskd's own ``extension`` field is frequently empty."""
        return os.path.splitext(self.filename)[1].lower()


@dataclass(frozen=True)
class SearchResponse:
    """One peer's reply to a search (an item of ``/searches/{id}/responses``).

    Only ``files`` are downloadable; ``locked_files`` need a privilege we
    don't have and are kept for completeness, never offered as candidates.
    """

    username: str | None
    upload_speed: int = 0
    has_free_upload_slot: bool | None = None
    queue_length: int | None = None
    files: list[PeerFile] = field(default_factory=list)
    locked_files: list[PeerFile] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: Any) -> SearchResponse:
        d = _dict(data)
        return cls(
            username=d.get("username") or "unknown",
            upload_speed=_num(d.get("uploadSpeed")) or 0,
            has_free_upload_slot=_bool(d.get("hasFreeUploadSlot")),
            queue_length=_num(d.get("queueLength")),
            files=[PeerFile.from_json(f) for f in d.get("files") or []],
            locked_files=[PeerFile.from_json(f) for f in d.get("lockedFiles") or []],
        )


@dataclass(frozen=True)
class Search:
    """A search as returned by ``POST /searches`` and ``GET /searches/{id}``."""

    id: str | None
    state: str = ""

    @classmethod
    def from_json(cls, data: Any) -> Search:
        d = _dict(data)
        return cls(id=d.get("id") or None, state=d.get("state") or "")

    @property
    def in_progress(self) -> bool:
        # slskd states are flags ("InProgress", "Completed, TimedOut", ...).
        # Anything but InProgress means the responses are final.
        return self.state == "InProgress"


class TransferOutcome(enum.StrEnum):
    """Terminal classification of a transfer. ``None`` means still pending
    (Requested, Queued Locally/Remotely, Initializing, InProgress)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


# Failure words in a state that are the peer's (or its file's) fault. Other
# failures — "Completed, TimedOut", "Completed, Cancelled" — may be ours or
# the network's, so they never blocklist.
PEER_FAULT_STATES = ("failed", "errored", "rejected", "aborted")
_FAILURE_WORDS = ("failed", "aborted", "errored", "rejected", "timedout")


@dataclass(frozen=True)
class Transfer:
    """One download (``/transfers/downloads/{username}`` → directories → files)."""

    id: str | None
    filename: str
    size: int | None = None
    state: str = ""
    username: str | None = None
    bytes_transferred: int | None = None
    average_speed: float | None = None  # bytes/s
    percent_complete: float | None = None

    @classmethod
    def from_json(cls, data: Any) -> Transfer:
        d = _dict(data)
        return cls(
            id=d.get("id") or None,
            filename=d.get("filename") or "",
            size=_num(d.get("size")),
            state=d.get("state") or "",
            username=d.get("username"),
            bytes_transferred=_num(d.get("bytesTransferred")),
            average_speed=_num(d.get("averageSpeed")),
            percent_complete=_num(d.get("percentComplete")),
        )

    @classmethod
    def list_from_all_json(cls, data: Any) -> list[Transfer]:
        """Flatten ``/transfers/downloads`` (a list of per-user payloads).
        A file without its own ``username`` takes the user's."""
        out = []
        for user in data if isinstance(data, list) else []:
            username = _dict(user).get("username")
            for t in cls.list_from_user_json(user):
                out.append(t if t.username else dataclasses.replace(t, username=username))
        return out

    @property
    def eta_seconds(self) -> float | None:
        """Remaining bytes over the average speed, while in progress."""
        if not (self.size and self.average_speed and self.bytes_transferred is not None):
            return None
        if self.outcome is not None:
            return None
        return max(0.0, (self.size - self.bytes_transferred) / self.average_speed)

    @classmethod
    def list_from_user_json(cls, data: Any) -> list[Transfer]:
        """Flatten a user's ``{"directories": [{"files": [...]}]}`` payload."""
        return [
            cls.from_json(f)
            for directory in _dict(data).get("directories") or []
            for f in _dict(directory).get("files") or []
        ]

    @property
    def basename(self) -> str:
        return remote_basename(self.filename)

    @property
    def outcome(self) -> TransferOutcome | None:
        """slskd's terminal states are all "Completed, <reason>". Only
        Succeeded is a success; Rejected, TimedOut, Errored, Aborted,
        Cancelled... are failures. Matching only failed/aborted/errored let
        "Completed, Rejected" and "Completed, TimedOut" run into the 300 s
        timeout."""
        s = self.state.lower()
        if "completed" in s:
            return TransferOutcome.SUCCEEDED if "succeeded" in s else TransferOutcome.FAILED
        if any(w in s for w in _FAILURE_WORDS):
            return TransferOutcome.FAILED
        return None

    @property
    def peer_fault(self) -> bool:
        s = self.state.lower()
        return any(w in s for w in PEER_FAULT_STATES)
