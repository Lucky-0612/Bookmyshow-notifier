"""
show_notifier.py

Checks one or more public BookMyShow movie listing pages for showtimes at a
chosen cinema (matched by a keyword) and emails you when NEW showtimes
appear that weren't there the last time you ran this script.

Two ways to use it:

1) Config-driven (recommended for ongoing tracking):
   Add entries to the `watches` list in config.yaml, then just run:
       python show_notifier.py

2) Ad-hoc (quick one-off check against any URL):
       python show_notifier.py --url "<listing url>" --keyword "Allu" --name "quick check"
   This does NOT need to be in config.yaml. Useful for testing a URL before
   deciding whether to add it as a permanent watch.

This only reads public listing pages - no login, no seat-level scraping,
nothing that touches BookMyShow's booking/session APIs.

NOTE ON FETCHING: BookMyShow sits behind bot-protection (Cloudflare/Akamai)
that runs a JavaScript challenge before it will serve the real page. A plain
HTTP client (like `requests`) can never pass that, no matter the headers -
it doesn't execute JS. So this script uses Playwright to drive a real
headless Chromium browser instead, which renders the page (and the
challenge) exactly like a normal visitor's browser would.
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText

import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def load_config(path="config.yaml"):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def fetch_page(page, url, keyword=None, retries=2):
    """
    Load `url` in the given Playwright page (a real headless Chromium tab)
    and return the fully-rendered HTML.

    The showtimes on BookMyShow's page load via a separate API call *after*
    the initial page render, not in the first HTML response - so instead of
    a fixed sleep (unreliable: too short on a slow CI runner, wasteful if
    the data arrives fast), this polls the rendered DOM every second, up to
    ~20s, until either the target keyword or a HH:MM time pattern shows up.

    Retries once on failure/timeout - the bot-protection challenge
    occasionally needs a beat, and a second navigation after the first
    one's cookies are set tends to sail through.
    """
    time_re = re.compile(r"\d{1,2}:\d{2}\s?(?:AM|PM|am|pm)")
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until="load", timeout=30000)

            content = page.content()
            title = (page.title() or "").lower()
            if "just a moment" in title or "attention required" in content.lower():
                # Still on the Cloudflare interstitial - give it a beat.
                page.wait_for_timeout(5000)

            # Poll for the actual showtime content to show up client-side.
            deadline = time.time() + 20
            while time.time() < deadline:
                content = page.content()
                has_keyword = (not keyword) or (keyword.lower() in content.lower())
                has_times = bool(time_re.search(content))
                if has_keyword and has_times:
                    break
                page.wait_for_timeout(1000)

            return content
        except PlaywrightTimeoutError as e:
            last_error = e
            continue

    raise RuntimeError(
        f"Could not load {url} after {retries} tries (page kept timing out). "
        "If this keeps happening, BookMyShow may be blocking this specific "
        "IP/environment outright (common for cloud CI runners like GitHub "
        "Actions) rather than just challenging the browser - in that case "
        "try running this from a home connection instead."
    ) from last_error


def classify_status(color):
    """
    Classify a CSS color (e.g. 'rgb(241, 177, 3)') into a booking status
    using hue, so exact color values can drift without breaking this:
      - low saturation (grayscale)      -> SOLD_OUT
      - reddish hue                     -> SOLD_OUT
      - yellow/orange hue               -> FAST_FILLING
      - green hue                       -> AVAILABLE
      - anything else recognizable      -> AVAILABLE (assume open; better to
                                            over-notify than silently miss it)
    Returns "UNKNOWN" if the color string can't be parsed at all.
    """
    if not color:
        return "UNKNOWN"

    m = re.search(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", color)
    if not m:
        return "UNKNOWN"

    r, g, b = (int(x) for x in m.groups())
    mx, mn = max(r, g, b), min(r, g, b)
    delta = mx - mn

    if mx == 0 or delta / mx < 0.15:
        return "SOLD_OUT"  # grayscale-ish -> treated as closed/unavailable

    if mx == r:
        hue = 60 * (((g - b) / delta) % 6)
    elif mx == g:
        hue = 60 * (((b - r) / delta) + 2)
    else:
        hue = 60 * (((r - g) / delta) + 4)

    if hue < 20 or hue >= 340:
        return "SOLD_OUT"       # reddish
    if 20 <= hue < 70:
        return "FAST_FILLING"   # yellow/orange
    if 70 <= hue < 170:
        return "AVAILABLE"      # green
    return "AVAILABLE"           # unrecognized hue - assume open


# JS run inside the page itself. Rather than trying to reverse-engineer
# where BookMyShow's CSS actually lives (inline <style>, an external
# stylesheet file, CSS-in-JS runtime injection - all of these are possible
# and fragile to special-case), this asks the browser directly what color
# it actually painted each showtime button's border, via getComputedStyle.
# That's guaranteed correct regardless of the underlying CSS delivery
# mechanism, since it's the same thing the browser itself uses to render.
_EXTRACT_JS = r"""
(needle) => {
  const timeRe = /\d{1,2}:\d{2}\s?(AM|PM|am|pm)/;
  const results = {};

  function addShow(label, btn) {
    const aria = btn.getAttribute('aria-label') || '';
    const text = btn.textContent || '';
    const m = timeRe.exec(aria) || timeRe.exec(text);
    if (!m) return;
    const showTime = m[0].toUpperCase().trim();
    const style = getComputedStyle(btn);
    const color = style.borderColor || style.borderTopColor || '';
    if (!results[label]) results[label] = {};
    results[label][showTime] = color;
  }

  if (needle) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const seenContainers = new Set();
    let node;
    while ((node = walker.nextNode())) {
      if (!node.nodeValue || !node.nodeValue.toLowerCase().includes(needle.toLowerCase())) {
        continue;
      }
      // Climb until we find the smallest ancestor that actually contains a
      // showtime button - this naturally stops at the right card boundary
      // no matter how deeply the real page nests things, instead of
      // guessing a fixed number of hops (which can overshoot into a
      // sibling cinema's card, or undershoot and find nothing).
      let container = node.parentElement;
      while (container && !container.querySelector('[role="button"]')) {
        container = container.parentElement;
      }
      if (!container || seenContainers.has(container)) continue;
      seenContainers.add(container);
      const buttons = container.querySelectorAll('[role="button"]');
      for (const btn of buttons) addShow(needle, btn);
    }
  } else {
    const buttons = document.querySelectorAll('[role="button"]');
    for (const btn of buttons) addShow('ANY_CINEMA', btn);
  }

  return results;
}
"""


def extract_cinema_shows(page, html, keyword):
    """
    Extract showtimes (with booking status) for cinemas whose name contains
    `keyword` (or ALL cinemas, if keyword is empty), by querying the live
    Playwright page for each showtime button's real computed border color.

    Returns: {cinema_name_or_label: {time_string: status_string}}
    where status_string is one of AVAILABLE / FAST_FILLING / SOLD_OUT /
    UNKNOWN (UNKNOWN means a color came back that couldn't be classified -
    treated as "not open" so it won't trigger alerts on its own, but is
    still recorded so you can sanity-check the logs).

    Falls back to a plain regex scan of the static HTML (status UNKNOWN
    for everything) only if the live query finds no showtime buttons at
    all near the keyword - keeping the watch from going silently empty if
    BookMyShow's markup changes in a way that breaks the button lookup.
    """
    raw = page.evaluate(_EXTRACT_JS, keyword or "")
    results = {label: {t: classify_status(c) for t, c in shows.items()}
               for label, shows in raw.items()}

    if not results and keyword:
        soup = BeautifulSoup(html, "html.parser")
        time_re = re.compile(r"\d{1,2}:\d{2}\s?(?:AM|PM|am|pm)")
        found = {}
        for tag in soup.find_all(string=re.compile(re.escape(keyword), re.IGNORECASE)):
            container = tag.find_parent()
            if not container:
                continue
            for _ in range(3):
                if container.find_parent():
                    container = container.find_parent()
            block_text = container.get_text(" ", strip=True)
            for t in time_re.findall(block_text):
                found.setdefault(t.upper(), "UNKNOWN")
        if found:
            results[keyword] = found

    return results


def state_filename_for(name):
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip().lower())
    return f"state_{safe}.json"


def load_state(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_email(cfg, subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = cfg["gmail_address"]
    msg["To"] = cfg["notify_to"]

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(cfg["gmail_address"], cfg["gmail_app_password"])
        server.sendmail(cfg["gmail_address"], [cfg["notify_to"]], msg.as_string())


OPEN_STATUSES = {"AVAILABLE", "FAST_FILLING"}


def check_one(cfg, page, name, url, keyword, send_email_on_new=True):
    state_path = state_filename_for(name)
    print(f"\n[{name}] Checking {url} for showtimes"
          + (f" containing '{keyword}'..." if keyword else " (any cinema)..."))

    try:
        html = fetch_page(page, url, keyword=keyword)
    except RuntimeError as e:
        print(f"[{name}] Could not fetch the page: {e}")
        return

    current = extract_cinema_shows(page, html, keyword)
    previous = load_state(state_path)

    if not current:
        print(f"[{name}] No matching shows found on the page yet. Nothing to report."
              f" (page length: {len(html)} chars, "
              f"'{keyword}' present: {keyword.lower() in html.lower() if keyword else 'n/a'})")
        return

    # Log what was actually seen, so a run can be sanity-checked against
    # what's visible in a browser (useful while the color classification
    # is still being verified against real data).
    any_unknown = False
    for cinema, shows in current.items():
        summary = ", ".join(f"{t}={status}" for t, status in sorted(shows.items()))
        print(f"[{name}] {cinema}: {summary}")
        if any(status == "UNKNOWN" for status in shows.values()):
            any_unknown = True

    if any_unknown:
        try:
            total_buttons = page.evaluate(
                "document.querySelectorAll('[role=\"button\"]').length"
            )
        except Exception:
            total_buttons = "?"
        print(f"[{name}] Diagnostic: {total_buttons} [role=\"button\"] elements "
              f"found on the page in total (helps tell whether the button "
              f"selector itself is stale vs. just this cinema's colors).")

    newly_open = {}
    for cinema, shows in current.items():
        prev_shows = previous.get(cinema, {})
        if isinstance(prev_shows, list):
            # Migrate old state format (a plain list of time strings, from
            # before status tracking existed) - treat those as already-seen
            # so we don't immediately re-alert on every one of them.
            prev_shows = {t: "AVAILABLE" for t in prev_shows}

        added = {}
        for show_time, status in shows.items():
            prev_status = prev_shows.get(show_time)
            was_open = prev_status in OPEN_STATUSES
            is_open_now = status in OPEN_STATUSES
            if is_open_now and not was_open:
                added[show_time] = status
        if added:
            newly_open[cinema] = added

    if newly_open:
        lines = [
            f"{cinema}: " + ", ".join(f"{t} ({status})" for t, status in sorted(shows.items()))
            for cinema, shows in newly_open.items()
        ]
        body = (
            f"Seats just opened up for '{name}':\n\n"
            + "\n".join(lines)
            + f"\n\nCheck and book: {url}"
        )
        print(f"[{name}] Newly open showtime(s) found!")
        if send_email_on_new:
            send_email(cfg, f"Seats available: {name}", body)
            print(f"[{name}] Email sent.")
    else:
        print(f"[{name}] Matching cinema(s) found, but nothing newly open since last check.")

    save_state(state_path, current)


def expand_watch(w):
    """
    Turn one config 'watches' entry into a list of (name, url, keyword) to check.

    Two supported entry shapes:
    1) Plain, single-date entry:
         - name: "..."
           url: "https://.../20260805?..."
           cinema_keyword: "Allu"
       -> returns exactly that one (name, url, keyword)

    2) Auto date-range entry (checks several upcoming dates automatically):
         - name: "Spiderman - Allu Cinemas"
           url_template: "https://in.bookmyshow.com/movies/hyderabad/spider-man-brand-new-day/buytickets/ET00502689/{date}"
           cinema_keyword: "Allu"
           date_range_days: 10       # how many days ahead to check
           start_date: "20260805"    # optional, YYYYMMDD. Defaults to today
                                     # if omitted.
       -> generates one (name, url, keyword) per day starting from
          start_date (or today), with {date} replaced by YYYYMMDD, and name
          suffixed with that date so each gets its own saved state file.
    """
    keyword = w.get("cinema_keyword", "")

    if "url_template" in w:
        days = int(w.get("date_range_days", 7))
        start_date_str = w.get("start_date")
        start = (
            datetime.strptime(str(start_date_str), "%Y%m%d").date()
            if start_date_str else date.today()
        )
        out = []
        for i in range(days):
            d = start + timedelta(days=i)
            date_str = d.strftime("%Y%m%d")
            url = w["url_template"].replace("{date}", date_str)
            name = f'{w["name"]} - {d.strftime("%d %b")}'
            out.append((name, url, keyword))
        return out

    # plain single entry
    return [(w["name"], w["url"], keyword)]


def main():
    parser = argparse.ArgumentParser(description="Check BookMyShow listing pages for new showtimes.")
    parser.add_argument("--url", help="Ad-hoc listing page URL to check (overrides config watchlist)")
    parser.add_argument("--keyword", default="", help="Cinema name substring to match (blank = any cinema)")
    parser.add_argument("--name", default="ad-hoc check", help="Label for this ad-hoc check")
    args = parser.parse_args()

    cfg = load_config()

    # Override config.yaml values with GitHub Secrets (env vars), if present
    cfg["gmail_address"] = os.getenv("GMAIL_ADDRESS", cfg.get("gmail_address", ""))
    cfg["gmail_app_password"] = os.getenv("GMAIL_APP_PASSWORD", cfg.get("gmail_app_password", ""))
    cfg["notify_to"] = os.getenv("NOTIFY_TO", cfg.get("notify_to", ""))

    if not cfg.get("gmail_address") or not cfg.get("gmail_app_password"):
        print("config.yaml is missing gmail_address / gmail_app_password. "
              "Fill those in before running (see README).")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
        )
        page = context.new_page()

        try:
            if args.url:
                # Ad-hoc single check, ignores the watches list in config.yaml
                check_one(cfg, page, args.name, args.url, args.keyword)
                return

            watches = cfg.get("watches") or []
            if not watches:
                print("No watches configured. Add entries under 'watches:' in config.yaml, "
                      "or run with --url to do a one-off check.")
                return

            for w in watches:
                for name, url, keyword in expand_watch(w):
                    check_one(cfg, page, name, url, keyword)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()