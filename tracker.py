import json
import os
import re
import time
import requests
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = "seen_shows.json"

TARGET_DATE = "20261217"  # Dec 17, 2026 ONLY
HEARTBEAT_INTERVAL = 30 * 60  # 30 minutes in seconds

SOURCES = [
    {
        "name": "BMS (Main)",
        "type": "bms",
        "url": f"https://in.bookmyshow.com/movies/chennai/avengers-doomsday/buytickets/ET00526312/{TARGET_DATE}",
    },
    {
        "name": "BMS (3D/IMAX)",
        "type": "bms",
        "url": f"https://in.bookmyshow.com/movies/chennai/avengers-doomsday/buytickets/ET00526306/{TARGET_DATE}",
    },
    {
        "name": "District",
        "type": "district",
        "url": "https://www.district.in/movies/avengers-doomsday-movie-tickets-in-chennai",
    },
    {
        "name": "PVR/INOX",
        "type": "pvr",
        "url": "https://www.pvrcinemas.com/moviesessions/Chennai/AVENGERS-DOOMSDAY/35312",
    },
]

TIME_PATTERN = re.compile(r"\b(0?\d|1[0-2]):[0-5]\d\s?(?:AM|PM)\b", re.IGNORECASE)
FORMAT_KEYWORDS = ["IMAX", "PXL", "4DX", "DOLBY", "MS-INFINITY", "MS INFVSN", "3D", "2D"]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {"seen": set(data), "last_heartbeat": 0}
            return {
                "seen": set(data.get("seen", [])),
                "last_heartbeat": data.get("last_heartbeat", 0),
            }
    return {"seen": set(), "last_heartbeat": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "seen": sorted(list(state["seen"])),
                "last_heartbeat": state["last_heartbeat"],
            },
            f,
            indent=2,
        )


def send_discord(text, urgent=False):
    links = (
        f"\n🔗 **[BMS Main]({SOURCES[0]['url']})** | "
        f"**[BMS 3D]({SOURCES[1]['url']})** | "
        f"**[District]({SOURCES[2]['url']})** | "
        f"**[PVR/INOX]({SOURCES[3]['url']})**"
    )
    full_text = text + links

    chunks = [full_text[i : i + 3900] for i in range(0, len(full_text), 3900)]
    for idx, chunk in enumerate(chunks):
        payload = {
            "username": "Avengers Doomsday Tracker",
            "content": "@everyone 🚨 **FDFS / MORNING ENGLISH TICKETS LIVE!**" if (urgent and idx == 0) else None,
            "embeds": [
                {
                    "description": chunk,
                    "color": 15158332 if urgent else 3447003,
                }
            ],
        }
        requests.post(WEBHOOK_URL, json=payload, timeout=15)


def get_time_slot(time_str):
    t = time_str.upper().strip()
    if "AM" not in t:
        return "OTHER"
    parts = t.replace("AM", "").strip().split(":")
    hour, minute = int(parts[0]), int(parts[1])
    if hour == 12 or hour < 8:
        return "FDFS"
    if hour < 10 or (hour == 10 and minute <= 30):
        return "MORNING"
    return "OTHER"


def detect_format(text):
    upper = text.upper()
    found = [kw for kw in FORMAT_KEYWORDS if kw in upper]
    return " / ".join(found) if found else "2D/3D"


def detect_language(text):
    upper = text.upper()
    if "TAMIL" in upper and "ENGLISH" not in upper:
        return "Tamil"
    return "English"


def scrape_bms(page, url, label):
    found = []
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    if "Attention Required! | Cloudflare" in page.content() or "Verify you are human" in page.content():
        return None

    default_lang = detect_language(page.inner_text("body"))
    venues = page.locator("li.list, [data-venue-code], div[class*='Venue']")
    for i in range(venues.count()):
        block = venues.nth(i).inner_text()
        full_times = [m.group(0).upper() for m in TIME_PATTERN.finditer(block)]
        if not full_times:
            continue
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        theatre = lines[0] if lines else "Unknown Theatre"
        lang = detect_language(block) if ("TAMIL" in block.upper() or "ENGLISH" in block.upper()) else default_lang
        fmt = detect_format(block)
        for t in full_times:
            found.append({
                "source": label, "theatre": theatre, "lang": lang,
                "format": fmt, "time": t, "slot": get_time_slot(t),
            })
    return found


def scrape_district(page, url):
    found = []
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    if "Access Denied" in page.content() or "Verify you are human" in page.content():
        return None

    dec17 = page.locator("text=/17\\s*Dec|Dec\\s*17/i").first
    if dec17.count() > 0:
        try:
            dec17.click(timeout=3000)
            page.wait_for_timeout(2000)
        except Exception:
            pass

    blocks = page.locator("div[class*='cinema'], div[class*='venue'], section")
    for i in range(min(blocks.count(), 120)):
        block = blocks.nth(i).inner_text()
        full_times = [m.group(0).upper() for m in TIME_PATTERN.finditer(block)]
        if not full_times:
            continue
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        theatre = lines[0] if lines else "Chennai Theatre"
        if len(theatre) > 90:
            continue
        for t in full_times:
            found.append({
                "source": "District", "theatre": theatre,
                "lang": detect_language(block), "format": detect_format(block),
                "time": t, "slot": get_time_slot(t),
            })
    return found


