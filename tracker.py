import json
import os
import re
import time
from bs4 import BeautifulSoup
import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = "seen_shows.json"
HEARTBEAT_INTERVAL = 30 * 60  # Send a heartbeat every ~30 mins (2 cron cycles at */15)

MIRROR_URL = "https://ticketradar.in/f/avengers-doomsday/chennai"
JUSTDIAL_URL = "https://www.justdial.com/Chennai/Avengers-Doomsday-3d-English-Movie/mct-16315131"

LINKS = {
    "bms_main": "https://in.bookmyshow.com/movies/chennai/avengers-doomsday/buytickets/ET00526312/20261217",
    "bms_3d": "https://in.bookmyshow.com/movies/chennai/avengers-doomsday/buytickets/ET00526306/20261217",
    "district": "https://www.district.in/movies/avengers-doomsday-movie-tickets-in-chennai",
    "pvr": "https://www.pvrinox.com/moviesessions/chennai/avengers-doomsday/35312",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

TIME_PATTERN = re.compile(r"\b(0?\d|1[0-2]):[0-5]\d\s?(?:AM|PM)\b", re.IGNORECASE)
THEATRE_SPLIT = re.compile(
    r"(?=(?:PVR|INOX|Cinepolis|AGS|Sangam|Kamala|Sathyam|Escape|Rohini|Kasi|Vettri|GK Cinemas|MovieMax|Rakki|Miraj|Devi|Woodlands|Albert|Ega|Udhayam|Mayajaal|HDFC Millennia PVR)[:\s])"
)


def time_to_minutes(time_str):
    t = time_str.upper().strip()
    is_pm = "PM" in t
    clean = t.replace("AM", "").replace("PM", "").strip()
    hour, minute = map(int, clean.split(":"))
    if hour == 12:
        hour = 12 if is_pm else 0
    elif is_pm:
        hour += 12
    return hour * 60 + minute


def get_time_slot(time_str):
    mins = time_to_minutes(time_str)
    if mins < 8 * 60:
        return "FDFS"
    if mins <= 10 * 60 + 30:
        return "MORNING"
    return "OTHER"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "seen": set(data.get("seen", [])),
                "known_theatres": set(data.get("known_theatres", [])),
                "last_heartbeat": data.get("last_heartbeat", 0),
            }
    return {"seen": set(), "known_theatres": set(), "last_heartbeat": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "seen": sorted(list(state["seen"])),
                "known_theatres": sorted(list(state["known_theatres"])),
                "last_heartbeat": state["last_heartbeat"],
            },
            f,
            indent=2,
        )


def send_discord(text, urgent=False, banner=None):
    links = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎟️ **TAP TO BOOK NOW:** **[BMS Main]({LINKS['bms_main']})** • "
        f"**[BMS 3D]({LINKS['bms_3d']})** • "
        f"**[District]({LINKS['district']})** • "
        f"**[PVR/INOX]({LINKS['pvr']})**"
    )
    full_text = text + links
    chunks = [full_text[i : i + 3900] for i in range(0, len(full_text), 3900)]
    for idx, chunk in enumerate(chunks):
        payload = {
            "username": "🚨 DOOMSDAY TICKET SNIPER",
            "content": banner if (urgent and idx == 0) else None,
            "embeds": [{"description": chunk, "color": 16711680 if urgent else 3447003}],
        }
        requests.post(WEBHOOK_URL, json=payload, timeout=15)


def scrape_ticketradar():
    resp = requests.get(MIRROR_URL, headers=HEADERS, timeout=25)
    if resp.status_code != 200:
        return [], f"TicketRadar HTTP {resp.status_code}"

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text("\n")

    if "Advance booking open" in text:
        text = text.split("Advance booking open", 1)[1]
    if "Premium screens in Chennai" in text:
        text = text.split("Premium screens in Chennai", 1)[0]

    found = []
    seen_local = set()

    for block in THEATRE_SPLIT.split(text):
        if "Thu 17 Dec" not in block:
            continue

        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        theatre = lines[0][:80]
        if "Book →" in theatre or len(theatre) < 5:
            continue

        lang_sections = re.split(r"(?=(?:English|Tamil|Hindi|Telugu)\s*·)", block)
        for sec in lang_sections:
            if "Thu 17 Dec" not in sec:
                continue

            sec_header = sec.splitlines()[0].strip()
            if sec_header.startswith("Tamil"):
                lang = "Tamil"
            elif sec_header.startswith("English"):
                lang = "English"
            else:
                continue

            fmt = sec_header.split("·", 1)[1].strip() if "·" in sec_header else "3D"
            dec17_chunk = sec.split("Thu 17 Dec", 1)[1].split("Fri 18 Dec")[0]
            times = [m.group(0).upper().lstrip("0") for m in TIME_PATTERN.finditer(dec17_chunk)]

            for t in times:
                uid = (theatre, lang, fmt, t)
                if uid in seen_local:
                    continue
                seen_local.add(uid)
                found.append({
                    "theatre": theatre,
                    "lang": lang,
                    "format": fmt,
                    "time": t,
                    "slot": get_time_slot(t),
                })

    return found, None


