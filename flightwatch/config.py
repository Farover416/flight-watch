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
class Window:
    """Dates to search across, rather than a handful of named days.

    The December trip knows exactly which days it can fly: two out, three
    back, six pairs. A trip that is only pinned to a school holiday knows no
    such thing - any departure and any return inside the window will do, as
    long as the stay is long enough to be worth the flight. That is 323 pairs
    for a six-week window at two to four weeks away, far too many to price
    every run, so the grid is walked in two passes. See ``pairs`` and
    ``scan`` below for what each pass covers.
    """

    start: str
    end: str
    min_nights: int = 14
    max_nights: int = 30
    # The coarse pass: every Nth departure date, at a few sample lengths.
    scan_step: int = 3
    scan_durations: list[int] = field(default_factory=lambda: [14, 18, 22, 26, 30])
    # The fine pass, around whatever the coarse pass liked best. More than one
    # centre because a six-week window usually has two cheap patches - the
    # start of the holiday and the run-up to Christmas - and refining only the
    # better of them is how the other one stays permanently unexamined.
    zoom_radius: int = 2
    zoom_around: int = 2


@dataclass
class Trip:
    """One thing being watched: where, when, for whom, and where it is filed.

    A trip is not a second program. It is this same watcher with some
    settings changed and its own directory to keep what it has learned in,
    which is why nothing downstream of here - no search, no price, no link,
    no signature - needs any notion of trips at all.
    """

    key: str
    name: str
    data_subdir: str = ""
    window: Window | None = None
    settings: dict = field(default_factory=dict)


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
    checked_bags: int = 0
    checked_bag_fees: dict[str, int] = field(default_factory=dict)
    max_stops: int | None = 1
    exclude_basic_economy: bool = False

    # None means no ceiling: report the cheapest there is and say when it is a
    # new low. A trip months away with no fixed budget has nothing to compare
    # a number against, so inventing one would only silence the alerts.
    max_total: int | None = 900
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
    areas: dict[str, list[str]] = field(default_factory=dict)
    layover: Layover = field(default_factory=Layover)

    # Everything being watched, and which of them this config currently is.
    # ``trip`` is None for the original December watch, which is the default
    # and keeps writing exactly where it always has.
    trips: dict[str, Trip] = field(default_factory=dict)
    trip: Trip | None = None

    keep_per_search: int = 5
    report_top: int = 12
    max_per_city_pair: int = 3
    # How many of the cheapest-regardless-of-connections trips to list.
    unrestricted_top: int = 5
    # How many searches to re-open in a real browser to read the prices Google
    # renders. Small on purpose: this is for the trips worth acting on, not the
    # whole sweep. 0 turns it off.
    verify_top: int = 6
    verify_rows: int = 4

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

    def resolve_destinations(self, terms) -> tuple[list[str], list[str]]:
        """Turn what someone typed into searchable city codes.

        Accepts an area name (Yunnan), a city name (Beijing) or a code (BJS),
        because nobody remembers the codes. Returns (resolved, unrecognised).
        """
        by_area = {name.casefold(): codes for name, codes in self.areas.items()}
        by_code = {code.casefold(): code for code in self.all_destinations}
        by_name: dict[str, str] = {}
        for code in self.all_destinations:
            by_name.setdefault(self.city_name(code).casefold(), code)

        resolved: list[str] = []
        unknown: list[str] = []
        for term in terms:
            key = str(term).strip().casefold()
            if not key:
                continue
            if key in by_area:
                for member in by_area[key]:
                    if member in self.all_destinations and member not in resolved:
                        resolved.append(member)
                continue
            code = by_code.get(key) or by_name.get(key)
            if code is None:
                hits = sorted({c for name, c in by_name.items() if key in name})
                code = hits[0] if len(hits) == 1 else None
            if code is None:
                unknown.append(str(term).strip())
            elif code not in resolved:
                resolved.append(code)
        return resolved, unknown

    def checked_bag_fee(self, airlines) -> int:
        """Estimated one-way checked-bag fee for an itinerary, in SGD.

        Google will not price this for us, so it is added from the table in
        config.yaml. Carriers absent from the table include a bag in the fare.
        """
        fee = 0
        for airline in airlines or ():
            name = str(airline).casefold()
            for key, amount in self.checked_bag_fees.items():
                if str(key).casefold() in name:
                    fee = max(fee, int(amount))
        return fee

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

    def for_trip(self, key: str) -> "Config":
        """This same config, as the named trip sees it.

        The overrides are applied to a copy and the data directory moves to
        the trip's own subdirectory. Keeping the trips in separate
        directories rather than tagging their rows is deliberate: the
        signature every price is filed under has no room for a trip, and
        widening it would re-key every alert and break every line already
        drawn on the chart. Separate directories cost nothing and leave the
        recorded past exactly as it is.
        """
        import dataclasses

        trip = self.trips.get(key)
        if trip is None:
            known = ", ".join(sorted(self.trips)) or "(none configured)"
            raise KeyError(f"no trip called {key!r} - I know: {known}")

        settings = dict(trip.settings)
        if trip.window is not None:
            # One source of truth for how long the stay may be, so the grid
            # and the pairing rules cannot drift apart.
            settings.setdefault("min_nights", trip.window.min_nights)
        unknown = [k for k in settings if not hasattr(self, k)]
        if unknown:
            raise KeyError(f"trip {key!r} sets unknown option(s): "
                           f"{', '.join(sorted(unknown))}")
        if "arrive_home_by" in settings:
            settings["arrive_home_by"] = _as_datetime(settings["arrive_home_by"])

        changed = dataclasses.replace(self, **settings)
        changed.trip = trip
        if trip.data_subdir:
            changed.data_dir = self.data_dir / trip.data_subdir
            changed.data_dir.mkdir(parents=True, exist_ok=True)
        return changed

    @property
    def trip_name(self) -> str:
        """What to call this watch in a message."""
        return self.trip.name if self.trip else "Singapore ⇄ China / Korea / Japan"

    def destinations_for_run(self, cursor: int) -> tuple[list[str], int]:
        """Priority cities plus the next slice of the extended rotation."""
        extended = self.extended_destinations
        if not extended or self.extended_per_run <= 0:
            return self.priority_destinations, cursor

        take = min(self.extended_per_run, len(extended))
        start = cursor % len(extended)
        slice_ = [extended[(start + i) % len(extended)] for i in range(take)]
        return self.priority_destinations + slice_, (start + take) % len(extended)


