"""Deep links to the booking sites Google Flights does not price.

Google shows airline fares plus the agencies that feed it. In this region the
ones that often undercut it - Trip.com above all on Chinese carriers - either
are not in that feed or are in it at their non-promo price. None of these sites
expose voucher, card or cashback pricing to a search either; that only appears
at checkout. So the job here is not to price them, it is to put you one tap away
from checking them on the exact route and dates the watcher found.

Every URL below is best-effort: booking sites change their search formats
without warning. A stale link lands you on the site's own search page rather
than breaking the alert.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime


def _d(date: str) -> datetime:
    return datetime.strptime(date, "%Y-%m-%d")


def trip(frm: str, to: str, out: str, back: str | None = None,
         locale: str = "en-SG", curr: str = "SGD") -> str:
    params = {
        "dcity": frm.lower(), "acity": to.lower(), "ddate": out,
        "triptype": "rt" if back else "ow", "class": "y", "quantity": 1,
        "locale": locale, "curr": curr,
    }
    if back:
        params["rdate"] = back
    return "https://www.trip.com/flights/showfarefirst?" + urllib.parse.urlencode(params)


def traveloka(frm: str, to: str, out: str, back: str | None = None) -> str:
    dates = f"{_d(out):%d-%m-%Y}." + (f"{_d(back):%d-%m-%Y}" if back else "NA")
    params = {"ap": f"{frm}.{to}", "dt": dates, "ps": "1.0.0", "sc": "ECONOMY"}
    return ("https://www.traveloka.com/en-sg/flight/fullsearch?"
            + urllib.parse.urlencode(params))


def kiwi(frm: str, to: str, out: str, back: str | None = None) -> str:
    tail = f"/{back}" if back else ""
    return f"https://www.kiwi.com/en/search/results/{frm}/{to}/{out}{tail}"


def skyscanner(frm: str, to: str, out: str, back: str | None = None) -> str:
    legs = f"{frm.lower()}/{to.lower()}/{_d(out):%y%m%d}"
    if back:
        legs += f"/{_d(back):%y%m%d}"
    return (f"https://www.skyscanner.com.sg/transport/flights/{legs}/"
            "?adultsv2=1&cabinclass=economy")


def for_combo(combo) -> list[tuple[str, list[tuple[str, str]]]]:
    """Comparison links for one trip, grouped for display.

    A round-trip quote is one ticket, so it gets one set of round-trip links.
    A one-way pair is two tickets bought separately, so each leg gets its own
    set - a round-trip search on those sites would price a different product.
    """
    if combo.back is None:
        frm, to = combo.out.search_from, combo.out.search_to
        out, back = combo.out.date, combo.back_date
        return [(
            "also check",
            [
                ("Trip", trip(frm, to, out, back)),
                ("Trip CN", trip(frm, to, out, back, locale="zh-CN", curr="CNY")),
                ("Traveloka", traveloka(frm, to, out, back)),
                ("Kiwi", kiwi(frm, to, out, back)),
                ("Skyscanner", skyscanner(frm, to, out, back)),
            ],
        )]

    groups = []
    for label, leg in (("out", combo.out), ("back", combo.back)):
        frm, to, date = leg.from_airport, leg.to_airport, leg.date
        groups.append((
            f"also check {label}",
            [
                ("Trip", trip(frm, to, date)),
                ("Trip CN", trip(frm, to, date, locale="zh-CN", curr="CNY")),
                ("Kiwi", kiwi(frm, to, date)),
                ("Skyscanner", skyscanner(frm, to, date)),
            ],
        ))
    return groups
