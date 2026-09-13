"""Talks to Google Flights through fast-flights and returns typed legs."""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from typing import Sequence

from fast_flights import (
    FlightQuery,
    Passengers,
    create_query,
    fetch_flights_html,
)
from fast_flights.exceptions import FlightsNotFound
from fast_flights.model import (
    Airline,
    Airport,
    Alliance,
    CarbonEmission,
    Flights,
    JsMetadata,
    SimpleDatetime,
    SingleFlight,
)
from fast_flights.parser import ResultList, _parse_time
from selectolax.lexbor import LexborHTMLParser

from .models import Layover, Leg

log = logging.getLogger("flightwatch.search")

LegSpec = tuple[str, str, str, int | None]  # from, to, date, earliest_departure_hour


def build_query(cfg, legs: Sequence[LegSpec], trip: str):
    """Assemble a fast-flights Query from (from, to, date, earliest_hour) tuples."""
    queries = [
        FlightQuery(
            date=date,
            from_airport=origin,
            to_airport=dest,
            max_stops=cfg.max_stops,
            earliest_departure_hour=earliest,
        )
        for origin, dest, date, earliest in legs
    ]
    return create_query(
        flights=queries,
        trip=trip,
        seat=cfg.seat,
        passengers=Passengers(adults=cfg.adults),
        currency=cfg.currency,
        language="en-US",
        max_stops=cfg.max_stops,
        carry_on_bags=cfg.carry_on_bags,
        checked_bags=cfg.checked_bags,
        exclude_basic_economy=cfg.exclude_basic_economy,
    )


def _to_datetime(simple) -> datetime:
    year, month, day = simple.date
    hour, minute = simple.time
    return datetime(year, month, day, hour, minute)


def _layovers(segments) -> tuple[Layover, ...]:
    """Gaps between consecutive flights, in the connecting airport's local time."""
    return tuple(
        Layover(
            airport=segments[i].to_airport.code,
            start=_to_datetime(segments[i].arrival),
            end=_to_datetime(segments[i + 1].departure),
        )
        for i in range(len(segments) - 1)
    )


def layovers_ok(cfg, layovers) -> bool:
    """Quick, or enough usable daytime to go into the city. Nothing between.

    A long stop is judged on how many hours of it fall in city hours, not on
    whether it sits tidily inside them - landing at 05:00 is fine, you just wait
    for the place to wake up. What it must not be is a night spent in a terminal
    to buy a few daylight hours, so the time outside city hours is capped too.
    """
    rules = cfg.layover
    for stop in layovers:
        minutes = stop.minutes
        if minutes < rules.min_minutes:
            return False
        if minutes <= rules.short_max_minutes:
            continue
        if not rules.explore_min_minutes <= minutes <= rules.explore_max_minutes:
            return False
        daylight = stop.daylight_minutes(rules.day_from_hour, rules.day_to_hour)
        if daylight < rules.min_daylight_minutes:
            return False
        if minutes - daylight > rules.max_dead_minutes:
            return False
    return True


def _airline_names(codes, metadata) -> tuple[str, ...]:
    lookup = {a.code: a.name for a in getattr(metadata, "airlines", [])}
    return tuple(lookup.get(code, code) for code in (codes or []))


# fast-flights' parser assumes Google returned flight results. When it did not,
# the parse falls over with one of these rather than returning an empty list.
NO_RESULTS_ERRORS = (TypeError, IndexError, KeyError, AttributeError)

# Markers of a page Google served instead of results.
THROTTLE_MARKERS = ("unusual traffic", "/sorry/", "captcha", "consent.google")


class NotAResultsPage(RuntimeError):
    """Google served something other than a search-results page.

    Worth retrying: a consent wall, a throttle page, a truncated response.
    """


class UnreadablePage(RuntimeError):
    """A results page whose payload we could not decode.

    Not worth retrying - the same request fetches the same page - but it must
    never be confused with "no flights", which is what was happening before:
    every one of these was recorded as an empty route, and roughly half of an
    average sweep vanished that way.
    """


def _ds_payloads(tree):
    """Google's data-callback scripts, keyed by name, ds:1 first.

    Google ships the page's data in ``AF_initDataCallback`` script tags named
    ds:0, ds:1, ds:2 ... fast-flights only ever reads ds:1, which is where the
    itineraries usually live but by no means always.
    """
    found = []
    for node in tree.css("script"):
        name = node.attributes.get("class") or ""
        if name.startswith("ds:"):
            found.append((name, node))
    found.sort(key=lambda pair: pair[0] != "ds:1")
    return found


def _at(seq, index):
    """seq[index], or None when it was not shipped."""
    try:
        return seq[index]
    except (TypeError, IndexError, KeyError):
        return None


