"""Loads config.yaml into a typed object."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class OutboundDay:
    date: str
    earliest_departure_hour: int | None = None


@dataclass
class Layover:
    """When a connection is worth having. See config.yaml for the reasoning."""

    min_minutes: int = 75
    short_max_minutes: int = 240
    explore_min_minutes: int = 420
    explore_max_minutes: int = 1080
    day_from_hour: int = 8
    day_to_hour: int = 21
    min_daylight_minutes: int = 300
    max_dead_minutes: int = 780


@dataclass
class Config:
    origin: str = "SIN"
    currency: str = "SGD"
    adults: int = 1
    seat: str = "economy"
    carry_on_bags: int = 1
    checked_bags: int = 1
    max_stops: int | None = 1
    exclude_basic_economy: bool = False

    max_total: int = 900
    realert_drop: int = 20
    new_best_drop: int = 40

    outbound: list[OutboundDay] = field(default_factory=list)
    return_dates: list[str] = field(default_factory=list)
    arrive_home_by: datetime = datetime(2026, 12, 30, 1, 0)
    min_nights: int = 7

    destinations: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    rail_groups: dict[str, list[str]] = field(default_factory=dict)
    check_round_trip: bool = True
    round_trip_candidates: int = 6
    extended_per_run: int = 12

    cities: dict[str, str] = field(default_factory=dict)
    layover: Layover = field(default_factory=Layover)

    keep_per_search: int = 5
    report_top: int = 12
    max_per_city_pair: int = 3

    request_delay_seconds: float = 3.0
    request_retries: int = 2
    request_backoff_seconds: float = 8.0

    proxy: str | None = None
    data_dir: Path = ROOT / "data"

    def _tier(self, name: str) -> list[str]:
        out: list[str] = []
        for tiers in self.destinations.values():
            out.extend(tiers.get(name, []))
        return out

    @property
    def priority_destinations(self) -> list[str]:
        return self._tier("priority")

    @property
    def extended_destinations(self) -> list[str]:
        return self._tier("extended")

    @property
    def all_destinations(self) -> list[str]:
        return self.priority_destinations + self.extended_destinations

    def country_of(self, code: str) -> str:
        for country, tiers in self.destinations.items():
            if code in tiers.get("priority", []) or code in tiers.get("extended", []):
                return country
        return "?"

    def city_name(self, code: str) -> str:
        """Readable name for an airport or city code, falling back to the code."""
        return self.cities.get(code, code)

    def rail_groups_of(self, code: str) -> set[str]:
        return {name for name, codes in self.rail_groups.items() if code in codes}

    def reachable_by_train(self, landed: str, leaving_from: str) -> bool:
        """Can you get from the city you landed in to the one you fly home from?

        True for the same city, or when the two share a rail group. A city in
        no group (an island) can only be flown round-trip.
        """
        if landed == leaving_from:
            return True
        return bool(self.rail_groups_of(landed) & self.rail_groups_of(leaving_from))

    def destinations_for_run(self, cursor: int) -> tuple[list[str], int]:
        """Priority cities plus the next slice of the extended rotation."""
        extended = self.extended_destinations
        if not extended or self.extended_per_run <= 0:
            return self.priority_destinations, cursor

        take = min(self.extended_per_run, len(extended))
        start = cursor % len(extended)
        slice_ = [extended[(start + i) % len(extended)] for i in range(take)]
        return self.priority_destinations + slice_, (start + take) % len(extended)


def load(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    cfg = Config()
    for key in (
        "origin", "currency", "adults", "seat", "carry_on_bags",
        "checked_bags", "max_stops",
        "exclude_basic_economy", "max_total", "realert_drop", "new_best_drop",
        "return_dates", "min_nights", "destinations", "rail_groups",
        "check_round_trip",
        "round_trip_candidates", "extended_per_run", "keep_per_search",
        "report_top", "max_per_city_pair", "cities",
        "request_delay_seconds", "request_retries",
        "request_backoff_seconds",
    ):
        if key in raw and raw[key] is not None:
            setattr(cfg, key, raw[key])

    cfg.outbound = [
        OutboundDay(
            date=str(day["date"]),
            earliest_departure_hour=day.get("earliest_departure_hour") or None,
        )
        for day in raw.get("outbound", [])
    ]
    cfg.return_dates = [str(d) for d in cfg.return_dates]

    rules = raw.get("layover") or {}
    cfg.layover = Layover(**{k: v for k, v in rules.items() if hasattr(Layover, k)})

    deadline = raw.get("arrive_home_by")
    if isinstance(deadline, datetime):
        cfg.arrive_home_by = deadline
    elif deadline:
        cfg.arrive_home_by = datetime.strptime(str(deadline), "%Y-%m-%d %H:%M")

    # A proxy is only needed if the scraper starts getting blocked.
    cfg.proxy = os.environ.get("FLIGHTWATCH_PROXY") or None
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
