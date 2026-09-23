"""Which of GitHub's machines is shown the fares a person in Singapore sees?

A one-off test, started by hand from the Actions tab ("fare probe"). It opens
the Qingdao 19-28 Dec search in several browser setups and says which of them
got the full list. On this search a phone or laptop in Singapore is shown
S$497, S$606 and S$627 plus self-transfer and two-ticket fares, while the
watcher on GitHub is shown only the airline fares, from S$749.

It saves nothing and changes nothing; the answer is in the job's log.
"""

import platform
import re
import sys
import time

from playwright.sync_api import sync_playwright

SEARCH = ("https://www.google.com/travel/flights?tfs=GhwSCjIwMjYtMTItMTkoAWoFEgNTSU5y"
          "BRIDVEFPGhwSCjIwMjYtMTItMjgoAWoFEgNUQU9yBRIDU0lOQgEBSAFqBBABGACYAQE%3D"
          "&hl=en-US&curr=SGD&gl=SG")

# A small page on the same site, only to read the browser's client hints.
IDENTITY_PAGE = "https://www.google.com/robots.txt"

# The airline-only list's cheapest, and the flights (by departure time) that
# only the full list had under it: S$497, S$606 and S$627 on 23 Sep.
AIRLINE_ONLY_CHEAPEST = 749
THEIRS = ("8:20 PM", "5:45 PM", "9:00 PM")
# Rows only the full list carries.
FULL_LIST_ROWS = re.compile(r"self transfer|separate tickets", re.I)

SG = "Asia/Singapore"
SETUPS = {
    "Linux": [
        ("chrome, hidden, UTC clock (the watcher now)", "chrome", True, "UTC"),
        ("chrome, hidden, Singapore clock", "chrome", True, SG),
        ("chrome, virtual screen, Singapore clock", "chrome", False, SG),
    ],
    "Windows": [
        ("chrome, hidden, UTC clock (the watcher's setup)", "chrome", True, "UTC"),
        ("chrome, hidden, Singapore clock", "chrome", True, SG),
        ("edge, hidden, Singapore clock", "msedge", True, SG),
        ("edge, window, Singapore clock", "msedge", False, SG),
        ("chrome, window, Singapore clock", "chrome", False, SG),
    ],
}

LOOK_JS = """() => {
  const rows = Array.from(new Set(Array.from(document.querySelectorAll('li'))
    .map(li => (li.innerText || '').replace(/\\s+/g, ' ').trim())
    .filter(t => /SGD\\s?\\d/.test(t) && /\\d{1,2}:\\d{2}\\s?(AM|PM)/.test(t)
              && t.length < 400)));
  const shown = (el) => {
    if (el.closest('[aria-hidden="true"]')) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
    const b = el.getBoundingClientRect();
    return b.width > 0 && b.height > 0;
  };
  const bar = Array.from(document.querySelectorAll('[role="progressbar"]')).some(shown);
  const tab = Array.from(document.querySelectorAll('[role="tab"]'))
    .find(b => /cheapest/i.test(b.textContent || ''));
  const said = tab ? (tab.innerText || '').replace(/\\s+/g, ' ').trim() : 'no Cheapest tab';
  return {rows: rows, busy: bar || /fetching/i.test(said), said: said};
}"""

CHEAPEST_JS = """() => {
  const tab = Array.from(document.querySelectorAll('[role="tab"]'))
    .find(b => /cheapest/i.test(b.textContent || ''));
  if (tab && tab.getAttribute('aria-selected') !== 'true') tab.click();
}"""

WHO_JS = """() => ({
  brands: (navigator.userAgentData && navigator.userAgentData.brands || [])
            .map(b => b.brand + ' ' + b.version).join(', '),
  agent: navigator.userAgent,
  webdriver: navigator.webdriver,
  clock: Intl.DateTimeFormat().resolvedOptions().timeZone,
})"""

HINTS_JS = """async () => navigator.userAgentData
  ? await navigator.userAgentData.getHighEntropyValues(['architecture',
      'bitness', 'model', 'platformVersion', 'fullVersionList', 'uaFullVersion'])
  : null"""


def price(text):
    found = re.search(r"SGD\s?([\d,]+)", text)
    return int(found.group(1).replace(",", "")) if found else 10**9


def first_line(exc):
    return (str(exc).strip().splitlines() or ["?"])[0][:160]


