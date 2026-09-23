"""Reads the prices Google actually renders, using a real browser.

The search payload we parse carries the airline's own fare and only the first
handful of itineraries. The cheaper agency prices - and the rest of the list -
arrive in a request the page makes *after* loading, which a plain fetch never
sees. Verified by experiment: the same query fetched with and without Google's
cheapest-sort parameter returns an identical payload quoting the airline fare,
while the rendered page shows a lower number and twice as many flights.

So for the few trips a sweep is actually going to recommend, open the real
page, switch to Google's own "Cheapest" view, and read what a person reads.
Ten page loads per sweep rather than two hundred - enough to make the numbers
you act on correct, small enough not to look like scraping.

Reading it means waiting for it. The first fares drawn are the airline's own,
and the cheaper agency ones replace them seconds later while the page says
"Checking online travel agencies and other providers..." - so a read taken the
moment fares appear is the dear number, which is the very mistake opening a
browser was meant to avoid. The page is read once its prices stop falling.

Everything here is optional by design. No browser, a consent wall, a bot check:
verification is skipped and the sweep reports its parsed numbers with that
fact attached. It never silently substitutes or invents a price.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

log = logging.getLogger("flightwatch.verify")

_PRICE = re.compile(r"SGD\s?([\d,]+)")
# The row text carries emissions chatter that means nothing to a fare decision.
_NOISE = re.compile(
    r"\s*\d+ kg CO2e|\s*[-+]\d+% emissions|\s*Avg emissions"
    r"|\s*Avoids as much CO2e.*$|\s*round trip$"
)
_BLOCKED = ("unusual traffic", "/sorry/", "captcha", "before you continue",
            "consent.google")

# One definition of what counts as a fare row, shared by the code that reads
# the page and the code that decides the page has stopped changing. Were those
# two to disagree, "settled" would be a statement about rows nobody reads.
_ROW_TEXTS_JS = """
  const rowTexts = () => Array.from(document.querySelectorAll('li'))
    .map(li => (li.innerText || '').replace(/\\s+/g, ' ').trim())
    .filter(t => /SGD\\s?\\d/.test(t)
              && /\\d{1,2}:\\d{2}\\s?(AM|PM)/.test(t)
              && t.length < 400);
  const priceIn = (t) => {
    const m = t.match(/SGD\\s?([\\d,]+)/);
    return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
  };
