import asyncio
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from curl_cffi import requests as cffi_requests
from playwright.async_api import async_playwright

# ==========================================
# CONFIGURATION
# ==========================================
def load_env_file(path: Path) -> None:
    """Loads KEY=VALUE lines from a .env file into os.environ (existing vars win)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# Secrets come from .env / the environment, never from source control.
load_env_file(Path(__file__).with_name(".env"))
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
RESY_AUTH_TOKEN = os.environ["RESY_AUTH_TOKEN"]

# Public API key used by the resy.com web client.
RESY_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"

TARGET_DATE = "2026-10-03"
PARTY_SIZE = 4
TIME_WINDOW = {"start": "17:00", "end": "22:00"}

BASE_POLL_INTERVAL = 900  # seconds between full passes
POLL_JITTER = 60  # +/- seconds, to avoid a fixed timing signature
REQUEST_DELAY = (2.0, 5.0)  # random pause (seconds) between venue checks
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
)

# (name, Resy venue_id, Resy URL slug)
RESY_VENUES = [
    ("Ambassadors Clubhouse New York", "94741", "ambassadors-clubhouse-new-york"),
    ("Torrisi", "64593", "torrisi"),
    ("Misi", "3015", "misi"),
    ("Monkey Bar", "60058", "monkey-bar"),
    ("COTE Korean Steakhouse", "72271", "cote-nyc"),
    ("4 Charles Prime Rib", "834", "4-charles-prime-rib"),
    ("Lilia", "418", "lilia"),
    ("Via Carota", "2567", "via-carota"),
    ("Carbone", "6194", "carbone"),
    ("Coqodaq", "76033", "coqodaq"),
    ("Tatiana by Kwame Onwuachi", "65452", "tatiana"),
    ("Balthazar", "50227", "balthazar-nyc"),
    ("Rubirosa", "466", "rubirosa"),
    ("Shuka", "1575", "shuka"),
    ("Fish Cheeks NoHo", "693", "fish-cheeks"),
    ("Au Cheval - NYC", "5769", "au-cheval-nyc"),
    ("Charlie Bird", "5", "charlie-bird"),
    ("Pasquale Jones", "440", "pasquale-jones"),
    ("King", "646", "king"),
    ("Raoul's", "7241", "raoulsrestaurant"),
    ("Minetta Tavern", "9846", "minetta-tavern"),
    ("il Buco", "675", "il-buco"),
    ("Emily: West Village", "1325", "emily-west-village"),
    ("La Mercerie at The Guild", "4368", "la-mercerie-at-the-guild"),
    ("The Fulton by Jean-Georges", "5932", "the-fulton-by-jean-georges"),
    ("Sadelle's", "29967", "sadelles"),
    ("Dante", "1290", "dante"),
    ("Rosie's", "725", "rosies"),
    ("Thai Diner", "49453", "thai-diner"),
    ("Crown Shy", "10726", "crown-shy"),
    ("Chinese Tuxedo", "818", "chinese-tuxedo"),
    ("Vic's", "2790", "vics"),
    ("Wayan NYC", "4599", "wayan"),
    ("il Buco Alimentari e Vineria", "6583", "il-buco-alimentari"),
]

TARGETS: List[Dict[str, Any]] = [
    {
        "platform": "resy",
        "name": name,
        "venue_id": venue_id,
        "date": TARGET_DATE,
        "party_size": PARTY_SIZE,
        "time_window": TIME_WINDOW,
        "booking_url": f"https://resy.com/cities/new-york-ny/venues/{slug}",
    }
    for name, venue_id, slug in RESY_VENUES
]

sent_cache = set()


# ==========================================
# UTILITY FUNCTIONS
# ==========================================
def filter_slots_by_time(
    slots: List[str], start_str: str, end_str: str
) -> List[str]:
    """Filters time strings like ['6:30 PM', '9:15 PM'] against target start/end window."""
    start_time = datetime.strptime(start_str, "%H:%M").time()
    end_time = datetime.strptime(end_str, "%H:%M").time()

    filtered = []
    for slot in slots:
        try:
            slot_time = datetime.strptime(slot, "%I:%M %p").time()
        except ValueError:
            print(f"Skipping unparseable slot time: {slot!r}")
            continue
        if start_time <= slot_time <= end_time:
            filtered.append(slot)
    return filtered


async def run_blocking(func, *args, **kwargs):
    """Runs a blocking call in the default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