def settle(page, budget_s, quiet_s=4):
    """The page once nothing has changed for a while and nothing is loading."""
    deadline, last, since = time.time() + budget_s, None, time.time()
    snap = {"rows": [], "busy": True, "said": ""}
    while time.time() < deadline:
        snap = page.evaluate(LOOK_JS)
        mark = (tuple(sorted(price(r) for r in snap["rows"])), snap["busy"], snap["said"])
        if mark != last:
            last, since = mark, time.time()
        elif snap["rows"] and not snap["busy"] and time.time() - since > quiet_s:
            return snap, True
        page.wait_for_timeout(500)
    return snap, False


def plain_name(context, page):
    """What the watcher does: drop "Headless" from the name and client hints."""
    probe = context.new_page()
    try:
        probe.goto(IDENTITY_PAGE, timeout=20_000)
        hints = probe.evaluate(HINTS_JS)
    except Exception as exc:
        print(f"   (could not read client hints: {first_line(exc)})")
        return
    finally:
        probe.close()
    if not hints:
        return

    def plain(brands):
        return [{"brand": b["brand"].replace("Headless", ""), "version": b["version"]}
                for b in brands or []]

    agent = page.evaluate("navigator.userAgent")
    context.new_cdp_session(page).send("Emulation.setUserAgentOverride", {
        "userAgent": agent.replace("Headless", ""),
        "userAgentMetadata": {
            "brands": plain(hints.get("brands")),
            "fullVersionList": plain(hints.get("fullVersionList")),
            "fullVersion": hints.get("uaFullVersion", ""),
            "platform": hints.get("platform", ""),
            "platformVersion": hints.get("platformVersion", ""),
            "architecture": hints.get("architecture", ""),
            "model": hints.get("model", ""),
            "mobile": bool(hints.get("mobile")),
            "bitness": hints.get("bitness", ""),
        },
    })


def look(pw, name, channel, headless, clock):
    """Open the search once. Returns True for the full list, False if not, None if unread."""
    try:
        browser = pw.chromium.launch(
            channel=channel, headless=headless,
            args=["--disable-blink-features=AutomationControlled"])
    except Exception as exc:
        print(f"\n[{name}] would not start: {first_line(exc)}")
        return None
    started = time.time()
    try:
        context = browser.new_context(locale="en-SG", timezone_id=clock,
                                      viewport={"width": 1400, "height": 1000})
        page = context.new_page()
        if headless:
            plain_name(context, page)
        page.goto(SEARCH, wait_until="domcontentloaded", timeout=60_000)
        settle(page, 45)
        page.evaluate(CHEAPEST_JS)
        page.wait_for_timeout(1500)
        snap, settled = settle(page, 45)
        who = page.evaluate(WHO_JS)
        body = (page.inner_text("body") or "").lower()
    except Exception as exc:
        print(f"\n[{name}] could not read the page: {first_line(exc)}")
        return None
    finally:
        browser.close()

    rows = sorted(snap["rows"], key=price)
    full = any(FULL_LIST_ROWS.search(r) for r in rows)
    theirs = {t: next((price(r) for r in rows if r.startswith(t)), None) for t in THEIRS}
    cheap = sum(1 for p in theirs.values() if p and p < AIRLINE_ONLY_CHEAPEST)
    got = full or cheap >= 2

    print(f"\n[{name}] {len(rows)} fares after {time.time() - started:.0f}s"
          f"{'' if settled else ' (still changing when time ran out)'}")
    print(f"   Google says: {snap['said']}")
    print(f"   browser: {who['brands'] or who['agent']} | webdriver={who['webdriver']}"
          f" | clock {who['clock']}")
    if any(w in body for w in ("unusual traffic", "captcha", "before you continue")):
        print("   Google showed a bot check or consent page, not results")
    for row in rows[:6]:
        print(f"   S${price(row):<6} {row[:100]}")
    shown = ", ".join(f"{t} S${p}" if p else f"{t} not shown" for t, p in theirs.items())
    print(f"   -> {'FULL LIST' if got else 'airline-only list'} ({shown}; "
          f"{'has' if full else 'no'} self-transfer or two-ticket rows)")
    return got


def main():
    system = platform.system()
    print("machine:", platform.platform(), "| python", sys.version.split()[0])
    setups = SETUPS.get(system)
    if not setups:
        print("no setups for", system)
        return
    results = {}
    with sync_playwright() as pw:
        for name, channel, headless, clock in setups:
            results[name] = look(pw, name, channel, headless, clock)

    print(f"\n=== RESULT on GitHub's {system} machine ===")
    for name, got in results.items():
        state = "could not read" if got is None else "FULL LIST" if got else "airline-only"
        print(f"  {name:<50} {state}")


if __name__ == "__main__":
    main()
