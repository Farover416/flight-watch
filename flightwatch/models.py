"""Plain data structures shared across the watcher."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Iterable

GOOGLE_FLIGHTS = "https://www.google.com/travel/flights"


@dataclass(frozen=True)
class Layover:
    """A stop between two flights, in the connecting airport's local time."""

    airport: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def length(self) -> str:
        hours, minutes = divmod(self.minutes, 60)
        return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"

    def daylight_minutes(self, from_hour: int, to_hour: int) -> int:
        """Minutes of this stop that land in usable city hours.

        Counted across every day the stop spans, so an overnight stop that runs
        into the morning still earns credit for the morning part.
        """
        total = 0
        day = self.start.date()
        while day <= self.end.date():
            midnight = datetime.combine(day, time())
            window_start = midnight + timedelta(hours=from_hour)
            window_end = midnight + timedelta(hours=to_hour)
            overlap = min(self.end, window_end) - max(self.start, window_start)
            total += max(0, int(overlap.total_seconds() // 60))
            day += timedelta(days=1)
        return total

    def describe(self, name: str | None = None) -> str:
        return f"{name or self.airport} {self.length}"


@dataclass(frozen=True)
class Leg:
    """One priced one-way itinerary (may contain several flight segments)."""

    search_from: str  # code we searched, e.g. SIN
    search_to: str  # code we searched, e.g. TYO
    from_airport: str  # airport actually flown from, e.g. SIN
    to_airport: str  # airport actually landed at, e.g. NRT
    depart: datetime
    arrive: datetime
    duration_min: int
    stops: int
    airlines: tuple[str, ...]
    price: int          # all-in: fare plus the estimated checked-bag fee
    layovers: tuple[Layover, ...] = ()
    bag_fee: int = 0    # how much of price is the bag estimate

    @property
    def date(self) -> str:
        return self.depart.strftime("%Y-%m-%d")

    @property
    def airline_label(self) -> str:
        return ", ".join(self.airlines) if self.airlines else "?"

    def describe(self) -> str:
        stops = "direct" if self.stops == 0 else f"{self.stops} stop"
        return (
            f"{self.from_airport}→{self.to_airport} "
            f"{self.depart:%d %b %H:%M}→{self.arrive:%d %b %H:%M} "
            f"({stops}, {self.airline_label})"
        )


@dataclass(frozen=True)
class Combo:
    """A complete return trip.

    ``back`` is None for round-trip quotes, where Google prices the whole trip
    but only details the outbound half on the results page. ``total`` is always
    the full return fare including estimated carry-on fees.
    """

    out: Leg
    back_date: str
    country: str
    total: int
    back: Leg | None = None
    source: str = "one-way pair"

    @property
    def back_from(self) -> str:
        return self.back.from_airport if self.back else self.out.to_airport

    @property
    def back_city(self) -> str:
        """City code you fly home from, as opposed to the airport."""
        return self.back.search_from if self.back else self.out.search_to

    @property
    def back_verified(self) -> bool:
        return self.back is not None

    @property
    def nights(self) -> int:
        back_day = datetime.strptime(self.back_date, "%Y-%m-%d").date()
        return (back_day - self.out.arrive.date()).days

    @property
    def is_open_jaw(self) -> bool:
        return self.back_from != self.out.to_airport

    def signature(self) -> str:
        return (
            f"{self.out.to_airport}/{self.back_from}/"
            f"{self.out.date}/{self.back_date}/{self.source}"
        )

    def booking_urls(self, cfg) -> list[tuple[str, str]]:
        """Google Flights links that reproduce this price.

        A one-way pair is two separate tickets, and searching it as one
        multi-city trip usually prices higher, so it gets one link per leg.
        A round-trip quote is a single ticket and gets a single link.
        """
        from .search import build_query  # local import avoids a cycle

        def link(legs, trip):
            query = build_query(cfg, legs, trip=trip)
            return f"{GOOGLE_FLIGHTS}?{urllib.parse.urlencode(query.params())}"

        if self.back is not None:
            return [
                (
                    "outbound",
                    link(
                        [(self.out.from_airport, self.out.to_airport, self.out.date, None)],
                        "one-way",
                    ),
                ),
                (
                    "return",
                    link(
                        [(self.back.from_airport, self.back.to_airport, self.back_date, None)],
                        "one-way",
                    ),
                ),
            ]
        return [
            (
                "book",
                link(
                    [
                        (self.out.search_from, self.out.search_to, self.out.date, None),
                        (self.out.search_to, self.out.search_from, self.back_date, None),
                    ],
                    "round-trip",
                ),
            )
        ]

    def describe(self) -> str:
        tag = " · open-jaw" if self.is_open_jaw else ""
        lines = [
            f"S${self.total} — {self.country}{tag}",
            f"  out  {self.out.describe()}"
            + (f"  S${self.out.price}" if self.back else ""),
        ]
        if self.back is not None:
            lines.append(f"  back {self.back.describe()}  S${self.back.price}")
        else:
            lines.append(
                f"  back {self.back_from}→{self.out.search_from} "
                f"{self.back_date} (return times not pinned — verify arrival)"
            )
        lines.append(f"  {self.nights} nights · {self.source}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "signature": self.signature(),
            "total": self.total,
            "country": self.country,
            "source": self.source,
            "open_jaw": self.is_open_jaw,
            "nights": self.nights,
            "out": {
                "route": f"{self.out.from_airport}-{self.out.to_airport}",
                "depart": self.out.depart.isoformat(),
                "arrive": self.out.arrive.isoformat(),
                "stops": self.out.stops,
                "airlines": list(self.out.airlines),
                "price": self.out.price,
            },
            "back": (
                {
                    "route": f"{self.back.from_airport}-{self.back.to_airport}",
                    "depart": self.back.depart.isoformat(),
                    "arrive": self.back.arrive.isoformat(),
                    "stops": self.back.stops,
                    "airlines": list(self.back.airlines),
                    "price": self.back.price,
                }
                if self.back
                else {"route": f"{self.back_from}-{self.out.search_from}",
                      "date": self.back_date, "verified": False}
            ),
        }


@dataclass
class SweepResult:
    started: datetime
    finished: datetime | None = None
    combos: list[Combo] = field(default_factory=list)
    searches_run: int = 0
    searches_failed: int = 0
    legs_found: int = 0
    # Split out, because "legs found but no trips" is ambiguous until you know
    # which side came back empty - you need both to build a return trip.
    outbound_legs: int = 0
    inbound_legs: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def looks_blocked(self) -> bool:
        """Most searches ERRORED, rather than simply finding nothing.

        Finding nothing is a real answer - a small region with no service
        inside the date and layover rules returns zero legs and zero failures,
        and warning about that would be a false alarm. Being blocked shows up
        as searches raising, which _fetch now tells apart from an empty
        results page.
        """
        return (
            self.searches_run >= 5
            and self.searches_failed >= self.searches_run * 0.8
        )

    def best(self, n: int) -> list[Combo]:
        return sorted(self.combos, key=lambda c: c.total)[:n]


def dedupe_cheapest(combos: Iterable[Combo]) -> list[Combo]:
    """Keep only the cheapest combo per city pair + date pair + source."""
    best: dict[str, Combo] = {}
    for combo in combos:
        key = combo.signature()
        if key not in best or combo.total < best[key].total:
            best[key] = combo
    return sorted(best.values(), key=lambda c: c.total)
