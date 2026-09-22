"""Searching a span of dates rather than a handful of named days.

The December trip knows exactly when it can fly - two departure days, three
return days, six pairs per city. A trip pinned only to a school holiday knows
nothing of the sort: any departure and any return inside the window will do,
as long as the stay is long enough to be worth the flight. For 21 Nov to 31
Dec at fourteen to thirty nights that is 323 pairs, and Google is asked for
one exact date at a time - there is no month view, no flexible-dates call, no
calendar endpoint. Pricing all 323 every two hours would be thirty minutes of
searching per run and nearly four thousand requests a day for one route,
which is both slow and the fastest way to get the whole watcher blocked.

So the grid is walked twice. A coarse pass prices every third departure at a
few sample lengths, which is enough to see the shape of the month - fares
move in blocks around weekends and holidays, not day by day. A fine pass then
prices every pair within a couple of days of whatever the coarse pass liked
best, because that is where the actual cheapest pair will be. Around seventy
searches instead of 323, and the one number that matters comes out the same.

What this cannot do is notice a single freakishly cheap day that the coarse
pass stepped straight over and that sits nowhere near the cheap region. That
is the price of not making 323 requests. Lower ``scan_step`` to 1 in
config.yaml to search every pair and give that up.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger("flightwatch.window")

Pair = tuple[str, str]


def _span(window) -> tuple[date, int]:
    start = date.fromisoformat(str(window.start))
    end = date.fromisoformat(str(window.end))
    return start, (end - start).days


def _pair(start: date, out_day: int, back_day: int) -> Pair:
    return ((start + timedelta(out_day)).isoformat(),
            (start + timedelta(back_day)).isoformat())


def _fits(window, span: int, out_day: int, back_day: int) -> bool:
    if not (0 <= out_day <= span and 0 <= back_day <= span):
        return False
    nights = back_day - out_day
    return window.min_nights <= nights <= window.max_nights


def pairs(window) -> list[Pair]:
    """Every departure-and-return the window allows. The whole grid."""
    start, span = _span(window)
    return [_pair(start, i, i + n)
            for i in range(span + 1)
            for n in range(window.min_nights, window.max_nights + 1)
            if i + n <= span]


def scan(window) -> list[Pair]:
    """The coarse pass: every Nth departure, at a few sample lengths.

    The last departure that can still fit the shortest stay is always
    included, whatever the step lands on. The end of a window is exactly
    where a cheap short trip hides, and stepping over it would mean never
    looking there at all.
    """
    start, span = _span(window)
    step = max(1, int(window.scan_step))
    lengths = sorted({int(n) for n in window.scan_durations
                      if window.min_nights <= int(n) <= window.max_nights})
    if not lengths:
        lengths = [window.min_nights]

    days = list(range(0, span + 1, step))
    last = span - window.min_nights
    if last >= 0 and last not in days:
        days.append(last)

    seen: set[Pair] = set()
    out: list[Pair] = []
    for out_day in sorted(days):
        for nights in lengths:
            if not _fits(window, span, out_day, out_day + nights):
                continue
            found = _pair(start, out_day, out_day + nights)
            if found not in seen:
                seen.add(found)
                out.append(found)
    return out


def zoom(window, around: Pair, skip=()) -> list[Pair]:
    """The fine pass: every pair within a couple of days of the best one."""
    start, span = _span(window)
    radius = max(0, int(window.zoom_radius))
    here = date.fromisoformat(around[0]), date.fromisoformat(around[1])
    centre = ((here[0] - start).days, (here[1] - start).days)

    already = set(skip)
    out: list[Pair] = []
    for out_day in range(centre[0] - radius, centre[0] + radius + 1):
        for back_day in range(centre[1] - radius, centre[1] + radius + 1):
            if not _fits(window, span, out_day, back_day):
                continue
            found = _pair(start, out_day, back_day)
            if found not in already:
                already.add(found)
                out.append(found)
    return out


def describe(window) -> str:
    """What this window covers, for a log line."""
    whole, coarse = len(pairs(window)), len(scan(window))
    return (f"{window.start} to {window.end}, {window.min_nights}-"
            f"{window.max_nights} nights: {whole} date pairs, "
            f"{coarse} in the coarse pass")


def sweep(cfg) -> "SweepResult":
    """Price the window: coarse pass, then detail around what looked best.

    Every trip found is recorded, coarse and fine alike, so the chart keeps a
    continuous line for the sampled pairs - those are the same every run - and
    picks up extra points wherever a run went looking in detail.
    """
    from datetime import datetime

    from . import combine, search
    from .models import SweepResult
    from .search import search_round_trip

    window = cfg.trip.window
    result = SweepResult(started=datetime.utcnow())
    search.reset_unreadable()
    quotes: list[tuple] = []
    priced: dict[Pair, int] = {}

    def price(where: str, pair: Pair, label: str) -> None:
        result.searches_run += 1
        try:
            found = search_round_trip(cfg, where, pair[0], pair[1])
        except Exception as exc:
            result.searches_failed += 1
            result.errors.append(f"{where} {pair[0]}/{pair[1]}: {exc}")
            log.warning("%-28s FAILED (%s)", f"{where} {pair[0]}→{pair[1]}", exc)
            return
        quotes.extend((leg, total, pair[1]) for leg, total in found)
        if found:
            best = min(total for _, total in found)
            if pair not in priced or best < priced[pair]:
                priced[pair] = best
        log.info("%-8s %s → %s  %s", label, pair[0], pair[1],
                 f"S${min(total for _, total in found)}" if found else "-")

    for dest in cfg.priority_destinations:
        log.info("%s: %s", cfg.city_name(dest), describe(window))

        coarse = scan(window)
        for pair in coarse:
            price(dest, pair, "scan")

        # Where to look closely. Sorting by what the coarse pass actually
        # priced means the fine pass follows the fares rather than a guess.
        centres = sorted(priced, key=lambda p: priced[p])[:max(1, window.zoom_around)]
        seen = set(coarse)
        close: list[Pair] = []
        for centre in centres:
            found = zoom(window, centre, skip=seen)
            seen.update(found)
            close.extend(found)
        if centres:
            log.info("cheapest so far %s → %s at S$%d; %d pair(s) nearby",
                     centres[0][0], centres[0][1], priced[centres[0]], len(close))
        for pair in close:
            price(dest, pair, "zoom")

    result.legs_found = len(quotes)
    result.outbound_legs = len(quotes)
    result.combos = combine.merge(cfg, combine.round_trip_combos(
        cfg, [q for q in quotes if q[0].layover_ok]))
    result.any_combos = combine.merge(cfg, combine.round_trip_combos(cfg, quotes))
    result.searches_unparsed = search.unreadable_count()
    result.finished = datetime.utcnow()
    log.info("combos: %d (cheapest S$%s) from %d searches",
             len(result.combos),
             result.combos[0].price if result.combos else "-",
             result.searches_run)
    return result
