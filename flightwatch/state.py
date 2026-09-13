"""Remembers what has already been seen so you are not pinged twice."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .models import Combo


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.alerted_path = self.dir / "alerted.json"
        self.best_path = self.dir / "best.json"
        self.history_path = self.dir / "history.jsonl"
        self.health_path = self.dir / "health.json"

        self.alerted: dict[str, int] = _read_json(self.alerted_path, {})
        self.best: dict = _read_json(self.best_path, {})
        self.health: dict = _read_json(self.health_path, {})

    # -- deal alerts ---------------------------------------------------------
    def is_new_deal(self, combo: Combo, realert_drop: int) -> bool:
        """True when this trip has never been alerted, or got meaningfully cheaper."""
        seen = self.alerted.get(combo.signature())
        if seen is None:
            return True
        return combo.total <= seen - realert_drop

    def record_alert(self, combo: Combo) -> None:
        key = combo.signature()
        seen = self.alerted.get(key)
        if seen is None or combo.total < seen:
            self.alerted[key] = combo.total

    # -- best-ever tracking --------------------------------------------------
    def best_total(self) -> int | None:
        value = self.best.get("total")
        return int(value) if value is not None else None

    def update_best(self, combo: Combo) -> None:
        current = self.best_total()
        if current is None or combo.total < current:
            self.best = {
                "total": combo.total,
                "signature": combo.signature(),
                "seen_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "detail": combo.to_json(),
            }

    # -- health --------------------------------------------------------------
    def should_warn_blocked(self, cooldown_hours: int = 12) -> bool:
        last = self.health.get("last_blocked_alert")
        if not last:
            return True
        try:
            when = datetime.fromisoformat(last.rstrip("Z"))
        except ValueError:
            return True
        return datetime.utcnow() - when > timedelta(hours=cooldown_hours)

    def record_blocked_warning(self) -> None:
        self.health["last_blocked_alert"] = (
            datetime.utcnow().isoformat(timespec="seconds") + "Z"
        )

    # -- extended-city rotation ---------------------------------------------
    def rotation_cursor(self) -> int:
        try:
            return int(self.health.get("rotation_cursor", 0))
        except (TypeError, ValueError):
            return 0

    def set_rotation_cursor(self, cursor: int) -> None:
        self.health["rotation_cursor"] = int(cursor)

    # -- history -------------------------------------------------------------
    def append_history(self, result) -> None:
        row = {
            "ts": result.started.isoformat(timespec="seconds") + "Z",
            "searches_run": result.searches_run,
            "searches_failed": result.searches_failed,
            "legs_found": result.legs_found,
            "cheapest": [c.to_json() for c in result.best(5)],
        }
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def save(self) -> None:
        _write_json(self.alerted_path, self.alerted)
        _write_json(self.best_path, self.best)
        _write_json(self.health_path, self.health)


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
