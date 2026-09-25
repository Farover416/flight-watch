"""Open every search page, instead of the few the search feed ranks cheapest.

The feed a sweep reads carries the airlines' own fares only. The page a
person sees, once it has loaded, also carries the agency, two-ticket and
self-transfer fares - and those do not follow the airline fares around. On
Qingdao 19-28 Dec the feed put the 8:20 PM Cathay flight at S$976 while the
page, read from a laptop in Singapore, sold it for S$497. A watcher that only
opens the six searches the feed likes best will never open that one.

So for a trip with a handful of fixed dates, open all of them:

  one ticket, same city     a round-trip search per city and date pair
  one ticket, open jaw      a multi-city search per pair of cities you can
                            travel between by train, both ways round -
                            Beijing in and Qingdao out, and the reverse
  two tickets               a one-way search per city and day, out and back,
                            paired afterwards the way the sweep pairs them

For a one-ticket search the first screen prices each outbound with its
cheapest return - which may be a return you would never take. So outbounds
are opened cheapest first, and each one's return screen is read for the
cheapest return you could actually take. Since no trip on an outbound can
cost less than that outbound's first-screen price, opening stops as soon as
the next outbound's price is no lower than the trip already found: what is
reported is the cheapest trip on the page, not the cheapest of the first two
tried.

A stop long enough to leave the airport for is only within your rules if
enough of it falls in daylight, and a row never says when its stop happens.
So such a row is opened up on the page ("Flight details"), which lists every
flight's times, and judged on those.

It takes a while - forty-odd pages and their return screens - and that is
the point. It stops at the first sign of a bot check rather than pressing on.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import combine
from .models import Combo, Layover, Leg
from .search import layovers_ok
from .verify import (_BLOCKED, _CHEAPEST_JS, _RESORT_MS, _RETURNS_MS, _ROWS_JS,
                     _SETTLE_MS, RenderedFare, _bag_fee, _clock_time, _launch,
                     _plain_name, _search, _settle_prices, _trustworthy,
                     connection_ok, connections, departs_at, leg_from_row)

log = logging.getLogger("flightwatch.survey")

# The most return screens one one-ticket page may open. Most pages need one:
# when the cheapest outbound's cheapest return is one you would take, nothing
# else on the page can beat it. This caps the odd page where every cheap
# outbound comes with a return you would not take.
MAX_SCREENS = 5

# No new page is started after this long, so the run finishes - and its prices
# are saved and sent - inside its time limit: 100 minutes for a check on the
# laptop, 90 for GitHub's whole job, Taipei included when it is due.
BUDGET_MIN = 80
BUDGET_MIN_GITHUB = 60

# On a one-way page, rows are opened up for their stop times cheapest first
# until this many flights within the rules are known, or this many rows have
# been opened. A pair is built from each page's cheapest flights, so the
# dearer end of a thirty-row list never decides anything.
KNOWN_GOOD_ONE_WAY = 3
MAX_OPENED_ONE_WAY = 12


class Blocked(RuntimeError):
    """Google answered with a bot check or a consent wall, not results."""


@dataclass(frozen=True)
class Page:
    """One search to open, and what a trip read off it would be."""

    kind: str                 # "rt" one ticket, "mc" open jaw, "ow" one way
    label: str
    url: str
    key: tuple
    out_city: str             # where the first flight lands (or the one-way's end)
    back_city: str            # where the flight home leaves from
    out_date: str
    back_date: str | None
    earliest: int | None      # first flight not before this hour, if any
    frm: str = ""             # one-way pages: the search's own ends
    to: str = ""


@dataclass
class SurveyResult:
    combos: list = field(default_factory=list)       # within your rules
    any_combos: list = field(default_factory=list)   # rules aside
    read: set = field(default_factory=set)           # keys of pages read
    pages: int = 0
    screens: int = 0
    failed: list = field(default_factory=list)
    unread: list = field(default_factory=list)       # not reached in time
    blocked: bool = False
    seconds: float = 0.0
    opened_up: int = 0                               # rows opened for stop times
    untimed: int = 0                                 # ... that could not be read

    def best(self) -> dict:
        """The cheapest trip in each category, within your rules."""
        out: dict = {}
        for combo in self.combos:
            key = category(combo)
            if key not in out or combo.price < out[key].price:
                out[key] = combo
        return out

    def floor(self):
        return min(self.any_combos, key=lambda c: c.price, default=None)


def category(combo) -> tuple:
    """(tickets, city you land in, city you fly home from)."""
    tickets = 1 if combo.source in ("round-trip", "multi-city") else 2
    return tickets, combo.out.search_to, combo.back_city


def plan(cfg, cities) -> list[Page]:
    """Every page the trip needs, in the order they are opened."""
    origin = cfg.origin
    pages: list[Page] = []

    def label(a, b, day, back):
        where = cfg.city_name(a) if a == b else \
            f"{cfg.city_name(a)} in, {cfg.city_name(b)} out"
        return f"{where} {day[5:]}/{back[5:]}"

    for city in cities:
        for day in cfg.outbound:
            for back in cfg.return_dates:
                pages.append(Page(
                    "rt", label(city, city, day.date, back),
                    _search(cfg, [(origin, city, day.date, day.earliest_departure_hour),
                                  (city, origin, back, None)], "round-trip"),
                    ("rt", city, day.date, back), city, city, day.date, back,
                    day.earliest_departure_hour))
    for into in cities:
        for home in cities:
            if into == home or not cfg.reachable_by_train(into, home):
                continue
            for day in cfg.outbound:
                for back in cfg.return_dates:
                    pages.append(Page(
                        "mc", label(into, home, day.date, back),
                        _search(cfg, [(origin, into, day.date,
                                       day.earliest_departure_hour),
                                      (home, origin, back, None)], "multi-city"),
                        ("mc", into, home, day.date, back), into, home,
                        day.date, back, day.earliest_departure_hour))
    for city in cities:
        for day in cfg.outbound:
            pages.append(Page(
                "ow", f"{cfg.city_name(city)} out {day.date[5:]}",
                _search(cfg, [(origin, city, day.date,
                               day.earliest_departure_hour)], "one-way"),
                ("ow", origin, city, day.date), city, city, day.date, None,
                day.earliest_departure_hour, frm=origin, to=city))
        for back in cfg.return_dates:
            pages.append(Page(
                "ow", f"{cfg.city_name(city)} back {back[5:]}",
                _search(cfg, [(city, origin, back, None)], "one-way"),
                ("ow", city, origin, back), city, city, back, None, None,
                frm=city, to=origin))
    return pages


# -- reading rows ------------------------------------------------------------

def _price(text: str) -> int | None:
    fare = RenderedFare.parse(text)
    return fare.price if fare else None


def _early(text: str, earliest: int | None) -> bool:
    """Leaves before the day's earliest allowed hour (the 21:00 on the 18th)."""
    if not earliest:
        return False
    when = departs_at(text)
    if not when:
        return True
    try:
        return datetime.strptime(when.replace(" ", ""), "%I:%M%p").hour < earliest
    except ValueError:
        return True