def scrape_pvr(page, url):
    found = []
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    if "Access Denied" in page.content() or "Verify you are human" in page.content():
        return None

    dec17 = page.locator("text=/17\\s*Dec|Dec\\s*17|\\b17\\b/i").first
    if dec17.count() > 0:
        try:
            dec17.click(timeout=3000)
            page.wait_for_timeout(2500)
        except Exception:
            pass

    blocks = page.locator("div[class*='cinema'], div[class*='theatre'], div[class*='session']")
    for i in range(min(blocks.count(), 100)):
        block = blocks.nth(i).inner_text()
        full_times = [m.group(0).upper() for m in TIME_PATTERN.finditer(block)]
        if not full_times:
            continue
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        theatre = lines[0] if lines else "PVR/INOX Chennai"
        if len(theatre) > 90 or not any(
            k in theatre.upper() for k in ["PVR", "INOX", "SATHYAM", "ESCAPE", "PALAZZO", "LUXE", "VR"]
        ):
            continue
        for t in full_times:
            found.append({
                "source": "PVR/INOX", "theatre": theatre,
                "lang": detect_language(block), "format": detect_format(block),
                "time": t, "slot": get_time_slot(t),
            })
    return found


def group_by_theatre(items):
    grouped = {}
    for x in items:
        label = f"**{x['theatre']}** ({x['lang']} • {x['format']} • {x['source']})"
        grouped.setdefault(label, []).append(x["time"])
    return [f"• {k}\n  └ {', '.join(sorted(set(v)))}" for k, v in grouped.items()]


def format_report(title, items):
    eng_fdfs = [x for x in items if x["lang"] == "English" and x["slot"] == "FDFS"]
    eng_morning = [x for x in items if x["lang"] == "English" and x["slot"] == "MORNING"]
    eng_other = [x for x in items if x["lang"] == "English" and x["slot"] == "OTHER"]
    tamil_all = [x for x in items if x["lang"] == "Tamil"]

    lines = [f"### {title}", ""]
    if eng_fdfs:
        lines.append("🔥 **ENGLISH FDFS (Before 8 AM)**")
        lines.extend(group_by_theatre(eng_fdfs))
        lines.append("")
    if eng_morning:
        lines.append("🌅 **ENGLISH MORNING (8 AM – 10:30 AM)**")
        lines.extend(group_by_theatre(eng_morning))
        lines.append("")
    if eng_other:
        lines.append("🎬 **ENGLISH OTHER SHOWS**")
        lines.extend(group_by_theatre(eng_other))
        lines.append("")
    if tamil_all:
        lines.append("🎟️ **TAMIL SHOWS (Backup)**")
        lines.extend(group_by_theatre(tamil_all))
        lines.append("")

    urgent = bool(eng_fdfs or eng_morning)
    return "\n".join(lines), urgent


def main():
    state = load_state()
    seen = state["seen"]
    now = int(time.time())
    is_first_run = len(seen) == 0
    heartbeat_due = (now - state["last_heartbeat"]) >= HEARTBEAT_INTERVAL

    all_scraped = []
    blocked_sources = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = context.new_page()
        stealth_sync(page)

        for src in SOURCES:
            try:
                if src["type"] == "bms":
                    results = scrape_bms(page, src["url"], src["name"])
                elif src["type"] == "district":
                    results = scrape_district(page, src["url"])
                else:
                    results = scrape_pvr(page, src["url"])

                if results is None:
                    blocked_sources.append(src["name"])
                    continue
                all_scraped.extend(results)
            except Exception as e:
                print(f"Error scraping {src['name']}: {e}")

        browser.close()

    new_items = []
    for item in all_scraped:
        key = f"{item['source']}|{item['theatre']}|{item['lang']}|{item['format']}|{item['time']}"
        if key not in seen:
            new_items.append(item)
            seen.add(key)

    state_changed = False

    if new_items:
        header = "📋 INITIAL DEC 17 SNAPSHOT" if is_first_run else "🚨 NEW DEC 17 SHOWS OPENED"
        msg, urgent = format_report(header, new_items)
        send_discord(msg, urgent=(urgent or is_first_run))
        state["last_heartbeat"] = now
        state_changed = True
    elif heartbeat_due:
        if all_scraped:
            msg, _ = format_report("⏱️ 30-MIN STATUS: CURRENT DEC 17 SHOWS", all_scraped)
        else:
            msg = f"⏱️ **30-MIN STATUS:** Tracker running, 0 shows parsed. Blocked: {', '.join(blocked_sources) or 'None'}."
        send_discord(msg, urgent=False)
        state["last_heartbeat"] = now
        state_changed = True

    if state_changed:
        save_state(state)


if __name__ == "__main__":
    main()