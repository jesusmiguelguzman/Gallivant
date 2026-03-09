#!/usr/bin/env python3
"""
Gallivant — Hunts cheap flights, cruises & hotel error fares across multiple sources.
Sends Telegram alerts in real time when new deals are found.

RSS sources:  SecretFlying (10 feeds), Fly4free (6), TheFlightDeal (2),
              HolidayPirates (2), ThePointsGuy, ViewFromTheWing, OneMilleAtATime,
              Travelzoo (3)
JS sources:   Airfarewatchdog (Playwright)
Scrape:       ManyFlights, Flightlist, Wandr
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

# ─── Tier config ──────────────────────────────────────────────────────────────
# MIN_TIER: "drizzle deal" | "simmering save" | "flambé fare" | "hullabaloo deal"
# (also accepts short forms: drizzle | simmering | flambe | hullabaloo)
_TIER_LEVELS = {
    "drizzle deal": 0,    "drizzle": 0,
    "simmering save": 1,  "simmering": 1,
    "flambé fare": 2,     "flambe fare": 2, "flambé": 2, "flambe": 2,
    "hullabaloo deal": 3, "hullabaloo": 3,
}
MIN_TIER_LEVEL = _TIER_LEVELS.get(os.environ.get("MIN_TIER", "drizzle deal").lower(), 0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_FILE   = DATA_DIR / "seen_deals.json"
DIGEST_FILE  = DATA_DIR / "daily_digest.json"
PREFS_FILE   = DATA_DIR / "user_prefs.json"
MAX_SEEN     = 10_000
DIGEST_HOUR  = int(os.environ.get("DIGEST_HOUR", "20"))   # UTC hour to send daily digest

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


# ─── Model ────────────────────────────────────────────────────────────────────
@dataclass
class Deal:
    deal_id:       str
    source:        str
    title:         str
    url:           str
    price:         str
    region:        str
    deal_type:     str        # "Vuelo", "Hotel", "Crucero", "Paquete", "Millas", "Deal"
    is_error_fare: bool
    published:     str
    description:   str = ""

# ─── Helpers ──────────────────────────────────────────────────────────────────
ERROR_KEYWORDS = {
    "error fare", "mistake fare", "mistake", "glitch", "accidental",
    "misfiled", "pricing error", "error fares", "mistake deal",
}

# ─── Deal Tiers ───────────────────────────────────────────────────────────────
# (name, emoji, level)  level 0 = lowest, 3 = highest
TIERS = [
    ("Drizzle Deal",    "🌧️", 0),
    ("Simmering Save",  "🍲", 1),
    ("Flambé Fare",     "🔥", 2),
    ("Hullabaloo Deal", "🤯", 3),
]

# Keywords that bump a deal to a higher tier (checked in order Hullabaloo → Simmering)
_HULLABALOO_KW = {
    "error fare", "mistake fare", "glitch fare", "accidental fare", "misfiled fare",
    "pricing error", "too good to be true", "unbelievable deal",
}
_FLAMBE_KW = {
    "flash sale", "massive discount", "incredible deal", "insane deal",
    "steal of the century", "ridiculously cheap", "jaw-dropping", "once in a lifetime",
    "limited time offer", "mega sale",
}
_SIMMERING_KW = {
    "great deal", "good deal", "solid deal", "budget flight", "affordable",
    "cheap flight", "discount fare", "bargain",
}


def _parse_pct(text: str) -> int | None:
    """Extract the first explicit '% off' percentage found in text, or None."""
    m = re.search(r"(\d{1,3})\s*%\s*off", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _parse_was_now(text: str) -> float | None:
    """
    Try to extract an implied discount % from 'was $X now $Y' patterns.
    Returns a float 0-1, or None if not found / not a valid discount.
    """
    pat = re.search(
        r"(?:was|from|regularly|normally|rrp|valued at)[^\d$€£]*[$€£]?([\d,]+)"
        r".{1,40}?(?:now|for|only|just)[^\d$€£]*[$€£]?([\d,]+)",
        text, re.IGNORECASE,
    )
    if pat:
        original = float(pat.group(1).replace(",", ""))
        current  = float(pat.group(2).replace(",", ""))
        if original > 0 and 0 < current < original:
            return (original - current) / original
    return None


def classify_tier(is_error_fare_flag: bool, title: str, description: str) -> tuple[str, str, int]:
    """Return (tier_name, tier_emoji, tier_level) for a deal."""
    full = f"{title} {description}".lower()

    # Error-fare flag → always Hullabaloo
    if is_error_fare_flag:
        return TIERS[3]

    # Explicit % off in text
    # Drizzle ~20% | Simmering ~40% | Flambé ~60% | Hullabaloo ~80-90%
    pct = _parse_pct(full)
    if pct is not None:
        if pct >= 75: return TIERS[3]   # 🤯 Hullabaloo Deal
        if pct >= 50: return TIERS[2]   # 🔥 Flambé Fare
        if pct >= 30: return TIERS[1]   # 🍲 Simmering Save
        return TIERS[0]                 # 🌧️ Drizzle Deal

    # Price-comparison pattern (was / now)
    disc = _parse_was_now(full)
    if disc is not None:
        if disc >= 0.75: return TIERS[3]
        if disc >= 0.50: return TIERS[2]
        if disc >= 0.30: return TIERS[1]
        return TIERS[0]

    # Keyword heuristics
    if any(kw in full for kw in _HULLABALOO_KW): return TIERS[3]
    if any(kw in full for kw in _FLAMBE_KW):     return TIERS[2]
    if any(kw in full for kw in _SIMMERING_KW):  return TIERS[1]

    return TIERS[0]  # default: Drizzle

# Source homepage URLs for the "Fuente" link
SOURCE_URLS: dict[str, str] = {
    "SecretFlying":    "https://www.secretflying.com",
    "Fly4free":        "https://www.fly4free.com",
    "TheFlightDeal":   "https://www.theflightdeal.com",
    "HolidayPirates":  "https://www.holidaypirates.com",
    "Travelzoo":       "https://www.travelzoo.com",
    "ThePointsGuy":    "https://thepointsguy.com",
    "ViewFromTheWing": "https://viewfromthewing.com",
    "OneMilleAtATime": "https://onemileatatime.com",
    "Airfarewatchdog": "https://www.airfarewatchdog.com",
    "ManyFlights":     "https://manyflights.io",
    "Flightlist":      "https://www.flightlist.io",
    "Wandr":           "https://wandr.me",
}

# ─── Country detection ────────────────────────────────────────────────────────
# (flag_emoji, hashtag) keyed by lowercase country name variants
COUNTRY_MAP: dict[str, tuple[str, str]] = {
    # Americas
    "united states": ("🇺🇸", "#USA"), "usa": ("🇺🇸", "#USA"), "u.s.": ("🇺🇸", "#USA"),
    "canada": ("🇨🇦", "#Canada"),
    "mexico": ("🇲🇽", "#Mexico"), "méxico": ("🇲🇽", "#Mexico"),
    "brazil": ("🇧🇷", "#Brazil"), "brasil": ("🇧🇷", "#Brazil"),
    "argentina": ("🇦🇷", "#Argentina"),
    "chile": ("🇨🇱", "#Chile"),
    "colombia": ("🇨🇴", "#Colombia"),
    "peru": ("🇵🇪", "#Peru"), "perú": ("🇵🇪", "#Peru"),
    "ecuador": ("🇪🇨", "#Ecuador"),
    "bolivia": ("🇧🇴", "#Bolivia"),
    "venezuela": ("🇻🇪", "#Venezuela"),
    "uruguay": ("🇺🇾", "#Uruguay"),
    "paraguay": ("🇵🇾", "#Paraguay"),
    "costa rica": ("🇨🇷", "#CostaRica"),
    "panama": ("🇵🇦", "#Panama"), "panamá": ("🇵🇦", "#Panama"),
    "cuba": ("🇨🇺", "#Cuba"),
    "dominican republic": ("🇩🇴", "#DominicanRepublic"),
    "puerto rico": ("🇵🇷", "#PuertoRico"),
    "jamaica": ("🇯🇲", "#Jamaica"),
    "bahamas": ("🇧🇸", "#Bahamas"),
    "barbados": ("🇧🇧", "#Barbados"),
    "trinidad": ("🇹🇹", "#Trinidad"),
    "guatemala": ("🇬🇹", "#Guatemala"),
    "honduras": ("🇭🇳", "#Honduras"),
    "nicaragua": ("🇳🇮", "#Nicaragua"),
    "el salvador": ("🇸🇻", "#ElSalvador"),
    # Europe
    "united kingdom": ("🇬🇧", "#UK"), "uk": ("🇬🇧", "#UK"), "britain": ("🇬🇧", "#UK"),
    "england": ("🏴󠁧󠁢󠁥󠁮󠁧󠁿", "#England"),
    "scotland": ("🏴󠁧󠁢󠁳󠁣󠁴󠁿", "#Scotland"),
    "ireland": ("🇮🇪", "#Ireland"),
    "france": ("🇫🇷", "#France"),
    "spain": ("🇪🇸", "#Spain"), "españa": ("🇪🇸", "#Spain"),
    "italy": ("🇮🇹", "#Italy"), "italia": ("🇮🇹", "#Italy"),
    "germany": ("🇩🇪", "#Germany"), "deutschland": ("🇩🇪", "#Germany"),
    "portugal": ("🇵🇹", "#Portugal"),
    "netherlands": ("🇳🇱", "#Netherlands"), "holland": ("🇳🇱", "#Netherlands"),
    "belgium": ("🇧🇪", "#Belgium"),
    "switzerland": ("🇨🇭", "#Switzerland"),
    "austria": ("🇦🇹", "#Austria"),
    "sweden": ("🇸🇪", "#Sweden"),
    "norway": ("🇳🇴", "#Norway"),
    "denmark": ("🇩🇰", "#Denmark"),
    "finland": ("🇫🇮", "#Finland"),
    "poland": ("🇵🇱", "#Poland"),
    "czech republic": ("🇨🇿", "#CzechRepublic"), "czechia": ("🇨🇿", "#Czechia"),
    "greece": ("🇬🇷", "#Greece"), "grecia": ("🇬🇷", "#Greece"),
    "croatia": ("🇭🇷", "#Croatia"),
    "hungary": ("🇭🇺", "#Hungary"),
    "romania": ("🇷🇴", "#Romania"),
    "turkey": ("🇹🇷", "#Turkey"), "türkiye": ("🇹🇷", "#Turkey"),
    "russia": ("🇷🇺", "#Russia"),
    "ukraine": ("🇺🇦", "#Ukraine"),
    "iceland": ("🇮🇸", "#Iceland"),
    "malta": ("🇲🇹", "#Malta"),
    "luxembourg": ("🇱🇺", "#Luxembourg"),
    "slovakia": ("🇸🇰", "#Slovakia"),
    "slovenia": ("🇸🇮", "#Slovenia"),
    "serbia": ("🇷🇸", "#Serbia"),
    "bulgaria": ("🇧🇬", "#Bulgaria"),
    "latvia": ("🇱🇻", "#Latvia"),
    "lithuania": ("🇱🇹", "#Lithuania"),
    "estonia": ("🇪🇪", "#Estonia"),
    # Asia
    "japan": ("🇯🇵", "#Japan"), "japón": ("🇯🇵", "#Japan"),
    "china": ("🇨🇳", "#China"),
    "south korea": ("🇰🇷", "#SouthKorea"), "korea": ("🇰🇷", "#Korea"),
    "thailand": ("🇹🇭", "#Thailand"), "tailandia": ("🇹🇭", "#Thailand"),
    "vietnam": ("🇻🇳", "#Vietnam"),
    "indonesia": ("🇮🇩", "#Indonesia"),
    "philippines": ("🇵🇭", "#Philippines"), "filipinas": ("🇵🇭", "#Philippines"),
    "malaysia": ("🇲🇾", "#Malaysia"),
    "singapore": ("🇸🇬", "#Singapore"),
    "india": ("🇮🇳", "#India"),
    "nepal": ("🇳🇵", "#Nepal"),
    "sri lanka": ("🇱🇰", "#SriLanka"),
    "maldives": ("🇲🇻", "#Maldives"), "maldivas": ("🇲🇻", "#Maldives"),
    "cambodia": ("🇰🇭", "#Cambodia"),
    "myanmar": ("🇲🇲", "#Myanmar"),
    "laos": ("🇱🇦", "#Laos"),
    "mongolia": ("🇲🇳", "#Mongolia"),
    "taiwan": ("🇹🇼", "#Taiwan"),
    "hong kong": ("🇭🇰", "#HongKong"),
    "macau": ("🇲🇴", "#Macau"),
    "bangladesh": ("🇧🇩", "#Bangladesh"),
    "pakistan": ("🇵🇰", "#Pakistan"),
    "kazakhstan": ("🇰🇿", "#Kazakhstan"),
    "uzbekistan": ("🇺🇿", "#Uzbekistan"),
    # Middle East
    "united arab emirates": ("🇦🇪", "#UAE"), "uae": ("🇦🇪", "#UAE"), "dubai": ("🇦🇪", "#Dubai"),
    "saudi arabia": ("🇸🇦", "#SaudiArabia"),
    "qatar": ("🇶🇦", "#Qatar"),
    "israel": ("🇮🇱", "#Israel"),
    "jordan": ("🇯🇴", "#Jordan"),
    "egypt": ("🇪🇬", "#Egypt"), "egipto": ("🇪🇬", "#Egypt"),
    "oman": ("🇴🇲", "#Oman"),
    "kuwait": ("🇰🇼", "#Kuwait"),
    "bahrain": ("🇧🇭", "#Bahrain"),
    "lebanon": ("🇱🇧", "#Lebanon"),
    "iran": ("🇮🇷", "#Iran"),
    "iraq": ("🇮🇶", "#Iraq"),
    # Africa
    "south africa": ("🇿🇦", "#SouthAfrica"),
    "kenya": ("🇰🇪", "#Kenya"),
    "tanzania": ("🇹🇿", "#Tanzania"),
    "ethiopia": ("🇪🇹", "#Ethiopia"),
    "ghana": ("🇬🇭", "#Ghana"),
    "nigeria": ("🇳🇬", "#Nigeria"),
    "morocco": ("🇲🇦", "#Morocco"), "marruecos": ("🇲🇦", "#Morocco"),
    "tunisia": ("🇹🇳", "#Tunisia"),
    "senegal": ("🇸🇳", "#Senegal"),
    "mozambique": ("🇲🇿", "#Mozambique"),
    "madagascar": ("🇲🇬", "#Madagascar"),
    "seychelles": ("🇸🇨", "#Seychelles"),
    "mauritius": ("🇲🇺", "#Mauritius"),
    "cape verde": ("🇨🇻", "#CapeVerde"),
    "cameroon": ("🇨🇲", "#Cameroon"),
    "ivory coast": ("🇨🇮", "#IvoryCoast"),
    "zimbabwe": ("🇿🇼", "#Zimbabwe"),
    "zambia": ("🇿🇲", "#Zambia"),
    "botswana": ("🇧🇼", "#Botswana"),
    "rwanda": ("🇷🇼", "#Rwanda"),
    "uganda": ("🇺🇬", "#Uganda"),
    # Oceania
    "australia": ("🇦🇺", "#Australia"),
    "new zealand": ("🇳🇿", "#NewZealand"),
    "fiji": ("🇫🇯", "#Fiji"),
    "hawaii": ("🇺🇸", "#Hawaii"),
    "bali": ("🇮🇩", "#Bali"),
    "french polynesia": ("🇵🇫", "#FrenchPolynesia"), "tahiti": ("🇵🇫", "#Tahiti"),
    "papua new guinea": ("🇵🇬", "#PapuaNewGuinea"),
}

# Major city → country lookup
CITY_COUNTRY: dict[str, str] = {
    # USA
    "new york": "united states", "nyc": "united states", "los angeles": "united states",
    "chicago": "united states", "miami": "united states", "las vegas": "united states",
    "san francisco": "united states", "boston": "united states", "orlando": "united states",
    "seattle": "united states", "dallas": "united states", "houston": "united states",
    "atlanta": "united states", "denver": "united states", "phoenix": "united states",
    "washington": "united states", "philadelphia": "united states", "portland": "united states",
    "minneapolis": "united states", "detroit": "united states", "baltimore": "united states",
    "honolulu": "hawaii", "anchorage": "united states",
    # Canada
    "toronto": "canada", "vancouver": "canada", "montreal": "canada",
    "calgary": "canada", "ottawa": "canada",
    # UK
    "london": "united kingdom", "manchester": "united kingdom", "edinburgh": "scotland",
    "glasgow": "scotland", "birmingham": "united kingdom", "dublin": "ireland",
    # Europe
    "paris": "france", "nice": "france", "lyon": "france",
    "madrid": "spain", "barcelona": "spain", "seville": "spain", "malaga": "spain",
    "rome": "italy", "milan": "italy", "venice": "italy", "florence": "italy", "naples": "italy",
    "berlin": "germany", "munich": "germany", "frankfurt": "germany", "hamburg": "germany",
    "amsterdam": "netherlands",
    "lisbon": "portugal", "porto": "portugal",
    "brussels": "belgium",
    "zurich": "switzerland", "geneva": "switzerland",
    "vienna": "austria",
    "stockholm": "sweden", "oslo": "norway", "copenhagen": "denmark", "helsinki": "finland",
    "warsaw": "poland", "krakow": "poland",
    "prague": "czech republic",
    "budapest": "hungary",
    "bucharest": "romania",
    "athens": "greece", "santorini": "greece", "mykonos": "greece",
    "dubrovnik": "croatia", "split": "croatia",
    "reykjavik": "iceland",
    "istanbul": "turkey", "ankara": "turkey",
    "moscow": "russia", "st. petersburg": "russia",
    # Asia
    "tokyo": "japan", "osaka": "japan", "kyoto": "japan",
    "beijing": "china", "shanghai": "china", "guangzhou": "china", "shenzhen": "china",
    "seoul": "south korea", "busan": "south korea",
    "bangkok": "thailand", "phuket": "thailand", "chiang mai": "thailand",
    "hanoi": "vietnam", "ho chi minh": "vietnam", "danang": "vietnam",
    "bali": "indonesia", "jakarta": "indonesia",
    "manila": "philippines", "cebu": "philippines",
    "kuala lumpur": "malaysia", "penang": "malaysia",
    "singapore": "singapore",
    "mumbai": "india", "delhi": "india", "goa": "india",
    "kathmandu": "nepal",
    "colombo": "sri lanka",
    "male": "maldives",
    "siem reap": "cambodia", "phnom penh": "cambodia",
    "taipei": "taiwan",
    "hong kong": "hong kong",
    "ulaanbaatar": "mongolia",
    # Middle East
    "dubai": "united arab emirates", "abu dhabi": "united arab emirates",
    "riyadh": "saudi arabia", "jeddah": "saudi arabia",
    "doha": "qatar",
    "tel aviv": "israel", "jerusalem": "israel",
    "amman": "jordan", "petra": "jordan",
    "cairo": "egypt", "luxor": "egypt",
    "muscat": "oman",
    # Africa
    "cape town": "south africa", "johannesburg": "south africa",
    "nairobi": "kenya",
    "dar es salaam": "tanzania", "zanzibar": "tanzania",
    "addis ababa": "ethiopia",
    "accra": "ghana",
    "casablanca": "morocco", "marrakech": "morocco", "marrakesh": "morocco",
    "tunis": "tunisia",
    "dakar": "senegal",
    "victoria": "seychelles",
    "port louis": "mauritius",
    # Latam
    "bogota": "colombia", "bogotá": "colombia", "medellín": "colombia",
    "lima": "peru",
    "santiago": "chile",
    "buenos aires": "argentina",
    "sao paulo": "brazil", "são paulo": "brazil", "rio de janeiro": "brazil", "rio": "brazil",
    "montevideo": "uruguay",
    "quito": "ecuador",
    "la paz": "bolivia",
    "caracas": "venezuela",
    "asuncion": "paraguay",
    "cancun": "mexico", "cancún": "mexico", "mexico city": "mexico", "guadalajara": "mexico",
    "san jose": "costa rica",
    "panama city": "panama",
    "havana": "cuba",
    "santo domingo": "dominican republic", "punta cana": "dominican republic",
    "san juan": "puerto rico",
    "kingston": "jamaica",
    "nassau": "bahamas",
    # Oceania
    "sydney": "australia", "melbourne": "australia", "brisbane": "australia", "perth": "australia",
    "auckland": "new zealand", "queenstown": "new zealand",
    "nadi": "fiji",
    "papeete": "french polynesia",
}


# Adjective forms → country key (e.g. "french" → "france")
COUNTRY_ADJECTIVES: dict[str, str] = {
    "french": "france", "italian": "italy", "spanish": "spain",
    "german": "germany", "portuguese": "portugal", "greek": "greece",
    "british": "united kingdom", "english": "united kingdom", "scottish": "scotland",
    "irish": "ireland", "dutch": "netherlands", "belgian": "belgium",
    "swiss": "switzerland", "austrian": "austria", "swedish": "sweden",
    "norwegian": "norway", "danish": "denmark", "finnish": "finland",
    "polish": "poland", "czech": "czech republic", "hungarian": "hungary",
    "romanian": "romania", "turkish": "turkey", "russian": "russia",
    "ukrainian": "ukraine", "icelandic": "iceland",
    "japanese": "japan", "chinese": "china", "korean": "south korea",
    "thai": "thailand", "vietnamese": "vietnam", "indonesian": "indonesia",
    "filipino": "philippines", "malaysian": "malaysia", "singaporean": "singapore",
    "indian": "india", "nepalese": "nepal",
    "australian": "australia", "kiwi": "new zealand",
    "mexican": "mexico", "colombian": "colombia", "peruvian": "peru",
    "chilean": "chile", "argentinian": "argentina", "argentinean": "argentina",
    "brazilian": "brazil", "cuban": "cuba", "jamaican": "cuba",
    "moroccan": "morocco", "egyptian": "egypt", "kenyan": "kenya",
    "south african": "south africa", "tanzanian": "tanzania",
    "emirati": "united arab emirates", "saudi": "saudi arabia",
    "qatari": "qatar", "israeli": "israel", "jordanian": "jordan",
    "riviera": "france",   # French/Italian Riviera → defaults to France unless adj found
}

def detect_countries(text: str) -> list[tuple[str, str]]:
    """Return list of (flag, hashtag) for ALL countries detected in text (up to 3)."""
    tl = text.lower()
    found: dict[str, tuple[str, str]] = {}  # key → (flag, hashtag), deduped

    # 1. Direct country name match
    for name, (flag, tag) in COUNTRY_MAP.items():
        if name in tl:
            found[tag] = (flag, tag)

    # 2. Adjective forms
    for adj, country_key in COUNTRY_ADJECTIVES.items():
        if adj in tl and country_key in COUNTRY_MAP:
            flag, tag = COUNTRY_MAP[country_key]
            found[tag] = (flag, tag)

    # 3. City lookup
    for city, country_key in CITY_COUNTRY.items():
        if city in tl and country_key in COUNTRY_MAP:
            flag, tag = COUNTRY_MAP[country_key]
            found[tag] = (flag, tag)

    return list(found.values())[:3]


DEAL_TYPE_RULES: list[tuple[str, list[str]]] = [
    ("Cruise",   ["cruise", "crucero", "ship", "sailing", "carnival", "norwegian", "royal caribbean", "msc ", "celebrity cruise"]),
    ("Hotel",    ["hotel", "resort", "hostel", "inn", "lodge", "motel", "accommodation", "stay", "nights", "night stay", "airbnb"]),
    ("Package",  ["package", "vacation package", "holiday package", "all-inclusive", "all inclusive", "bundle"]),
    ("Miles",    ["miles", "points", "millas", "puntos", "award", "redemption", "bonus miles", "frequent flyer"]),
    ("Flight",   ["flight", "fly", "airline", "airfare", "roundtrip", "round trip", "nonstop", "one-way", "departure", "→", "->", " to "]),
]


def detect_deal_type(text: str, region: str) -> str:
    if region.lower() == "cruise":
        return "Cruise"
    if region.lower() == "hotel":
        return "Hotel"
    tl = text.lower()
    for dtype, keywords in DEAL_TYPE_RULES:
        if any(kw in tl for kw in keywords):
            return dtype
    return "Deal"

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


def load_digest_state() -> dict:
    if DIGEST_FILE.exists():
        try:
            return json.loads(DIGEST_FILE.read_text())
        except Exception:
            pass
    return {"date": "", "deals": [], "total_scanned": 0, "sent": False}


def save_digest_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DIGEST_FILE.write_text(json.dumps(state))


def load_prefs() -> dict:
    if PREFS_FILE.exists():
        try:
            return json.loads(PREFS_FILE.read_text())
        except Exception:
            pass
    return {"paused_until": None, "update_offset": 0}


def save_prefs(prefs: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(prefs))

# ─── Telegram ─────────────────────────────────────────────────────────────────
TG_URL           = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
TG_UPDATES_URL   = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
TG_ANSWER_CB_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
TG_SET_CMDS_URL  = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
TG_EDIT_URL      = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"

# ─── Inline keyboards ─────────────────────────────────────────────────────────

HOME_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🛫 Skedaddling",  "callback_data": "menu:skedaddling"},
            {"text": "🔕 Lollygagging", "callback_data": "menu:lollygagging"},
        ],
        [
            {"text": "🗺️ Meandering",   "callback_data": "menu:meandering"},
            {"text": "🔍 Mulling",       "callback_data": "menu:mulling"},
        ],
        [
            {"text": "📖 Wandering",    "callback_data": "menu:wandering"},
        ],
    ]
}

DEAL_TYPE_EMOJIS = {
    "Flight":  "✈️",
    "Hotel":   "🏨",
    "Cruise":  "🚢",
    "Package": "🧳",
    "Miles":   "🎯",
    "Deal":    "🔖",
}

# ─── Message helpers ──────────────────────────────────────────────────────────

AIRLINE_NAMES = [
    "American Airlines", "Delta", "United", "Southwest", "JetBlue",
    "Alaska Airlines", "Spirit", "Frontier", "Hawaiian", "Sun Country",
    "Lufthansa", "British Airways", "Air France", "KLM", "Iberia", "Swiss",
    "Austrian", "Brussels Airlines", "Finnair", "SAS", "Norwegian",
    "Ryanair", "EasyJet", "Wizz Air", "Vueling", "TAP", "Aer Lingus",
    "Emirates", "Qatar Airways", "Etihad", "Turkish Airlines", "El Al",
    "Singapore Airlines", "Cathay Pacific", "ANA", "JAL", "Korean Air",
    "Asiana", "Thai Airways", "Vietnam Airlines", "Garuda",
    "Philippine Airlines", "Malaysia Airlines", "Air India", "IndiGo",
    "AirAsia", "Scoot", "Cebu Pacific",
    "LATAM", "Avianca", "Copa Airlines", "Aeromexico", "Volaris",
    "Sky Airline", "Air Canada", "WestJet", "Porter Airlines",
    "Qantas", "Virgin Australia", "Air New Zealand",
    "Ethiopian Airlines", "Kenya Airways", "Royal Air Maroc", "EgyptAir",
    "South African Airways",
]

# Spanglish spinner words for the daily digest header
SPELUNK_WORDS = [
    "Fare-Spelunkeamos", "Deal-Sauteamos", "Price-Smoosheamos",
    "Route-Gallivantamos", "Error-Sniffeamos", "Seat-Wrangleamos",
    "Fare-Julienneamos", "Rate-Flambéamos", "Discount-Fermentamos",
    "Airline-Befuddleamos", "Ticket-Moonwalkeamos", "Fare-Shenaniganeamos",
    "Price-Combobulamos", "Deal-Percolamos", "Route-Zigzagueamos",
]

_MONTH_PAT = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)


def extract_dates(text: str) -> str | None:
    """Extract a travel date range from deal text, e.g. 'Mar–May 2026'."""
    patterns = [
        rf"({_MONTH_PAT}[\s\d]*[-–—]{_MONTH_PAT}[\s,\d]{{0,6}}(?:20\d{{2}})?)",
        rf"({_MONTH_PAT}\s+to\s+{_MONTH_PAT}(?:\s+20\d{{2}})?)",
        rf"for travel\s+({_MONTH_PAT}[\s\d]*[-–—]{_MONTH_PAT}[\s,\d]{{0,6}}(?:20\d{{2}})?)",
        rf"(?:valid|travel|fly)\s+(?:through|until|thru)\s+({_MONTH_PAT}(?:\s+20\d{{2}})?)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def extract_airline(text: str) -> str | None:
    tl = text.lower()
    for airline in AIRLINE_NAMES:
        if airline.lower() in tl:
            return airline
    return None


def extract_original_price(text: str) -> str | None:
    """Look for 'was $X', 'normally $X', etc. and return formatted string."""
    m = re.search(
        r"(?:was|normally?|regular(?:ly)?|usual(?:ly)?|valued?\s+at|rrp|normal)[:\s]*"
        r"([$€£])?([\d,]+(?:\.\d{2})?)",
        text, re.IGNORECASE,
    )
    if m:
        sym = m.group(1) or "$"
        try:
            n = float(m.group(2).replace(",", ""))
            if n > 10:
                return f"{sym}{int(n):,}" if n == int(n) else f"{sym}{n:,.0f}"
        except ValueError:
            pass
    return None


def extract_pct(text: str) -> str | None:
    """Return formatted '−XX%' string if an explicit percentage is found."""
    pct = _parse_pct(text)
    if pct:
        return f"−{pct}%"
    disc = _parse_was_now(text)
    if disc:
        return f"−{int(disc * 100)}%"
    return None


def skedaddle_alert(tier_level: int) -> str | None:
    if tier_level == 3:
        return "⏰ *Skedaddle Alert:* estos duran 2-6 horas"
    if tier_level == 2:
        return "⏰ *Skedaddle Alert:* 24-48 horas antes de que vuele"
    return None


def _source_url(deal: Deal) -> str:
    return SOURCE_URLS.get(deal.source, "")


# ─── Telegram helpers ─────────────────────────────────────────────────────────

async def send_text(
    client: httpx.AsyncClient,
    chat_id: int | str,
    text: str,
    *,
    silent: bool = False,
    reply_markup: dict | None = None,
) -> None:
    payload: dict = {
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "Markdown",
        "disable_web_page_preview": True,
        "disable_notification":     silent,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await client.post(TG_URL, json=payload)


async def edit_text(
    client: httpx.AsyncClient,
    chat_id: int | str,
    message_id: int,
    text: str,
    *,
    reply_markup: dict | None = None,
) -> None:
    payload: dict = {
        "chat_id":                  chat_id,
        "message_id":               message_id,
        "text":                     text,
        "parse_mode":               "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await client.post(TG_EDIT_URL, json=payload)


async def answer_callback(
    client: httpx.AsyncClient,
    callback_query_id: str,
    *,
    text: str | None = None,
) -> None:
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    await client.post(TG_ANSWER_CB_URL, json=payload)


# ─── Command handlers ─────────────────────────────────────────────────────────

async def _respond(
    client: httpx.AsyncClient,
    chat_id: int | str,
    text: str,
    *,
    message_id: int | None = None,
    silent: bool = True,
    reply_markup: dict | None = None,
) -> None:
    """Edita el mensaje existente si viene de un botón, envía nuevo si viene de un comando."""
    if message_id:
        await edit_text(client, chat_id, message_id, text, reply_markup=reply_markup)
    else:
        await send_text(client, chat_id, text, silent=silent, reply_markup=reply_markup)


async def cmd_ver_todos(
    client: httpx.AsyncClient, chat_id: int | str, *, message_id: int | None = None
) -> None:
    digest_state = load_digest_state()
    deals = digest_state.get("deals", [])
    if not deals:
        await _respond(client, chat_id, "📭 No hay deals registrados hoy todavía.",
                       message_id=message_id, reply_markup=HOME_KEYBOARD)
        return
    lines = [f"📋 *Deals de hoy* — {len(deals)} encontrados\n"]
    for d in sorted(deals, key=lambda x: -x["tier_level"])[:15]:
        pct_str = f", {d['pct']}" if d.get("pct") else ""
        tier_short = d["tier_name"].split()[0]
        lines.append(
            f"{d['tier_emoji']} {md_esc(d['title'])}: {md_esc(d['price'])} "
            f"_({tier_short}{pct_str})_"
        )
    await _respond(client, chat_id, "\n".join(lines), message_id=message_id, reply_markup=HOME_KEYBOARD)


async def cmd_mis_rutas(
    client: httpx.AsyncClient, chat_id: int | str, *, message_id: int | None = None
) -> None:
    await _respond(
        client, chat_id,
        "🗺️ *Meandering* — Próximamente podrás guardar rutas favoritas "
        "y recibir alertas personalizadas.",
        message_id=message_id, reply_markup=HOME_KEYBOARD,
    )


async def cmd_pausar(
    client: httpx.AsyncClient, chat_id: int | str, prefs: dict, *, message_id: int | None = None
) -> None:
    now = datetime.now(timezone.utc)
    paused_until = prefs.get("paused_until")
    if paused_until:
        paused_dt = datetime.fromisoformat(paused_until)
        if paused_dt > now:
            prefs["paused_until"] = None
            save_prefs(prefs)
            await _respond(client, chat_id, "🔔 Alertas reactivadas.",
                           message_id=message_id, reply_markup=HOME_KEYBOARD)
            return
    until = (now + timedelta(hours=24)).isoformat()
    prefs["paused_until"] = until
    save_prefs(prefs)
    await _respond(
        client, chat_id,
        "🔕 Alertas pausadas por 24 horas. Toca *Lollygagging* de nuevo para reactivarlas.",
        message_id=message_id, reply_markup=HOME_KEYBOARD,
    )


async def cmd_mute_ruta(
    client: httpx.AsyncClient, callback_query_id: str, deal_id: str
) -> None:
    """Ghosting — muestra un toast en el botón, sin crear burbuja nueva."""
    await answer_callback(client, callback_query_id, text="🔕 Ruta silenciada.")


async def cmd_mulling(
    client: httpx.AsyncClient, chat_id: int | str, *, message_id: int | None = None
) -> None:
    prefs = load_prefs()
    paused_until = prefs.get("paused_until")
    if paused_until:
        paused_dt = datetime.fromisoformat(paused_until)
        now = datetime.now(timezone.utc)
        if paused_dt > now:
            hours = int((paused_dt - now).total_seconds() // 3600)
            await _respond(client, chat_id, f"🔕 Alertas pausadas — {hours}h restantes.",
                           message_id=message_id, reply_markup=HOME_KEYBOARD)
            return
    digest_state = load_digest_state()
    deals_today = len(digest_state.get("deals", []))
    await _respond(
        client, chat_id,
        f"✅ *Gallivant* está despierto y cazando.\n"
        f"📋 Deals encontrados hoy: *{deals_today}*",
        message_id=message_id, reply_markup=HOME_KEYBOARD,
    )


_WANDERING_TEXT = (
    "🗺️ *Wandering — el idioma de Gallivant*\n\n"
    "🛫 *Skedaddling* — ver los deals de hoy\n"
    "🔕 *Lollygagging* — pausar o reactivar alertas\n"
    "🗺️ *Meandering* — mis rutas favoritas _(próximamente)_\n"
    "📖 *Wandering* — este glosario\n"
    "🔍 *Mulling* — estado del bot\n"
    "🔕 *Ghosting* — silenciar un deal puntual\n"
    "✈️ *Gallivanting* — volver al inicio\n\n"
    "🌧️ *Drizzle Deal* — deal decente, no urgente\n"
    "🍲 *Simmering Save* — vale la pena, guardalo\n"
    "🔥 *Flambé Fare* — oferta fuerte, revisalo ya\n"
    "🤯 *Hullabaloo Deal* — error fare o precio insano, skedaddle ahora"
)


async def cmd_wandering(
    client: httpx.AsyncClient, chat_id: int | str, *, message_id: int | None = None
) -> None:
    await _respond(client, chat_id, _WANDERING_TEXT,
                   message_id=message_id, reply_markup=HOME_KEYBOARD)


async def dispatch_callback(
    client: httpx.AsyncClient,
    callback_query_id: str,
    chat_id: int | str,
    message_id: int,
    data: str,
    prefs: dict,
) -> None:
    if data.startswith("mute:"):
        # Toast en el botón — sin burbuja, sin editar el deal
        await cmd_mute_ruta(client, callback_query_id, data[5:])
        return

    await answer_callback(client, callback_query_id)

    if data == "menu:home":
        await edit_text(client, chat_id, message_id,
                        "✈️ *Gallivant* — Hunting error fares & cheap flights.",
                        reply_markup=HOME_KEYBOARD)
    elif data == "menu:skedaddling":
        await cmd_ver_todos(client, chat_id, message_id=message_id)
    elif data == "menu:lollygagging":
        await cmd_pausar(client, chat_id, prefs, message_id=message_id)
    elif data == "menu:meandering":
        await cmd_mis_rutas(client, chat_id, message_id=message_id)
    elif data == "menu:mulling":
        await cmd_mulling(client, chat_id, message_id=message_id)
    elif data == "menu:wandering":
        await cmd_wandering(client, chat_id, message_id=message_id)


async def dispatch_command(
    client: httpx.AsyncClient, chat_id: int | str, text: str, prefs: dict
) -> None:
    parts = text.strip().split(None, 1)
    cmd = parts[0].lower().split("@")[0]  # strip @BotName suffix
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/gallivanting"):
        if arg == "all":
            await cmd_ver_todos(client, chat_id)
        elif arg == "routes":
            await cmd_mis_rutas(client, chat_id)
        elif arg == "pause":
            await cmd_pausar(client, chat_id, prefs)
        elif arg.startswith("mute_"):
            await cmd_mute_ruta(client, chat_id, arg[5:])
        else:
            await send_text(
                client, chat_id,
                "✈️ *Gallivant* — Hunting error fares & cheap flights.\n\n"
                "Recibirás alertas automáticas cuando aparezcan nuevos deals.",
                silent=True, reply_markup=HOME_KEYBOARD,
            )
    elif cmd == "/skedaddling":
        await cmd_ver_todos(client, chat_id)
    elif cmd == "/lollygagging":
        await cmd_pausar(client, chat_id, prefs)
    elif cmd == "/meandering":
        await cmd_mis_rutas(client, chat_id)
    elif cmd == "/mulling":
        await cmd_mulling(client, chat_id)
    elif cmd == "/wandering":
        await cmd_wandering(client, chat_id)


async def handle_updates(client: httpx.AsyncClient, prefs: dict) -> None:
    offset = prefs.get("update_offset", 0)
    resp = await client.get(
        TG_UPDATES_URL,
        params={"offset": offset, "timeout": 10, "allowed_updates": ["message", "callback_query"]},
        timeout=20,
    )
    data = resp.json()
    if not data.get("ok"):
        return
    for update in data["result"]:
        offset = update["update_id"] + 1
        if "callback_query" in update:
            cb      = update["callback_query"]
            cb_id   = cb["id"]
            cb_data = cb.get("data", "")
            chat_id = cb["message"]["chat"]["id"]
            msg_id  = cb["message"]["message_id"]
            await dispatch_callback(client, cb_id, chat_id, msg_id, cb_data, prefs)
        elif "message" in update:
            msg     = update["message"]
            text    = msg.get("text", "")
            chat_id = msg.get("chat", {}).get("id")
            if text and chat_id:
                await dispatch_command(client, chat_id, text, prefs)
    prefs["update_offset"] = offset
    save_prefs(prefs)


# ─── Telegram senders ─────────────────────────────────────────────────────────

async def send_deal(client: httpx.AsyncClient, deal: Deal) -> None:
    # Check if alerts are paused
    prefs = load_prefs()
    paused_until = prefs.get("paused_until")
    if paused_until:
        if datetime.fromisoformat(paused_until) > datetime.now(timezone.utc):
            log.info(f"  Alerts paused — skipping {deal.title}")
            return

    tier_name, tier_emoji, tier_level = classify_tier(
        deal.is_error_fare, deal.title, deal.description
    )
    type_emoji = DEAL_TYPE_EMOJIS.get(deal.deal_type, "🔖")
    src_url    = _source_url(deal)
    full_text  = f"{deal.title} {deal.description}"

    # ── Line 1: tier + deal type label ───────────────────────────────────────
    type_label = "Error Fare" if deal.is_error_fare else deal.deal_type
    tier_display = tier_name.upper() if tier_level == 3 else tier_name
    header = f"{tier_emoji} *{tier_display}* · {type_label}"

    # ── Line 2: title ─────────────────────────────────────────────────────────
    title_line = f"{type_emoji} {md_esc(deal.title)}"

    # ── Line 3: price (+ original if found) ──────────────────────────────────
    orig = extract_original_price(full_text)
    price_line = f"💰 {md_esc(deal.price)}"
    if orig:
        price_line += f" _(normal: ~{md_esc(orig)})_"

    # ── Optional lines ────────────────────────────────────────────────────────
    dates   = extract_dates(full_text)
    airline = extract_airline(full_text)

    if airline:
        booking_tip = "⚡ Reserva DIRECTO con la aerolínea"
    elif deal.deal_type == "Hotel":
        booking_tip = "⚡ Verifica disponibilidad antes de reservar"
    elif deal.deal_type == "Cruise":
        booking_tip = "⚡ Confirma cabina disponible antes de reservar"
    else:
        booking_tip = "⚡ Confirma precio antes de completar la reserva"

    alert = skedaddle_alert(tier_level)

    # ── Assemble ──────────────────────────────────────────────────────────────
    parts = [header, title_line, price_line]
    if dates:
        parts.append(f"📅 {md_esc(dates)}")
    if airline:
        parts.append(f"🛑 Aerolínea: {md_esc(airline)}")
    parts.append(booking_tip)
    if alert:
        parts += ["", alert]

    # ── Inline keyboard buttons ────────────────────────────────────────────────
    btn_row1 = [{"text": "🔗 Reservar ahora", "url": deal.url}]
    if src_url:
        btn_row1.append({"text": "📰 Ver fuente", "url": src_url})
    btn_row2 = [
        {"text": "🔕 Ghosting",      "callback_data": f"mute:{deal.deal_id}"},
        {"text": "✈️ Gallivanting",  "callback_data": "menu:home"},
    ]
    reply_markup = {"inline_keyboard": [btn_row1, btn_row2]}

    # Hullabaloo → audible push; everything else → silent
    silent = tier_level < 3

    resp = await client.post(TG_URL, json={
        "chat_id":                  CHAT_ID,
        "text":                     "\n".join(parts),
        "parse_mode":               "Markdown",
        "disable_web_page_preview": True,
        "disable_notification":     silent,
        "reply_markup":             reply_markup,
    })
    resp.raise_for_status()


async def send_daily_digest(client: httpx.AsyncClient, digest_deals: list[dict], total_scanned: int) -> None:
    """Send the daily Simmering Saves digest."""
    if not digest_deals:
        return

    # Pick dominant tier (highest level deal of the day)
    top_level = max(d["tier_level"] for d in digest_deals)
    _, top_emoji, _ = TIERS[top_level]
    top_name = TIERS[top_level][0]

    spinner = random.choice(SPELUNK_WORDS)
    header  = f"{top_emoji} *{top_name.upper()}* · Resumen del día"

    parts = [
        header,
        f"Hoy {spinner} {total_scanned:,} rutas.",
        "",
    ]

    # Show up to 8 best deals (sorted by tier desc, then first found)
    shown = sorted(digest_deals, key=lambda d: -d["tier_level"])[:8]
    for d in shown:
        t_emoji = d["tier_emoji"]
        pct_str = f", {d['pct']}" if d.get("pct") else ""
        tier_short = d["tier_name"].split()[0]   # "Hullabaloo", "Flambé", etc.
        parts.append(
            f"{t_emoji} {md_esc(d['title'])}: {md_esc(d['price'])} "
            f"_({tier_short}{pct_str})_"
        )

    parts += [
        "",
        "📊 [Ver todos](https://t.me/GallivantBot?start=all) · "
        "⚙️ [Mis rutas](https://t.me/GallivantBot?start=routes) · "
        "🔕 [Pausar alertas](https://t.me/GallivantBot?start=pause)",
    ]

    resp = await client.post(TG_URL, json={
        "chat_id":                  CHAT_ID,
        "text":                     "\n".join(parts),
        "parse_mode":               "Markdown",
        "disable_web_page_preview": True,
        "disable_notification":     False,   # digest always has sound
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
        deal_type     = detect_deal_type(full, region),
        is_error_fare = force_error or is_error_fare(full),
        published     = pub,
        description   = summary[:300],
    )

# ─── RSS Scrapers ─────────────────────────────────────────────────────────────

# SecretFlying — 10 feeds ──────────────────────────────────────────────────────
SECRET_FLYING_FEEDS = [
    ("https://www.secretflying.com/posts/category/europe/feed/",      "Europe",      False),
    ("https://www.secretflying.com/posts/category/asia/feed/",        "Asia",        False),
    ("https://www.secretflying.com/posts/category/caribbean/feed/",   "Caribbean",   False),
    ("https://www.secretflying.com/posts/category/africa/feed/",      "Africa",      False),
    ("https://www.secretflying.com/posts/category/middle-east/feed/", "Middle East", False),
    ("https://www.secretflying.com/posts/category/oceania/feed/",     "Oceania",     False),
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
    ("https://www.fly4free.com/feed/",                                      "Global", False),
    ("https://www.fly4free.com/flight-deals/north-america/feed/",           "USA",    False),
    ("https://www.fly4free.com/flight-deals/europe/feed/",                  "Europe", False),
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
    ("https://www.holidaypirates.com/feed", "Europe"),
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
    ("https://www.travelzoo.com/rss/",                  "Global"),
    ("https://www.travelzoo.com/rss/top20/us/",         "USA"),
    ("https://www.travelzoo.com/rss/top20/uk/",         "Europe"),
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
    ("https://thepointsguy.com/feed/", "Global"),
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
        ("https://viewfromthewing.com/feed/", "Global"),
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
        ("https://onemileatatime.com/feed/", "Global"),
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
                        deal_type     = detect_deal_type(text, "USA"),
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
                deal_type     = detect_deal_type(title, "USA"),
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
                deal_type     = detect_deal_type(title, "Global"),
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
                    deal_type     = detect_deal_type(title, "Global"),
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
                    deal_type     = detect_deal_type(title, "Global"),
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

    ("Airfarewatchdog", scrape_airfarewatchdog),   # Playwright — runs last
    ("ManyFlights",     scrape_manyflights),
    ("Flightlist",      scrape_flightlist),
    ("Wandr",           scrape_wandr),
]


async def run_poll() -> None:
    seen         = load_seen()
    is_first_run = len(seen) == 0
    now          = datetime.now(timezone.utc)
    today        = now.strftime("%Y-%m-%d")
    digest_state = load_digest_state()

    # Reset digest state on new day
    if digest_state["date"] != today:
        digest_state = {"date": today, "deals": [], "total_scanned": 0, "sent": False}

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        all_deals: list[Deal] = []

        for name, scraper in SCRAPERS:
            log.info(f"Scraping {name}…")
            deals = await scraper(client)
            all_deals.extend(deals)

        new_deals = [d for d in all_deals if d.deal_id not in seen]

        # Update digest total (all deals scanned, not just new)
        digest_state["total_scanned"] = max(digest_state["total_scanned"], len(all_deals))

        # Apply tier filter (MIN_TIER env var)
        if MIN_TIER_LEVEL > 0:
            filtered = [
                d for d in new_deals
                if classify_tier(d.is_error_fare, d.title, d.description)[2] >= MIN_TIER_LEVEL
            ]
            log.info(
                f"Total: {len(all_deals)} | New: {len(new_deals)} | "
                f"After tier filter (≥{MIN_TIER_LEVEL}): {len(filtered)}"
            )
            new_deals = filtered
        else:
            log.info(f"Total: {len(all_deals)} | New: {len(new_deals)}")

        if is_first_run:
            log.info("First run — seeding state, no alerts sent.")
            for d in all_deals:
                seen.add(d.deal_id)
            save_seen(seen)
            save_digest_state(digest_state)
            return

        sent = 0
        for deal in new_deals:
            try:
                await send_deal(client, deal)
                seen.add(deal.deal_id)
                sent += 1

                # Accumulate in digest (store lightweight summary)
                t_name, t_emoji, t_level = classify_tier(
                    deal.is_error_fare, deal.title, deal.description
                )
                full = f"{deal.title} {deal.description}"
                digest_state["deals"].append({
                    "title":      deal.title[:60],
                    "price":      deal.price,
                    "tier_name":  t_name,
                    "tier_emoji": t_emoji,
                    "tier_level": t_level,
                    "pct":        extract_pct(full),
                })

                await asyncio.sleep(0.8)
            except Exception as exc:
                log.error(f"  Send failed [{deal.deal_id}]: {exc}")

        log.info(f"Sent {sent} alert(s).")
        save_seen(seen)

        # Send daily digest once DIGEST_HOUR is reached
        if now.hour >= DIGEST_HOUR and not digest_state["sent"] and digest_state["deals"]:
            log.info("Sending daily digest…")
            try:
                await send_daily_digest(client, digest_state["deals"], digest_state["total_scanned"])
                digest_state["sent"] = True
                log.info("Daily digest sent.")
            except Exception as exc:
                log.error(f"Digest send failed: {exc}")

        save_digest_state(digest_state)


async def poll_loop() -> None:
    while True:
        try:
            await run_poll()
        except Exception as exc:
            log.error(f"Poll cycle error: {exc}")
        log.info(f"Sleeping {POLL_INTERVAL}s…")
        await asyncio.sleep(POLL_INTERVAL)


async def updates_loop() -> None:
    prefs = load_prefs()
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        while True:
            try:
                await handle_updates(client, prefs)
            except Exception as exc:
                log.warning(f"Update loop error: {exc}")
            await asyncio.sleep(3)


async def set_commands() -> None:
    commands = [
        {"command": "gallivanting", "description": "Roaming aimlessly until a steal smacks you in the face."},
        {"command": "skedaddling",  "description": "Bolting through today's full haul of fares and error deals."},
        {"command": "lollygagging", "description": "Dawdling around and taking a breather from deal alerts."},
        {"command": "mulling",      "description": "Mulling over sources and reporting back on what's cooking."},
        {"command": "meandering",   "description": "Drifting through your saved routes, wherever they may lead."},
        {"command": "wandering",    "description": "Lost in translation? A guide to Gallivant's lingo."},
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(TG_SET_CMDS_URL, json={"commands": commands})
    log.info("Commands registered.")


async def main() -> None:
    log.info("✈️  Gallivant started")
    await set_commands()
    await asyncio.gather(poll_loop(), updates_loop())


if __name__ == "__main__":
    asyncio.run(main())