def _sorted(rows):
    priced = [(r, _price(r)) for r in rows]
    return sorted([(r, p) for r, p in priced if p is not None], key=lambda x: x[1])


def _by_cost(cfg, rows):
    """(row, fare, bag), cheapest first with the bag counted - a fare that
    looks cheaper can cost more once its carrier's bag is added."""
    return sorted(((text, fare, _bag_fee(cfg, [text])) for text, fare in _sorted(rows)),
                  key=lambda row: row[1] + row[2])


def _matching(sweep_legs, leg: Leg):
    """The feed's own copy of a flight read off a page, if the sweep had it.

    The feed says when each stop happens, which a page row never does, so a
    twin found here saves opening the row up on the page.
    """
    for other in sweep_legs:
        if (other.depart == leg.depart and other.arrive == leg.arrive
                and other.stops == leg.stops):
            return other
    return None


def _flight(text: str) -> str:
    """The part of a row that says which flight it is - not what it costs.

    The same flight is listed on several pages (its round trip, its one-way
    search, as a return), priced differently on each; what its stops are is
    decided once.
    """
    return re.split(r"\s+\d[\d,]* kg CO2e|\s+SGD\s?\d", text or "", maxsplit=1)[0]


def _needs_daylight(cfg, text) -> bool:
    """Whether a stop on this row is long enough for the daylight rule to
    decide it - the only kind whose clock times matter."""
    rules = cfg.layover
    return any(rules.explore_min_minutes <= gap <= rules.explore_max_minutes
               for gap in connections(text) or [])


