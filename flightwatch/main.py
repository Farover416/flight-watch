"""Entry point: sweep every route, combine, alert."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta

from . import combine, config, search, survey, verify
from .models import Leg, SweepResult
from . import sales as sales_feed
from .notify import NoChatYet, Telegram, check_messages, format_sales
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
    result.legs_out, result.legs_in = list(outbound), list(inbound)
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


# After Google answers with a bot check, leave the full survey alone for this
# long and fall back to opening only the few cheapest pages. Pressing on
# would only teach it to block this connection for longer - and at home that
# connection is also the one you browse on.
SURVEY_REST_HOURS = 6
# Two cities is 34 pages and about forty minutes. A hand-picked check of
# many more would run for hours, so past this it opens only the cheapest few.
SURVEY_MAX_PAGES = 40


def _survey(cfg, result: SweepResult, store, cities):
    """Open every search page, and let what they show replace the feed's guesses.

    Trips on a page the survey read are priced from that page; trips whose
    page could not be read keep the feed's price rather than vanishing. The
    survey's own trips - one ticket and two, open jaws included - join the
    list whether or not the feed ever returned them.
    """
    if not verify.available():
        log.info("no browser available - skipping the survey")
        return None
    rested = store.health.get("survey_blocked_at")
    if rested:
        try:
            since = datetime.utcnow() - datetime.fromisoformat(rested.rstrip("Z"))
        except ValueError:
            since = None
        if since is not None and since.total_seconds() < SURVEY_REST_HOURS * 3600:
            log.warning("Google showed a bot check %.1f h ago - opening only the "
                        "cheapest few pages this time", since.total_seconds() / 3600)
            return None
    planned = len(survey.plan(cfg, cities))
    if planned > SURVEY_MAX_PAGES:
        log.info("%d pages is too many to open them all - opening the cheapest "
                 "%d trips instead", planned, cfg.verify_top)
        return None
    try:
        found = survey.run(cfg, result, cities)
    except Exception as exc:
        log.warning("survey unavailable (%s)", exc)
        return None
    if found.blocked:
        store.health["survey_blocked_at"] = (
            datetime.utcnow().isoformat(timespec="seconds") + "Z")
    if not found.pages:
        return None
    keep = [c for c in result.combos if not survey.covered(c, found.read)]
    keep_any = [c for c in result.any_combos if not survey.covered(c, found.read)]
    result.combos = combine.merge(cfg, keep, found.combos)
    result.any_combos = combine.merge(cfg, keep_any, found.any_combos)
    log.info("survey: %d trips within the rules, cheapest S$%s", len(found.combos),
             min((c.price for c in found.combos), default="-"))
    return found


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
        verified = verify.verify(cfg, targets, rows_each=cfg.verify_rows)
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
    # Flights the page showed that the feed never had - the cheapest of them
    # can beat everything the sweep found, and until now was only a footnote.
    extra = verify.found_on_page(cfg, result.combos, verified)
    if extra:
        result.combos.extend(extra)
        result.any_combos.extend(extra)
        log.info("added %d trip(s) found only on the page", len(extra))
    # Re-rank: a verified price that undercuts its parsed one changes the order.
    result.combos.sort(key=lambda c: c.price)
    result.any_combos.sort(key=lambda c: c.price)
    log.info("verified %d of %d trip(s)", len(verified), len(targets))
    return verified


# While the laptop is checking, GitHub leaves the messages to it: its prices are
# the complete ones, and one set of messages is the point. A laptop check takes
# over an hour and starts every two, so four hours since its last one means it
# has stopped; Taipei is checked from there every six.
LAPTOP_COVERS = timedelta(hours=4)
LAPTOP_COVERS_TAIPEI = timedelta(hours=8)


def _laptop_covering(cfg) -> timedelta | None:
    """On GitHub: how long ago the laptop checked this trip, when that is
    recent enough for its messages to stand for this run's. Else None."""
    if cfg.at_home:
        return None
    window = (LAPTOP_COVERS_TAIPEI if cfg.trip is not None and cfg.trip.window
              is not None else LAPTOP_COVERS)
    try:
        lines = (cfg.data_dir / "home" / "history.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        when = datetime.fromisoformat(json.loads(lines[-1])["ts"].rstrip("Z"))
    except (OSError, IndexError, KeyError, ValueError):
        return None
    ago = datetime.utcnow() - when
    return ago if timedelta(0) <= ago < window else None


def _seen_elsewhere(cfg) -> list[str]:
    """Sales the other side - GitHub or the laptop - has already announced."""
    other = (cfg.data_dir.parent if cfg.at_home else cfg.data_dir / "home")
    try:
        seen = json.loads((other / "health.json").read_text(encoding="utf-8"))
        return [str(uid) for uid in seen.get("seen_sales") or []]
    except (OSError, ValueError, AttributeError):
        return []


def _pairs(cfg, cities) -> list[tuple[str, str]]:
    """The kinds of trip a check reports, in the order asked for: each city
    as a return, then each open jaw you can make by train, both ways round.
    None for a trip to one place, which gets a single list."""
    if cfg.trip is not None and cfg.trip.window is not None:
        return None
    cities = list(dict.fromkeys(cities or []))
    if not cities:
        return None
    pairs = [(c, c) for c in cities]
    pairs += [(a, b) for a in cities for b in cities
              if a != b and cfg.reachable_by_train(a, b)]
    return pairs[:4]


def report(cfg, result: SweepResult, store: Store, telegram: Telegram,
           dry_run: bool, focus: str | None = None, cities=None) -> int:
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

    covered_for = _laptop_covering(cfg)
    if covered_for is not None:
        log.info("the laptop checked %.1f h ago - its messages stand for this "
                 "run, which only saves its prices", covered_for.total_seconds() / 3600)

    # Announcements first: a sale expires, a fare drift does not. Only the
    # main watch announces them, and a sale either side has already announced
    # is not announced again.
    fresh = []
    if cfg.trip is None:
        try:
            fresh = sales_feed.new_since(sales_feed.fetch(),
                                         store.seen_sales() + _seen_elsewhere(cfg))
        except Exception as exc:  # never let the feed cost a run
            log.info("sale check skipped (%s)", exc)
    if fresh:
        log.info("%d new sale announcement(s)", len(fresh))
        if covered_for is None:
            deliver(format_sales(fresh))
        store.record_sales(fresh)

    if result.looks_blocked:
        log.error("every search came back empty — Google Flights is likely blocking us")
        if store.should_warn_blocked() and covered_for is None:
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
    surveyed = None
    if cfg.survey and cities and (cfg.trip is None or cfg.trip.window is None):
        surveyed = _survey(cfg, result, store, cities)
    if surveyed is None:
        _verify(cfg, result)

    # Every run reports what it actually found. Suppressing fares because an
    # earlier run already mentioned them hid the cheapest ones and surfaced
    # worse alternatives, which is the opposite of useful.
    result.combos.sort(key=lambda c: c.price)
    result.any_combos.sort(key=lambda c: c.price)
    under_budget = (list(result.combos) if cfg.max_total is None
                    else [c for c in result.combos if c.price <= cfg.max_total])
    if result.combos:
        store.update_best(result.combos[0])
    else:
        log.info("no valid trips found this run")

    # One message per kind of trip, three trips in each - see check_messages.
    if covered_for is None:
        for text in check_messages(cfg, result, _pairs(cfg, cities)):
            deliver(text)
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
    parser.add_argument("--trip", default="",
                        help="watch one of the other trips in config.yaml "
                             "instead of the December one, e.g. taipei")
    parser.add_argument("--whoami", action="store_true",
                        help="print your Telegram chat id and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S", stream=sys.stdout,
    )

    cfg = config.load(args.config)
    # The chat is the same chat whichever trip is running, so this one stays
    # in the top-level data directory rather than following the trip.
    telegram = Telegram(cache_path=cfg.data_dir / "telegram.json")

    if args.whoami:
        print(telegram.whoami())
        return 0

    if args.trip:
        try:
            cfg = cfg.for_trip(args.trip)
        except KeyError as exc:
            log.error("%s", exc.args[0] if exc.args else exc)
            return 2
        log.info("watching %s - %d adult(s), prices filed under %s",
                 cfg.trip_name, cfg.adults, cfg.data_dir.name)

    if cfg.at_home:
        # The laptop's prices live beside GitHub's, never in the same files:
        # data/home for December, data/taipei/home for Taipei. Each side only
        # ever writes its own, so the two can save at the same moment without
        # a clash, and each keeps its own record of what it has alerted. The
        # Telegram chat above stays shared - it is the same chat either way.
        cfg.data_dir = cfg.data_dir / "home"
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        log.info("checking from this laptop - prices filed under %s",
                 cfg.data_dir)

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

    if cfg.trip is not None and cfg.trip.window is not None:
        # A trip fixed to a window has no list of days to iterate; it has a
        # grid of date pairs, walked coarse-then-fine. See window.py.
        from . import window as window_search
        result = window_search.sweep(cfg)
    else:
        destinations, next_cursor = cfg.destinations_for_run(
            store.rotation_cursor())
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
        return report(cfg, result, store, telegram, dry_run=args.dry_run,
                      focus=focus, cities=only or destinations)
    return report(cfg, result, store, telegram,
                  dry_run=args.dry_run, focus=focus)


if __name__ == "__main__":
    raise SystemExit(main())