def _decode_itinerary(entry) -> Flights:
    """One priced itinerary out of Google's positional arrays.

    Only the fields we actually use are required. Carbon figures are optional
    because Google omits them often enough to matter and we never read them.
    """
    flight = entry[0]
    price = entry[1][0][1]

    segments = []
    for seg in flight[2]:
        segments.append(
            SingleFlight(
                from_airport=Airport(code=seg[3], name=seg[4]),
                to_airport=Airport(code=seg[6], name=seg[5]),
                departure=SimpleDatetime(
                    date=tuple(seg[20]), time=_parse_time(seg[8])
                ),
                arrival=SimpleDatetime(
                    date=tuple(seg[21]), time=_parse_time(seg[10])
                ),
                duration=_at(seg, 11),
                plane_type=_at(seg, 17),
            )
        )
    if not segments:
        raise ValueError("itinerary with no flights")

    extras = _at(flight, 22)
    return Flights(
        type=flight[0],
        price=price,
        airlines=flight[1],
        flights=segments,
        carbon=CarbonEmission(
            typical_on_route=_at(extras, 8), emission=_at(extras, 7)
        ),
    )


def _decode_payload(text: str) -> tuple[ResultList, int]:
    """Decode one AF_initDataCallback payload. Returns (itineraries, skipped).

    This is fast-flights' parse_js, rewritten so that one malformed itinerary
    costs you that itinerary rather than the whole page. Google ships each
    flight as a positional array and does not always ship every position; the
    library indexes straight in, so a single short entry anywhere in a
    fifty-result page raised IndexError and the search was then recorded as
    "this route has no flights". The busiest routes carry the most results and
    so tripped it most often - which is to say, the cheap ones.
    """
    data = text.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise FlightsNotFound("no flights found; received error")
    payload = json.loads(data)

    # Most ds: scripts on the page are not the flights payload at all.
    if not isinstance(payload, list) or len(payload) <= 7:
        raise ValueError("not a flights payload")

    results = ResultList()
    directory = _at(_at(payload, 7), 1)
    alliances, airlines = [], []
    try:
        alliances = [Alliance(code=c, name=n) for c, n in directory[0]]
    except Exception:  # nice-to-have; never worth losing the fares over
        pass
    try:
        airlines = [Airline(code=c, name=n) for c, n in directory[1]]
    except Exception:
        pass
    results.metadata = JsMetadata(alliances=alliances, airlines=airlines)

    section = _at(payload, 3)
    if section is None:
        # Google nulls the whole results section when it has nothing for this
        # route and date. That is a real "no flights" - but only believe it on
        # a payload that is otherwise a flights payload, which the airline
        # directory establishes. Guessing wrong here means silently reporting
        # an empty region, which is the failure this whole path exists to stop.
        if not airlines:
            raise ValueError("no results section and no airline directory")
        log.info("empty results section (%d airlines listed)", len(airlines))
        return results, 0

    entries = _at(section, 0)
    if entries is not None and not isinstance(entries, list):
        raise ValueError(f"itinerary list is a {type(entries).__name__}")

    skipped = 0
    for entry in entries or []:
        try:
            results.append(_decode_itinerary(entry))
        except Exception:
            skipped += 1
    return results, skipped


def _parse_results(html: str):
    """Parse Google's itinerary payload, wherever on the page it landed.

    ds:1 is where it normally lives and is tried first; if that payload is not
    the results one, the other ds: scripts get a turn.
    """
    global _skipped
    tree = LexborHTMLParser(html)
    payloads = _ds_payloads(tree)
    if not payloads:
        marker = next((m for m in THROTTLE_MARKERS if m in html[:20_000].lower()), None)
        raise NotAResultsPage(
            f"no ds: payload in {len(html)} bytes"
            + (f" ({marker})" if marker else "")
        )

    first_error: str | None = None
    best: ResultList | None = None
    best_skipped = 0
    for name, node in payloads:
        text = node.text()
        if "data:" not in text:
            continue
        try:
            parsed, skipped = _decode_payload(text)
        except FlightsNotFound:
            raise
        except Exception as exc:
            if first_error is None:
                first_error = f"{name} {type(exc).__name__}: {exc}"
            continue
        if name == "ds:1" and not (skipped and not parsed):
            # The usual place - trust it even when it is legitimately empty,
            # but not when every entry in it failed to decode, which means we
            # are reading the wrong payload and should keep looking.
            best, best_skipped = parsed, skipped
            break
        if best is None or len(parsed) > len(best):
            best, best_skipped = parsed, skipped

    if best is None:
        raise UnreadablePage(
            f"{len(payloads)} payload(s) [{', '.join(n for n, _ in payloads)}] "
            f"in {len(html)} bytes, none parsed; first error: {first_error}"
        )
    if best_skipped:
        _skipped += best_skipped
        log.info("skipped %d unreadable itinerar(y/ies), kept %d",
                 best_skipped, len(best))
    return best


# How many results pages this process could see but not read, and how many
# individual itineraries were dropped out of pages that did read. A sweep that
# cannot decode half its pages has not discovered that the world has no
# flights, and the run needs to say so out loud.
_unreadable = 0
_skipped = 0


def unreadable_count() -> int:
    return _unreadable


def skipped_count() -> int:
    return _skipped


def reset_unreadable() -> None:
    global _unreadable, _skipped
    _unreadable = 0
    _skipped = 0


