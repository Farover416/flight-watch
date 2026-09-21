"""Notices of airline sales, from SingPromos' published feed.

The big drops on these routes do not come from fares drifting; they come from
time-boxed sales that get announced - Scoot's Tuesday flash sale, the
occasional one-day sale. No amount of price sampling predicts one. Reading
the announcement does.

This uses the RSS feed the site publishes and names in its own robots.txt,
not its pages: one request, no crawling, nothing disallowed. There is no
working per-category feed - asking for the airlines category returns the
whole site - so the category is filtered out of each item's link instead.

Entirely best effort. The feed being down, slow, malformed or reorganised
costs a run nothing: it reports no sales and the search proceeds.
"""

from __future__ import annotations

import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

log = logging.getLogger("flightwatch.sales")

FEED = "https://singpromos.com/feed/"
CATEGORY = "/airlines-flights/"

# A sale worth interrupting someone for, rather than a route launch or a
# credit-card tie-in that happens to mention an airline.
# A bare "off" is not in here on purpose: it matches "takes off", and a
# route launch is not a sale.
WORTH_IT = re.compile(
    r"(\bflash sale\b|\bone[- ]day sale\b|\bsale\b|\bpromo\b|"
    r"\bpromotion\b|\bfares? from\b|\bfrom S?\$?\d|\d+% off|"
    r"\bdiscount)", re.I)

# Carriers that actually fly the routes this watcher searches. Anything else
# in the airlines category is noted but not treated as urgent.
OURS = re.compile(
    r"\b(scoot|singapore airlines|sia|china southern|china eastern|"
    r"air china|xiamen air|shandong|juneyao|cathay|hong kong airlines|"
    r"jetstar|airasia|vietjet|thai lion)\b", re.I)


@dataclass(frozen=True)
class Sale:
    """One announcement, as the feed gave it."""

    uid: str
    title: str
    link: str
    published: str
    ours: bool

    def describe(self) -> str:
        return self.title


def _text(node, tag: str) -> str:
    found = node.find(tag)
    return (found.text or "").strip() if found is not None else ""


def parse(xml_text: str) -> list[Sale]:
    """Airline-sale items in a feed, newest first. Never raises."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("sales feed did not parse (%s)", exc)
        return []

    out: list[Sale] = []
    for item in root.iter("item"):
        link = _text(item, "link")
        title = _text(item, "title")
        if CATEGORY not in link:
            continue
        if not WORTH_IT.search(title):
            continue
        out.append(Sale(
            uid=_text(item, "guid") or link,
            title=title,
            link=link,
            published=_text(item, "pubDate"),
            ours=bool(OURS.search(title)),
        ))
    return out


def fetch(timeout: int = 20) -> list[Sale]:
    """Read the feed. Returns [] on any problem, having said so in the log."""
    try:
        request = urllib.request.Request(
            FEED,
            headers={"User-Agent": "flight-watch/1.0 (personal fare watcher)"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return parse(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        log.info("sales feed unavailable (%s)", exc)
        return []


def new_since(sales: list[Sale], seen: list[str]) -> list[Sale]:
    """The ones not reported before, ours first.

    A sale nobody told you about is the point; a sale you were told about
    yesterday is noise, and announcing it every two hours for a week is how
    an alert gets ignored.
    """
    known = set(seen or ())
    fresh = [s for s in sales if s.uid not in known]
    fresh.sort(key=lambda s: not s.ours)
    return fresh