def _as_datetime(value) -> datetime:
    """A deadline however YAML handed it over - already parsed, or text."""
    if isinstance(value, datetime):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d %H:%M")


def load(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    cfg = Config()
    for key in (
        "origin", "currency", "adults", "seat", "carry_on_bags",
        "checked_bags", "checked_bag_fees", "max_stops",
        "exclude_basic_economy", "max_total", "realert_drop", "new_best_drop",
        "return_dates", "min_nights", "destinations", "rail_groups",
        "check_round_trip",
        "round_trip_candidates", "extended_per_run", "keep_per_search",
        "report_top", "max_per_city_pair", "unrestricted_top",
        "verify_top", "verify_rows", "cities", "areas",
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

    # max_total may be set to nothing on purpose, which the loop above cannot
    # express: it skips None so that an absent key keeps the default.
    if "max_total" in raw:
        cfg.max_total = raw["max_total"]

    cfg.trips = {}
    for key, spec in (raw.get("trips") or {}).items():
        spec = spec or {}
        window = spec.get("window")
        cfg.trips[str(key)] = Trip(
            key=str(key),
            name=str(spec.get("name") or key),
            data_subdir=str(spec.get("data_subdir") or ""),
            window=Window(
                start=str(window["from"]),
                end=str(window["to"]),
                **{k: v for k, v in window.items()
                   if k not in ("from", "to") and hasattr(Window, k)},
            ) if window else None,
            settings=dict(spec.get("settings") or {}),
        )

    deadline = raw.get("arrive_home_by")
    if deadline:
        cfg.arrive_home_by = _as_datetime(deadline)

    # A proxy is only needed if the scraper starts getting blocked.
    cfg.proxy = os.environ.get("FLIGHTWATCH_PROXY") or None
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
