#!/usr/bin/env python3
"""
Gallivant — Hunts cheap flights, cruises & hotel error fares across multiple sources.
Sends Telegram alerts in real time when new deals are found.

RSS sources:  SecretFlying (10 feeds), Fly4free (6), TheFlightDeal (2),
              HolidayPirates (2), ThePointsGuy, ViewFromTheWing, OneMilleAtATime,
              Travelzoo (3)
JS sources:   Airfarewatchdog (Playwright)
JSON API:     Reddit (r/flightdeals, r/Flights, r/CruiseDeals, r/HotelDeals,
                      r/travel, r/solotravel, r/awardtravel, r/churning)
Scrape:       ManyFlights, Flightlist, Wandr
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ─── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ["BOT_TOKEN"]
CHAT_ID       = os.environ["CHAT_ID"]
DATA_DIR      = Path(os.environ.get("DATA_DIR", "/data"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "1800"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_FILE = DATA_DIR / "seen_deals.json"
MAX_SEEN   = 10_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

REDDIT_HEADERS = {
    "User-Agent": "Gallivant/1.0 (flight deal alert bot; contact via github.com/jesusmiguelguzman/Gallivant)",
    "Accept": "application/json",
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
    "error fare", "mistake fare", "mistake", "glitch", "accidental",
    "misfiled", "pricing error", "error fares", "mistake deal",
}

DEAL_KEYWORDS = {
    "flight", "fare", "airline", "fly", "roundtrip", "round trip",
    "airfare", "cheap", "deal", "sale", "cruise", "hotel", "resort",
    "nonstop", "$", "€", "£", "usd", "eur", "gbp", "off", "discount",
    "book now", "limited time", "flash sale",
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
    "hotel":         "🏨",
    "global":        "🌐",
}


def make_id(*parts: str) -> str:
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


def is_error_fare(text: str) -> bool:
    tl = text.lower()
    return any(kw in tl for kw in ERROR_KEYWORDS)


def is_travel_deal(text: str) -> bool:
    tl = text.lower()
    return any(kw in tl for kw in DEAL_KEYWORDS)


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

# ─── RSS core (httpx-powered — bypasses User-Agent blocks) ────────────────────

async def fetch_rss_entries(
    client: httpx.AsyncClient,
    url: str,
    *,
    limit: int = 30,
) -> list:
    r = await client.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    feed = feedparser.parse(r.text)
    return feed.entries[:limit]


def entry_to_deal(
    entry,
    source: str,
    region: str,
    *,
    force_error: bool = False,
    deal_filter: bool = False,
) -> "Deal | None":
    title   = entry.get("title", "").strip()
    url     = entry.get("link",  "").strip()
    summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
    pub     = entry.get("published", datetime.now(timezone.utc).isoformat())

    if not title or not url:
        return None

    full = f"{title} {summary}"
    if deal_filter and not is_travel_deal(full):
        return None

    return Deal(
        deal_id       = make_id(source, url),
        source        = source,
        title         = title,
        url           = url,
        price         = extract_price(full),
        region        = region,
        is_error_fare = force_error or is_error_fare(full),
        published     = pub,
        description   = summary[:300],
    )

# ─── RSS Scrapers ─────────────────────────────────────────────────────────────

# SecretFlying — 10 feeds ──────────────────────────────────────────────────────
SECRET_FLYING_FEEDS = [
    ("https://www.secretflying.com/posts/category/usa-canada/feed/",    "USA",           False),
    ("https://www.secretflying.com/posts/category/europe/feed/",        "Europe",        False),
    ("https://www.secretflying.com/posts/category/asia/feed/",          "Asia",          False),
    ("https://www.secretflying.com/posts/category/caribbean/feed/",     "Caribbean",     False),
    ("https://www.secretflying.com/posts/category/latin-america/feed/", "Latin-America", False),
    ("https://www.secretflying.com/posts/category/africa/feed/",        "Africa",        False),
    ("https://www.secretflying.com/posts/category/middle-east/feed/",   "Middle East",   False),
    ("https://www.secretflying.com/posts/category/oceania/feed/",       "Oceania",       False),
    ("https://www.secretflying.com/posts/category/error-fares/feed/",   "Global",        True),
    ("https://www.secretflying.com/posts/category/cruises/feed/",       "Cruise",        False),
]


async def scrape_secretflying(client: httpx.AsyncClient) -> list[Deal]:
    deals: list[Deal] = []
    for url, region, error in SECRET_FLYING_FEEDS:
        try:
            entries = await fetch_rss_entries(client, url)
            d = [e for e in (entry_to_deal(e, "SecretFlying", region, force_error=error) for e in entries) if e]
            deals.extend(d)
            log.info(f"  SecretFlying [{region}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  SecretFlying [{region}] failed: {exc}")
    return deals


# Fly4free — 6 feeds ───────────────────────────────────────────────────────────
FLY4FREE_FEEDS = [
    ("https://www.fly4free.com/feed/",                                       "Global",    False),
    ("https://www.fly4free.com/flight-deals/north-america/feed/",            "USA",       False),
    ("https://www.fly4free.com/flight-deals/europe/feed/",                   "Europe",    False),
    ("https://www.fly4free.com/flight-deals/asia-pacific/feed/",             "Asia",      False),
    ("https://www.fly4free.com/flight-deals/latin-america-caribbean/feed/",  "Caribbean", False),
    ("https://www.fly4free.com/error-fares/feed/",                           "Global",    True),
]


async def scrape_fly4free(client: httpx.AsyncClient) -> list[Deal]:
    deals: list[Deal] = []
    for url, region, error in FLY4FREE_FEEDS:
        try:
            entries = await fetch_rss_entries(client, url)
            d = [e for e in (entry_to_deal(e, "Fly4free", region, force_error=error) for e in entries) if e]
            deals.extend(d)
            log.info(f"  Fly4free [{region}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  Fly4free [{region}] failed: {exc}")
    return deals


# TheFlightDeal — 2 feeds ──────────────────────────────────────────────────────
THE_FLIGHT_DEAL_FEEDS = [
    ("https://www.theflightdeal.com/feed/",                     "USA",    False),
    ("https://www.theflightdeal.com/category/error-fare/feed/", "Global", True),
]


async def scrape_theflightdeal(client: httpx.AsyncClient) -> list[Deal]:
    deals: list[Deal] = []
    for url, region, error in THE_FLIGHT_DEAL_FEEDS:
        try:
            entries = await fetch_rss_entries(client, url)
            d = [e for e in (entry_to_deal(e, "TheFlightDeal", region, force_error=error) for e in entries) if e]
            deals.extend(d)
            log.info(f"  TheFlightDeal [{region}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  TheFlightDeal [{region}] failed: {exc}")
    return deals


# HolidayPirates — 2 feeds ────────────────────────────────────────────────────
HOLIDAY_PIRATES_FEEDS = [
    ("https://www.holidaypirates.com/feed",    "Europe"),
    ("https://cruisepirates.com/feed",         "Cruise"),
]


async def scrape_holidaypirates(client: httpx.AsyncClient) -> list[Deal]:
    deals: list[Deal] = []
    for url, region in HOLIDAY_PIRATES_FEEDS:
        try:
            entries = await fetch_rss_entries(client, url)
            d = [e for e in (entry_to_deal(e, "HolidayPirates", region) for e in entries) if e]
            deals.extend(d)
            log.info(f"  HolidayPirates [{region}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  HolidayPirates [{region}] failed: {exc}")
    return deals


# Travelzoo — 3 feeds ─────────────────────────────────────────────────────────
TRAVELZOO_FEEDS = [
    ("https://www.travelzoo.com/rss/deals/flights/",        "Global"),
    ("https://www.travelzoo.com/rss/deals/flights/us/",     "USA"),
    ("https://www.travelzoo.com/rss/deals/flights/europe/", "Europe"),
    ("https://www.travelzoo.com/rss/deals/hotels/",         "Hotel"),
    ("https://www.travelzoo.com/rss/deals/cruises/",        "Cruise"),
]


async def scrape_travelzoo(client: httpx.AsyncClient) -> list[Deal]:
    deals: list[Deal] = []
    for url, region in TRAVELZOO_FEEDS:
        try:
            entries = await fetch_rss_entries(client, url)
            d = [e for e in (entry_to_deal(e, "Travelzoo", region) for e in entries) if e]
            deals.extend(d)
            log.info(f"  Travelzoo [{region}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  Travelzoo [{region}] failed: {exc}")
    return deals


# ThePointsGuy — deal-filtered ────────────────────────────────────────────────
TPG_FEEDS = [
    ("https://thepointsguy.com/feed/",              "Global"),
    ("https://thepointsguy.com/deals/feed/",        "Global"),
    ("https://thepointsguy.com/guide/cheap-flights/feed/", "Global"),
]


async def scrape_thepointsguy(client: httpx.AsyncClient) -> list[Deal]:
    deals: list[Deal] = []
    for url, region in TPG_FEEDS:
        try:
            entries = await fetch_rss_entries(client, url)
            d = [e for e in (entry_to_deal(e, "ThePointsGuy", region, deal_filter=True) for e in entries) if e]
            deals.extend(d)
            log.info(f"  ThePointsGuy [{url.split('/')[-2]}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  ThePointsGuy [{url}] failed: {exc}")
    return deals


# View from the Wing — deal-filtered ──────────────────────────────────────────
async def scrape_viewfromthewing(client: httpx.AsyncClient) -> list[Deal]:
    feeds = [
        ("https://viewfromthewing.com/feed/",                          "Global"),
        ("https://viewfromthewing.com/category/deals/feed/",           "Global"),
        ("https://viewfromthewing.com/category/airfare-deals/feed/",   "Global"),
    ]
    deals: list[Deal] = []
    for url, region in feeds:
        try:
            entries = await fetch_rss_entries(client, url)
            d = [e for e in (entry_to_deal(e, "ViewFromTheWing", region, deal_filter=True) for e in entries) if e]
            deals.extend(d)
            log.info(f"  ViewFromTheWing [{url.split('/')[-2]}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  ViewFromTheWing [{url}] failed: {exc}")
    return deals


# One Mile at a Time — deal-filtered ──────────────────────────────────────────
async def scrape_onemileatatime(client: httpx.AsyncClient) -> list[Deal]:
    feeds = [
        ("https://onemileatatime.com/feed/",                         "Global"),
        ("https://onemileatatime.com/category/deals/feed/",          "Global"),
        ("https://onemileatatime.com/category/flight-deals/feed/",   "Global"),
    ]
    deals: list[Deal] = []
    for url, region in feeds:
        try:
            entries = await fetch_rss_entries(client, url)
            d = [e for e in (entry_to_deal(e, "OneMilleAtATime", region, deal_filter=True) for e in entries) if e]
            deals.extend(d)
            log.info(f"  OneMilleAtATime [{url.split('/')[-2]}]: {len(d)}")
        except Exception as exc:
            log.warning(f"  OneMilleAtATime [{url}] failed: {exc}")
    return deals

# ─── Reddit JSON API ──────────────────────────────────────────────────────────

REDDIT_SUBS = [
    # (subreddit, region, force_error, deal_filter)
    ("flightdeals",  "Global", False, False),   # 100% deals
    ("Flights",      "Global", False, True),    # mixed, filter
    ("CruiseDeals",  "Cruise", False, False),   # 100% cruise deals
    ("HotelDeals",   "Hotel",  False, False),   # 100% hotel deals
    ("travel",       "Global", False, True),    # filter
    ("solotravel",   "Global", False, True),    # filter
    ("awardtravel",  "Global", False, True),    # filter
    ("churning",     "Global", False, True),    # credit card deals, filter
    ("vacationdeals","Global", False, False),   # 100% deals
    ("deals",        "Global", False, True),    # broad, filter for travel
]


async def scrape_reddit(client: httpx.AsyncClient) -> list[Deal]:
    deals: list[Deal] = []
    for sub, region, force_error, deal_filter in REDDIT_SUBS:
        url = f"https://www.reddit.com/r/{sub}/new.json?limit=25"
        try:
            r = await client.get(url, headers=REDDIT_HEADERS, timeout=20)
            r.raise_for_status()
            posts = r.json()["data"]["children"]
            sub_deals: list[Deal] = []

            for post in posts:
                p     = post["data"]
                title = p.get("title", "").strip()
                href  = f"https://reddit.com{p.get('permalink', '')}"
                body  = p.get("selftext", "")[:500]
                full  = f"{title} {body}"

                if deal_filter and not is_travel_deal(full):
                    continue

                # Skip meta/mod posts
                if any(kw in title.lower() for kw in ("weekly thread", "megathread", "discussion", "question", "mod post")):
                    continue

                pub = datetime.fromtimestamp(
                    p.get("created_utc", 0), tz=timezone.utc
                ).isoformat()

                sub_deals.append(Deal(
                    deal_id       = make_id("reddit", p.get("id", href)),
                    source        = f"Reddit r/{sub}",
                    title         = title,
                    url           = href,
                    price         = extract_price(full),
                    region        = region,
                    is_error_fare = force_error or is_error_fare(full),
                    published     = pub,
                    description   = body[:300],
                ))

            deals.extend(sub_deals)
            log.info(f"  Reddit r/{sub}: {len(sub_deals)}")
            await asyncio.sleep(0.5)   # Reddit rate limit

        except Exception as exc:
            log.warning(f"  Reddit r/{sub} failed: {exc}")

    return deals

# ─── Playwright scrapers (JavaScript-heavy sites) ─────────────────────────────

async def fetch_with_playwright(url: str, wait: str = "networkidle") -> str:
    """Render a JS-heavy page and return its HTML."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx     = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page    = await ctx.new_page()
        await page.goto(url, timeout=40_000, wait_until=wait)
        await page.wait_for_timeout(3000)   # let JS settle
        html = await page.content()
        await browser.close()
    return html