def scrape_justdial():
    try:
        resp = requests.get(JUSTDIAL_URL, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return [], None
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text("\n")
        found = []
        for block in THEATRE_SPLIT.split(text):
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            if not lines:
                continue
            theatre = lines[0].split(",")[0][:75]
            times = [m.group(0).upper().lstrip("0") for m in TIME_PATTERN.finditer(block[:450])]
            for t in times:
                found.append({
                    "theatre": theatre,
                    "lang": "English",
                    "format": "3D (Justdial)",
                    "time": t,
                    "slot": get_time_slot(t),
                })
        return found, None
    except Exception:
        return [], None


def group_by_theatre(items, known_theatres=None, highlight_new=False):
    grouped = {}
    for x in items:
        is_new_theatre = known_theatres is not None and x["theatre"] not in known_theatres
        badge = "🆕 **[NEW THEATRE]** " if (highlight_new and is_new_theatre) else ""
        label = f"{badge}**{x['theatre']}** ({x['lang']} • {x['format']})"
        grouped.setdefault(label, []).append(x["time"])

    output = []
    for k, v in grouped.items():
        sorted_times = sorted(set(v), key=time_to_minutes)
        output.append(f"• {k}\n  └ 🕒 **{', '.join(sorted_times)}**")
    return output


def format_report(title, items, known_theatres=None, highlight_new=False):
    eng_fdfs = [x for x in items if x["lang"] == "English" and x["slot"] == "FDFS"]
    eng_morning = [x for x in items if x["lang"] == "English" and x["slot"] == "MORNING"]
    eng_other = [x for x in items if x["lang"] == "English" and x["slot"] == "OTHER"]
    tamil_all = [x for x in items if x["lang"] == "Tamil"]

    lines = [f"## {title}", ""]
    if eng_fdfs:
        lines.append("🔥🔥 **ENGLISH FDFS (6:00 AM – 7:59 AM)** 🔥🔥")
        lines.extend(group_by_theatre(eng_fdfs, known_theatres, highlight_new))
        lines.append("")
    if eng_morning:
        lines.append("🌅 **ENGLISH MORNING (8:00 AM – 10:30 AM)**")
        lines.extend(group_by_theatre(eng_morning, known_theatres, highlight_new))
        lines.append("")
    if eng_other:
        lines.append("🎬 **ENGLISH AFTERNOON / NIGHT**")
        lines.extend(group_by_theatre(eng_other, known_theatres, highlight_new))
        lines.append("")
    if tamil_all:
        lines.append("🎟️ **TAMIL SHOWS (Backup)**")
        lines.extend(group_by_theatre(tamil_all, known_theatres, highlight_new))
        lines.append("")

    has_priority_english = bool(eng_fdfs or eng_morning)
    return "\n".join(lines), has_priority_english


def main():
    state = load_state()
    seen = state["seen"]
    known_theatres = state["known_theatres"]
    now = int(time.time())
    is_first_run = len(seen) == 0
    heartbeat_due = (now - state["last_heartbeat"]) >= HEARTBEAT_INTERVAL

    shows, err = scrape_ticketradar()
    jd_shows, _ = scrape_justdial()

    existing_theatres = {s["theatre"].split(":")[0].strip().lower() for s in shows}
    for j in jd_shows:
        prefix = j["theatre"].split("(")[0].strip().lower()
        if not any(prefix[:8] in ex for ex in existing_theatres):
            shows.append(j)

    if err and not shows:
        if heartbeat_due or is_first_run:
            send_discord(f"⚠️ **Tracker Fetch Error:** {err}", urgent=False)
            state["last_heartbeat"] = now
            save_state(state)
        return

    new_items = []
    brand_new_theatres = set()

    for item in shows:
        key = f"{item['theatre']}|{item['lang']}|{item['format']}|{item['time']}"
        if key not in seen:
            new_items.append(item)
            seen.add(key)
            if not is_first_run and item["theatre"] not in known_theatres:
                brand_new_theatres.add(item["theatre"])

    state_changed = False
    if new_items:
        if is_first_run:
            header = f"📋 INITIAL DEC 17 SNAPSHOT ({len({x['theatre'] for x in new_items})} THEATRES LIVE)"
            msg, _ = format_report(header, new_items)
            banner = "@everyone 📋 **DOOMSDAY TRACKER LIVE — INITIAL CHENNAI LIST**"
            send_discord(msg, urgent=True, banner=banner)
        else:
            new_t_count = len(brand_new_theatres)
            header = (
                f"🚨🚨 MASSIVE DROP: {new_t_count} NEW THEATRE(S) & {len(new_items)} NEW SHOW(S) ADDED! 🚨🚨"
                if new_t_count > 0
                else f"🚨 NEW DEC 17 SHOWTIMES ADDED ({len(new_items)} NEW SLOTS)!"
            )
            msg, has_priority_english = format_report(
                header, new_items, known_theatres=known_theatres, highlight_new=True
            )
            urgent = has_priority_english or (new_t_count > 0)
            banner = (
                "@everyone 🚨🚨 **NEW CHENNAI SCREEN / FDFS ENGLISH TICKETS JUST OPENED! BOOK NOW!** 🚨🚨"
                if urgent
                else None
            )
            send_discord(msg, urgent=urgent, banner=banner)

        for item in shows:
            known_theatres.add(item["theatre"])
        state["last_heartbeat"] = now
        state_changed = True

    elif heartbeat_due:
        header = f"⏱️ 30-MIN HEARTBEAT ({len({x['theatre'] for x in shows})} THEATRES • {len(shows)} SHOWS LIVE)"
        msg, _ = format_report(header, shows)
        send_discord(msg, urgent=False)
        state["last_heartbeat"] = now
        state_changed = True

    if state_changed:
        save_state(state)


if __name__ == "__main__":
    main()