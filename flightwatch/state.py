"""Remembers what has already been seen so you are not pinged twice."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .models import Combo


class Store:
    def __init__(self, data_dir: Path, cfg=None):
        # cfg is optional: it is only needed to write the Google Flights links,
        # which are built by the same code that runs the searches. Without it
        # everything else still records exactly as before.
        self.cfg = cfg
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.alerted_path = self.dir / "alerted.json"
        self.best_path = self.dir / "best.json"
        self.history_path = self.dir / "history.jsonl"
        self.health_path = self.dir / "health.json"
        # One file per month: small enough to fetch over mobile data, and it
        # keeps each run's commit to a one-line diff.
        self.series_dir = self.dir / "series"
        self.latest_path = self.dir / "latest.json"

        self.alerted: dict[str, int] = _read_json(self.alerted_path, {})
        self.best: dict = _read_json(self.best_path, {})
        self.health: dict = _read_json(self.health_path, {})

    # -- deal alerts ---------------------------------------------------------
    def is_new_deal(self, combo: Combo, realert_drop: int) -> bool:
        """True when this trip has never been alerted, or got meaningfully cheaper."""
        seen = self.alerted.get(combo.signature())
        if seen is None:
            return True
        return combo.price <= seen - realert_drop

    def record_alert(self, combo: Combo) -> None:
        key = combo.signature()
        seen = self.alerted.get(key)
        if seen is None or combo.price < seen:
            self.alerted[key] = combo.price

    # -- best-ever tracking --------------------------------------------------
    def best_total(self) -> int | None:
        value = self.best.get("total")
        return int(value) if value is not None else None

    def update_best(self, combo: Combo) -> None:
        current = self.best_total()
        if current is None or combo.price < current:
            self.best = {
                "total": combo.price,
                "verified": combo.verified,
                "signature": combo.signature(),
                "seen_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "detail": combo.to_json(),
            }

    # -- health --------------------------------------------------------------
    def should_warn_blocked(self, cooldown_hours: int = 12) -> bool:
        last = self.health.get("last_blocked_alert")
        if not last:
            return True
        try:
            when = datetime.fromisoformat(last.rstrip("Z"))
        except ValueError:
            return True
        return datetime.utcnow() - when > timedelta(hours=cooldown_hours)

    def record_blocked_warning(self) -> None:
        self.health["last_blocked_alert"] = (
            datetime.utcnow().isoformat(timespec="seconds") + "Z"
        )

    # -- sale announcements --------------------------------------------------
    def seen_sales(self) -> list[str]:
        value = self.health.get("seen_sales")
        return list(value) if isinstance(value, list) else []

    def record_sales(self, sales) -> None:
        """Remember what has been announced, newest last, bounded.

        Without this the same sale is announced every run for as long as it
        sits in the feed, which is the fastest way to make an alert ignored.
        """
        seen = self.seen_sales()
        seen.extend(s.uid for s in sales if s.uid not in seen)
        self.health["seen_sales"] = seen[-80:]

    # -- extended-city rotation ---------------------------------------------
    def rotation_cursor(self) -> int:
        try:
            return int(self.health.get("rotation_cursor", 0))
        except (TypeError, ValueError):
            return 0

    def set_rotation_cursor(self, cursor: int) -> None:
        self.health["rotation_cursor"] = int(cursor)

    # -- history -------------------------------------------------------------
    def append_history(self, result) -> None:
        row = {
            "ts": result.started.isoformat(timespec="seconds") + "Z",
            "searches_run": result.searches_run,
            "searches_failed": result.searches_failed,
            "searches_unparsed": result.searches_unparsed,
            "legs_found": result.legs_found,
            "outbound_legs": result.outbound_legs,
            "inbound_legs": result.inbound_legs,
            # Deep enough to answer "what about this particular routing?"
            # afterwards without re-running the sweep. Five only ever showed
            # the headline, which is never the one you end up asking about.
            "cheapest": [c.to_json() for c in result.best(25)],
            "cheapest_any": [c.to_json() for c in
                             sorted(result.any_combos, key=lambda c: c.total)[:10]],
        }
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.record_series(result)

    # -- price series --------------------------------------------------------
    def record_series(self, result) -> None:
        """Every trip this run priced, one compact row each, for the graph.

        ``history.jsonl`` keeps a run's top few in full detail, which answers
        "what was cheapest last Tuesday" but cannot answer "what has this
        particular trip been doing": the moment a trip drops out of the top
        few it vanishes from the record, and a line drawn through that breaks
        every time the ranking shifts rather than when the trip stopped being
        available. So every priced trip gets a row here, and a gap in a line
        means the trip genuinely was not found.

        A trip appears once per connection standard - the cheapest that passes
        the layover rules, and the cheapest regardless of them. Those answer
        different questions, so they are separate rows rather than one
        averaged number; when no connection was objectionable they are the
        same trip and only one row is written.

        Rows are positional to keep the file small enough to open on a phone:
        [signature, parsed, passes_rules, outbound, return, search,
        verified, verified_back], where verified is null unless a browser read
        the page and verified_back is the return flight it found there, and a
        flight is [airlines, departure, arrival, stops, from, to] and a return
        of None means the run only ever saw a round-trip quote, which prices
        both halves but details only the outbound.

        ``search`` is the pair of city codes the search was actually run on
        (SIN, BJS) rather than the airports flown (SIN, PKX). Both are kept
        because they answer different questions: the airports identify the
        flight, the city codes are what the comparison sites want, and
        Telegram already links them by city. Without these the page would
        quietly send you to a narrower search than your alerts do.
        """
        comfy: dict[str, object] = {}
        loose: dict[str, object] = {}
        for combos, best in ((result.combos, comfy), (result.any_combos, loose)):
            for combo in combos:
                key = combo.signature()
                if key not in best or combo.price < best[key].price:
                    best[key] = combo

        picked: list[tuple[object, int]] = [(c, 1) for c in comfy.values()]
        for key, combo in loose.items():
            kept = comfy.get(key)
            # Only worth its own row when relaxing the rules actually buys
            # something; otherwise it is the same trip at the same price.
            if kept is None or combo.price < kept.price:
                picked.append((combo, 0))

        rows = [
            [combo.signature(), combo.total, ok,
             _flight(combo.out), _flight(combo.back),
             [combo.out.search_from, combo.out.search_to],
             combo.verified, combo.verified_back]
            for combo, ok in sorted(picked, key=lambda pair: pair[0].price)
        ]

        month = result.started.strftime("%Y-%m")
        self.series_dir.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": result.started.isoformat(timespec="seconds") + "Z",
            "rows": rows,
        }
        # A run that priced nothing still gets a line. Silence and "found
        # nothing" look identical on a chart otherwise, and telling those
        # apart is the whole reason this watcher is trusted.
        with (self.series_dir / f"{month}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False,
                                    separators=(",", ":")) + "\n")

        self._record_latest(result)

    def _record_latest(self, result) -> None:
        """The current run's Google Flights links, rewritten each time.

        These are the protobuf-encoded links the searches themselves use, so
        they reproduce a price exactly - and nothing but this code can build
        them, which is why they cannot be assembled in a browser. Keeping
        them for the newest run only is the point: a link is worth having for
        a price you might act on now, and storing one per trip per run would
        more than double a file that has to load over mobile data.

        Everything here is best effort. No cfg, no links, and the page falls
        back to the comparison sites it can build itself.
        """
        if self.cfg is None:
            return
        best: dict[str, object] = {}
        for combo in list(result.combos) + list(result.any_combos):
            key = combo.signature()
            if key not in best or combo.total < best[key].total:
                best[key] = combo
        links: dict[str, list] = {}
        for key, combo in best.items():
            try:
                # A verified trip links to the very pages that were read, so
                # tapping through lands on the number the watcher acted on.
                links[key] = ([list(pair) for pair in combo.verified_urls]
                              or [list(pair) for pair in combo.booking_urls(self.cfg)])
            except Exception:
                continue  # one unbuildable link must not cost the rest
        _write_json(self.latest_path, {
            "ts": result.started.isoformat(timespec="seconds") + "Z",
            "links": links,
        })


    def save(self) -> None:
        _write_json(self.alerted_path, self.alerted)
        _write_json(self.best_path, self.best)
        _write_json(self.health_path, self.health)


def _flight(leg) -> list | None:
    """The identifying detail of one leg: who flew it, when, and between where.

    The airports are here as well as the times because a trip bought as two
    one-way tickets is checked leg by leg on the comparison sites, and each
    leg's own airports are what those searches take.
    """
    if leg is None:
        return None
    sold = f" ({leg.ticketing})" if getattr(leg, "ticketing", "") else ""
    return [
        (", ".join(leg.airlines) if leg.airlines else "?") + sold,
        leg.depart.isoformat(timespec="minutes"),
        leg.arrive.isoformat(timespec="minutes"),
        leg.stops,
        leg.from_airport,
        leg.to_airport,
    ]


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
