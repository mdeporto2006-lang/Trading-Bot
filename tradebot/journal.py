"""Append-only trade journal (JSONL) plus a small state file for open trades."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .clock import to_et


class Journal:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path = self.path.with_name("open_trades.json")
        self.outcomes_path = self.path.with_name("outcomes.jsonl")

    def record(self, now: datetime, **entry: Any) -> str:
        row = {"id": uuid.uuid4().hex, "ts": now.isoformat(), **entry}
        with self.path.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        return row["id"]

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def records(self) -> list[dict]:
        return self._read_jsonl(self.path)

    def outcomes(self) -> dict[str, dict]:
        """Outcome rows keyed by journal record id."""
        return {o["record_id"]: o for o in self._read_jsonl(self.outcomes_path)}

    def record_outcome(self, outcome: dict) -> None:
        with self.outcomes_path.open("a") as f:
            f.write(json.dumps(outcome, default=str) + "\n")

    def trades_on(self, now: datetime) -> int:
        """Orders submitted on the same ET date as `now`."""
        if not self.path.exists():
            return 0
        day = to_et(now).date()
        n = 0
        for line in self.path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("order_id") and to_et(datetime.fromisoformat(row["ts"])).date() == day:
                n += 1
        return n

    def open_trades(self) -> dict[str, dict]:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text())

    def save_open_trades(self, trades: dict[str, dict]) -> None:
        self.state_path.write_text(json.dumps(trades, indent=2))
