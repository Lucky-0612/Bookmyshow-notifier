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
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import date, timedelta
from email.mime.text import MIMEText
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup

# A fuller, more realistic browser header set. A bare User-Agent is one of
# the first things bot-protection (Cloudflare/Akamai, which BookMyShow
# uses) checks for - real browsers always send Accept, Accept-Language,
# Accept-Encoding, sec-ch-ua, etc. alongside it.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# Reused across requests so cookies (incl. any Cloudflare clearance cookie)
# persist between the warm-up request and the real one.
_session = requests.Session()
_session.headers.update(HEADERS)


def load_config(path="config.yaml"):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def fetch_page(url, retries=3):
    """
    Fetch a page like a real browser would:
      1. Visit the site's homepage first (so cookies/session state exist
         and the Referer on the next request is legitimate), then
      2. Request the actual URL with that Referer set.
    Retries with backoff on 403s, since bot-protection sometimes lets a
    request through on a second/third try once cookies are set.
    """
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            if attempt == 1:
                # Warm-up request: establishes cookies and is a normal
                # thing for a real visitor to have done already.
                _session.get(origin, timeout=20)

            resp = _session.get(
                url,
                headers={"Referer": origin + "/"},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.text
        except requests.HTTPError as e:
            last_error = e
            status = e.response.status_code if e.response is not None else None
            if status == 403 and attempt < retries:
                time.sleep(2 * attempt)  # back off and try again
                continue
            if status == 403:
                break  # fall through to the RuntimeError below
            raise
        except requests.RequestException as e:
            last_error = e
            raise

    # All retries exhausted on 403
    raise RuntimeError(
        f"Still getting 403 Forbidden after {retries} tries for {url}. "
        "This means BookMyShow's bot-protection (Cloudflare/Akamai) is "
        "blocking plain HTTP requests outright - it can detect that this "
        "isn't a real browser regardless of headers. A plain `requests` "
        "script generally cannot get past this. Options: (1) run this from "
        "a residential/home IP rather than a cloud CI runner - GitHub "
        "Actions IPs are commonly blocked outright, or (2) switch to a "
        "headless-browser approach (e.g. Playwright) that actually renders "
        "the page and passes the JS challenge. See README for details."
    ) from last_error


def extract_cinema_shows(html, keyword):
    """
    Best-effort extraction of showtimes for cinemas whose name contains
    `keyword` (or ALL cinemas, if keyword is empty). BookMyShow's markup
    changes over time, so this uses a couple of fallback strategies rather
    than one brittle selector.
    Returns: {cinema_name_or_label: [showtime_strings]}
    """
    soup = BeautifulSoup(html, "html.parser")
    results = {}
    time_re = re.compile(r"\d{1,2}:\d{2}\s?(?:AM|PM|am|pm)?")

    if keyword:
        needles = [keyword]
    else:
        # No keyword given: try to pull distinct cinema-name-looking strings
        # generically isn't reliable, so fall back to a single "ANY" bucket
        # that just grabs every showtime-looking string on the page.
        needles = [None]

    for needle in needles:
        label = needle if needle else "ANY_CINEMA"

        # Strategy 1: embedded JSON/script blobs
        for script in soup.find_all("script"):
            text = script.string or ""
            if needle is None:
                window = text
            elif needle.lower() in text.lower():
                idx = text.lower().find(needle.lower())
                window = text[idx: idx + 3000]
            else:
                continue
            times = time_re.findall(window)
            if times:
                results.setdefault(label, []).extend(times)

        # Strategy 2: visible DOM text near the keyword
        if needle:
            for tag in soup.find_all(string=re.compile(re.escape(needle), re.IGNORECASE)):
                parent = tag.find_parent()
                if not parent:
                    continue
                container = parent
                for _ in range(3):
                    if container.find_parent():
                        container = container.find_parent()
                block_text = container.get_text(" ", strip=True)
                times = time_re.findall(block_text)
                if times:
                    results.setdefault(label, []).extend(times)

    for k in results:
        results[k] = sorted(set(results[k]))

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


def check_one(cfg, name, url, keyword, send_email_on_new=True):
    state_path = state_filename_for(name)
    print(f"\n[{name}] Checking {url} for showtimes"
          + (f" containing '{keyword}'..." if keyword else " (any cinema)..."))

    try:
        html = fetch_page(url)
    except (requests.RequestException, RuntimeError) as e:
        print(f"[{name}] Could not fetch the page: {e}")
        return

    current = extract_cinema_shows(html, keyword)
    previous = load_state(state_path)

    if not current:
        print(f"[{name}] No matching shows found on the page yet. Nothing to report.")
        return

    new_times = {}
    for cinema, times in current.items():
        old_times = set(previous.get(cinema, []))
        added = sorted(set(times) - old_times)
        if added:
            new_times[cinema] = added

    if new_times:
        lines = [f"{cinema}: {', '.join(times)}" for cinema, times in new_times.items()]
        body = (
            f"New showtimes found for '{name}':\n\n"
            + "\n".join(lines)
            + f"\n\nCheck and book: {url}"
        )
        print(f"[{name}] New shows found!")
        if send_email_on_new:
            send_email(cfg, f"New show alert: {name}", body)
            print(f"[{name}] Email sent.")
    else:
        print(f"[{name}] Matching cinema(s) found, but no NEW showtimes since last check.")

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
           date_range_days: 10       # how many days ahead to check, starting today
       -> generates one (name, url, keyword) per day, with {date} replaced by
          YYYYMMDD, and name suffixed with that date so each gets its own
          saved state file.
    """
    keyword = w.get("cinema_keyword", "")

    if "url_template" in w:
        days = int(w.get("date_range_days", 7))
        start = date.today()
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

    if args.url:
        # Ad-hoc single check, ignores the watches list in config.yaml
        check_one(cfg, args.name, args.url, args.keyword)
        return

    watches = cfg.get("watches") or []
    if not watches:
        print("No watches configured. Add entries under 'watches:' in config.yaml, "
              "or run with --url to do a one-off check.")
        return

    for w in watches:
        for name, url, keyword in expand_watch(w):
            check_one(cfg, name, url, keyword)


if __name__ == "__main__":
    main()