async def scrape_airfarewatchdog(_: httpx.AsyncClient) -> list[Deal]:
    try:
        html = await fetch_with_playwright("https://www.airfarewatchdog.com/cheap-flights/")
        soup = BeautifulSoup(html, "html.parser")
        deals: list[Deal] = []

        cards = []
        for sel in (
            "article.deal", ".deal-card", ".fare-deal",
            "[class*='DealCard']", "[class*='deal-item']",
            "[class*='FareDeal']", "[data-testid*='deal']",
        ):
            cards = soup.select(sel)
            if cards:
                break

        # Fallback: any link that contains a price
        if not cards:
            for a in soup.select("a[href]")[:60]:
                text = a.get_text(" ", strip=True)
                if extract_price(text) != "Ver oferta" and len(text) > 15:
                    title = text[:120]
                    href  = a["href"]
                    if not href.startswith("http"):
                        href = "https://www.airfarewatchdog.com" + href
                    deals.append(Deal(
                        deal_id       = make_id("airfarewatchdog", href),
                        source        = "Airfarewatchdog",
                        title         = title,
                        url           = href,
                        price         = extract_price(text),
                        region        = "USA",
                        is_error_fare = is_error_fare(title),
                        published     = datetime.now(timezone.utc).isoformat(),
                    ))

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
                published     = datetime.now(timezone.utc).isoformat(),
            ))

        log.info(f"  Airfarewatchdog: {len(deals)}")
        return deals
    except Exception as exc:
        log.warning(f"  Airfarewatchdog failed: {exc}")
        return []