# A row opened up lists each flight as its departure and its arrival: a time,
# then the airport - "12:10 AM+1Hong Kong International Airport (HKG)" on a
# wide window, the airport on the next line on a narrow one. The "+1" counts
# from the day the trip leaves, and every time is local to its airport.
_STEP = re.compile(
    r"^[ \t]*(\d{1,2}:\d{2}\s?[AP]M)(?:\s*\+(\d+))?[ \t]*(?:\n[ \t]*)?"
    r"[^\n(]{0,90}?\(([A-Z]{3})\)", re.M)


def stops_from_details(text, on_date):
    """(departure, arrival, stops) from an opened-up row, or None.

    The stops carry their real clock times at the connecting airport - read
    off the page, not worked out - so the daylight half of the rules can be
    applied to them exactly as it is to the feed's flights.
    """
    steps = _STEP.findall(text or "")
    if len(steps) < 2 or len(steps) % 2:
        return None
    try:
        day = datetime.strptime(on_date, "%Y-%m-%d").date()
        when = [(datetime.combine(day + timedelta(days=int(plus or 0)),
                                  _clock_time(clock)), airport)
                for clock, plus, airport in steps]
    except ValueError:
        return None
    stops = []
    for i in range(1, len(when) - 1, 2):
        (landed, here), (left, _there) = when[i], when[i + 1]
        if left < landed:
            return None
        stops.append(Layover(airport=here, start=landed, end=left))
    return when[0][0], when[-1][0], tuple(stops)


def _agrees(details, leg: Leg, text) -> bool:
    """The opened-up row is the row it was opened from: same times, same stops."""
    depart, arrive, stops = details
    gaps = connections(text) or []
    return (depart == leg.depart and arrive == leg.arrive
            and len(stops) == leg.stops == len(gaps)
            and all(abs(s.minutes - g) <= 1 for s, g in zip(stops, gaps)))


# Opens one exact row up, reads what it says, and folds it back - waiting for
# the row to read as it did, since the list is found by its rows' text.
_DETAILS_JS = """async (want) => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const row = Array.from(document.querySelectorAll('li'))
    .find(li => norm(li.innerText) === want);
  if (!row) return {error: 'no match'};
  const toggle = () => row.querySelector('button[aria-label^="Flight details"]');
  const button = toggle();
  if (!button) return {error: 'no details button'};
  if (button.getAttribute('aria-expanded') !== 'true') button.click();
  let text = null;
  for (let i = 0; i < 40 && text === null; i++) {
    await sleep(150);
    if (/Travel time/i.test(row.innerText || '')) {
      await sleep(300);
      text = row.innerText;
    }
  }
  const again = toggle();
  if (again && again.getAttribute('aria-expanded') === 'true') again.click();
  let restored = false;
  for (let i = 0; i < 40 && !restored; i++) {
    await sleep(150);
    restored = norm(row.innerText) === want;
  }
  return {text, restored};
}"""