"""

# Reading the rendered list, after Google's own Cheapest tab is selected.
_ROWS_JS = "() => {" + _ROW_TEXTS_JS + """
  return Array.from(new Set(rowTexts()));
}"""

# One look at the page: how many fares it is showing, the cheapest of them,
# whether it says it is still working, and what it claims its cheapest is.
_PROBE_JS = "() => {" + _ROW_TEXTS_JS + """
  const rows = Array.from(new Set(rowTexts()));
  const prices = rows.map(priceIn).filter(n => n !== null);

  // Google leaves its progress bars in the page when they finish: still
  // 1112 by 4 pixels, still passing a bare checkVisibility(), but marked
  // aria-hidden and faded to opacity 0. Testing only for a box meant every
  // page looked permanently busy, so no read ever settled and each one ran
  // its whole budget out - a five-second read became a minute.
  const shown = (el) => {
    if (el.closest('[aria-hidden="true"]')) return false;
    if (el.checkVisibility && !el.checkVisibility(
          {opacityProperty: true, visibilityProperty: true,
           contentVisibilityAuto: true})) return false;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
      return false;
    }
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const busy = Array.from(document.querySelectorAll('[role="progressbar"]'))
    .some(shown);

  // Google prints the cheapest figure it already knows above a list that has
  // not caught up: the tab reads "Cheapest from SGD 605" while the rows still
  // start at 718. That number is the page telling us our read is unfinished.
  // It sits inside the tab on a phone and beside it on a desktop, so look at
  // the tab and then at the strip around it, keeping the match tight enough
  // to not pick up the Best tab's price instead.
  let quoted = null;
  const tab = Array.from(document.querySelectorAll('[role="tab"]'))
    .find(t => /cheapest/i.test(t.textContent || ''));
  if (tab) {
    const nearby = [tab.textContent || '',
                    (tab.parentElement && tab.parentElement.textContent) || ''];
    for (const text of nearby) {
      const m = text.match(/cheapest[\\s\\S]{0,40}?SGD\\s?([\\d,]+)/i);
      if (m) { quoted = parseInt(m[1].replace(/,/g, ''), 10); break; }
    }
  }

  return {rows: rows.length,
          min: prices.length ? Math.min.apply(null, prices) : null,
          busy: busy,
          quoted: quoted};
}"""

# How long a page gets to stop changing. The sweep is the slow part of the
# first load; switching tabs re-renders the list and can fetch again.
_SETTLE_MS = 30_000
_RESORT_MS = 20_000
_RETURNS_MS = 25_000
# Nothing changing for this long is what "the sweep has finished" looks like
# from outside. It is also the shortest a read can take, which is the point:
# it is the window in which a falling price would have shown itself.
_QUIET_MS = 2_500
# The same window for a page that never said it was working and never quoted
# its own cheapest fare - if Google restyles either of those, stability is the
# only thing left, and a short quiet window would mistake the pause between
# the airline fares and the agency ones for the end of the list. Being slow
# when the page stops explaining itself is the right way to be wrong.
_BLIND_QUIET_MS = 8_000

# Google prices a round trip as one ticket and its result list only details
# the outbound - the return is chosen on the next screen. So open the
# cheapest outbound and read what comes back, which is also the only way to
# learn the real total: the advertised price belongs to one specific return,
# and the first return offered is often dearer than it.
#
# It opens OUR flight, not the cheapest one on the page. Opening the cheapest
# is what this used to do, and it is how a 29-hour routing with a 21-hour
# layover at Beijing ended up supplying the price printed beside a civilised
# China Southern morning flight - two different trips, one number, and the
# layover rules bypassed entirely at the last step.
_OPEN_ROW_JS = """(want) => {
  const rows = Array.from(document.querySelectorAll('li')).filter(
    li => /SGD\\s?\\d/.test(li.innerText || '')
       && /\\d{1,2}:\\d{2}\\s?(AM|PM)/.test(li.innerText || '')
       && (li.innerText || '').length < 400);
  if (!rows.length) return 'no rows';
  const tidy = (s) => (s || '').replace(/\\u202f/g, ' ').toUpperCase();
  const departs = (li) => {
    const m = tidy(li.innerText).match(/(\\d{1,2}:\\d{2}\\s?[AP]M)/);
    return m ? m[1] : null;
  };
  const hit = rows.find((li) => {
    if (departs(li) !== tidy(want.depart)) return false;
    if (!want.airlines || !want.airlines.length) return true;
    const text = tidy(li.innerText);
    return want.airlines.some((a) => text.includes(tidy(a)));
  });
  if (!hit) return 'no match';
  const link = hit.querySelector('[role="link"]');
  if (!link) return 'no link';
  link.click();
  return 'opened';
}"""

_RETURNS_READY_JS = """() => Array.from(document.querySelectorAll('h1,h2,h3'))
  .some(h => /returning flights/i.test(h.innerText || ''))"""

_CHEAPEST_JS = """async () => {
  const tab = Array.from(document.querySelectorAll('[role="tab"]'))
    .find(b => /cheapest/i.test(b.textContent || ''));
  if (!tab) return 'no tab';
  if (tab.getAttribute('aria-selected') === 'true') return 'already';
  tab.click();
  return 'clicked';
}"""


# A row's own shape, read back out of the text Google renders.
_DEPART = re.compile(r"(\d{1,2}:\d{2}\s?[AP]M)")
_STOPS = re.compile(r"\b(Nonstop|(\d+) stops?)\b", re.I)
# What sits after "1 stop": the connection's length and where it is spent.
_LAYOVER = re.compile(r"(?:(\d+)\s*hr\s*)?(?:(\d+)\s*min\s*)?([A-Z]{3})\b")


class NotVerifiable(RuntimeError):
    """The page could not be read - no browser, a wall, or no results."""


@dataclass(frozen=True)
class RenderedFare:
    """One row exactly as Google drew it, with the noise stripped."""

    price: int
    summary: str

    @classmethod
    def parse(cls, text: str) -> "RenderedFare | None":
        found = _PRICE.search(text)
        if not found:
            return None
        try:
            price = int(found.group(1).replace(",", ""))
        except ValueError:
            return None
        summary = _PRICE.sub("", _NOISE.sub("", text)).strip(" ·-")
        return cls(price=price, summary=re.sub(r"\s{2,}", " ", summary).strip())


def available() -> bool:
    """True when a browser we can drive is actually installed."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return True