# ─── HTTP Scrapers ────────────────────────────────────────────────────────────

async def scrape_manyflights(client: httpx.AsyncClient) -> list[Deal]:
    try:
        r    = await client.get("https://manyflights.io/", headers=HEADERS, timeout=20)
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
                published     = datetime.now(timezone.utc).isoformat(),
            ))

        log.info(f"  ManyFlights: {len(deals)}")
        return deals
    except Exception as exc:
        log.warning(f"  ManyFlights failed: {exc}")
        return []


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
                    published     = datetime.now(timezone.utc).isoformat(),
                ))

            if deals:
                log.info(f"  Flightlist: {len(deals)}")
                return deals
        except Exception as exc:
            log.warning(f"  Flightlist [{page_url}] failed: {exc}")

    return []


async def scrape_wandr(client: httpx.AsyncClient) -> list[Deal]:
    for url in ("https://wandr.me/deals", "https://wandr.me/"):
        try:
            r    = await client.get(url, headers=HEADERS, timeout=20)
            soup = BeautifulSoup(r.text, "html.parser")
            deals: list[Deal] = []

            for card in soup.select("[class*='deal'], article, [class*='flight'], [class*='card']")[:25]:
                title_el = card.select_one("h2, h3, [class*='title']")
                price_el = card.select_one("[class*='price']")
                link_el  = card.select_one("a[href]")
                if not title_el or not link_el:
                    continue

                title = title_el.get_text(" ", strip=True)
                href  = link_el["href"]
                if not href.startswith("http"):
                    href = "https://wandr.me" + href
                price = price_el.get_text(strip=True) if price_el else extract_price(card.get_text())

                deals.append(Deal(
                    deal_id       = make_id("wandr", href),
                    source        = "Wandr",
                    title         = title,
                    url           = href,
                    price         = price,
                    region        = "Global",
                    is_error_fare = is_error_fare(title),
                    published     = datetime.now(timezone.utc).isoformat(),
                ))

            if deals:
                log.info(f"  Wandr: {len(deals)}")
                return deals
        except Exception as exc:
            log.warning(f"  Wandr [{url}] failed: {exc}")

    return []