class Judge:
    """Whether a row's stops pass your layover rules - all of them.

    The length half reads off the row. The daylight half needs to know when a
    stop happens: from the feed's copy of the flight when the sweep had it,
    otherwise by opening the row up on the page. A stop that could not be
    timed is not counted as within the rules - it stays in the rules-aside
    list, where it belongs until someone has looked at it.
    """

    def __init__(self, cfg, sweep_legs):
        self.cfg = cfg
        self.sweep_legs = list(sweep_legs)
        self.known: dict = {}
        self.opened = 0
        self.untimed = 0

    def leg(self, text, on_date, frm, to, page=None, price=0, bag=0):
        """The row as a flight, its layover_ok decided, or None if unreadable.

        Without ``page`` a row that needs opening up is judged not within the
        rules, and not remembered - it can still be decided later, on its page.
        """
        key = (_flight(text), on_date, frm, to)
        leg = self.known.get(key)
        if leg is None:
            leg, final = self._decide(text, on_date, frm, to, page)
            if leg is None:
                return None
            if final:
                self.known[key] = leg
        return dataclasses.replace(leg, price=price, bag_fee=bag)

    def needs_page(self, text, on_date, frm, to) -> bool:
        """Whether judging this row means opening it up on the page."""
        cfg = self.cfg
        if ((_flight(text), on_date, frm, to) in self.known
                or not connection_ok(cfg, text) or not _needs_daylight(cfg, text)):
            return False
        leg = leg_from_row(cfg, text, on_date, frm, to, 0)
        if leg is None:
            return False
        twin = _matching(self.sweep_legs, leg)
        return twin is None or not twin.layovers

    def _decide(self, text, on_date, frm, to, page):
        cfg = self.cfg
        leg = leg_from_row(cfg, text, on_date, frm, to, 0)
        if leg is None:
            return None, True
        if not connection_ok(cfg, text):
            return dataclasses.replace(leg, layover_ok=False), True
        if not _needs_daylight(cfg, text):
            return leg, True
        twin = _matching(self.sweep_legs, leg)
        if twin is not None and twin.layovers:
            return dataclasses.replace(leg, layovers=twin.layovers,
                                       layover_ok=twin.layover_ok), True
        if page is None:
            return dataclasses.replace(leg, layover_ok=False), False
        try:
            got = page.evaluate(_DETAILS_JS, text) or {}
        except Exception as exc:
            log.info("    could not open up %s (%s)", text[:48], exc)
            got = {}
        if got.get("error") == "no match":
            # not on the screen just now - left undecided, not marked unreadable
            return dataclasses.replace(leg, layover_ok=False), False
        self.opened += 1
        details = stops_from_details(got.get("text"), on_date)
        if details is None or not _agrees(details, leg, text):
            self.untimed += 1
            log.info("    could not time the stop on %s", text[:60])
            return dataclasses.replace(leg, layover_ok=False), True
        stops = details[2]
        return dataclasses.replace(leg, layovers=stops,
                                   layover_ok=layovers_ok(cfg, stops)), True


def _long_enough(cfg, target: Page, out: Leg) -> bool:
    return (datetime.strptime(target.back_date, "%Y-%m-%d").date()
            - out.arrive.date()).days >= cfg.min_nights


def one_ticket(cfg, target: Page, out: Leg, back: Leg, fare: int, url: str):
    """A one-ticket trip: the return screen's price for both, bags added."""
    if not _long_enough(cfg, target, out):
        return None
    total = fare + out.bag_fee + back.bag_fee
    return Combo(out=out, back=back, back_date=target.back_date,
                 country=cfg.country_of(target.out_city), total=total,
                 source="round-trip" if target.kind == "rt" else "multi-city",
                 verified=total, verified_urls=(("book", url),))


def pick_returns(cfg, judge, target: Page, out: Leg, homes, url, page=None):
    """On one outbound's return screen: (trip within your rules, trip rules
    aside) - each the cheapest you could take, landing by your deadline.

    The screen lists each flight home priced as the whole trip, so its price
    is the trip's. Rows are judged cheapest first and only as far as needed.
    """
    within = aside = None
    for text, fare, bag in _by_cost(cfg, homes):
        back = judge.leg(text, target.back_date, target.back_city, cfg.origin,
                         None, bag=bag)
        if back is None or back.arrive > cfg.arrive_home_by:
            continue
        if out.layover_ok and not back.layover_ok and page is not None:
            # worth knowing for sure only if the outbound is within the rules
            back = judge.leg(text, target.back_date, target.back_city, cfg.origin,
                             page, bag=bag)
        trip = one_ticket(cfg, target, out, back, fare, url)
        if trip is None:
            return None, None          # the stay is too short on any return
        if aside is None:
            aside = trip
        if not out.layover_ok:
            break                      # nothing here can be within the rules
        if back.layover_ok:
            within = trip
            break
    return within, aside


