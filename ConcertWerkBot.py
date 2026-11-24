import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------- Configuration ----------
load_dotenv()
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

CHECK_INTERVAL_SECONDS = 30 * 60   # check every 30 minutes
# What happens the FIRST time a venue is seen (e.g. right after you add a new site):
# True  = send all its current events
# False = silently remember them, only post events that appear later
SEND_ON_FIRST_RUN = True
SEND_DELAY_SECONDS = 5           # Telegram allows ~20 messages/minute per group
FLUC_WEEKS_AHEAD = 4             # how many weekly Fluc pages to read (this week + next ones)
STATE_FILE = Path(__file__).with_name("seen_events.json")
ODOO_URL = os.environ.get("ODOO_URL", "")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")
ODOO_STATE_FILE = Path(__file__).with_name("odoo_events_sent.json")
LOCAL_TZ = ZoneInfo("Europe/Vienna")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------- Shared helpers ----------
_page_cache = {}  # cleared at the start of every check, so a page is downloaded once per round


def fetch_soup(url):
    """Download a page (once per round) and return it as a parsed HTML tree."""
    if url in _page_cache:
        return _page_cache[url]
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; WerkBot/1.0)"},
        timeout=20,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
    _page_cache[url] = soup
    return soup


def make_date(day, month, clock=None):
    """Builds the date text used in messages: '21.11 23:00' (or just '21.11' if no time is known)."""
    text = f"{int(day):02d}.{int(month):02d}"
    return f"{text} {clock.zfill(5)}" if clock else text


# ---------- Scrapers: ONE FUNCTION PER WEBSITE ----------
# Every scraper returns a list of dicts: {"title": "...", "date": "21.11 23:00"}

# --- Das Werk: date labels look like "SA. 21.11. // 23:00" ---
WERK_DATE_RE = re.compile(r"^[A-Za-zÄÖÜäöü]{2}\.\s*(\d{1,2})\.(\d{1,2})\.\s*//\s*(\d{1,2}:\d{2})$")


def scrape_das_werk():
    soup = fetch_soup("https://www.daswerk.org/program")
    events = []
    for p in soup.find_all("p"):
        match = WERK_DATE_RE.match(p.get_text(strip=True))
        if not match:
            continue
        title_tag = p.find_next_sibling("p")  # the title is the next <p> in the card
        if title_tag is None:
            continue
        events.append({
            "title": title_tag.get_text(strip=True),
            "date": make_date(*match.groups()),
        })
    return events


# --- EXIL: cards are <article class="event-card">, date like "FRI 27 NOV 2026".
#     The start time is only in the pop-up (modal) and only on some events. ---
MONTHS_EN = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
             "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
EXIL_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})\w*\s+(\d{4})")


def scrape_exil():
    soup = fetch_soup("https://www.club-exil.at/en")
    events = []
    for card in soup.select("article.event-card"):
        date_tag = card.select_one("time.event-card__date")
        title_tag = card.select_one(".event-card__title")
        if not date_tag or not title_tag:
            continue
        match = EXIL_DATE_RE.search(date_tag.get_text(strip=True))
        if not match:
            continue
        day, month_name, _year = match.groups()
        month = MONTHS_EN.get(month_name.upper())
        if not month:
            continue

        # The card and its pop-up are linked by an id: data-open-modal="X" -> id="modal-X"
        clock = None
        opener = card.select_one("[data-open-modal]")
        if opener:
            modal = soup.find(id="modal-" + opener["data-open-modal"])
            time_tag = modal.select_one(".modal__time") if modal else None
            if time_tag and re.fullmatch(r"\d{1,2}:\d{2}", time_tag.get_text(strip=True)):
                clock = time_tag.get_text(strip=True)  # ignore texts like "HiTech Edition"

        events.append({"title": title_tag.get_text(strip=True), "date": make_date(day, month, clock)})
    return events


# --- Fluc and Fluc Wanne: one HTML page per calendar week.
#     Day rows are <li id="f_20261120"> (Fluc) and <li id="w_20261120"> (Wanne);
#     every event title is in <strong>, the start time is the text right before it. ---
FLUC_BASE = "https://www.fluc.at/programm/"
FLUC_TIME_RE = re.compile(r"(\d{1,2})(?:[:.](\d{2}))?(?:\s*-\s*\d{1,2}(?:[:.]\d{2})?)?\s*:?$")


