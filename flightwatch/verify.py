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


def verify(searches: list[tuple[str, str]], rows_each: int = 4,
           timeout_ms: int = 45_000) -> dict[str, list[RenderedFare]]:
    """Open each (label, url) and return the cheapest rendered fares.

    A search that cannot be read is simply absent from the result, so callers
    can tell "verified and cheap" from "not verified" without guessing.
    """
    from playwright.sync_api import sync_playwright

    out: dict[str, list[RenderedFare]] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(
                locale="en-SG",
                viewport={"width": 1400, "height": 1000},
            )
            page = context.new_page()
            for label, url in searches:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_timeout(4000)
                    body = (page.inner_text("body") or "").lower()[:4000]
                    if any(m in body for m in _BLOCKED):
                        log.warning("%-28s not a results page - skipped", label)
                        continue
                    if page.evaluate(_CHEAPEST_JS) == "clicked":
                        page.wait_for_timeout(4500)
                    fares = _fares_from(page.evaluate(_ROWS_JS), rows_each)
                    if fares:
                        out[label] = fares
                        log.info("%-28s verified, cheapest S$%d", label, fares[0].price)
                    else:
                        log.warning("%-28s rendered no fares", label)
                except Exception as exc:  # one bad page must not lose the rest
                    log.warning("%-28s could not be read (%s)", label, exc)
        finally:
            browser.close()
    return out


def targets(cfg, combos, limit: int) -> list[tuple[str, str]]:
    """The searches worth opening: one per city-and-dates, cheapest first.

    Several recommended trips usually share a search, so this collapses them -
    six page loads covers a dozen recommendations.
    """
    from .search import build_query
    import urllib.parse

    seen: set[tuple] = set()
    picked: list[tuple[str, str]] = []
    for combo in sorted(combos, key=lambda c: c.total):
        city = combo.out.search_to
        key = (city, combo.out.date, combo.back_date)
        if key in seen:
            continue
        seen.add(key)
        query = build_query(
            cfg,
            [(cfg.origin, city, combo.out.date, None),
             (city, cfg.origin, combo.back_date, None)],
            trip="round-trip",
        )
        url = ("https://www.google.com/travel/flights?"
               + urllib.parse.urlencode(query.params()))
        label = (f"{cfg.city_name(city)} {combo.out.date[5:]}/{combo.back_date[5:]}")
        picked.append((label, url))
        if len(picked) >= limit:
            break
    return picked