def one_way_legs(cfg, rows, target: Page, judge, page=None) -> list[Leg]:
    """Every flight on a one-way page, priced with its bag, ready to pair.

    The cheapest are opened up for their stop times until a few within the
    rules are known; beyond that a stop that needs timing is left untimed.
    """
    legs, good, opened = [], 0, 0
    for text, price, bag in _by_cost(cfg, rows):
        if _early(text, target.earliest):
            continue
        leg = judge.leg(text, target.out_date, target.frm, target.to, None,
                        price=price + bag, bag=bag)
        if leg is None:
            continue
        if target.to == cfg.origin and leg.arrive > cfg.arrive_home_by:
            continue
        if (not leg.layover_ok and page is not None and good < KNOWN_GOOD_ONE_WAY
                and opened < MAX_OPENED_ONE_WAY and connection_ok(cfg, text)
                and _needs_daylight(cfg, text)):
            before = judge.opened
            leg = judge.leg(text, target.out_date, target.frm, target.to, page,
                            price=price + bag, bag=bag)
            opened += judge.opened - before
        good += leg.layover_ok
        legs.append(leg)
    return legs


def two_ticket(cfg, outs, backs, urls: dict):
    """Pair one-way flights into trips - open jaws included - within the
    rules and rules aside, each priced as read."""
    def mark(combos):
        out = []
        for combo in combos:
            links = tuple((name, urls[key]) for name, key in (
                ("outbound", ("ow", combo.out.search_from, combo.out.search_to,
                              combo.out.date)),
                ("return", ("ow", combo.back.search_from, combo.back.search_to,
                            combo.back_date))) if key in urls)
            out.append(dataclasses.replace(combo, verified=combo.total,
                                           verified_urls=links))
        return out

    comfy = combine.build_combos(cfg, [l for l in outs if l.layover_ok],
                                 [l for l in backs if l.layover_ok])
    loose = combine.build_combos(cfg, outs, backs)
    return mark(comfy), mark(loose)


def covered(combo, read: set) -> bool:
    """Whether the page(s) that price this sweep trip were read.

    A trip the survey read supersedes the feed's guess at it; a trip whose
    page could not be read keeps the feed's price rather than vanishing.
    """
    if combo.back is None:
        return ("rt", combo.out.search_to, combo.out.date, combo.back_date) in read
    return (("ow", combo.out.search_from, combo.out.search_to, combo.out.date) in read
            and ("ow", combo.back.search_from, combo.back.search_to,
                 combo.back_date) in read)


# -- the browser ---------------------------------------------------------------

_MORE_JS = """() => {
  const more = Array.from(document.querySelectorAll('button,[role="button"]'))
    .find(b => /^\\s*(view|show) more flights\\s*$/i
                 .test((b.innerText || b.getAttribute('aria-label') || '')));
  if (!more) return false;
  more.click();
  return true;
}"""

# Opens one exact row - the same normalised text _ROWS_JS returned for it.
# Matching on departure time and airline was enough to find our own flight,
# but two rows can share both (a flight and its two-ticket twin), and the
# point here is to open the one that was chosen, not its neighbour.
_OPEN_EXACT_JS = """(want) => {
  const hit = Array.from(document.querySelectorAll('li')).find(
    li => (li.innerText || '').replace(/\\s+/g, ' ').trim() === want);
  if (!hit) return 'no match';
  const link = hit.querySelector('[role="link"]') || hit;
  link.click();
  return 'opened';
}"""

_HAS_ROW_JS = """(want) => Array.from(document.querySelectorAll('li')).some(
  li => (li.innerText || '').replace(/\\s+/g, ' ').trim() === want)"""