# ─── Orchestration ────────────────────────────────────────────────────────────
SCRAPERS = [
    ("SecretFlying",    scrape_secretflying),
    ("Fly4free",        scrape_fly4free),
    ("TheFlightDeal",   scrape_theflightdeal),
    ("HolidayPirates",  scrape_holidaypirates),
    ("Travelzoo",       scrape_travelzoo),
    ("ThePointsGuy",    scrape_thepointsguy),
    ("ViewFromTheWing", scrape_viewfromthewing),
    ("OneMilleAtATime", scrape_onemileatatime),
    ("Reddit",          scrape_reddit),
    ("Airfarewatchdog", scrape_airfarewatchdog),   # Playwright — runs last
    ("ManyFlights",     scrape_manyflights),
    ("Flightlist",      scrape_flightlist),
    ("Wandr",           scrape_wandr),
]


async def run_poll() -> None:
    seen         = load_seen()
    is_first_run = len(seen) == 0

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        all_deals: list[Deal] = []

        for name, scraper in SCRAPERS:
            log.info(f"Scraping {name}…")
            deals = await scraper(client)
            all_deals.extend(deals)

        new_deals = [d for d in all_deals if d.deal_id not in seen]
        log.info(f"Total: {len(all_deals)} | New: {len(new_deals)}")

        if is_first_run:
            log.info("First run — seeding state, no alerts sent.")
            for d in all_deals:
                seen.add(d.deal_id)
            save_seen(seen)
            return

        sent = 0
        for deal in new_deals:
            try:
                await send_deal(client, deal)
                seen.add(deal.deal_id)
                sent += 1
                await asyncio.sleep(0.8)
            except Exception as exc:
                log.error(f"  Send failed [{deal.deal_id}]: {exc}")

        log.info(f"Sent {sent} alert(s).")
        save_seen(seen)


async def main() -> None:
    log.info("✈️  Gallivant started")
    while True:
        try:
            await run_poll()
        except Exception as exc:
            log.error(f"Poll cycle error: {exc}")
        log.info(f"Sleeping {POLL_INTERVAL}s…")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
