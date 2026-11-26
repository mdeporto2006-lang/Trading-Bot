"""What the bot may change about itself, and the versioned store of changes.

Only settings in TUNABLE can be changed, only within their bounds. The bounds
never go looser than the owner's original rules for the checklist (R:R stays
>= 2, average volume >= 1M, spread <= 0.1%, relative volume >= 2x, catalyst
<= 24h). Risk limits, trading mode, live switch, flatten time and the model are
not tunable at all. Every setting here can be tested by replaying past plans
(see replay.py); settings that only change what Claude would have planned
can't be replayed, so they are left out.

Layout under improvement.dir (default "tuning/"):
  active.yaml          the adopted overrides + lessons (layered over config.yaml)
  history/vNNNN.yaml   every adopted version, for rollback
  proposals/<id>.json  every proposal with its replay results and status
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..config import Config

# dotted path -> (min, max)
TUNABLE: dict[str, tuple[float, float]] = {
    "setup.catalyst_max_age_hours": (2.0, 24.0),
    "setup.min_relative_volume": (2.0, 6.0),
    "setup.min_avg_daily_volume": (1_000_000, 20_000_000),
    "setup.max_spread_pct": (0.01, 0.10),
    "setup.min_reward_risk": (2.0, 4.0),
    "setup.max_level_distance_atr": (0.25, 1.5),
    "exits.trail_after_r": (0.5, 3.0),
    "exits.trail_distance_r": (0.5, 2.0),
    "risk.min_confidence": (0.6, 0.95),
}

MAX_LESSONS = 10
MAX_LESSON_CHARS = 300


class TuningError(ValueError):
    pass


def get_param(cfg: Config, path: str) -> Any:
    section, name = path.split(".")
    return getattr(getattr(cfg, section), name)


def validate_changes(changes: dict[str, float]) -> dict[str, float]:
    out = {}
    for path, value in changes.items():
        if path not in TUNABLE:
            raise TuningError(f"{path} is not a tunable setting")
        lo, hi = TUNABLE[path]
        v = float(value)
        if not lo <= v <= hi:
            raise TuningError(f"{path}={v} is outside the allowed range [{lo}, {hi}]")
        out[path] = v
    return out


def validate_lessons(lessons: list[str]) -> list[str]:
    clean = [l.strip() for l in lessons if l and l.strip()]
    if len(clean) > MAX_LESSONS:
        raise TuningError(f"at most {MAX_LESSONS} lessons")
    for l in clean:
        if len(l) > MAX_LESSON_CHARS:
            raise TuningError(f"lesson longer than {MAX_LESSON_CHARS} characters")
    return clean


def apply_overrides(cfg: Config, changes: dict[str, float], lessons: list[str] | None = None) -> Config:
    """Return a copy of cfg with validated changes applied."""
    new = cfg.model_copy(deep=True)
    for path, v in validate_changes(changes).items():
        section, name = path.split(".")
        sec = getattr(new, section)
        current = getattr(sec, name)
        setattr(sec, name, type(current)(v) if isinstance(current, int) and float(v).is_integer() else v)
    if lessons is not None:
        new.improvement.lessons = validate_lessons(lessons)
    return new


class TuningStore:
    def __init__(self, root: str):
        self.root = Path(root)
        self.active_path = self.root / "active.yaml"
        self.history = self.root / "history"
        self.proposals = self.root / "proposals"

    # ---- active version ----------------------------------------------
    def active(self) -> dict:
        if not self.active_path.exists():
            return {"version": 0, "changes": {}, "lessons": []}
        return yaml.safe_load(self.active_path.read_text()) or {"version": 0, "changes": {}, "lessons": []}

    def layer(self, cfg: Config) -> Config:
        a = self.active()
        return apply_overrides(cfg, a.get("changes", {}), a.get("lessons", []))

    def adopt(self, changes: dict[str, float], lessons: list[str], note: str) -> int:
        cur = self.active()
        merged = {**cur.get("changes", {}), **validate_changes(changes)}
        version = int(cur.get("version", 0)) + 1
        doc = {
            "version": version,
            "adopted_at": datetime.now(timezone.utc).isoformat(),
            "note": note,
            "changes": merged,
            "lessons": validate_lessons(lessons),
        }
        self.history.mkdir(parents=True, exist_ok=True)
        (self.history / f"v{version:04d}.yaml").write_text(yaml.safe_dump(doc, sort_keys=True))
        self.active_path.write_text(yaml.safe_dump(doc, sort_keys=True))
        return version

    def rollback(self, to_version: int | None = None) -> int:
        """Go back to an earlier version (default: the previous one). Version 0
        means no overrides at all, i.e. exactly config.yaml."""
        cur = int(self.active().get("version", 0))
        target = cur - 1 if to_version is None else to_version
        if target < 0 or target >= cur:
            raise TuningError(f"cannot roll back from v{cur} to v{target}")
        if target == 0:
            doc = {"version": 0, "changes": {}, "lessons": []}
        else:
            path = self.history / f"v{target:04d}.yaml"
            if not path.exists():
                raise TuningError(f"no saved version v{target}")
            doc = yaml.safe_load(path.read_text())
        doc = {**doc, "rolled_back_from": cur, "rolled_back_at": datetime.now(timezone.utc).isoformat()}
        self.root.mkdir(parents=True, exist_ok=True)
        self.active_path.write_text(yaml.safe_dump(doc, sort_keys=True))
        return target

    # ---- proposals ----------------------------------------------------
    def save_proposal(self, proposal: dict) -> Path:
        self.proposals.mkdir(parents=True, exist_ok=True)
        p = self.proposals / f"{proposal['id']}.json"
        p.write_text(json.dumps(proposal, indent=2, default=str))
        return p

    def load_proposal(self, pid: str) -> dict:
        p = self.proposals / f"{pid}.json"
        if not p.exists():
            raise TuningError(f"no proposal {pid}")
        return json.loads(p.read_text())

    def list_proposals(self) -> list[dict]:
        if not self.proposals.exists():
            return []
        return sorted(
            (json.loads(p.read_text()) for p in self.proposals.glob("*.json")),
            key=lambda d: d.get("created_at", ""),
        )