# The second screen has arrived when its rows are flights home: "TAO–SIN",
# where the first screen's were "SIN–TAO". Round trips title that screen
# "Returning flights" and multi-city searches "Top flights to Singapore", so
# the rows are the one thing both share.
_HOME_ROWS_JS = """(origin) => Array.from(document.querySelectorAll('li')).some(li => {
  const t = li.innerText || '';
  return /SGD\\s?\\d/.test(t)
      && (t.indexOf('\\u2013' + origin) >= 0 || t.indexOf('-' + origin) >= 0);
})"""


def _load(page, url, tag, timeout_ms=45_000):
    """Every row a search page lists, Cheapest view, fully loaded - or None."""
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    settled, snapshot = _settle_prices(page, _SETTLE_MS)
    if snapshot["min"] is None:
        body = (page.inner_text("body") or "").lower()[:4000]
        if any(mark in body for mark in _BLOCKED):
            raise Blocked(tag)
        log.warning("%-30s rendered no fares", tag)
        return None
    if page.evaluate(_CHEAPEST_JS) == "clicked":
        settled, snapshot = _settle_prices(page, _RESORT_MS)
    if page.evaluate(_MORE_JS):
        settled, snapshot = _settle_prices(page, _RESORT_MS)
    if not _trustworthy(tag, settled, snapshot):
        return None
    return page.evaluate(_ROWS_JS)


