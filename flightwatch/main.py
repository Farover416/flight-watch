"""Entry point: sweep every route, combine, alert."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from . import combine, config, present, search, verify
from .models import Leg, SweepResult
from .notify import (NoChatYet, Telegram, format_deals, format_unrestricted,
                     format_verified)
from .search import search_one_way, search_round_trip
from .state import Store

log = logging.getLogger("flightwatch")


def sweep(
    cfg,
    only: list[str] | None = None,
    round_trip: bool = True,
    destinations: list[str] | None = None,
) -> SweepResult:
    if only:
        destinations = [d for d in cfg.all_destinations if d in only]
    elif destinations is None:
        destinations = cfg.priority_destinations
    result = SweepResult(started=datetime.utcnow())
    search.reset_unreadable()

    outbound: list[Leg] = []
    inbound: list[Leg] = []

    def run(label, fn):
        result.searches_run += 1
        try:
            found = fn()
            log.info("%-28s %d result(s)", label, len(found))
            return found
        except Exception as exc:
            result.searches_failed += 1
            result.errors.append(f"{label}: {exc}")
            log.warning("%-28s FAILED (%s)", label, exc)
            return []

    # --- outbound: Singapore -> each city, on each candidate departure day ---
    for dest in destinations:
        for day in cfg.outbound:
            legs = run(
                f"{cfg.origin}->{dest} {day.date}",
                lambda d=dest, y=day: search_one_way(
                    cfg, cfg.origin, d, y.date,
                    earliest_departure_hour=y.earliest_departure_hour,
                ),
            )
            outbound.extend(legs)

    # --- return: each city -> Singapore, landing before the deadline ---------
    for dest in destinations:
        for date in cfg.return_dates:
            legs = run(
                f"{dest}->{cfg.origin} {date}",
                lambda d=dest, dt=date: search_one_way(
                    cfg, d, cfg.origin, dt, arrive_by=cfg.arrive_home_by
                ),
            )
            inbound.extend(legs)

    result.outbound_legs = len(outbound)
    result.inbound_legs = len(inbound)
    result.legs_found = len(outbound) + len(inbound)
    log.info("legs: %d outbound, %d return", len(outbound), len(inbound))

    # Two parallel worlds from the same searches: the trips whose connections
    # you would actually accept, and every trip regardless. They are kept apart
    # end to end so a cheap-but-grim itinerary can never displace a good one
    # from the main list.
    comfy_out = [leg for leg in outbound if leg.layover_ok]
    comfy_in = [leg for leg in inbound if leg.layover_ok]
    one_way = combine.build_combos(cfg, comfy_out, comfy_in)
    groups = [one_way]
    loose = [combine.build_combos(cfg, outbound, inbound)]

    # --- round-trip cross-check ---------------------------------------------
    # Only for the cities the one-way sweep says are worth a second look, so
    # this costs a handful of searches instead of one per city.
    if round_trip and cfg.check_round_trip:
        candidates: list[str] = []
        for combo in one_way:
            code = combo.out.search_to
            if code not in candidates:
                candidates.append(code)
            if len(candidates) >= cfg.round_trip_candidates:
                break
        log.info("round-trip re-check: %s", ", ".join(candidates) or "(none)")

        quotes: list[tuple[Leg, int, str]] = []
        for dest in candidates:
            for day in cfg.outbound:
                for back_date in cfg.return_dates:
                    found = run(
                        f"RT {dest} {day.date}/{back_date}",
                        lambda d=dest, y=day, b=back_date: search_round_trip(
                            cfg, d, y.date, b,
                            earliest_departure_hour=y.earliest_departure_hour,
                        ),
                    )
                    quotes.extend((leg, total, back_date) for leg, total in found)
        result.legs_found += len(quotes)
        groups.append(combine.round_trip_combos(
            cfg, [q for q in quotes if q[0].layover_ok]))
        loose.append(combine.round_trip_combos(cfg, quotes))

    result.combos = combine.merge(cfg, *groups)
    result.any_combos = combine.merge(cfg, *loose)
    result.searches_unparsed = search.unreadable_count()
    result.finished = datetime.utcnow()
    if result.searches_unparsed:
        log.warning(
            "%d of %d searches returned a page we could not read - those "
            "routes are unknown, not empty",
            result.searches_unparsed, result.searches_run,
        )
    if search.skipped_count():
        log.info("%d individual itineraries were skipped as unreadable",
                 search.skipped_count())
        for reason, count in sorted(search.skip_reasons().items(),
                                    key=lambda kv: -kv[1]):
            log.info("  skip reason x%-4d %s", count, reason)
    log.info("combos: %d (cheapest S$%s)", len(result.combos),
             result.combos[0].price if result.combos else "-")
    return result


def _verify(cfg, result: SweepResult) -> dict:
    """Read the real page for the top few trips and fold the prices in.

    The returned prices replace nothing: each trip keeps its parsed ``total``
    and gains a ``verified`` one, so the gap between what a fetch sees and
    what a person sees stays visible. What changes is which number the rest
    of the run acts on.

    Entirely optional. No browser, a consent wall, a bot check, an unreadable
    page: the run carries on with parsed prices and says so.
    """
    import dataclasses

    if not cfg.verify_top or not result.combos:
        return {}
    if not verify.available():
        log.info("no browser available - skipping price verification")
        return {}

    targets = verify.targets(cfg, result.combos, cfg.verify_top)
    log.info("verifying %d trip(s) in a browser (%d page loads)",
             len(targets), sum(len(t["parts"]) for t in targets))
    try:
        verified = verify.verify(targets, rows_each=cfg.verify_rows)
    except Exception as exc:
        log.warning("verification unavailable (%s)", exc)
        return {}
    if not verified:
        log.warning("nothing verified - acting on parsed prices only")
        return {}

    for group in (result.combos, result.any_combos):
        for i, combo in enumerate(group):
            found = verified.get(combo.signature())
            if found is None:
                continue
            backs = found.get("returns") or []
            group[i] = dataclasses.replace(
                combo, verified=found["total"],
                verified_back=backs[0].summary if backs else None,
                verified_urls=tuple((p["label"] or "book", p["url"])
                                    for p in found["parts"]))
    # Re-rank: a verified price that undercuts its parsed one changes the order.
    result.combos.sort(key=lambda c: c.price)
    result.any_combos.sort(key=lambda c: c.price)
    log.info("verified %d of %d trip(s)", len(verified), len(targets))
    return verified


def report(cfg, result: SweepResult, store: Store, telegram: Telegram,
           dry_run: bool, focus: str | None = None) -> int:
    def deliver(text: str) -> None:
        plain = (text.replace("<b>", "").replace("</b>", "")
                 .replace("<i>", "").replace("</i>", ""))
        if dry_run or not telegram.configured:
            print("\n" + plain)
            return
        try:
            telegram.send(text)
        except NoChatYet as exc:
            log.warning("%s", exc)
            print("\n" + plain)

    if result.looks_blocked:
        log.error("every search came back empty — Google Flights is likely blocking us")
        if store.should_warn_blocked():
            deliver(
                "<b>⚠ Flight watcher is not getting results</b>\n\n"
                f"{result.blind_searches} of {result.searches_run} searches "
                "came back with nothing readable. Google Flights is probably "
                "blocking this runner's IP. Running the same script from your "
                "laptop usually fixes it."
            )
            store.record_blocked_warning()
        store.append_history(result)
        store.save()
        return 1

    # The parsed prices are the airline's fare off a short list. For the few
    # trips worth acting on, go and read what the page actually renders -
    # BEFORE deciding anything, because these are the numbers to decide on.
    # Reading them afterwards, as this used to, meant every alert, every
    # budget test and every new low ran on the number we trust least.
    verified = _verify(cfg, result)

    # Every run reports what it actually found. Suppressing fares because an
    # earlier run already mentioned them hid the cheapest ones and surfaced
    # worse alternatives, which is the opposite of useful.
    under_budget = [c for c in result.combos if c.price <= cfg.max_total]
    previous_best = store.best_total()
    if result.combos:
        store.update_best(result.combos[0])

    if not result.combos:
        log.info("no valid trips found this run")
        note = (
            f"No trips matched this run - {result.searches_run} searches, "
            f"{result.searches_failed} failed."
        )
        if result.searches_unparsed:
            # "Nothing found" and "could not look" are different answers, and
            # only one of them means you should stop hoping.
            note += (
                f"\n\n{result.searches_unparsed} of them came back as a page "
                "that could not be read, so those routes are unknown rather "
                "than empty."
            )
        deliver(note)
        # Nothing passed the connection rules, but something may still fly.
        # That is the most useful moment for this list, not the least.
        if result.any_combos:
            deliver(format_unrestricted(cfg, result.any_combos,
                                        cfg.unrestricted_top))
        store.append_history(result)
        store.save()
        return 0

    where = focus or "Singapore ⇄ China / Korea / Japan"
    items = present.prepare(cfg, under_budget or result.combos)
    if under_budget:
        heading = (
            f"🔥 {len(under_budget)} fare(s) under S${cfg.max_total}, "
            f"best {len(items)} - {where}"
        )
    else:
        heading = (
            f"Nothing under S${cfg.max_total} - cheapest {len(items)} "
            f"right now - {where}"
        )
    if previous_best is not None and result.combos[0].price < previous_best:
        heading += f" · new low, was S${previous_best}"

    deliver(format_deals(cfg, items, heading))
    if result.any_combos:
        deliver(format_unrestricted(cfg, result.any_combos, cfg.unrestricted_top))

    if verified:
        deliver(format_verified(cfg, verified))
    for combo in under_budget:
        store.record_alert(combo)

    store.append_history(result)
    store.save()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flightwatch")
    parser.add_argument("--config", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="print instead of sending to Telegram")
    parser.add_argument("--summary", action="store_true",
                        help="no-op: every run now reports the current cheapest")
    parser.add_argument("--only", default="",
                        help="check just these places - area, city or code, "
                             "e.g. Yunnan or Beijing or CTU,CKG")
    parser.add_argument("--no-round-trip", action="store_true")
    parser.add_argument("--whoami", action="store_true",
                        help="print your Telegram chat id and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S", stream=sys.stdout,
    )

    cfg = config.load(args.config)
    telegram = Telegram(cache_path=cfg.data_dir / "telegram.json")

    if args.whoami:
        print(telegram.whoami())
        return 0

    store = Store(cfg.data_dir, cfg)
    wanted = [t for t in args.only.split(",") if t.strip()]
    only, unknown = cfg.resolve_destinations(wanted) if wanted else ([], [])
    if unknown:
        log.error("not a place I search: %s", ", ".join(unknown))
    if wanted and not only:
        log.error("nothing left to search - check the spelling")
        return 2

    focus = None
    if only:
        names = ", ".join(cfg.city_name(code) for code in only)
        focus = f"Singapore ⇄ {names}"
        # A focused check is about one place, so show the spread rather than
        # capping at three options per city pair.
        cfg.max_per_city_pair = cfg.report_top
        log.info("checking %s only", names)

    destinations, next_cursor = cfg.destinations_for_run(store.rotation_cursor())
    if not only:
        log.info(
            "this run: %d cities (%d priority + %d from the rotation of %d)",
            len(destinations), len(cfg.priority_destinations),
            len(destinations) - len(cfg.priority_destinations),
            len(cfg.extended_destinations),
        )
        store.set_rotation_cursor(next_cursor)

    result = sweep(cfg, only=only or None, destinations=destinations,
                   round_trip=not args.no_round_trip)
    return report(cfg, result, store, telegram,
                  dry_run=args.dry_run, focus=focus)


if __name__ == "__main__":
    raise SystemExit(main())