def fluc_events(prefix):
    """prefix 'f' = Fluc, 'w' = Fluc Wanne."""
    today = date.today()
    year, week, _ = today.isocalendar()
    url = f"{FLUC_BASE}{year}_Flucwoche{week:02d}.html"
    row_id = re.compile(rf"^{prefix}_(\d{{8}})$")
    events = []

    for _ in range(FLUC_WEEKS_AHEAD):
        try:
            soup = fetch_soup(url)
        except requests.RequestException as error:
            logging.info("Fluc page not available (%s): %s", url, error)
            break

        for li in soup.find_all("li", id=row_id):
            ymd = row_id.match(li["id"]).group(1)
            if date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:])) < today:
                continue  # skip days that are already over
            for strong in li.find_all("strong"):
                title = " ".join(strong.get_text().split())  # also joins titles split over two lines
                if not title:
                    continue
                clock = None
                before = strong.previous_sibling  # text like "  15:00-18:00: "
                if isinstance(before, str) and before.strip():
                    match = FLUC_TIME_RE.search(before.strip().splitlines()[-1].strip())
                    if match:
                        clock = f"{int(match.group(1)):02d}:{match.group(2) or '00'}"
                events.append({"title": title, "date": make_date(ymd[6:], ymd[4:6], clock)})

        # follow the "next week" link of the page
        next_url = None
        for link in soup.find_all("a", rel="next"):
            if re.search(r"Flucwoche\d+\.html$", link.get("href", "")):
                next_url = urljoin(url, link["href"])
                break
        if not next_url:
            break
        url = next_url
    return events


def scrape_fluc():
    return fluc_events("f")


def scrape_fluc_wanne():
    return fluc_events("w")


# --- The Loft: every event is a <div class="box-wrap"> with date, time and title ---
def scrape_the_loft():
    soup = fetch_soup("https://www.theloft.at/programm/")
    events = []
    for box in soup.select("div.box-wrap"):
        datum = box.select_one(".datum")          # "Do. 19.11.2026"
        title_tag = box.select_one(".content-middle")
        if not datum or not title_tag:
            continue
        match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", datum.get_text())
        if not match:
            continue
        open_tag = box.select_one(".open")        # "23:00"
        clock = open_tag.get_text(strip=True) if open_tag else None
        events.append({
            "title": title_tag.get_text(strip=True),
            "date": make_date(match.group(1), match.group(2), clock),
        })
    return events


# --- Grelle Forelle: the title itself starts with the date, e.g. "20/11 FISH MARKET ...".
#     The listing has no start time. ---
GF_TITLE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})\s+(.+)$")


def scrape_grelle_forelle():
    soup = fetch_soup("https://www.grelleforelle.com/programm/")
    events = []
    for link in soup.select("div.et_pb_portfolio_item h2 a"):
        match = GF_TITLE_RE.match(link.get_text(strip=True))
        if not match:
            continue
        events.append({"title": match.group(3).strip(), "date": make_date(match.group(1), match.group(2))})
    return events


# TO ADD A NEW SITE: write a scrape_xyz() function like the ones above,
# then add a line to this list: ("Display Name", scrape_xyz)
SCRAPERS = [
    ("Das Werk", scrape_das_werk),
    ("EXIL", scrape_exil),
    ("Fluc", scrape_fluc),
    ("Fluc Wanne", scrape_fluc_wanne),
    ("The Loft", scrape_the_loft),
    ("Grelle Forelle", scrape_grelle_forelle),
]


# ---------- Remembering what was already sent ----------
def event_key(venue, event):
    return f"{venue}|{event['date']}|{event['title']}"


def load_seen():
    if not STATE_FILE.exists():
        return set()
    seen = set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    # Migration: old entries were "date|title" (Das Werk only). Add the venue prefix.
    return {k if k.count("|") >= 2 else f"Das Werk|{k}" for k in seen}


def save_seen(seen):
    STATE_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------- Telegram ----------
def format_message(venue, event):
    return (
        f"🎵 {venue.upper()} EVENT\n\n"
        f"Event: {venue}\n"
        f"Title: {event['title']}\n"
        f"Date: {event['date']}"
    )


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for attempt in range(2):
        response = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=20)
        if response.status_code == 429 and attempt == 0:  # sending too fast: wait as told, retry once
            wait = response.json().get("parameters", {}).get("retry_after", 5)
            logging.warning("Telegram rate limit hit, waiting %s s", wait)
            time.sleep(wait + 1)
            continue
        if not response.ok:
            raise RuntimeError(f"Telegram error {response.status_code}: {response.text}")
        return