def _fetch(cfg, query):
    """Fetch with retries. Returns a ResultList, or None for no flights."""
    global _unreadable
    last_error: Exception | None = None
    for attempt in range(cfg.request_retries + 1):
        try:
            html = fetch_flights_html(query, proxy=cfg.proxy)
            return _parse_results(html)
        except FlightsNotFound:
            return None  # a genuine "no flights on this route/date"
        except UnreadablePage as exc:
            _unreadable += 1
            log.warning("UNREADABLE results page: %s", exc)
            return None
        except (NotAResultsPage, *NO_RESULTS_ERRORS) as exc:
            last_error = exc
            log.warning("not a results page - retrying (%s)", exc)
        except Exception as exc:  # network, rate limit
            last_error = exc
        if attempt < cfg.request_retries:
            wait = cfg.request_backoff_seconds * (attempt + 1)
            log.warning("retrying in %.0fs (%s)", wait, last_error)
            time.sleep(wait)
    raise last_error if last_error else RuntimeError("fetch failed")


def _pace(cfg) -> None:
    time.sleep(cfg.request_delay_seconds * random.uniform(0.8, 1.3))


def search_one_way(
    cfg,
    origin: str,
    dest: str,
    date: str,
    *,
    earliest_departure_hour: int | None = None,
    arrive_by: datetime | None = None,
) -> list[Leg]:
    """Cheapest one-way itineraries for a single date, already time-filtered."""
    query = build_query(cfg, [(origin, dest, date, earliest_departure_hour)], "one-way")
    results = _fetch(cfg, query)
    _pace(cfg)
    if not results:
        return []

    wanted_date = datetime.strptime(date, "%Y-%m-%d").date()
    legs: list[Leg] = []
    for item in results:
        segments = item.flights
        if not segments:
            continue
        depart = _to_datetime(segments[0].departure)
        arrive = _to_datetime(segments[-1].arrival)

        # Google occasionally slips in a neighbouring date; enforce ours.
        if depart.date() != wanted_date:
            continue
        if earliest_departure_hour and depart.hour < earliest_departure_hour:
            continue
        if arrive_by and arrive > arrive_by:
            continue
        if not isinstance(item.price, int) or item.price <= 0:
            continue
        layovers = _layovers(segments)
        if not layovers_ok(cfg, layovers):
            continue
        airlines = _airline_names(item.airlines, results.metadata)
        # Google will not price a checked bag, so add our own estimate for the
        # one leg this itinerary covers.
        bag_fee = cfg.checked_bag_fee(airlines)

        legs.append(
            Leg(
                search_from=origin,
                search_to=dest,
                from_airport=segments[0].from_airport.code,
                to_airport=segments[-1].to_airport.code,
                depart=depart,
                arrive=arrive,
                duration_min=sum(s.duration or 0 for s in segments),
                stops=len(segments) - 1,
                airlines=airlines,
                price=item.price + bag_fee,
                layovers=layovers,
                bag_fee=bag_fee,
            )
        )

    legs.sort(key=lambda leg: leg.price)
    return legs[: cfg.keep_per_search]


def search_round_trip(
    cfg,
    dest: str,
    out_date: str,
    back_date: str,
    *,
    earliest_departure_hour: int | None = None,
) -> list[tuple[Leg, int]]:
    """Round-trip search. Returns (outbound leg, total return price) pairs.

    Google prices a round trip as a whole: the headline price on each outbound
    option is the total for the trip, which is exactly the number we score.
    """
    query = build_query(
        cfg,
        [
            (cfg.origin, dest, out_date, earliest_departure_hour),
            (dest, cfg.origin, back_date, None),
        ],
        "round-trip",
    )
    results = _fetch(cfg, query)
    _pace(cfg)
    if not results:
        return []

    wanted_date = datetime.strptime(out_date, "%Y-%m-%d").date()
    quotes: list[tuple[Leg, int]] = []
    for item in results:
        segments = item.flights
        if not segments:
            continue
        depart = _to_datetime(segments[0].departure)
        arrive = _to_datetime(segments[-1].arrival)
        if depart.date() != wanted_date:
            continue
        if earliest_departure_hour and depart.hour < earliest_departure_hour:
            continue
        if not isinstance(item.price, int) or item.price <= 0:
            continue
        layovers = _layovers(segments)
        if not layovers_ok(cfg, layovers):
            continue
        airlines = _airline_names(item.airlines, results.metadata)
        # A round-trip quote covers both legs, so the bag is paid twice.
        bag_fee = cfg.checked_bag_fee(airlines) * 2

        leg = Leg(
            search_from=cfg.origin,
            search_to=dest,
            from_airport=segments[0].from_airport.code,
            to_airport=segments[-1].to_airport.code,
            depart=depart,
            arrive=arrive,
            duration_min=sum(s.duration or 0 for s in segments),
            stops=len(segments) - 1,
            airlines=airlines,
            price=item.price + bag_fee,
            layovers=layovers,
            bag_fee=bag_fee,
        )
        quotes.append((leg, item.price + bag_fee))

    quotes.sort(key=lambda pair: pair[1])
    return quotes[: cfg.keep_per_search]
