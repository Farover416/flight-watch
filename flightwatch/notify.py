"""Telegram delivery."""

from __future__ import annotations

import html
import json
import os
import urllib.error
import urllib.parse
import urllib.request
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
    else:
        stops = "direct" if leg.stops == 0 else f"{leg.stops} stop"
    return (
        f"{esc(cfg.city_name(leg.from_airport))} → {esc(cfg.city_name(leg.to_airport))}"
        f"  {leg.depart:%d %b %H:%M} → {leg.arrive:%d %b %H:%M}"
        f"  ({esc(stops)}, {esc(leg.airline_label)})"
    )


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
        lines.append(f"<b>S${combo.total}</b> — {esc(where)}{tag}  {links}")
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

        for group_name, group_links in compare.for_combo(combo):
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
    lines.append(
        f"<i>Prices include an estimated carry-on bag; budget S${cfg.max_total}. "
        "Google's price is a ceiling - vouchers, card promos and cashback only "
        "show at checkout, so the comparison links are worth the minute.</i>"
    )
    return "\n".join(lines).rstrip()
