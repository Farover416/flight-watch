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
         locale: str = "en-SG", curr: str = "SGD", adults: int = 1) -> str:
    params = {
        "dcity": frm.lower(), "acity": to.lower(), "ddate": out,
        "triptype": "rt" if back else "ow", "class": "y", "quantity": adults,
        "locale": locale, "curr": curr,
    }
    if back:
        params["rdate"] = back
    return "https://www.trip.com/flights/showfarefirst?" + urllib.parse.urlencode(params)


def traveloka(frm: str, to: str, out: str, back: str | None = None,
              adults: int = 1) -> str:
    dates = f"{_d(out):%d-%m-%Y}." + (f"{_d(back):%d-%m-%Y}" if back else "NA")
    # ps is adults.children.infants
    params = {"ap": f"{frm}.{to}", "dt": dates, "ps": f"{adults}.0.0",
              "sc": "ECONOMY"}
    return ("https://www.traveloka.com/en-sg/flight/fullsearch?"
            + urllib.parse.urlencode(params))


def kiwi(frm: str, to: str, out: str, back: str | None = None,
         adults: int = 1) -> str:
    tail = f"/{back}" if back else ""
    seats = f"?adults={adults}" if adults != 1 else ""
    return f"https://www.kiwi.com/en/search/results/{frm}/{to}/{out}{tail}{seats}"


def skyscanner(frm: str, to: str, out: str, back: str | None = None,
               adults: int = 1) -> str:
    legs = f"{frm.lower()}/{to.lower()}/{_d(out):%y%m%d}"
    if back:
        legs += f"/{_d(back):%y%m%d}"
    return (f"https://www.skyscanner.com.sg/transport/flights/{legs}/"
            f"?adultsv2={adults}&cabinclass=economy")


def for_combo(combo, adults: int = 1) -> list[tuple[str, list[tuple[str, str]]]]:
    """Comparison links for one trip, grouped for display.

    ``adults`` has to be carried through: a link that quietly prices one seat
    beside a total for two is worse than no link, because it looks like the
    watcher found something cheaper than it did.

    A round-trip quote is one ticket, so it gets one set of round-trip links.
    A one-way pair is two tickets bought separately, so each leg gets its own
    set - a round-trip search on those sites would price a different product.
    """
    if combo.back is None or combo.source == "round-trip":
        frm, to = combo.out.search_from, combo.out.search_to
        out, back = combo.out.date, combo.back_date
        return [(
            "also check",
            [
                ("Trip", trip(frm, to, out, back, adults=adults)),
                ("Trip CN", trip(frm, to, out, back, locale="zh-CN",
                                 curr="CNY", adults=adults)),
                ("Traveloka", traveloka(frm, to, out, back, adults=adults)),
                ("Kiwi", kiwi(frm, to, out, back, adults=adults)),
                ("Skyscanner", skyscanner(frm, to, out, back, adults=adults)),
            ],
        )]

    groups = []
    for label, leg in (("out", combo.out), ("back", combo.back)):
        frm, to, date = leg.from_airport, leg.to_airport, leg.date
        groups.append((
            f"also check {label}",
            [
                ("Trip", trip(frm, to, date, adults=adults)),
                ("Trip CN", trip(frm, to, date, locale="zh-CN", curr="CNY",
                                 adults=adults)),
                ("Kiwi", kiwi(frm, to, date, adults=adults)),
                ("Skyscanner", skyscanner(frm, to, date, adults=adults)),
            ],
        ))
    return groups
