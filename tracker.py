import json
import os
import re
import time
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = "seen_shows.json"

TARGET_DATE = "20261217"  # Dec 17, 2026
TARGET_DATE_DASH = "2026-12-17"
HEARTBEAT_INTERVAL = 30 * 60

LINKS = {
    "bms_main": f"https://in.bookmyshow.com/movies/chennai/avengers-doomsday/buytickets/ET00526312/{TARGET_DATE}",
    "bms_3d": f"https://in.bookmyshow.com/movies/chennai/avengers-doomsday/buytickets/ET00526306/{TARGET_DATE}",
    "district": "https://www.district.in/movies/avengers-doomsday-movie-tickets-in-chennai",
    "pvr": "https://www.pvrcinemas.com/moviesessions/Chennai/AVENGERS-DOOMSDAY/35312",
}

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
        f"\n🔗 **[BMS Main]({LINKS['bms_main']})** | "
        f"**[BMS 3D]({LINKS['bms_3d']})** | "
        f"**[District]({LINKS['district']})** | "
        f"**[PVR/INOX]({LINKS['pvr']})**"
    )
    full_text = text + links
    chunks = [full_text[i : i + 3900] for i in range(0, len(full_text), 3900)]
    for idx, chunk in enumerate(chunks):
        payload = {
            "username": "Avengers Doomsday Tracker",
            "content": "@everyone 🚨 **FDFS / MORNING ENGLISH TICKETS LIVE!**" if (urgent and idx == 0) else None,
            "embeds": [{"description": chunk, "color": 15158332 if urgent else 3447003}],
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


def fetch_pvr_api(session):
    """Queries PVR/INOX backend session API directly."""
    found = []
    api_url = "https://api3.pvrcinemas.com/api/v1/booking/content/msessions"
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.pvrcinemas.com",
        "Referer": "https://www.pvrcinemas.com/",
        "city": "Chennai",
        "platform": "WEB",
        "country": "INDIA",
    }
    payload = {
        "city": "Chennai",
        "mid": "35312",
        "experience": "ALL",
        "lang": "ALL",
        "format": "ALL",
        "dated": TARGET_DATE_DASH,
    }
    resp = session.post(api_url, json=payload, headers=headers, timeout=20)
    if resp.status_code != 200:
        return None

    data = resp.json()
    cinemas = data.get("output", {}).get("c", [])
    for cinema in cinemas:
        theatre = cinema.get("n", "PVR/INOX Chennai")
        for s_group in cinema.get("s", []):
            lang = s_group.get("l", "English").title()
            fmt = s_group.get("f", "2D/3D")
            for show in s_group.get("t", []):
                t_str = show.get("st", "").upper()
                if t_str:
                    found.append({
                        "source": "PVR/INOX",
                        "theatre": theatre,
                        "lang": lang,
                        "format": fmt,
                        "time": t_str,
                        "slot": get_time_slot(t_str),
                    })
    return found


def fetch_html_source(session, url, source_name):
    """Fetches District or BMS using Chrome 131 TLS impersonation."""
    resp = session.get(url, timeout=20)
    if resp.status_code in (403, 429, 503) or "Verify you are human" in resp.text:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    found = []

    # Parse embedded Next.js / JSON state if present
    next_data = soup.find("script", id="__NEXT_DATA__")
    raw_text = next_data.string if next_data and next_data.string else soup.get_text("\n")

    for line in raw_text.splitlines():
        times = [m.group(0).upper() for m in TIME_PATTERN.finditer(line)]
        if not times:
            continue
        clean = re.sub(r"\s+", " ", line).strip()
        if len(clean) > 120:
            continue
        for t in times:
            found.append({
                "source": source_name,
                "theatre": clean.split(t)[0].strip(" -|:") or f"{source_name} Chennai",
                "lang": detect_language(clean),
                "format": detect_format(clean),
                "time": t,
                "slot": get_time_slot(t),
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

    return "\n".join(lines), bool(eng_fdfs or eng_morning)


def main():
    state = load_state()
    seen = state["seen"]
    now = int(time.time())
    is_first_run = len(seen) == 0
    heartbeat_due = (now - state["last_heartbeat"]) >= HEARTBEAT_INTERVAL

    all_scraped = []
    blocked_sources = []

    session = cffi_requests.Session(impersonate="chrome131")

    checks = [
        ("PVR/INOX", lambda: fetch_pvr_api(session)),
        ("District", lambda: fetch_html_source(session, LINKS["district"], "District")),
        ("BMS (Main)", lambda: fetch_html_source(session, LINKS["bms_main"], "BMS Main")),
        ("BMS (3D/IMAX)", lambda: fetch_html_source(session, LINKS["bms_3d"], "BMS 3D")),
    ]

    for name, fn in checks:
        try:
            res = fn()
            if res is None:
                blocked_sources.append(name)
            else:
                all_scraped.extend(res)
        except Exception as e:
            blocked_sources.append(f"{name} (err)")
            print(f"{name} failed: {e}")

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
            msg = f"⏱️ **30-MIN STATUS:** 0 shows parsed. Blocked: {', '.join(blocked_sources) or 'None'}."
        send_discord(msg, urgent=False)
        state["last_heartbeat"] = now
        state_changed = True

    if state_changed:
        save_state(state)


if __name__ == "__main__":
    main()