def _fares_from(rows: list[str], limit: int) -> list[RenderedFare]:
    fares = [f for f in (RenderedFare.parse(r) for r in rows) if f is not None]
    fares.sort(key=lambda f: f.price)
    return fares[:limit]


def departs_at(text: str) -> str | None:
    """The row's departure time, as Google writes it: '5:45 PM'."""
    found = _DEPART.search(text or "")
    return found.group(1).replace(" ", " ") if found else None


def stops_in(text: str) -> int | None:
    found = _STOPS.search(text or "")
    if not found:
        return None
    return 0 if found.group(2) is None else int(found.group(2))


def connections(text: str) -> list[int] | None:
    """How long each connection is, in minutes, or None if it cannot be read.

    Google writes them as '1 stop 21 hr 5 min PEK'. Nonstop has none. An
    unreadable row returns None rather than [], because "no connections" and
    "could not tell" must not lead to the same decision.
    """
    count = stops_in(text)
    if count is None:
        return None
    if count == 0:
        return []
    after = _STOPS.split(text, maxsplit=1)[-1]
    out: list[int] = []
    for hours, minutes, _airport in _LAYOVER.findall(after):
        if not hours and not minutes:
            continue
        out.append(int(hours or 0) * 60 + int(minutes or 0))
        if len(out) == count:
            break
    return out if len(out) == count else None


def connection_ok(cfg, text: str) -> bool:
    """Whether a rendered row's connections are ones you would accept.

    This is the length half of the layover rules in config.yaml, applied to
    what the page says. The daylight half cannot be checked here: the row
    gives how long a connection is but never when it happens, so a rule about
    usable daytime has nothing to work with. Anything this passes still has
    to be looked at - it is "not obviously unacceptable", not "approved".
    """
    found = connections(text)
    if found is None:
        return False
    if cfg.max_stops is not None and len(found) > cfg.max_stops:
        return False
    rules = cfg.layover
    for minutes in found:
        if minutes < rules.min_minutes:
            return False
        if minutes <= rules.short_max_minutes:
            continue
        if not rules.explore_min_minutes <= minutes <= rules.explore_max_minutes:
            return False
    return True


def matches(text: str, want: dict) -> bool:
    """Whether a rendered row is the flight we set out to price.

    Departure time and airline, because those are what both sides agree on:
    the row never names a flight number, and the search feed never gives the
    row's wording. A trip that cannot be found this way stays unverified,
    which is the honest answer - the alternative is pricing one flight and
    labelling it another, which is the mistake this check exists to stop.
    """
    if not want:
        return False
    when = departs_at(text)
    if not when or when.upper() != str(want.get("depart", "")).upper():
        return False
    wanted = [a.casefold() for a in want.get("airlines") or () if a]
    if not wanted:
        return True
    low = (text or "").casefold()
    return any(name in low for name in wanted)


def _search(cfg, legs, trip):
    from .search import build_query
    import urllib.parse
    query = build_query(cfg, legs, trip=trip)
    return ("https://www.google.com/travel/flights?"
            + urllib.parse.urlencode(query.params()))


