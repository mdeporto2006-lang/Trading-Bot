"""What the bot is doing right now, for the dashboard.

Two files sit next to the journal:
  status.json   a snapshot of the account, positions, limits and current
                activity, rewritten every loop (atomically)
  events.jsonl  an append-only event log (scans, decisions, orders, stop
                moves, flatten, errors)

Writing telemetry must never interfere with trading, so every write here
swallows and logs its own errors.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class Telemetry:
    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory)
        self.status_path = self.dir / "status.json"
        self.events_path = self.dir / "events.jsonl"
        self._status: dict[str, Any] = {}

    def event(self, now: datetime, kind: str, message: str, level: str = "info", **data: Any) -> None:
        row = {"ts": now.isoformat(), "kind": kind, "level": level, "msg": message, **data}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception:
            log.exception("could not write event")

    def update(self, now: datetime, **fields: Any) -> None:
        """Merge fields into status.json and rewrite it."""
        self._status.update(fields)
        self._status["updated_at"] = now.isoformat()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".status-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(self._status, f, default=str)
            os.replace(tmp, self.status_path)
        except Exception:
            log.exception("could not write status")


def read_status(directory: str | os.PathLike) -> dict | None:
    p = Path(directory) / "status.json"
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def tail_events(directory: str | os.PathLike, limit: int = 200, max_bytes: int = 512_000) -> list[dict]:
    """Last `limit` events, newest last. Reads only the end of the file."""
    p = Path(directory) / "events.jsonl"
    if not p.exists():
        return []
    with p.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        chunk = f.read().decode("utf-8", errors="replace")
    lines = chunk.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # first line is probably cut
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
