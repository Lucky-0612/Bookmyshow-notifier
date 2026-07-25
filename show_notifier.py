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
from email.mime.text import MIMEText

import requests
import yaml
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def load_config(path="config.yaml"):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def fetch_page(url):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


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
    except requests.RequestException as e:
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


def main():
    parser = argparse.ArgumentParser(description="Check BookMyShow listing pages for new showtimes.")
    parser.add_argument("--url", help="Ad-hoc listing page URL to check (overrides config watchlist)")
    parser.add_argument("--keyword", default="", help="Cinema name substring to match (blank = any cinema)")
    parser.add_argument("--name", default="ad-hoc check", help="Label for this ad-hoc check")
    args = parser.parse_args()

    cfg = load_config()
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
        check_one(cfg, w["name"], w["url"], w.get("cinema_keyword", ""))


if __name__ == "__main__":
    main()