def _target(cfg, combo) -> dict:
    """The page(s) that price this exact trip.

    This used to open a round-trip search for every trip, whatever the trip
    was. For a round-trip quote that is right. For a one-way pair - two
    separate tickets, and 57% of the trips found here are built that way -
    a round-trip search prices a different product, so what came back was
    not a correction of that trip's price. It was a different price for a
    different thing, reported beside it. Now each trip is searched as what
    it actually is.
    """
    origin, city = cfg.origin, combo.out.search_to
    label = f"{cfg.city_name(city)} {combo.out.date[5:]}/{combo.back_date[5:]}"

    if combo.back is None:
        parts = [("", _search(cfg, [(origin, city, combo.out.date, None),
                                    (city, origin, combo.back_date, None)],
                              "round-trip"), _leg_match(combo.out))]
    else:
        parts = [
            ("out", _search(cfg, [(combo.out.from_airport, combo.out.to_airport,
                                   combo.out.date, None)], "one-way"),
             _leg_match(combo.out)),
            ("back", _search(cfg, [(combo.back.from_airport, combo.back.to_airport,
                                    combo.back_date, None)], "one-way"),
             _leg_match(combo.back)),
        ]
    return {"key": combo.signature(), "label": label, "parts": parts}


def _bag_fee(cfg, summaries) -> int:
    """The checked-bag estimate for these flights, one leg at a time.

    The whole row is handed to the fee table rather than a parsed airline
    name: the table matches on a case-insensitive substring, and the row text
    names every carrier on the itinerary - "ScootSingapore Airlines" and all -
    so the dearest applicable fee is found without having to split the row up.
    """
    return sum(cfg.checked_bag_fee([text or ""]) for text in summaries)


def _clock(when) -> str:
    """A datetime written the way Google writes a departure: '5:45 PM'."""
    return when.strftime("%I:%M %p").lstrip("0")


def _leg_match(leg) -> dict:
    """Enough of a leg to recognise its row on the page."""
    return {"depart": _clock(leg.depart), "airlines": tuple(leg.airlines or ())}


def targets(cfg, combos, limit: int) -> list[dict]:
    """The trips worth opening: breadth across cities before depth on dates.

    Recommendations cluster - one cheap city easily supplies the top six, all
    of them the same flights on different return days. Checking those six
    tells you almost nothing you did not know, so take each city's cheapest
    trip first and only come back for second dates once every city has had a
    turn. ``limit`` counts trips, not page loads; a two-ticket trip opens two.
    """
    ordered = sorted(combos, key=lambda c: c.total)
    by_city: dict[str, list] = {}
    seen: set = set()
    for combo in ordered:
        key = combo.signature()
        if key in seen:
            continue
        seen.add(key)
        by_city.setdefault(combo.out.search_to, []).append(combo)

    cities = sorted(by_city, key=lambda c: by_city[c][0].total)
    picked: list = []
    depth = 0
    while len(picked) < limit:
        added = False
        for city in cities:
            if len(by_city[city]) > depth:
                picked.append(by_city[city][depth])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        depth += 1

    picked.sort(key=lambda c: c.total)
    return [_target(cfg, c) for c in picked]