async def send_discord_alert(
    platform: str, name: str, date: str, slots: List[str], url: str
) -> None:
    """Dispatches a formatted embed alert card to Discord."""
    slot_key = f"{name}-{date}-" + ",".join(slots)
    if slot_key in sent_cache:
        print(f"⏩ Alert already sent recently for {name}. Skipping Discord post.")
        return

    color_map = {
        "resy": 0xE74C3C,
        "sevenrooms": 0x2C3E50,
        "opentable": 0xDA3743,
    }
    color = color_map.get(platform.lower(), 0x2ECC71)
    formatted_slots = "\n".join(f"• **{slot}**" for slot in slots)

    payload = {
        "username": "Reservation Sniper",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/3448/3448609.png",
        "embeds": [
            {
                "title": f"🚨 Open Table at {name}!",
                "description": f"New reservation openings found for **{date}**.",
                "url": url,
                "color": color,
                "fields": [
                    {
                        "name": "Platform",
                        "value": platform.capitalize(),
                        "inline": True,
                    },
                    {
                        "name": "Matching Slots",
                        "value": formatted_slots,
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": f"Scraped at {datetime.now().strftime('%H:%M:%S')}"
                },
            }
        ],
    }

    try:
        response = await run_blocking(
            cffi_requests.post,
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            impersonate="chrome120",
        )
        if response.status_code in (200, 204):
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Discord alert sent for {name}!")
            sent_cache.add(slot_key)
        else:
            print(f"❌ Failed to post to Discord: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"❌ Discord alert error: {e}")


# ==========================================
# PLATFORM SCRAPERS
# ==========================================
def _resy_get_json(
    url: str, headers: Dict[str, str], params: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """GETs a Resy endpoint, returning None on non-200, empty, or non-JSON responses."""
    response = cffi_requests.get(
        url, headers=headers, params=params, impersonate="chrome120", timeout=10
    )
    if response.status_code != 200 or not response.text.strip():
        return None
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def _resy_slot_time(slot: Dict[str, Any]) -> str:
    """Extracts the raw start timestamp from a Resy slot, whatever its shape."""
    date = slot.get("date")
    if isinstance(date, dict):
        return date.get("start", "")
    if isinstance(date, str):
        return date
    return slot.get("start", "")


async def check_resy(target: Dict[str, Any]) -> List[str]:
    """Queries Resy /4/find for a venue's slots, retrying once if the request fails."""
    headers = {
        "Authorization": f'ResyAPI api_key="{RESY_API_KEY}"',
        "X-Resy-Auth-Token": RESY_AUTH_TOKEN,
        "Accept": "application/json, text/plain, */*",
        "User-Agent": USER_AGENT,
        "Origin": "https://resy.com",
        "Referer": f"https://resy.com/cities/ny?date={target['date']}&seats={target['party_size']}",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    params = {
        "day": target["date"],
        "party_size": target["party_size"],
        "venue_id": target["venue_id"],
    }

    try:
        find_params = {**params, "lat": "0", "long": "0"}
        data = await run_blocking(
            _resy_get_json, "https://api.resy.com/4/find", headers, find_params
        )
        if data is None:
            # Request failed; retry once after a short pause.
            await asyncio.sleep(random.uniform(*REQUEST_DELAY))
            data = await run_blocking(
                _resy_get_json, "https://api.resy.com/4/find", headers, find_params
            )

        slots_data = []
        if data is not None:
            venues = data.get("results", {}).get("venues", []) or data.get("venues", [])
            if venues:
                slots_data = venues[0].get("slots", [])

        formatted = []
        for slot in slots_data:
            raw_time = _resy_slot_time(slot)
            if raw_time:
                cleaned = raw_time.replace("T", " ").split("+")[0].split(".")[0]
                dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
                formatted.append(dt.strftime("%I:%M %p"))
        return formatted

    except Exception as e:
        print(f"Error checking Resy ({target['name']}): {e}")
        return []


async def check_sevenrooms(target: Dict[str, Any]) -> List[str]:
    """Queries SevenRooms REST widget API directly."""
    date_obj = datetime.strptime(target["date"], "%Y-%m-%d")
    params = {
        "venue": target["venue_id"],
        "time": "19:00",
        "party_size": target["party_size"],
        "halo_size_interval": "16",
        "search_date": date_obj.strftime("%m-%d-%Y"),
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://www.sevenrooms.com/reservations/{target['venue_id']}",
    }

    try:
        response = await run_blocking(
            cffi_requests.get,
            "https://www.sevenrooms.com/api-yoogsl/availability/search",
            params=params,
            headers=headers,
            impersonate="chrome120",
            timeout=10,
        )
        if response.status_code != 200:
            return []

        times = response.json().get("data", {}).get("times", [])
        formatted = []
        for slot in times:
            if slot.get("access_persistent") is True or slot.get("type") == "bookable":
                time_str = slot.get("time")
                if time_str:
                    dt = datetime.strptime(time_str, "%H:%M:%S")
                    formatted.append(dt.strftime("%I:%M %p"))
        return formatted
    except Exception as e:
        print(f"Error checking SevenRooms ({target['name']}): {e}")
        return []


async def check_opentable(target: Dict[str, Any], playwright_context) -> List[str]:
    """Playwright DOM reader for OpenTable pages."""
    page = await playwright_context.new_page()
    slots = []
    try:
        await page.goto(target["booking_url"], wait_until="networkidle", timeout=15000)
        await asyncio.sleep(2)
        for el in await page.query_selector_all('[data-test="time-slot"]'):
            text = (await el.inner_text()).strip()
            if text:
                slots.append(text)
    except Exception as e:
        print(f"Error checking OpenTable ({target['name']}): {e}")
    finally:
        await page.close()
    return slots


# ==========================================
# MAIN EXECUTION ENGINE
# ==========================================
async def check_target(target: Dict[str, Any], context) -> None:
    platform = target["platform"]

    if platform == "resy":
        slots = await check_resy(target)
    elif platform == "sevenrooms":
        slots = await check_sevenrooms(target)
    elif platform == "opentable":
        slots = await check_opentable(target, context)
    else:
        print(f"Unknown platform {platform!r} for {target['name']}")
        return

    if slots and "time_window" in target:
        slots = filter_slots_by_time(
            slots, target["time_window"]["start"], target["time_window"]["end"]
        )

    if not slots:
        print(f"❌ No matching slots found for {target['name']}")
        return

    print(f"✅ Found {len(slots)} slots for {target['name']}: {', '.join(slots)}")
    await send_discord_alert(
        platform=platform,
        name=target["name"],
        date=target["date"],
        slots=slots,
        url=target["booking_url"],
    )


async def run_pipeline() -> None:
    print(f"\n--- Running Reservation Check [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ---")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        try:
            for i, target in enumerate(TARGETS):
                if i:
                    await asyncio.sleep(random.uniform(*REQUEST_DELAY))
                await check_target(target, context)
        finally:
            await browser.close()


async def main() -> None:
    while True:
        await run_pipeline()

        sleep_for = max(10, BASE_POLL_INTERVAL + random.uniform(-POLL_JITTER, POLL_JITTER))
        print(f"Sleeping for {sleep_for:.1f} seconds (~{sleep_for / 60:.1f} mins)...")
        await asyncio.sleep(sleep_for)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
