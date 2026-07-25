# Show Notifier

Checks a public BookMyShow movie page and emails you when a new showtime
appears at a cinema you care about (default: anything with "Allu" in the name,
for Allu Cinemas Kokapet). No login, no automation of booking - just reads
the public page you'd see in a browser.

## 1. Install Python (if you don't have it)

Download from https://www.python.org/downloads/ (3.9+). On Windows, tick
"Add python.exe to PATH" during install.

## 2. Get the project files

Put `show_notifier.py`, `config.yaml`, and `requirements.txt` in one folder
on your laptop, e.g. `C:\Users\<you>\show_notifier\` or `~/show_notifier/`.

## 3. Install dependencies

Open a terminal (Command Prompt / PowerShell / Terminal) in that folder:

```
pip install -r requirements.txt
```

## 4. Create a Gmail App Password

Your normal Gmail password won't work for sending mail from a script.

1. Go to https://myaccount.google.com/security
2. Turn on 2-Step Verification if it isn't already on
3. Go to https://myaccount.google.com/apppasswords
4. Create an app password (name it "show notifier"), copy the 16-character code

## 5. Edit config.yaml

Under `watches:`, add one entry per movie+cinema you want tracked:

```yaml
watches:
  - name: "Spiderman - Allu Cinemas"
    url: "https://in.bookmyshow.com/movies/hyd/spider-man-brand-new-day/ET00XXXXXX"
    cinema_keyword: "Allu"
```

- `name`: any short label - used in the email subject and to name that
  watch's saved state file, so multiple watches don't clash
- `url`: the movie's **listing page** (all cinemas shown at once) - not a
  `/seat-layout/...` page, that's a different kind of page entirely
- `cinema_keyword`: substring to match a cinema's name (case-insensitive).
  Leave it `""` to get notified about any new showtime at any cinema on
  that page

You can add as many entries under `watches:` as you like - each is checked
independently.

Then fill in:

- `gmail_address`: your Gmail address
- `gmail_app_password`: the 16-character app password from step 4
- `notify_to`: where you want the alert emailed (can be the same address)

## 6. Run it

```
python show_notifier.py
```

This checks every entry in `watches:`. First run per watch just saves what's
currently showing (so it won't false-alarm on shows that already exist).
Run it again later - if a NEW showtime has appeared, you'll get an email.

### Quick one-off check (no config editing)

To test any URL on the fly without adding it to `watches:`:

```
python show_notifier.py --url "https://in.bookmyshow.com/movies/hyd/some-movie/ET00XXXXXX" --keyword "Allu" --name "quick test"
```

This still uses the Gmail settings from config.yaml, but doesn't touch or
need the `watches:` list.

## 7. (Optional) Run it automatically instead of by hand

- **Windows**: Task Scheduler → Create Basic Task → Trigger: Daily (or every
  few hours) → Action: Start a program → `python` with argument
  `C:\path\to\show_notifier.py`
- **Mac/Linux**: `crontab -e` and add a line like:
  ```
  0 * * * * cd /path/to/show_notifier && /usr/bin/python3 show_notifier.py
  ```
  (runs every hour)

## Notes / limitations

- BookMyShow's page structure can change; if you start getting "no shows
  found" even though the movie is clearly listed, open the page in a
  browser, view source, and let me know what the HTML around the cinema
  name looks like so I can adjust the parsing.
- This is read-only against a public page. It does not log in, does not
  hold/select seats, and does not book anything.
