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
cheapest return - which may be a return you would never take. So the
cheapest outbounds that pass your rules are each opened, and the return
screen is read for the cheapest return that passes them too: its price is
the price of a trip you would actually book.

It takes a while - forty-odd pages and their return screens - and that is
the point. It stops at the first sign of a bot check rather than pressing on.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import combine
from .models import Combo, Leg
from .verify import (_BLOCKED, _CHEAPEST_JS, _RESORT_MS, _RETURNS_MS, _ROWS_JS,
                     _SETTLE_MS, RenderedFare, _bag_fee, _launch, _plain_name,
                     _search, _settle_prices, _trustworthy, connection_ok,
                     departs_at, leg_from_row)

log = logging.getLogger("flightwatch.survey")

# How many of the cheapest acceptable outbounds on a one-ticket page get
# their return screen opened. The first sets the price nearly always; the
# second is there for when its cheapest return is one you would not take.
OPEN_PER_PAGE = 2


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
    blocked: bool = False
    seconds: float = 0.0

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


def _matching(sweep_legs, leg: Leg):
    """The feed's own copy of a flight read off a page, if the sweep had it.

    Worth finding because the feed says when a connection happens, which a
    page row never does - and without that, the half of the layover rules
    about daylight hours cannot be applied. A flight the feed never saw is
    judged on connection lengths alone, as it always was.
    """
    for other in sweep_legs:
        if (other.depart == leg.depart and other.arrive == leg.arrive
                and other.stops == leg.stops):
            return other
    return None


def _leg(cfg, text, on_date, frm, to, sweep_legs, price=0, bag=0):
    leg = leg_from_row(cfg, text, on_date, frm, to, price, bag_fee=bag)
    if leg is None:
        return None
    ok = connection_ok(cfg, text)
    twin = _matching(sweep_legs, leg)
    if twin is not None:
        ok = ok and twin.layover_ok
    return dataclasses.replace(leg, layover_ok=ok)


def first_choices(cfg, rows, earliest, sweep_legs, page: Page, count=OPEN_PER_PAGE):
    """The outbounds worth opening: the cheapest few within the rules, and
    the cheapest of all, rules aside."""
    ok, floor = [], None
    for text, price in _sorted(rows):
        if _early(text, earliest):
            continue
        leg = _leg(cfg, text, page.out_date, cfg.origin, page.out_city, sweep_legs)
        if leg is None:
            continue
        if floor is None:
            floor = text
        if leg.layover_ok and len(ok) < count:
            ok.append(text)
    return ok, floor


def best_return(cfg, rows, page: Page, sweep_legs, rules: bool):
    """The cheapest flight home on a return screen that you could take.

    Always: lands before the deadline. With ``rules``: its connections pass
    too. Returns (row text, leg) or (None, None).
    """
    for text, price in _sorted(rows):
        leg = _leg(cfg, text, page.back_date, page.back_city, cfg.origin, sweep_legs)
        if leg is None or leg.arrive > cfg.arrive_home_by:
            continue
        if rules and not leg.layover_ok:
            continue
        return text, leg
    return None, None


def one_ticket(cfg, page: Page, out_text, back_text, sweep_legs, url):
    """A one-ticket trip from the two rows that make it up, or None."""
    total = _price(back_text)
    out = _leg(cfg, out_text, page.out_date, cfg.origin, page.out_city, sweep_legs,
               bag=_bag_fee(cfg, [out_text]))
    back = _leg(cfg, back_text, page.back_date, page.back_city, cfg.origin,
                sweep_legs, bag=_bag_fee(cfg, [back_text]))
    if total is None or out is None or back is None:
        return None
    if (datetime.strptime(page.back_date, "%Y-%m-%d").date()
            - out.arrive.date()).days < cfg.min_nights:
        return None
    total += out.bag_fee + back.bag_fee
    return Combo(out=out, back=back, back_date=page.back_date,
                 country=cfg.country_of(page.out_city), total=total,
                 source="round-trip" if page.kind == "rt" else "multi-city",
                 verified=total, verified_urls=(("book", url),))


def one_way_legs(cfg, rows, page: Page, sweep_legs) -> list[Leg]:
    """Every flight on a one-way page, priced with its bag, ready to pair."""
    legs = []
    for text, price in _sorted(rows):
        if _early(text, page.earliest):
            continue
        bag = _bag_fee(cfg, [text])
        leg = _leg(cfg, text, page.out_date, page.frm, page.to, sweep_legs,
                   price=price + bag, bag=bag)
        if leg is None:
            continue
        if page.to == cfg.origin and leg.arrive > cfg.arrive_home_by:
            continue
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


def _second(page, url, first_text, origin, tag):
    """The flights home offered after choosing ``first_text`` - or None."""
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
        page.go_back(wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(1500)
    except Exception:
        pass
    return rows


def _one_ticket_page(cfg, page, target, rows, sweep_legs, found):
    """Open the outbounds worth opening and keep the best trip each way."""
    choices, floor = first_choices(cfg, rows, target.earliest, sweep_legs, target)
    opened: dict = {}
    for text in choices + ([floor] if floor and floor not in choices else []):
        try:
            homes = _second(page, target.url, text, cfg.origin, target.label)
        except Blocked:
            raise
        except Exception as exc:
            log.info("%-30s return screen failed (%s)", target.label, exc)
            homes = None
        if homes:
            found.screens += 1
            opened[text] = homes

    best = loose = None
    for text, homes in opened.items():
        if text in choices:
            row, _ = best_return(cfg, homes, target, sweep_legs, rules=True)
            combo = one_ticket(cfg, target, text, row, sweep_legs, target.url) \
                if row else None
            if combo and (best is None or combo.price < best.price):
                best = combo
        row, _ = best_return(cfg, homes, target, sweep_legs, rules=False)
        combo = one_ticket(cfg, target, text, row, sweep_legs, target.url) \
            if row else None
        if combo and (loose is None or combo.price < loose.price):
            loose = combo
    if best is not None:
        found.combos.append(best)
    if loose is not None:
        found.any_combos.append(loose)
    if opened:
        found.read.add(target.key)
    listed = [p for _, p in _sorted(rows)]
    log.info("%-30s page from S$%s | within rules S$%s | rules aside S$%s",
             target.label, listed[0] if listed else "-",
             best.price if best else "-", loose.price if loose else "-")


def run(cfg, result, cities) -> SurveyResult:
    """Open every page for these cities and turn what they show into trips."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    found = SurveyResult()
    sweep_legs = list(getattr(result, "legs_out", [])) + list(getattr(result, "legs_in", []))
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
            for target in pages:
                try:
                    rows = _load(page, target.url, target.label)
                    if not rows:
                        found.failed.append(target.label)
                        continue
                    found.pages += 1
                    if target.kind != "ow":
                        _one_ticket_page(cfg, page, target, rows, sweep_legs, found)
                        continue
                    legs = one_way_legs(cfg, rows, target, sweep_legs)
                    (backs if target.to == cfg.origin else outs).extend(legs)
                    urls[target.key] = target.url
                    found.read.add(target.key)
                    log.info("%-30s %d flights, cheapest S$%s", target.label,
                             len(legs), min((l.price for l in legs), default="-"))
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
    log.info("survey: %d pages and %d return screens in %.0f min%s", found.pages,
             found.screens, found.seconds / 60,
             " - stopped at a bot check" if found.blocked else "")
    return found