def verify(cfg, targets_: list[dict], rows_each: int = 4,
           timeout_ms: int = 45_000) -> dict:
    """Open each trip's page(s) and return what they actually priced.

    Keyed by signature, so a result can be matched back to the trip it
    corrects. A trip is only returned when EVERY part of it could be read:
    half of a two-ticket trip is not a price, and returning one leg as
    though it were the total would be exactly the kind of confidently wrong
    number this whole mechanism exists to catch.

    Two numbers come back per trip, and they are deliberately not merged.
    ``total`` is this trip's own price - the row on the page that IS this
    flight, so the number and the itinerary beside it belong together.
    ``other`` is the cheapest thing on the same page whose connections pass
    the layover rules, which may be a better trip than the sweep found and
    is reported as its own line rather than being written over anything.
    """
    from playwright.sync_api import sync_playwright

    out: dict = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(
                locale="en-SG", viewport={"width": 1400, "height": 1000})
            page = context.new_page()
            for target in targets_:
                one_ticket = len(target["parts"]) == 1
                parts, returns, others, ok = [], [], [], True
                for part_label, url, want in target["parts"]:
                    fares, backs, mine, other = _read(
                        cfg, page, target["label"], part_label, url,
                        rows_each, timeout_ms, follow_return=one_ticket,
                        want=want)
                    if not fares or mine is None:
                        ok = False
                        break
                    parts.append({"label": part_label, "url": url,
                                  "fares": fares, "mine": mine})
                    returns = backs
                    if other is not None:
                        others.append(other)
                if not ok:
                    continue
                # One ticket: the total is our outbound paired with its
                # cheapest return, not the headline "from" price and not the
                # first return offered. Two tickets: each leg is bought
                # separately, so the total is the sum of our two rows.
                if one_ticket and returns:
                    total = returns[0].price
                    legs = [parts[0]["mine"].summary, returns[0].summary]
                elif one_ticket:
                    total = parts[0]["mine"].price
                    # One row, two flights: the carrier we can see is the
                    # best guess for the one we cannot.
                    legs = [parts[0]["mine"].summary] * 2
                else:
                    total = sum(part["mine"].price for part in parts)
                    legs = [part["mine"].summary for part in parts]
                # Google will not price a checked bag, so the sweep adds an
                # estimate and this did not - which made a Scoot fare look
                # S$100 cheaper the moment a browser confirmed it, and put a
                # saving on the chart that was only ever the missing bag.
                # Both numbers now mean the same thing.
                bags = _bag_fee(cfg, legs)
                total += bags
                found = {
                    "label": target["label"], "total": total, "parts": parts,
                    "returns": returns, "bags": bags,
                }
                if len(others) == len(parts):
                    # For one ticket this is an advertised "from" price: it
                    # still has a return to be chosen, so it is a floor, not
                    # a total. Said plainly wherever it is shown.
                    other_legs = ([others[0].summary] * 2 if one_ticket
                                  else [o.summary for o in others])
                    found["other"] = {
                        "price": sum(o.price for o in others)
                                 + _bag_fee(cfg, other_legs),
                        "summary": " + ".join(o.summary for o in others),
                        "advertised": one_ticket,
                    }
                out[target["key"]] = found
                log.info("%-28s verified S$%d%s", target["label"], total,
                         f" (return: {returns[0].summary[:40]})" if returns else "")
        finally:
            browser.close()
    return out


def _settle(page, ready_js, timeout_ms):
    """Wait until the page has what we came for, or give up quietly."""
    try:
        page.wait_for_function(ready_js, timeout=timeout_ms)
        return True
    except Exception:
        return False


def _settle_prices(page, budget_ms, quiet_ms=_QUIET_MS, poll_ms=400):
    """Wait until the cheapest price on the page stops falling.

    The old condition here was "a fare row exists", which the page satisfies
    with the airline's own fares within a second or two - and then spends the
    next five to ten seconds replacing them with cheaper agency ones. Reading
    at the first condition recorded the dearer list every time.

    So watch the list instead of the clock: poll what is on the page, and
    accept it only once the row count and the cheapest price have held still
    for a quiet stretch, nothing says it is still working, and the cheapest
    row is no dearer than the price the page itself advertises.

    Returns (settled, snapshot) - the snapshot is the last look either way, so
    a caller can say what it saw rather than only that it gave up.
    """
    deadline = time.monotonic() + budget_ms / 1000
    snapshot = {"rows": 0, "min": None, "busy": False, "quoted": None}
    seen = None
    changed_at = time.monotonic()
    told = False           # the page has said something about its own progress
    while True:
        try:
            snapshot = page.evaluate(_PROBE_JS)
        except Exception:
            return False, snapshot
        now = time.monotonic()
        told = told or snapshot["busy"] or snapshot["quoted"] is not None
        quiet = (quiet_ms if told else _BLIND_QUIET_MS) / 1000
        mark = (snapshot["rows"], snapshot["min"])
        if mark != seen:
            seen, changed_at = mark, now
        elif (snapshot["min"] is not None
                and not snapshot["busy"]
                and now - changed_at >= quiet
                and (snapshot["quoted"] is None
                     or snapshot["min"] <= snapshot["quoted"])):
            return True, snapshot
        if now >= deadline:
            return False, snapshot
        page.wait_for_timeout(poll_ms)


