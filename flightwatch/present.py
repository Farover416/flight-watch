"""Turns a long list of matching trips into a short, readable one.

A sweep can easily find sixty fares under budget, but most of them are the same
flights on a different day. Two jobs here:

* collapse trips that are the same flights at the same times, keeping the
  cheapest and noting the other dates it is available on;
* cap how many options are shown for any one pair of cities, so a single cheap
  route cannot crowd out every other destination.
"""

from __future__ import annotations

from datetime import datetime


def _clock(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _shape(combo) -> tuple:
    """What makes two trips "the same flights, different dates"."""
    out = combo.out
    key = [
        "out", out.from_airport, out.to_airport, _clock(out.depart),
        _clock(out.arrive), out.airlines, out.stops, combo.source,
    ]
    if combo.back is not None:
        back = combo.back
        key += [
            "back", back.from_airport, back.to_airport, _clock(back.depart),
            _clock(back.arrive), back.airlines, back.stops,
        ]
    else:
        # A round-trip quote does not pin the return, so the outbound and the
        # city you fly home from are all there is to compare.
        key += ["back", combo.back_from]
    return tuple(str(part) for part in key)


def day_label(date: str) -> str:
    parsed = datetime.strptime(date, "%Y-%m-%d")
    return f"{parsed.day} {parsed:%b}"


def alternative_label(primary, alt) -> str:
    """How an alternative differs from the one being shown."""
    parts = []
    if alt.out.date != primary.out.date:
        parts.append(f"{day_label(alt.out.date)} out")
    if alt.back_date != primary.back_date:
        parts.append(f"{day_label(alt.back_date)} back")
    where = ", ".join(parts) if parts else "same dates"
    if alt.price == primary.price:
        return f"{where}, same price"
    return f"{where}, S${alt.price}"


def collapse(combos) -> list[tuple[object, list]]:
    """Group identical itineraries, cheapest first, alternatives attached."""
    groups: dict[tuple, list] = {}
    order: list[tuple] = []
    for combo in sorted(combos, key=lambda c: (c.price, c.out.date, c.back_date)):
        key = _shape(combo)
        if key not in groups:
            groups[key] = [combo]
            order.append(key)
        else:
            groups[key].append(combo)
    return [(groups[key][0], groups[key][1:]) for key in order]


def cap_per_city_pair(items, limit: int) -> list[tuple[object, list]]:
    """Keep at most ``limit`` options per (city you land in, city you leave from)."""
    if limit <= 0:
        return list(items)
    seen: dict[tuple, int] = {}
    kept = []
    for primary, alternatives in items:
        key = (primary.out.search_to, primary.back_city)
        if seen.get(key, 0) >= limit:
            continue
        seen[key] = seen.get(key, 0) + 1
        kept.append((primary, alternatives))
    return kept


def prepare(cfg, combos) -> list[tuple[object, list]]:
    """Everything above, in order, trimmed to what fits in one message."""
    items = cap_per_city_pair(collapse(combos), cfg.max_per_city_pair)
    return items[: cfg.report_top]
