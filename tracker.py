import json
import os
import re
import time
from bs4 import BeautifulSoup
import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = "seen_shows.json"
HEARTBEAT_INTERVAL = 30 * 60  # 30 minutes

MIRROR_URL = "https://ticketradar.in/f/avengers-doomsday/chennai"
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


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
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


def scrape_chennai_dec17():
    resp = requests.get(MIRROR_URL, headers=HEADERS, timeout=25)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text("\n")

    # Trim header and footer noise
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

        # Split theatre block by language sections (e.g. "English · Ms-Infinity Vsn 3D" vs "Tamil · Ms-Infinity Vsn 3D")
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
            # Extract ONLY Thu 17 Dec line/chunk before Fri 18 Dec
            dec17_chunk = sec.split("Thu 17 Dec", 1)[1].split("Fri 18 Dec")[0]
            times = [m.group(0).upper() for m in TIME_PATTERN.finditer(dec17_chunk)]

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


def group_by_theatre(items):
    grouped = {}
    for x in items:
        label = f"**{x['theatre']}** ({x['lang']} • {x['format']})"
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

    shows, err = scrape_chennai_dec17()
    if err:
        if heartbeat_due or is_first_run:
            send_discord(f"⚠️ **Tracker Fetch Error:** {err}", urgent=False)
            state["last_heartbeat"] = now
            save_state(state)
        return

    new_items = []
    for item in shows:
        key = f"{item['theatre']}|{item['lang']}|{item['format']}|{item['time']}"
        if key not in seen:
            new_items.append(item)
            seen.add(key)

    state_changed = False
    if new_items:
        header = "📋 INITIAL DEC 17 SNAPSHOT (11+ CHENNAI THEATRES)" if is_first_run else "🚨 NEW DEC 17 SHOWS OPENED"
        msg, urgent = format_report(header, new_items)
        send_discord(msg, urgent=(urgent or is_first_run))
        state["last_heartbeat"] = now
        state_changed = True
    elif heartbeat_due:
        msg, _ = format_report("⏱️ 30-MIN STATUS: CURRENT DEC 17 SHOWS", shows)
        send_discord(msg, urgent=False)
        state["last_heartbeat"] = now
        state_changed = True

    if state_changed:
        save_state(state)


if __name__ == "__main__":
    main()