def _second(page, url, first_text, origin, tag, pick):
    """Open the return screen behind ``first_text`` and run ``pick`` on its
    rows while it is still showing - rows are opened up there, if need be.
    Returns what ``pick`` returned, or None if the screen could not be read."""
    for attempt in (1, 2):
        if page.evaluate(_OPEN_EXACT_JS, first_text) == "opened":
            break
        # Back from a previous return screen and not re-rendered as it was:
        # load the search again, once.
        if attempt == 2 or _load(page, url, tag) is None:
            log.info("%-30s could not open %s", tag, first_text[:40])
            return None
    try:
        page.wait_for_function(_HOME_ROWS_JS, arg=origin, timeout=_RETURNS_MS)
    except Exception:
        log.info("%-30s return screen did not appear", tag)
        return None
    settled, snapshot = _settle_prices(page, _RETURNS_MS)
    # The flights home are listed best first, not cheapest first, so one
    # hidden behind "View more flights" can be the cheapest of them.
    if snapshot["min"] is not None and page.evaluate(_MORE_JS):
        settled, snapshot = _settle_prices(page, _RETURNS_MS)
    if snapshot["min"] is None or not _trustworthy(f"{tag} return", settled, snapshot):
        return None
    rows = page.evaluate(_ROWS_JS)
    try:
        return pick(rows)
    finally:
        try:
            page.go_back(wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(1500)
        except Exception:
            pass


def _money(combo) -> str:
    return f"S${combo.price}" if combo is not None else "-"


def _one_ticket_page(cfg, page, target, rows, judge, found):
    """Open outbounds cheapest first until none left could beat what was found.

    An outbound's first-screen price is what it costs with its cheapest return,
    so no trip on it can cost less (bags only add). Once the next outbound's
    price is no lower than the trip found - within the rules, and rules aside -
    every trip on the page has been beaten or matched.
    """
    best = loose = None
    screens = tries = 0
    left = None
    for text, price, bag in _by_cost(cfg, rows):
        if _early(text, target.earliest):
            continue
        least = price + bag
        best_done = best is not None and least >= best.price
        loose_done = loose is not None and least >= loose.price
        if best_done and loose_done:
            break
        # Whether the outbound is within the rules matters only while the
        # rules list can still improve - only then is it worth opening up.
        rules_matter = not best_done
        if (rules_matter
                and judge.needs_page(text, target.out_date, cfg.origin, target.out_city)
                and not page.evaluate(_HAS_ROW_JS, text)):
            _load(page, target.url, target.label)   # the list as first read
        out = judge.leg(text, target.out_date, cfg.origin, target.out_city,
                        page if rules_matter else None, bag=bag)
        if out is None or not _long_enough(cfg, target, out):
            continue
        if loose_done and not out.layover_ok:
            continue       # it could only help the rules list, and it fails the rules
        if tries >= MAX_SCREENS:
            left = least
            break
        tries += 1
        try:
            picked = _second(page, target.url, text, cfg.origin, target.label,
                             lambda homes: pick_returns(cfg, judge, target, out, homes,
                                                        target.url, page))
        except Blocked:
            raise
        except Exception as exc:
            log.info("%-30s return screen failed (%s)", target.label, exc)
            picked = None
        if picked is None:
            continue
        screens += 1
        found.screens += 1
        within, aside = picked
        log.info("%-36s   out %-8s S$%-5s%s -> within rules %s | rules aside %s",
                 target.label, departs_at(text), least,
                 "" if out.layover_ok else " (outside rules)",
                 _money(within), _money(aside))
        if within is not None and (best is None or within.price < best.price):
            best = within
        if aside is not None and (loose is None or aside.price < loose.price):
            loose = aside
    if best is not None:
        found.combos.append(best)
    if loose is not None:
        found.any_combos.append(loose)
    if screens:
        found.read.add(target.key)
    listed = [p for _, p in _sorted(rows)]
    log.info("%-36s page from S$%s | within rules %s | rules aside %s%s",
             target.label, listed[0] if listed else "-", _money(best), _money(loose),
             f" | stopped at {MAX_SCREENS} screens, outbounds from S${left} unopened"
             if left is not None else "")


def run(cfg, result, cities) -> SurveyResult:
    """Open every page for these cities and turn what they show into trips."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    budget = BUDGET_MIN if getattr(cfg, "at_home", False) else BUDGET_MIN_GITHUB
    found = SurveyResult()
    judge = Judge(cfg, list(getattr(result, "legs_out", []))
                  + list(getattr(result, "legs_in", [])))
    pages = plan(cfg, cities)
    log.info("survey: %d pages for %s", len(pages),
             ", ".join(cfg.city_name(c) for c in cities))
    outs, backs, urls = [], [], {}

    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            context = browser.new_context(locale="en-SG",
                                          viewport={"width": 1400, "height": 1000})
            page = context.new_page()
            _plain_name(context, page)
            for index, target in enumerate(pages):
                if time.monotonic() - started > budget * 60:
                    found.unread = [p.label for p in pages[index:]]
                    log.warning("survey: out of time after %d min - %d page(s) not "
                                "read", budget, len(found.unread))
                    break
                try:
                    rows = _load(page, target.url, target.label)
                    if not rows:
                        found.failed.append(target.label)
                        continue
                    found.pages += 1
                    if target.kind != "ow":
                        _one_ticket_page(cfg, page, target, rows, judge, found)
                        continue
                    legs = one_way_legs(cfg, rows, target, judge, page)
                    (backs if target.to == cfg.origin else outs).extend(legs)
                    urls[target.key] = target.url
                    found.read.add(target.key)
                    good = [l.price for l in legs if l.layover_ok]
                    log.info("%-36s %d flights, cheapest S$%s, within rules S$%s",
                             target.label, len(legs),
                             min((l.price for l in legs), default="-"),
                             min(good, default="-"))
                except Blocked:
                    log.error("%-30s Google showed a bot check - stopping the "
                              "survey here", target.label)
                    found.blocked = True
                    break
                except Exception as exc:  # one bad page must not lose the rest
                    log.warning("%-30s could not be read (%s)", target.label, exc)
                    found.failed.append(target.label)
        finally:
            browser.close()

    if outs and backs:
        comfy, loose = two_ticket(cfg, outs, backs, urls)
        found.combos.extend(comfy)
        found.any_combos.extend(loose)
    found.seconds = time.monotonic() - started
    found.opened_up, found.untimed = judge.opened, judge.untimed
    log.info("survey: %d pages and %d return screens in %.0f min, %d rows opened "
             "up for their stop times (%d unreadable)%s", found.pages, found.screens,
             found.seconds / 60, found.opened_up, found.untimed,
             " - stopped at a bot check" if found.blocked else "")
    return found
