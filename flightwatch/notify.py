"""Telegram delivery."""

from __future__ import annotations

import html
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

from . import compare, present

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 3800  # Telegram's hard cap is 4096; leave room for formatting


class NoChatYet(RuntimeError):
    """The bot exists but nobody has spoken to it, so we have nowhere to send."""


class Telegram:
    """Sends to your chat, working out the chat id by itself the first time.

    You never have to look up a chat id: say anything to your bot once and the
    next run finds it and caches it in data/telegram.json.
    """

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        cache_path: Path | None = None,
    ):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.cache_path = cache_path
        self.chat_id = (
            chat_id
            if chat_id is not None
            else os.environ.get("TELEGRAM_CHAT_ID", "") or self._cached()
        )

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _cached(self) -> str:
        if self.cache_path and self.cache_path.exists():
            try:
                return str(json.loads(self.cache_path.read_text())["chat_id"])
            except (json.JSONDecodeError, KeyError, OSError):
                return ""
        return ""

    def _remember(self, chat_id: str) -> None:
        self.chat_id = chat_id
        if self.cache_path:
            self.cache_path.write_text(
                json.dumps({"chat_id": chat_id}, indent=2) + "\n", encoding="utf-8"
            )

    def _call(self, method: str, payload: dict) -> dict:
        data = urllib.parse.urlencode(payload).encode()
        request = urllib.request.Request(
            API.format(token=self.token, method=method), data=data
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RuntimeError(
                    "Telegram rejected the bot token — check the "
                    "TELEGRAM_BOT_TOKEN secret"
                ) from exc
            raise

    def resolve_chat_id(self) -> str:
        """Find the chat to talk to: env var, cache, or whoever messaged the bot."""
        if self.chat_id:
            return self.chat_id
        for chat_id, _ in self._recent_chats():
            self._remember(chat_id)
            return chat_id
        raise NoChatYet(
            "Your bot has no conversation yet. Open Telegram, find your bot, "
            "press Start and send it any message — the next run will pick it up."
        )

    def _recent_chats(self) -> list[tuple[str, str]]:
        result = self._call("getUpdates", {})
        found: dict[str, str] = {}
        for update in result.get("result", []):
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if chat.get("id"):
                label = (
                    chat.get("username")
                    or chat.get("title")
                    or chat.get("first_name")
                    or "?"
                )
                found[str(chat["id"])] = f"{label} ({chat.get('type')})"
        return list(reversed(found.items()))

    def send(self, text: str) -> None:
        if not self.configured:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        self.resolve_chat_id()
        for chunk in _split(text, LIMIT):
            self._call(
                "sendMessage",
                {
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )

    def whoami(self) -> str:
        """Setup check: confirms the token works and says who it will message."""
        if not self.token:
            return "TELEGRAM_BOT_TOKEN is not set."
        me = self._call("getMe", {}).get("result", {})
        lines = [f"Bot: @{me.get('username', '?')} ({me.get('first_name', '?')})"]
        chats = self._recent_chats()
        if not chats:
            lines.append(
                "No conversation yet — open Telegram, find that bot, press Start "
                "and send it any message. Nothing else to configure."
            )
        else:
            lines.append("Will send to:")
            lines += [f"  {cid}  {name}" for cid, name in chats]
        return "\n".join(lines)


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > limit and current:
            chunks.append(current.rstrip())
            current = ""
        current += block + "\n\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def leg_line(cfg, leg) -> str:
    """One flight, named by city rather than airport code."""
    if leg.layovers:
        rules = cfg.layover
        parts = []
        for stop in leg.layovers:
            text = stop.describe(cfg.city_name(stop.airport))
            if stop.minutes > rules.short_max_minutes:
                out = stop.daylight_minutes(rules.day_from_hour, rules.day_to_hour)
                text += f", {out // 60}h out"
            parts.append(text)
        stops = "via " + ", ".join(parts)
    elif getattr(leg, "connection_note", ""):
        # Found on the page, so the stop's length and place are known but not
        # its clock times - no "hours out" figure, rather than a made-up one.
        stops = "via " + leg.connection_note
    else:
        stops = "direct" if leg.stops == 0 else f"{leg.stops} stop"
    bag = f" +S${leg.bag_fee} bag" if leg.bag_fee else ""
    if getattr(leg, "ticketing", ""):
        bag += f", {leg.ticketing}"
    return (
        f"{esc(cfg.city_name(leg.from_airport))} → {esc(cfg.city_name(leg.to_airport))}"
        f"  {leg.depart:%d %b %H:%M} → {leg.arrive:%d %b %H:%M}"
        f"  ({esc(stops)}, {esc(leg.airline_label)}{esc(bag)})"
    )


# -- the messages a check sends ----------------------------------------------
#
# One message per kind of trip, three trips in each: for December, Beijing and
# Qingdao each as a return, each open jaw both ways round, and the cheapest
# three with the layover rules off; for Taipei, the cheapest three. Asked for
# on 26 Sep, when a check was sending three long messages from each of two
# places and none of them was easy to read.

TOP = 3


def run_time(started) -> str:
    """When a check started, Singapore time - the runs log it in UTC."""
    return (started + timedelta(hours=8)).strftime("%H:%M")


def route_name(cfg, into: str, home: str) -> str:
    if into == home:
        return f"{cfg.city_name(into)} return"
    return f"{cfg.city_name(into)} in, {cfg.city_name(home)} out"


def top(combos, limit: int = TOP) -> list:
    """The cheapest few trips as (trip, same flights on other dates) pairs.

    The same flights a day later are one choice, not two, so they fold into
    the line of the cheapest date instead of taking up the list.
    """
    return present.collapse(sorted(combos, key=lambda c: c.price))[:limit]


def _trip(cfg, combo, alternatives, where: str = "") -> list[str]:
    tickets = ("one ticket" if combo.source in ("round-trip", "multi-city")
               else "two tickets")
    links = " · ".join(f'<a href="{esc(url)}">{esc(label)}</a>'
                       for label, url in combo.booking_urls(cfg))
    head = (f"<b>S${combo.price}</b>" + (f" · {esc(where)}" if where else "")
            + f" · {esc(present.day_label(combo.out.date))} → "
            f"{esc(present.day_label(combo.back_date))} · {tickets}  {links}")
    lines = [head, f"✈ {leg_line(cfg, combo.out)}"]
    if combo.back is not None:
        lines.append(f"↩ {leg_line(cfg, combo.back)}")
    else:
        lines.append(f"↩ {esc(cfg.city_name(combo.back_city))} → "
                     f"{esc(cfg.city_name(cfg.origin))}  "
                     f"{esc(present.day_label(combo.back_date))} "
                     "<i>(return time not read - check it)</i>")
    # Other dates only: the same flights on the same dates at a higher price
    # is not a choice anyone would make.
    others = [a for a in alternatives
              if (a.out.date, a.back_date) != (combo.out.date, combo.back_date)]
    if others:
        shown = "; ".join(present.alternative_label(combo, a) for a in others[:3])
        lines.append(f"<i>also {esc(shown)}</i>")
    return lines


def format_top(cfg, title: str, picks, footer: str, where=None) -> str:
    """A heading, up to three trips, one line saying where the prices came from."""
    lines = [f"<b>{esc(title)}</b>"]
    if not picks:
        lines += ["", "Nothing this check."]
    for combo, alternatives in picks:
        lines.append("")
        lines += _trip(cfg, combo, alternatives, where(combo) if where else "")
    lines += ["", f"<i>{esc(footer)}</i>"]
    return "\n".join(lines)


def check_messages(cfg, result, pairs=None) -> list[str]:
    """Everything a check has to say about prices, one message per kind of trip.

    ``pairs`` are the (city landed in, city flown home from) kinds to list, in
    order. Without them - a trip to one place, like Taipei - it is the cheapest
    three in a single message.
    """
    stamp = (f"Laptop check {run_time(result.started)}" if cfg.at_home else
             f"GitHub check {run_time(result.started)} - airline fares only, "
             "while your laptop is off")
    seats = f" · for {cfg.adults} adults" if cfg.adults > 1 else ""
    trouble = ""
    if result.searches_failed or result.searches_unparsed:
        trouble = (f" · {result.searches_failed + result.searches_unparsed} of "
                   f"{result.searches_run} searches could not be read")

    if not pairs:
        name = cfg.trip_name.split("⇄")[-1].strip()
        return [format_top(cfg, f"{name} · top {TOP}", top(result.combos),
                           f"{stamp}{seats}{trouble}")]

    within = f"{stamp} · within your transit rules · checked bag included{seats}"
    messages = []
    for into, home in pairs:
        mine = [c for c in result.combos
                if c.out.search_to == into and c.back_city == home]
        messages.append(format_top(cfg, f"{route_name(cfg, into, home)} · top {TOP}",
                                   top(mine), within))
    messages.append(format_top(
        cfg, f"Ignoring transit rules · top {TOP}", top(result.any_combos),
        f"{stamp} · layover rules off; dates, 1 stop and the bag still apply"
        f"{seats}{trouble}",
        where=lambda c: route_name(cfg, c.out.search_to, c.back_city)))
    return messages


def format_sales(sales) -> str:
    """A sale has been announced - said first, because it expires."""
    ours = [s for s in sales if s.ours]
    head = ("<b>Sale on a carrier that flies your routes</b>" if ours
            else "<b>Airline sale announced</b>")
    lines = [head, ""]
    for sale in sales[:5]:
        mark = "" if sale.ours else " <i>(other carrier)</i>"
        lines.append(f'· <a href="{esc(sale.link)}">{esc(sale.title)}</a>{mark}')
    lines.append("")
    lines.append("<i>From SingPromos. Prices below were checked just now, but a "
                 "sale loading mid-run will not show until the next one.</i>")
    return "\n".join(lines)


def _checked(combo) -> str:
    """Mark a price a browser actually read, and what it was before.

    Worth the few characters: these two numbers come from different places,
    and a price that moved because someone looked at the page should not be
    indistinguishable from one that moved because the fare changed.
    """
    if combo.verified is None:
        return ""
    if combo.verified < combo.total:
        return f" <i>(checked, was S${combo.total})</i>"
    return " <i>(checked)</i>"


def format_verified(cfg, verified: dict) -> str:
    """Prices read off the rendered page, for the trips worth acting on.

    Kept as its own message because these numbers come from somewhere else and
    should not be quietly mixed in with the parsed ones: what a plain fetch
    sees is the airline's fare on a short list; this is what you would see.
    """
    lines = [("<b>Checked in a browser on your laptop</b>" if cfg.at_home
              else "<b>Checked in a browser on GitHub</b>"), ""]
    for found in sorted(verified.values(), key=lambda v: v["total"]):
        # Say so when part of the total is our own bag estimate rather than
        # something Google quoted, because the rows below will not add up.
        bags = found.get("bags") or 0
        tail = f" <i>(incl. S${bags} bags)</i>" if bags else ""
        lines.append(
            f"<b>{esc(found['label'])}</b> — <b>S${found['total']}</b>{tail}")
        for part in found["parts"]:
            head = f"{part['label']} " if part["label"] else ""
            lines.append(
                f"  <a href=\"{esc(part['url'])}\">{esc(head)}open</a>")
            for fare in part["fares"]:
                lines.append(f"    S${fare.price}  {esc(fare.summary)}")
        # One ticket: the return is chosen on the next screen, so it is only
        # knowable by going there. Without it you are told a price and half
        # a trip.
        for fare in (found.get("returns") or [])[:3]:
            lines.append(f"    back  S${fare.price}  {esc(fare.summary)}")
        # Two different trips on the same page, kept as different numbers.
        # The first passes your layover rules; the second is the cheapest
        # there is, whatever its connections. The second is shown only when
        # it actually undercuts the first, so a rule you have already set is
        # not argued with on every line - but a price the page plainly shows
        # is never hidden from you either.
        def elsewhere(entry, label):
            if not entry or entry["price"] >= found["total"]:
                return
            floor = "from " if entry.get("advertised") else ""
            lines.append(
                f"    {label}: {floor}<b>S${entry['price']}</b> — "
                f"{esc(entry['summary'][:110])}")

        other, cheapest = found.get("other"), found.get("floor")
        elsewhere(other, "also on this page")
        if not other or (cheapest and cheapest["price"] < other["price"]):
            elsewhere(cheapest, "cheapest on the page, rules aside")
        lines.append("")
    seen = ("Google's own Cheapest view as your laptop gets it, agency and "
            "two-ticket fares included — the number you see on your phone. "
            if cfg.at_home else
            "Google's own Cheapest view as GitHub's servers get it: the "
            "airlines' own fares only. Google shows agency and two-ticket "
            "fares to home and phone connections, not to data centres, so "
            "your phone may well see lower — the laptop checks cover that "
            "when it is on. ")
    lines.append(
        f"<i>{seen}Each price is read off the row for that "
        "exact flight, so the number and the itinerary beside it belong "
        "together, and the same checked-bag estimate is added as everywhere "
        "else — Google will not price a bag, and a total without one is not "
        "comparable to a total with one.</i>"
    )
    return "\n".join(lines).rstrip()


def format_survey(cfg, found) -> str:
    """The cheapest trip of every kind, from a run that opened every page.

    One line per kind - each city as a return trip, and each open jaw both
    ways round, as one ticket and as two - because "cheapest overall" hides
    exactly the comparison you asked to see: what flying into Beijing and home
    from Qingdao costs next to the plain return, on the same run, read the
    same way.
    """
    where = "on your laptop" if cfg.at_home else "on GitHub"
    minutes = max(1, round(found.seconds / 60))
    lines = [f"<b>Every search checked in a browser {where}</b>",
             f"<i>{found.pages} pages and {found.screens} return screens in "
             f"{minutes} min"
             + (" - stopped early: Google showed a bot check" if found.blocked
                else "") + "</i>", ""]

    def name(into, home):
        if into == home:
            return f"{cfg.city_name(into)} return"
        return f"{cfg.city_name(into)} in, {cfg.city_name(home)} out"

    best = found.best()
    order = sorted(best.items(),
                   key=lambda kv: (kv[0][0], kv[0][1] != kv[0][2], kv[1].price))
    heading = None
    for (tickets, into, home), combo in order:
        now = "One ticket" if tickets == 1 else "Two tickets"
        if now != heading:
            if heading is not None:
                lines.append("")
            lines.append(f"<b>{now}</b>")
            heading = now
        links = " · ".join(f'<a href="{esc(url)}">{esc(label)}</a>'
                           for label, url in combo.verified_urls)
        lines.append(
            f"<b>S${combo.price}</b> {esc(name(into, home))} · "
            f"{esc(present.day_label(combo.out.date))} → "
            f"{esc(present.day_label(combo.back_date))}  {links}")
        lines.append(f"  ✈ {leg_line(cfg, combo.out)}")
        if combo.back is not None:
            lines.append(f"  ↩ {leg_line(cfg, combo.back)}")
    if not best:
        lines.append("Nothing on any page passed your rules.")

    floor = found.floor()
    if floor is not None and (not best or floor.price < min(c.price for c in best.values())):
        lines.append("")
        lines.append(
            f"<i>Cheapest on any page with the layover rules off: S${floor.price} — "
            f"{esc(name(floor.out.search_to, floor.back_city))}, "
            f"{esc(present.day_label(floor.out.date))} → "
            f"{esc(present.day_label(floor.back_date))}, "
            f"{esc(floor.out.airline_label)}</i>")
    if found.failed:
        shown = ", ".join(found.failed[:6]) + ("…" if len(found.failed) > 6 else "")
        lines.append(f"<i>Could not read {len(found.failed)}: {esc(shown)}</i>")
    unread = getattr(found, "unread", [])
    if unread:
        shown = ", ".join(unread[:6]) + ("…" if len(unread) > 6 else "")
        lines.append(f"<i>Ran out of time before {len(unread)} more: {esc(shown)}"
                     " - the search feed's prices stand for those</i>")
    lines.append("")
    lines.append(
        "<i>" + ("Google's own Cheapest view as your laptop gets it, agency and "
                 "two-ticket fares included. " if cfg.at_home else
                 "Google's own Cheapest view as GitHub's servers get it: the "
                 "airlines' own fares only - your laptop's checks see the agency "
                 "ones. ")
        + "One-ticket prices are the return you would actually take: each "
        "outbound's return screen was opened and the cheapest flight home "
        "within your rules and deadline read off it. Checked bags added as "
        "everywhere else.</i>")
    return "\n".join(lines).rstrip()


def format_unrestricted(cfg, combos, limit: int) -> str:
    """The cheapest trips with the connection rules switched off.

    Same dates, same one-stop cap, same checked bag priced in — only the
    quick-or-worth-leaving-the-airport rules are lifted. So this is the floor
    for the trip you asked for, and the gap to the list above is what insisting
    on decent connections is costing you.
    """
    lines = [f"<b>Cheapest {min(limit, len(combos))} ignoring transit rules</b>", ""]
    for combo in combos[:limit]:
        landed = cfg.city_name(combo.out.search_to)
        home_from = cfg.city_name(combo.back_city)
        where = landed if combo.back_city == combo.out.search_to \
            else f"{landed} → {home_from}"
        label, url = combo.booking_urls(cfg)[0]
        passes = " · would pass the rules anyway" if combo.comfortable else ""
        lines.append(
            f"<b>S${combo.price}</b>{_checked(combo)} — {esc(where)}<i>{esc(passes)}</i>  "
            f'<a href="{esc(url)}">{esc(label)}</a>'
        )
        lines.append(f"  ✈ {leg_line(cfg, combo.out)}")
        if combo.back is not None:
            lines.append(f"  ↩ {leg_line(cfg, combo.back)}")
        else:
            lines.append(
                f"  ↩ {esc(home_from)} → {esc(cfg.city_name(cfg.origin))}  "
                f"{esc(present.day_label(combo.back_date))}"
                " <i>(return times not pinned — check arrival)</i>"
            )
        lines.append(f"  <i>{combo.nights} nights · {esc(combo.source)}</i>")
        lines.append("")
    lines.append(
        "<i>Your dates, your 1-stop cap and the checked bag all still apply. "
        "Only the layover rules are off, so expect dead waits and overnights "
        "in terminals — check the times before getting excited.</i>"
    )
    return "\n".join(lines).rstrip()


def format_deals(cfg, items, heading: str) -> str:
    """Render (trip, alternatives) pairs as produced by present.prepare."""
    lines = [f"<b>{esc(heading)}</b>", ""]
    for combo, alternatives in items:
        landed = cfg.city_name(combo.out.search_to)
        home_from = cfg.city_name(combo.back_city)
        open_jaw = combo.back_city != combo.out.search_to

        where, tag = landed, ""
        if open_jaw:
            where = f"{landed} → {home_from}"
            shared = cfg.rail_groups_of(combo.out.search_to) & cfg.rail_groups_of(
                combo.back_city
            )
            group = next(iter(sorted(shared)), "").replace("_", " ")
            tag = f" · train across {group}" if group else ""

        links = " · ".join(
            f'<a href="{esc(url)}">{esc(label)}</a>'
            for label, url in combo.booking_urls(cfg)
        )
        lines.append(f"<b>S${combo.price}</b>{_checked(combo)} — {esc(where)}{tag}  {links}")
        lines.append(f"  ✈ {leg_line(cfg, combo.out)}")
        if combo.back is not None:
            lines.append(f"  ↩ {leg_line(cfg, combo.back)}")
        else:
            lines.append(
                f"  ↩ {esc(home_from)} → {esc(cfg.city_name(cfg.origin))}  "
                f"{esc(present.day_label(combo.back_date))}"
                " <i>(return times not pinned — check arrival)</i>"
            )
        lines.append(f"  <i>{combo.nights} nights · {esc(combo.source)}</i>")

        if alternatives:
            shown = [present.alternative_label(combo, a) for a in alternatives[:4]]
            more = len(alternatives) - len(shown)
            also = "; ".join(shown) + (f"; +{more} more" if more > 0 else "")
            lines.append(f"  <i>also: {esc(also)}</i>")

        for group_name, group_links in compare.for_combo(combo, cfg.adults):
            joined = " . ".join(
                f'<a href="{esc(url)}">{esc(name)}</a>' for name, url in group_links
            )
            lines.append(f"  <i>{esc(group_name)}:</i> {joined}")
        lines.append("")

    if any(c.source == "one-way pair" for c, _ in items):
        lines.append(
            "<i>“one-way pair” means two separate tickets — cheapest on low-cost "
            "carriers, but a delay on one leg is not protected by the other.</i>"
        )
    if any(c.source == "multi-city" for c, _ in items):
        lines.append(
            "<i>“multi-city” is one ticket: into one city, home from the other.</i>"
        )
    # Whose money, and how many seats. A total for two beside a budget for
    # one reads as a bargain, so both are said out loud.
    budget = ("No budget set - watching for new lows." if cfg.max_total is None
              else f"Budget S${cfg.max_total}.")
    seats = "" if cfg.adults == 1 else f" Prices are for {cfg.adults} adults."
    where = (
        "Checked from your laptop: prices marked checked were read off "
        "Google's page with agency fares included; the rest are the airline's "
        "own fare from the search feed, so treat those as a ceiling."
        if cfg.at_home else
        "Checked from GitHub, which Google only ever shows the airlines' own "
        "fares - a phone or laptop in Singapore also gets agency and "
        "two-ticket fares, often lower. Treat every number here as a ceiling "
        "and open the flight before judging it."
    )
    lines.append(
        f"<i>Totals include a checked bag: free on carriers that bundle one, "
        f"otherwise our own estimate (Google will not price it). "
        f"{budget}{seats} {where}</i>"
    )
    return "\n".join(lines).rstrip()
