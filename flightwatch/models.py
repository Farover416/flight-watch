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
    # Whether the connections pass the quick-or-worth-leaving-the-airport rules.
    # Failing legs are kept rather than dropped: they cost nothing extra to
    # carry, and they are what the unrestricted list is made of.
    layover_ok: bool = True
    # The connection as Google wrote it - "Beijing Capital 21h05m" - for a leg
    # read off a rendered page rather than the search feed. A row gives a
    # stop's length and airport but never when it starts, so such a leg
    # carries Layover objects only when the row was opened up on the page and
    # the real times read off it: inventing a start time would make every
    # "hours out in the city" figure derived from it quietly wrong.
    connection_note: str = ""
    # "self transfer" or "separate tickets" when the page sells the flight
    # that way: a connection you collect your bag at and check in again for,
    # or two tickets Google books together. Same price either way, but not
    # the same trip if the first flight runs late, so it is always said.
    ticketing: str = ""

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
    # What a browser actually read off the page for this exact trip, when one
    # was opened. ``total`` stays the parsed fare either way, so the gap
    # between the two is never lost - it is the reason verification exists.
    verified: int | None = None
    verified_urls: tuple[tuple[str, str], ...] = ()
    # The return flight, for a trip Google prices as one ticket. Its results
    # list only ever details the outbound, so this is read from the screen
    # after choosing an outbound - the only place the return is shown.
    verified_back: str | None = None

    @property
    def price(self) -> int:
        """The number to act on: what was read if it was read, else parsed.

        Everything that decides money after verification - the budget test,
        the new-low test, what gets alerted and ranked - uses this. The
        combination logic upstream still works on ``total``, because it runs
        before any page has been opened.
        """
        return self.total if self.verified is None else self.verified

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
    def comfortable(self) -> bool:
        """Every connection on this trip passes the layover rules."""
        return self.out.layover_ok and (self.back is None or self.back.layover_ok)

    @property
    def nights(self) -> int:
        back_day = datetime.strptime(self.back_date, "%Y-%m-%d").date()
        return (back_day - self.out.arrive.date()).days

    @property
    def is_open_jaw(self) -> bool:
        """Landing in one city and flying home from another.

        Compared by city, not by airport. Beijing has two - Capital and
        Daxing - and arriving at one while leaving from the other was being
        reported as an open jaw. It is not: it is the same city, and calling
        it an open jaw padded the list with trips that go nowhere new and
        pushed the real Beijing-to-Qingdao ones further down it.
        """
        return self.back_city != self.out.search_to

    def signature(self) -> str:
        return (
            f"{self.out.to_airport}/{self.back_from}/"
            f"{self.out.date}/{self.back_date}/{self.source}"
        )

    def booking_urls(self, cfg) -> list[tuple[str, str]]:
        """Google Flights links that reproduce this price.

        A one-way pair is two separate tickets, and searching it as one
        multi-city trip usually prices higher, so it gets one link per leg.
        A round-trip quote is a single ticket and gets a single link, and so
        does a multi-city one - one ticket into one city and home from the
        other - whether or not its return was read off the page.
        """
        from .search import build_query  # local import avoids a cycle

        def link(legs, trip):
            query = build_query(cfg, legs, trip=trip)
            return f"{GOOGLE_FLIGHTS}?{urllib.parse.urlencode(query.params())}"

        if self.source == "multi-city" and self.back is not None:
            return [("book", link(
                [(self.out.search_from, self.out.search_to, self.out.date, None),
                 (self.back.search_from, self.back.search_to, self.back_date, None)],
                "multi-city"))]
        if self.back is not None and self.source != "round-trip":
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
            "total": self.price,
            "parsed": self.total,
            "verified": self.verified,
            "verified_back": self.verified_back,
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
    # The same sweep with the layover rules switched off - the floor price, and
    # a measure of what insisting on good connections costs.
    any_combos: list[Combo] = field(default_factory=list)
    searches_run: int = 0
    searches_failed: int = 0
    # Pages that came back, looked like results, and could not be decoded.
    # These used to be filed as "this route has no flights", which is how a
    # sweep could report nothing at all for a city that has daily service.
    searches_unparsed: int = 0
    legs_found: int = 0
    # Split out, because "legs found but no trips" is ambiguous until you know
    # which side came back empty - you need both to build a return trip.
    outbound_legs: int = 0
    inbound_legs: int = 0
    errors: list[str] = field(default_factory=list)
    # Every leg the feed returned, rules or not. The survey matches the rows
    # it reads against these: the feed knows when a connection happens, which
    # a page row never says, and the daylight half of the layover rules
    # needs exactly that.
    legs_out: list = field(default_factory=list)
    legs_in: list = field(default_factory=list)

    @property
    def blind_searches(self) -> int:
        """Searches that returned no usable answer either way."""
        return self.searches_failed + self.searches_unparsed

    @property
    def looks_blocked(self) -> bool:
        """Most searches came back with nothing we could read.

        Finding nothing is a real answer - a small region with no service
        inside the date and layover rules returns zero legs, zero failures and
        zero unreadable pages, and warning about that would be a false alarm.
        Being unable to see is different, and counts whether the search raised
        or merely handed back a page we could not decode.
        """
        return (
            self.searches_run >= 5
            and self.blind_searches >= self.searches_run * 0.8
        )

    def best(self, n: int) -> list[Combo]:
        return sorted(self.combos, key=lambda c: c.price)[:n]


def dedupe_cheapest(combos: Iterable[Combo]) -> list[Combo]:
    """Keep only the cheapest combo per city pair + date pair + source."""
    best: dict[str, Combo] = {}
    for combo in combos:
        key = combo.signature()
        if key not in best or combo.total < best[key].total:
            best[key] = combo
    return sorted(best.values(), key=lambda c: c.total)
