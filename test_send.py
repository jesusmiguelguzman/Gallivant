#!/usr/bin/env python3
"""
test_send.py — Sends sample deals + a daily digest to verify the message format.
Usage:  BOT_TOKEN=xxx CHAT_ID=yyy python3 test_send.py
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import httpx
from bot import Deal, send_deal, send_daily_digest, make_id

# ── Sample deals covering all 4 tiers ────────────────────────────────────────
SAMPLE_DEALS = [
    Deal(
        deal_id       = make_id("test", "hullabaloo-1"),
        source        = "TheFlightDeal",
        title         = "Lima → Tokyo (Narita)",
        url           = "https://www.theflightdeal.com/sample",
        price         = "$247 ida y vuelta",
        region        = "Asia",
        deal_type     = "Flight",
        is_error_fare = True,
        published     = "2026-03-08T10:00:00+00:00",
        description   = "Error fare ANA via São Paulo. Travel Mar-May 2026. Was normally $1,400 roundtrip.",
    ),
    Deal(
        deal_id       = make_id("test", "flambe-1"),
        source        = "SecretFlying",
        title         = "Bogotá → París — Roundtrip",
        url           = "https://www.secretflying.com/sample",
        price         = "$389",
        region        = "Europe",
        deal_type     = "Flight",
        is_error_fare = False,
        published     = "2026-03-08T10:00:00+00:00",
        description   = "Flash sale Air France. 55% off regular price $865. Travel Apr-Jun 2026.",
    ),
    Deal(
        deal_id       = make_id("test", "simmering-1"),
        source        = "Fly4free",
        title         = "Lima → Bangkok — Ida y Vuelta",
        url           = "https://www.fly4free.com/sample",
        price         = "$612",
        region        = "Asia",
        deal_type     = "Flight",
        is_error_fare = False,
        published     = "2026-03-08T10:00:00+00:00",
        description   = "Great deal with LATAM and Thai Airways. 41% off. Travel May-Aug 2026.",
    ),
    Deal(
        deal_id       = make_id("test", "drizzle-1"),
        source        = "Travelzoo",
        title         = "Lima → Miami — Roundtrip",
        url           = "https://www.travelzoo.com/sample",
        price         = "$285",
        region        = "USA",
        deal_type     = "Flight",
        is_error_fare = False,
        published     = "2026-03-08T10:00:00+00:00",
        description   = "Affordable fare with American Airlines. 22% off usual price $365. Jun-Jul 2026.",
    ),
]

SAMPLE_DIGEST = [
    {"title": "Lima → Tokyo (Narita)",   "price": "$247", "tier_name": "Hullabaloo Deal", "tier_emoji": "🤯", "tier_level": 3, "pct": "−87%"},
    {"title": "Bogotá → París",          "price": "$389", "tier_name": "Flambé Fare",     "tier_emoji": "🔥", "tier_level": 2, "pct": "−55%"},
    {"title": "Lima → Bangkok",          "price": "$612", "tier_name": "Simmering Save",  "tier_emoji": "🍲", "tier_level": 1, "pct": "−41%"},
    {"title": "Lima → Miami",            "price": "$285", "tier_name": "Drizzle Deal",    "tier_emoji": "🌧️", "tier_level": 0, "pct": "−22%"},
]


async def main() -> None:
    print("Sending test deals…")
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:

        for deal in SAMPLE_DEALS:
            print(f"  → {deal.title}")
            await send_deal(client, deal)
            await asyncio.sleep(1.5)

        print("Sending daily digest…")
        await send_daily_digest(client, SAMPLE_DIGEST, total_scanned=14_329)

    print("Done. Check Telegram.")


if __name__ == "__main__":
    asyncio.run(main())