# ---------- Odoo ----------
DATE_TEXT_RE = re.compile(r"^(\d{2})\.(\d{2})(?:\s+(\d{1,2}:\d{2}))?$")
EVENT_LENGTH_HOURS = 4  # the sites show no end time, so we assume this length


def to_odoo_datetimes(date_text):
    """Turns '21.11 23:00' (Vienna time, no year) into (start, end) in UTC for Odoo."""
    match = DATE_TEXT_RE.match(date_text)
    if not match:
        return None
    day, month, clock = int(match.group(1)), int(match.group(2)), match.group(3)
    try:
        today = date.today()
        year = today.year
        if date(year, month, day) < today - timedelta(days=60):
            year += 1  # e.g. in December, a '10.01' event means next year
        if clock:
            hour, minute = (int(x) for x in clock.split(":"))
            begin = datetime(year, month, day, hour, minute, tzinfo=LOCAL_TZ)
            end = begin + timedelta(hours=EVENT_LENGTH_HOURS)
        else:
            begin = datetime(year, month, day, 0, 0, tzinfo=LOCAL_TZ)
            end = datetime(year, month, day, 23, 59, tzinfo=LOCAL_TZ)
    except ValueError:
        return None
    fmt = "%Y-%m-%d %H:%M:%S"
    return (
        begin.astimezone(timezone.utc).strftime(fmt),
        end.astimezone(timezone.utc).strftime(fmt),
    )


def load_odoo_sent():
    if not ODOO_STATE_FILE.exists():
        return set()
    return set(json.loads(ODOO_STATE_FILE.read_text(encoding="utf-8")))


def save_odoo_sent(sent):
    ODOO_STATE_FILE.write_text(
        json.dumps(sorted(sent), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def sync_to_odoo(venue, events):
    """Create Odoo Events for events Odoo has not received yet. Never stops the bot."""
    if not ODOO_API_KEY:
        return  # Odoo not configured, skip silently
    try:
        sent = load_odoo_sent()
        keys, vals_list = [], []
        for e in events:
            key = event_key(venue, e)
            if key in sent:
                continue
            times = to_odoo_datetimes(e["date"])
            if not times:
                logging.warning("[%s] skipped, unreadable date: %s", venue, e)
                continue
            keys.append(key)
            vals_list.append({
                "name": f"{venue}: {e['title']}",
                "date_begin": times[0],
                "date_end": times[1],
                "date_tz": "Europe/Vienna",
            })
        if not vals_list:
            return
        response = requests.post(
            f"{ODOO_URL}/json/2/event.event/create",
            headers={"Authorization": f"bearer {ODOO_API_KEY}", "X-Odoo-Database": ODOO_DB},
            json={"vals_list": vals_list},
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(f"Odoo error {response.status_code}: {response.text}")
        sent.update(keys)
        save_odoo_sent(sent)
        logging.info("[%s] saved %d event(s) to Odoo Events", venue, len(vals_list))
    except Exception:
        logging.exception("[%s] could not save to Odoo, will retry next round", venue)


# ---------- Main logic ----------
def check_venue(venue, scraper, seen):
    """Scrape one site and send its new events. Changes `seen` in place."""
    events = scraper()
    logging.info("[%s] found %d events", venue, len(events))
    if not events:
        logging.warning("[%s] no events parsed, run with --debug to inspect", venue)
        return
    sync_to_odoo(venue, events)

    known_venue = any(k.startswith(f"{venue}|") for k in seen)
    should_send = SEND_ON_FIRST_RUN or known_venue

    new_events = [e for e in events if event_key(venue, e) not in seen]
    for event in new_events:
        if should_send:
            send_telegram(format_message(venue, event))
            logging.info("[%s] sent: %s (%s)", venue, event["title"], event["date"])
            time.sleep(SEND_DELAY_SECONDS)
        seen.add(event_key(venue, event))
    logging.info("[%s] %d new event(s)", venue, len(new_events))


def check_all():
    _page_cache.clear()
    seen = load_seen()
    for venue, scraper in SCRAPERS:
        try:
            check_venue(venue, scraper, seen)
        except Exception:
            # one broken site must never stop the others
            logging.exception("[%s] failed, continuing with the next site", venue)
        finally:
            save_seen(seen)  # saved after every site, so nothing is sent twice


def main():
    if "--debug" in sys.argv:
        for venue, scraper in SCRAPERS:
            print(f"=== {venue} ===")
            try:
                events = scraper()
            except Exception as error:
                print("ERROR:", error)
                continue
            print(f"({len(events)} events)")
            for e in events:
                print(e)
        return

    while True:
        check_all()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()