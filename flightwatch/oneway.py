"""The flight home, on its own, once the way out is booked.

On 4 Oct the December trip's outbound was settled - into Qingdao on the 19th,
on to Weihai, then Beijing - and the one thing left to buy was the flight home.
So the December watch can be set (``one_way_home`` in config.yaml) to price
nothing else: a one-way flight from one city to Singapore on a few days, read
from the search feed and then off the search page itself, where the agency
fares are, with every long stop timed against the layover rules.

Each flight is a OneWay (models.py), shaped like a trip where the record and
the messages look, so the same files, the same chart and the same message
layout carry it.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime

from . import search
from .models import OneWay, SweepResult
from .search import search_one_way
from .survey import Page
from .verify import _search

log = logging.getLogger("flightwatch.oneway")


def for_run(cfg):
    """This config as the watch of the flight home reads it.

    The December landing limit (01:00 on the 30th) was for a trip that came
    home by the 29th; flying home on the 30th cannot meet it. Only a limit
    set in ``one_way_home`` applies.
    """
    plan = cfg.one_way_home
    cfg = copy.copy(cfg)
    cfg.arrive_home_by = plan.arrive_by or datetime.max
    return cfg


def trips(legs, read: dict | None = None):
    """(within the rules, rules aside), each a OneWay, cheapest first.

    ``read`` maps a day to the page its flights were read off: those count as
    checked, at the price the page showed, and link to that page.
    """
    read = read or {}

    def make(leg):
        url = read.get(leg.date)
        return OneWay(out=leg, total=leg.price, verified=leg.price if url else None,
                      verified_urls=(("book", url),) if url else ())

    every = sorted((make(leg) for leg in legs), key=lambda t: t.price)
    return [t for t in every if t.out.layover_ok], every


def sweep(cfg) -> SweepResult:
    """Every flight home the search feed has on each of the days."""
    plan = cfg.one_way_home
    result = SweepResult(started=datetime.utcnow())
    search.reset_unreadable()
    legs = []
    for date in plan.dates:
        label = f"{plan.city}->{cfg.origin} {date}"
        result.searches_run += 1
        try:
            found = search_one_way(cfg, plan.city, cfg.origin, date,
                                   arrive_by=plan.arrive_by)
            log.info("%-28s %d result(s)", label, len(found))
        except Exception as exc:
            result.searches_failed += 1
            result.errors.append(f"{label}: {exc}")
            log.warning("%-28s FAILED (%s)", label, exc)
            found = []
        legs.extend(found)
    result.legs_in = list(legs)
    result.inbound_legs = result.legs_found = len(legs)
    result.searches_unparsed = search.unreadable_count()
    result.combos, result.any_combos = trips(legs)
    result.finished = datetime.utcnow()
    log.info("flights home: %d (cheapest S$%s)", len(legs),
             min((leg.price for leg in legs), default="-"))
    return result


def pages(cfg) -> list[Page]:
    """Each day's one-way search page, for the browser to read."""
    plan = cfg.one_way_home
    name = cfg.city_name(plan.city)
    return [Page("ow", f"{name} home {date[5:]}",
                 _search(cfg, [(plan.city, cfg.origin, date, None)], "one-way"),
                 ("ow", plan.city, cfg.origin, date), plan.city, plan.city, date,
                 None, None, frm=plan.city, to=cfg.origin)
            for date in plan.dates]


def apply_survey(result, found, read_pages) -> None:
    """The flights on a page that was read replace the feed's for that day;
    a day whose page could not be read keeps the feed's."""
    read: dict[str, str] = {}
    from_pages = []
    for target in read_pages:
        legs = found.legs.get(target.key)
        if legs is None:
            continue
        read[target.out_date] = target.url
        from_pages.extend(legs)
    kept = [leg for leg in result.legs_in if leg.date not in read]
    result.combos, result.any_combos = trips(kept + from_pages, read)