def _trustworthy(tag, settled, snapshot) -> bool:
    """Whether a list that ran out of time is still worth recording.

    Out of time with a price at or below what the page advertises means the
    read is consistent with the page's own headline - slow, not wrong. Out of
    time with every row dearer than that headline means the cheaper fares
    never arrived, and recording the dear one is the mistake this exists to
    prevent, so nothing is recorded and the trip keeps its unverified price.
    """
    if settled:
        return True
    quoted, cheapest = snapshot.get("quoted"), snapshot.get("min")
    if quoted is not None and cheapest is not None and cheapest > quoted:
        log.warning("%-28s page advertises S$%d, list stopped at S$%d "
                    "- not recording", tag, quoted, cheapest)
        return False
    log.info("%-28s list never went quiet; reading it at S$%s", tag, cheapest)
    return True


def _pick(cfg, rows: list[str], want: dict):
    """Our own row, and the cheapest one worth considering beside it.

    Both come from the same rendered list. The first is the flight we set out
    to price; the second is the cheapest whose connections pass the layover
    rules, which is a different trip and stays a different number.
    """
    fares = [(text, RenderedFare.parse(text)) for text in rows]
    fares = [(text, fare) for text, fare in fares if fare is not None]
    fares.sort(key=lambda pair: pair[1].price)

    mine = next((f for text, f in fares if matches(text, want)), None)
    other = next((f for text, f in fares if connection_ok(cfg, text)), None)
    return mine, other


def _read(cfg, page, label, part_label, url, rows_each, timeout_ms,
          follow_return=False, want=None):
    """What one search page prices.

    Returns (fares, returns, mine, other): everything listed, the return
    options behind OUR outbound for a round trip, our own row, and the
    cheapest row on the page with acceptable connections.
    """
    tag = f"{label} {part_label}".strip() or label
    want = want or {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        settled, snapshot = _settle_prices(page, _SETTLE_MS)
        if snapshot["min"] is None:
            body = (page.inner_text("body") or "").lower()[:4000]
            why = ("not a results page" if any(m in body for m in _BLOCKED)
                   else "rendered no fares")
            log.warning("%-28s %s - skipped", tag, why)
            return [], [], None, None
        # Switching tabs re-renders the list, so whatever settled a moment ago
        # has to settle again; an already-selected tab changes nothing.
        if page.evaluate(_CHEAPEST_JS) == "clicked":
            settled, snapshot = _settle_prices(page, _RESORT_MS)
        if not _trustworthy(tag, settled, snapshot):
            return [], [], None, None

        rows = page.evaluate(_ROWS_JS)
        fares = _fares_from(rows, rows_each)
        if not fares:
            log.warning("%-28s rendered no fares", tag)
            return [], [], None, None
        mine, other = _pick(cfg, rows, want)
        if mine is None:
            # Better to say nothing than to price a different flight and put
            # this trip's name on it.
            log.info("%-28s our flight (%s %s) is not on the page - "
                     "left unverified", tag, want.get("depart", "?"),
                     ", ".join(want.get("airlines") or ()) or "?")
            return [], [], None, other
        if not follow_return:
            return fares, [], mine, other

        opened = page.evaluate(_OPEN_ROW_JS, want)
        if opened != "opened":
            log.info("%-28s could not open our flight (%s)", tag, opened)
            return fares, [], mine, other
        if not _settle(page, _RETURNS_READY_JS, _RETURNS_MS):
            log.info("%-28s return list did not appear", tag)
            return fares, [], mine, other
        # The second screen sweeps the agencies exactly as the first one does,
        # so the first returns drawn are the dear ones just the same.
        settled, snapshot = _settle_prices(page, _RETURNS_MS)
        if snapshot["min"] is None or not _trustworthy(f"{tag} return",
                                                       settled, snapshot):
            log.info("%-28s return list was unreadable", tag)
            return fares, [], mine, other
        returns = _fares_from(page.evaluate(_ROWS_JS), rows_each)
        if not returns:
            log.info("%-28s return list was unreadable", tag)
        return fares, returns, mine, other
    except Exception as exc:  # one bad page must not lose the rest
        log.warning("%-28s could not be read (%s)", tag, exc)
        return [], [], None, None
