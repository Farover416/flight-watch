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

Everything here is optional by design. No browser, a consent wall, a bot check:
verification is skipped and the sweep reports its parsed numbers with that
fact attached. It never silently substitutes or invents a price.
"""

from __future__ import annotations

import logging
import re
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

# Reading the rendered list, after Google's own Cheapest tab is selected.
_ROWS_JS = """() => {
  const rows = Array.from(document.querySelectorAll('li'))
    .map(li => (li.innerText || '').replace(/\\s+/g, ' ').trim())
    .filter(t => /SGD\\s?\\d/.test(t)
              && /\\d{1,2}:\\d{2}\\s?(AM|PM)/.test(t)
              && t.length < 400);
  return Array.from(new Set(rows));
}"""

# Waiting until the fares exist beats sleeping for a guessed 8.5 seconds:
# a fast response is read in two, a slow one is still read correctly.
_HAS_ROWS_JS = """() => Array.from(document.querySelectorAll('li'))
  .some(li => /SGD\\s?\\d/.test(li.innerText || '')
           && /\\d{1,2}:\\d{2}\\s?(AM|PM)/.test(li.innerText || ''))"""

# Google prices a round trip as one ticket and its result list only details
# the outbound - the return is chosen on the next screen. So open the
# cheapest outbound and read what comes back, which is also the only way to
# learn the real total: the advertised price belongs to one specific return,
# and the first return offered is often dearer than it.
_OPEN_CHEAPEST_JS = """() => {
  const rows = Array.from(document.querySelectorAll('li')).filter(
    li => /SGD\\s?\\d/.test(li.innerText || '')
       && /\\d{1,2}:\\d{2}\\s?(AM|PM)/.test(li.innerText || '')
       && (li.innerText || '').length < 400);
  if (!rows.length) return 'no rows';
  const price = (li) => {
    const m = (li.innerText || '').match(/SGD\\s?([\\d,]+)/);
    return m ? parseInt(m[1].replace(/,/g, ''), 10) : Infinity;
  };
  const best = rows.reduce((a, b) => (price(b) < price(a) ? b : a));
  const link = best.querySelector('[role="link"]');
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
                              "round-trip"))]
    else:
        parts = [
            ("out", _search(cfg, [(combo.out.from_airport, combo.out.to_airport,
                                   combo.out.date, None)], "one-way")),
            ("back", _search(cfg, [(combo.back.from_airport, combo.back.to_airport,
                                    combo.back_date, None)], "one-way")),
        ]
    return {"key": combo.signature(), "label": label, "parts": parts}


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


def verify(targets_: list[dict], rows_each: int = 4,
           timeout_ms: int = 45_000) -> dict:
    """Open each trip's page(s) and return what they actually priced.

    Keyed by signature, so a result can be matched back to the trip it
    corrects. A trip is only returned when EVERY part of it could be read:
    half of a two-ticket trip is not a price, and returning one leg as
    though it were the total would be exactly the kind of confidently wrong
    number this whole mechanism exists to catch.
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
                parts, returns, ok = [], [], True
                for part_label, url in target["parts"]:
                    fares, backs = _read(page, target["label"], part_label,
                                         url, rows_each, timeout_ms,
                                         follow_return=one_ticket)
                    if not fares:
                        ok = False
                        break
                    parts.append({"label": part_label, "url": url,
                                  "fares": fares})
                    returns = backs
                if not ok:
                    continue
                # One ticket: the total is the cheapest outbound-and-return
                # pairing, not the headline "from" price, and not the first
                # return offered. Two tickets: each leg is bought separately,
                # so the total is the sum.
                if one_ticket and returns:
                    total = returns[0].price
                else:
                    total = sum(part["fares"][0].price for part in parts)
                out[target["key"]] = {
                    "label": target["label"], "total": total, "parts": parts,
                    "returns": returns,
                }
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


def _read(page, label, part_label, url, rows_each, timeout_ms,
          follow_return=False):
    """What one search page prices.

    Returns (fares, returns): the fares listed, and - for a round trip, where
    the list only details the outbound - the return options behind the
    cheapest of them. ``returns`` is empty for a one-way search, which needs
    no second step.
    """
    tag = f"{label} {part_label}".strip() or label
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if not _settle(page, _HAS_ROWS_JS, 25_000):
            body = (page.inner_text("body") or "").lower()[:4000]
            why = ("not a results page" if any(m in body for m in _BLOCKED)
                   else "rendered no fares")
            log.warning("%-28s %s - skipped", tag, why)
            return [], []
        if page.evaluate(_CHEAPEST_JS) == "clicked":
            page.wait_for_timeout(2500)
            _settle(page, _HAS_ROWS_JS, 15_000)

        fares = _fares_from(page.evaluate(_ROWS_JS), rows_each)
        if not fares:
            log.warning("%-28s rendered no fares", tag)
            return [], []
        if not follow_return:
            return fares, []

        opened = page.evaluate(_OPEN_CHEAPEST_JS)
        if opened != "opened":
            log.info("%-28s could not open the return list (%s)", tag, opened)
            return fares, []
        if not _settle(page, _RETURNS_READY_JS, 25_000):
            log.info("%-28s return list did not appear", tag)
            return fares, []
        _settle(page, _HAS_ROWS_JS, 10_000)
        returns = _fares_from(page.evaluate(_ROWS_JS), rows_each)
        if not returns:
            log.info("%-28s return list was unreadable", tag)
        return fares, returns
    except Exception as exc:  # one bad page must not lose the rest
        log.warning("%-28s could not be read (%s)", tag, exc)
        return [], []
