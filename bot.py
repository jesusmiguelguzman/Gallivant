#!/usr/bin/env python3
"""
Gallivant Bot — Hunts cheap flights & error fares across multiple sources.
Sends Telegram alerts in real time when new deals are found.

Sources: SecretFlying, Fly4free, TheFlightDeal, HolidayPirates,
         Airfarewatchdog, ManyFlights, Flightlist
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import feedparser
import httpx
from bs4 import BeautifulSoup

# ─── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ["BOT_TOKEN"]
CHAT_ID       = os.environ["CHAT_ID"]
DATA_DIR      = Path(os.environ.get("DATA_DIR", "/data"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "1800"))  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_FILE = DATA_DIR / "seen_deals.json"
MAX_SEEN   = 5_000  # cap set size to avoid unbounded growth

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ─── Model ────────────────────────────────────────────────────────────────────
@dataclass
class Deal:
    deal_id:       str
    source:        str
    title:         str
    url:           str
    price:         str
    region:        str
    is_error_fare: bool
    published:     str
    description:   str = ""

# ─── Helpers ──────────────────────────────────────────────────────────────────
ERROR_KEYWORDS = {
    "error fare", "mistake fare", "mistake", "glitch",
    "accidental", "misfiled", "pricing error", "error fares",
}

REGION_EMOJIS = {
    "usa":           "🇺🇸",
    "canada":        "🇨🇦",
    "europe":        "🇪🇺",
    "asia":          "🌏",
    "caribbean":     "🏝️",
    "latin-america": "🌎",
    "latin america": "🌎",
    "africa":        "🌍",
    "oceania":       "🌊",
    "middle east":   "🕌",
    "cruise":        "🚢",
    "global":        "🌐",
}


def make_id(*parts: str) -> str:
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


def is_error_fare(text: str) -> bool:
    tl = text.lower()
    return any(kw in tl for kw in ERROR_KEYWORDS)


def extract_price(text: str) -> str:
    for pat in [
        r"\$\s*[\d,]+(?:\.\d{2})?",
        r"€\s*[\d,]+(?:\.\d{2})?",
        r"£\s*[\d,]+(?:\.\d{2})?",
        r"[\d,]+\s*(?:USD|EUR|GBP|AUD|CAD)\b",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return "Ver oferta"


def region_emoji(region: str) -> str:
    return REGION_EMOJIS.get(region.lower(), "✈️")


def md_esc(s: str) -> str:
    """Escape Markdown v1 special chars inside user-supplied strings."""
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s

# ─── State ────────────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen(seen: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lst = list(seen)
    if len(lst) > MAX_SEEN:
        lst = lst[-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps(lst))

# ─── Telegram ─────────────────────────────────────────────────────────────────
TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


async def send_deal(client: httpx.AsyncClient, deal: Deal) -> None:
    emoji = region_emoji(deal.region)
    parts = []

    if deal.is_error_fare:
        parts += ["🚨 *ERROR FARE* 🚨", ""]

    parts += [
        f"{emoji} *{md_esc(deal.title)}*",
        "",
        f"💰 {md_esc(deal.price)}",
        f"🗺️ Región: {deal.region.title()}",
        f"📡 Fuente: {deal.source}",
        "",
        f"🔗 [Ver y reservar]({deal.url})",
    ]

    resp = await client.post(TG_URL, json={
        "chat_id":                  CHAT_ID,
        "text":                     "\n".join(parts),
        "parse_mode":               "Markdown",
        "disable_web_page_preview": False,
    })
    resp.raise_for_status()

# ─── RSS helper ───────────────────────────────────────────────────────────────

def parse_rss(feed_url: str, source: str, region: str, *, force_error: bool = False) -> list[Deal]:
    feed  = feedparser.parse(feed_url)
    deals: list[Deal] = []

    for entry in feed.entries[:30]:
        title   = entry.get("title", "").strip()
        url     = entry.get("link",  "").strip()
        summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
        pub     = entry.get("published", datetime.utcnow().isoformat())

        if not title or not url:
            continue

        full = f"{title} {summary}"
        deals.append(Deal(
            deal_id       = make_id(source, url),
            source        = source,
            title         = title,
            url           = url,
            price         = extract_price(full),
            region        = region,
            is_error_fare = force_error or is_error_fare(full),
            published     = pub,
            description   = summary[:300],
        ))

    return deals

# ─── Scrapers ─────────────────────────────────────────────────────────────────

# SecretFlying ──────────────────────────────────────────────────────────────────
SECRET_FLYING_FEEDS = [
    ("https://www.secretflying.com/posts/category/usa-canada/feed/",   "USA",           False),
    ("https://www.secretflying.com/posts/category/europe/feed/",       "Europe",        False),
    ("https://www.secretflying.com/posts/category/asia/feed/",         "Asia",          False),
    ("https://www.secretflying.com/posts/category/caribbean/feed/",    "Caribbean",     False),
    ("https://www.secretflying.com/posts/category/latin-america/feed/","Latin-America", False),
    ("https://www.secretflying.com/posts/category/africa/feed/",       "Africa",        False),
    ("https://www.secretflying.com/posts/category/middle-east/feed/",  "Middle East",   False),
    ("https://www.secretflying.com/posts/category/oceania/feed/",      "Oceania",       False),
    ("https://www.secretflying.com/posts/category/error-fares/feed/",  "Global",        True),
    ("https://www.secretflying.com/posts/category/cruises/feed/",      "Cruise",        False),
]


async def scrape_secretflying(_: httpx.AsyncClient) -> list[Deal]:
    deals: list[Deal] = []
    for url, region, error in SECRET_FLYING_FEEDS:
        try:
            d = parse_rss(url, "SecretFlying", region, force_error=error)
            deals.extend(d)
            log.info(f"  SecretFlying [{region}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  SecretFlying [{region}] failed: {exc}")
    return deals


# Fly4free ──────────────────────────────────────────────────────────────────────
FLY4FREE_FEEDS = [
    ("https://www.fly4free.com/feed/",                                      "Global", False),
    ("https://www.fly4free.com/flight-deals/north-america/feed/",           "USA",    False),
    ("https://www.fly4free.com/flight-deals/europe/feed/",                  "Europe", False),
    ("https://www.fly4free.com/flight-deals/asia-pacific/feed/",            "Asia",   False),
    ("https://www.fly4free.com/flight-deals/latin-america-caribbean/feed/", "Caribbean", False),
    ("https://www.fly4free.com/error-fares/feed/",                          "Global", True),
]


async def scrape_fly4free(_: httpx.AsyncClient) -> list[Deal]:
    deals: list[Deal] = []
    for url, region, error in FLY4FREE_FEEDS:
        try:
            d = parse_rss(url, "Fly4free", region, force_error=error)
            deals.extend(d)
            log.info(f"  Fly4free [{region}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  Fly4free [{region}] failed: {exc}")
    return deals


# TheFlightDeal ─────────────────────────────────────────────────────────────────
THE_FLIGHT_DEAL_FEEDS = [
    ("https://www.theflightdeal.com/feed/",                    "USA",    False),
    ("https://www.theflightdeal.com/category/error-fare/feed/","Global", True),
]


async def scrape_theflightdeal(_: httpx.AsyncClient) -> list[Deal]:
    deals: list[Deal] = []
    for url, region, error in THE_FLIGHT_DEAL_FEEDS:
        try:
            d = parse_rss(url, "TheFlightDeal", region, force_error=error)
            deals.extend(d)
            log.info(f"  TheFlightDeal [{region}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  TheFlightDeal [{region}] failed: {exc}")
    return deals


# HolidayPirates ────────────────────────────────────────────────────────────────
HOLIDAY_PIRATES_FEEDS = [
    ("https://us.holidaypirates.com/feed",  "USA"),
    ("https://www.holidaypirates.com/feed", "Europe"),
]


async def scrape_holidaypirates(_: httpx.AsyncClient) -> list[Deal]:
    deals: list[Deal] = []
    for url, region in HOLIDAY_PIRATES_FEEDS:
        try:
            d = parse_rss(url, "HolidayPirates", region)
            deals.extend(d)
            log.info(f"  HolidayPirates [{region}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  HolidayPirates [{region}] failed: {exc}")
    return deals


# Airfarewatchdog ───────────────────────────────────────────────────────────────
async def scrape_airfarewatchdog(client: httpx.AsyncClient) -> list[Deal]:
    url = "https://www.airfarewatchdog.com/cheap-flights/"
    try:
        r    = await client.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        deals: list[Deal] = []

        # Try multiple CSS patterns — site may change layout
        cards = []
        for sel in ("article.deal", ".deal-card", ".fare-deal",
                    "[class*='DealCard']", "[class*='deal-item']"):
            cards = soup.select(sel)
            if cards:
                break

        for card in cards[:25]:
            title_el = card.select_one("h2, h3, [class*='title']")
            price_el = card.select_one("[class*='price'], .price")
            link_el  = card.select_one("a[href]")
            if not title_el or not link_el:
                continue

            title = title_el.get_text(" ", strip=True)
            price = price_el.get_text(strip=True) if price_el else extract_price(card.get_text())
            href  = link_el["href"]
            if not href.startswith("http"):
                href = "https://www.airfarewatchdog.com" + href

            deals.append(Deal(
                deal_id       = make_id("airfarewatchdog", href),
                source        = "Airfarewatchdog",
                title         = title,
                url           = href,
                price         = price,
                region        = "USA",
                is_error_fare = is_error_fare(title),
                published     = datetime.utcnow().isoformat(),
            ))

        log.info(f"  Airfarewatchdog: {len(deals)}")
        return deals
    except Exception as exc:
        log.warning(f"  Airfarewatchdog failed: {exc}")
        return []


# ManyFlights ───────────────────────────────────────────────────────────────────
async def scrape_manyflights(client: httpx.AsyncClient) -> list[Deal]:
    url = "https://manyflights.io/"
    try:
        r    = await client.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        deals: list[Deal] = []

        for card in soup.select("article, [class*='deal'], [class*='flight']")[:25]:
            title_el = card.select_one("h2, h3, [class*='title']")
            price_el = card.select_one("[class*='price']")
            link_el  = card.select_one("a[href]")
            if not title_el or not link_el:
                continue

            title = title_el.get_text(" ", strip=True)
            href  = link_el["href"]
            if not href.startswith("http"):
                href = "https://manyflights.io" + href
            price = price_el.get_text(strip=True) if price_el else extract_price(card.get_text())

            deals.append(Deal(
                deal_id       = make_id("manyflights", href),
                source        = "ManyFlights",
                title         = title,
                url           = href,
                price         = price,
                region        = "Global",
                is_error_fare = is_error_fare(title),
                published     = datetime.utcnow().isoformat(),
            ))

        log.info(f"  ManyFlights: {len(deals)}")
        return deals
    except Exception as exc:
        log.warning(f"  ManyFlights failed: {exc}")
        return []


# Flightlist ────────────────────────────────────────────────────────────────────
async def scrape_flightlist(client: httpx.AsyncClient) -> list[Deal]:
    for page_url in ("https://www.flightlist.io/deals", "https://www.flightlist.io/"):
        try:
            r    = await client.get(page_url, headers=HEADERS, timeout=20)
            soup = BeautifulSoup(r.text, "html.parser")
            deals: list[Deal] = []

            for card in soup.select("[class*='deal'], article, [class*='card']")[:25]:
                title_el = card.select_one("h2, h3, [class*='title']")
                price_el = card.select_one("[class*='price']")
                link_el  = card.select_one("a[href]")
                if not title_el or not link_el:
                    continue

                title = title_el.get_text(" ", strip=True)
                href  = link_el["href"]
                if not href.startswith("http"):
                    href = "https://www.flightlist.io" + href
                price = price_el.get_text(strip=True) if price_el else extract_price(card.get_text())

                deals.append(Deal(
                    deal_id       = make_id("flightlist", href),
                    source        = "Flightlist",
                    title         = title,
                    url           = href,
                    price         = price,
                    region        = "Global",
                    is_error_fare = is_error_fare(title),
                    published     = datetime.utcnow().isoformat(),
                ))

            if deals:
                log.info(f"  Flightlist: {len(deals)}")
                return deals
        except Exception as exc:
            log.warning(f"  Flightlist [{page_url}] failed: {exc}")

    return []

# ─── Orchestration ────────────────────────────────────────────────────────────
SCRAPERS = [
    ("SecretFlying",    scrape_secretflying),
    ("Fly4free",        scrape_fly4free),
    ("TheFlightDeal",   scrape_theflightdeal),
    ("HolidayPirates",  scrape_holidaypirates),
    ("Airfarewatchdog", scrape_airfarewatchdog),
    ("ManyFlights",     scrape_manyflights),
    ("Flightlist",      scrape_flightlist),
]


async def run_poll() -> None:
    seen         = load_seen()
    is_first_run = len(seen) == 0

    async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
        all_deals: list[Deal] = []

        for name, scraper in SCRAPERS:
            log.info(f"Scraping {name}…")
            deals = await scraper(client)
            all_deals.extend(deals)

        new_deals = [d for d in all_deals if d.deal_id not in seen]
        log.info(f"Total: {len(all_deals)} | New: {len(new_deals)}")

        # First run: seed state silently — no alerts
        if is_first_run:
            log.info("First run — seeding state, no alerts sent.")
            for d in all_deals:
                seen.add(d.deal_id)
            save_seen(seen)
            return

        # Send new deals
        sent = 0
        for deal in new_deals:
            try:
                await send_deal(client, deal)
                seen.add(deal.deal_id)
                sent += 1
                await asyncio.sleep(0.8)   # Telegram rate-limit buffer
            except Exception as exc:
                log.error(f"  Send failed [{deal.deal_id}]: {exc}")

        log.info(f"Sent {sent} alert(s).")
        save_seen(seen)


async def main() -> None:
    log.info("✈️  Gallivant Bot started")
    while True:
        try:
            await run_poll()
        except Exception as exc:
            log.error(f"Poll cycle error: {exc}")
        log.info(f"Sleeping {POLL_INTERVAL}s…")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
