"""Pairs outbound legs with return legs into complete trips, including open-jaws."""

from __future__ import annotations

from collections import defaultdict

from .models import Combo, Leg, dedupe_cheapest


def build_combos(cfg, outbound: list[Leg], inbound: list[Leg]) -> list[Combo]:
    """Every valid outbound + return pairing, cheapest kept per route/date pair.

    A pairing is valid when you could get from the city you landed in to the
    city you fly home from by train, the return departs after you land, and the
    trip is at least ``min_nights`` long.
    """
    by_city: dict[str, list[Leg]] = defaultdict(list)
    for leg in inbound:
        by_city[leg.search_from].append(leg)

    combos: list[Combo] = []
    for out in outbound:
        country = cfg.country_of(out.search_to)
        candidates = [
            leg
            for city, legs in by_city.items()
            if cfg.reachable_by_train(out.search_to, city)
            for leg in legs
        ]

        for back in candidates:
            if back.depart <= out.arrive:
                continue
            nights = (back.depart.date() - out.arrive.date()).days
            if nights < cfg.min_nights:
                continue
            combos.append(
                Combo(
                    out=out,
                    back=back,
                    back_date=back.date,
                    country=country,
                    total=out.price + back.price,
                )
            )

    return dedupe_cheapest(combos)


def round_trip_combos(cfg, quotes: list[tuple[Leg, int, str]]) -> list[Combo]:
    """Turn (outbound leg, total price, return date) quotes into combos."""
    combos = [
        Combo(
            out=leg,
            back=None,
            back_date=back_date,
            country=cfg.country_of(leg.search_to),
            total=total,
            source="round-trip",
        )
        for leg, total, back_date in quotes
    ]
    return dedupe_cheapest(combos)


def merge(cfg, *groups: list[Combo]) -> list[Combo]:
    """Merge combo lists, preferring the cheapest price for an identical trip.

    A one-way pair and a round-trip quote for the same cities and dates
    describe the same journey, so only the cheaper of the two is kept — but the
    one-way pair wins ties because its return timing is verified.
    """
    best: dict[tuple, Combo] = {}
    for combo in [c for group in groups for c in group]:
        key = (combo.out.to_airport, combo.back_from, combo.out.date, combo.back_date)
        current = best.get(key)
        if current is None:
            best[key] = combo
        elif combo.total < current.total:
            best[key] = combo
        elif combo.total == current.total and combo.back_verified:
            best[key] = combo
    return sorted(best.values(), key=lambda c: c.total)
