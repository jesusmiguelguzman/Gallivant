#!/usr/bin/env python3
"""
Quick smoke-test: sends one sample deal per source so you can verify
the new message format in Telegram.

Usage:
    BOT_TOKEN=xxx CHAT_ID=yyy python3 test_send_samples.py
"""
import asyncio
import os
import sys

# Inject minimal env so bot.py imports cleanly
os.environ.setdefault("BOT_TOKEN",  os.environ.get("BOT_TOKEN", ""))
os.environ.setdefault("CHAT_ID",    os.environ.get("CHAT_ID", ""))
os.environ.setdefault("DATA_DIR",   "/tmp/gallivant_test")

if not os.environ["BOT_TOKEN"] or not os.environ["CHAT_ID"]:
    print("ERROR: set BOT_TOKEN and CHAT_ID env vars before running this script.")
    sys.exit(1)

import httpx
from bot import Deal, send_deal, make_id, SOURCE_URLS

SAMPLES = [
    Deal(
        deal_id       = make_id("SecretFlying", "test-01"),
        source        = "SecretFlying",
        title         = "New York → Paris from $349 round-trip",
        url           = SOURCE_URLS["SecretFlying"],
        price         = "$349",
        region        = "Europe",
        deal_type     = "Flight",
        is_error_fare = False,
        published     = "2026-03-10",
        description   = "Fly JFK to CDG with Air France, travel Mar–May 2026. Was $750.",
    ),
    Deal(
        deal_id       = make_id("Fly4free", "test-02"),
        source        = "Fly4free",
        title         = "London → Tokyo from £420 r/t on ANA",
        url           = SOURCE_URLS["Fly4free"],
        price         = "£420",
        region        = "Asia",
        deal_type     = "Flight",
        is_error_fare = False,
        published     = "2026-03-10",
        description   = "Great deal flying LHR→NRT with ANA. Travel Apr–Jun 2026. Was £980.",
    ),
    Deal(
        deal_id       = make_id("TheFlightDeal", "test-03"),
        source        = "TheFlightDeal",
        title         = "ERROR FARE: Miami → Rome $199 round-trip",
        url           = SOURCE_URLS["TheFlightDeal"],
        price         = "$199",
        region        = "Europe",
        deal_type     = "Flight",
        is_error_fare = True,
        published     = "2026-03-10",
        description   = "Mistake fare on ITA Airways, MIA→FCO. Book immediately, normally $1,100.",
    ),
    Deal(
        deal_id       = make_id("HolidayPirates", "test-04"),
        source        = "HolidayPirates",
        title         = "5-night Tenerife package from €299 incl. flights",
        url           = SOURCE_URLS["HolidayPirates"],
        price         = "€299",
        region        = "Europe",
        deal_type     = "Package",
        is_error_fare = False,
        published     = "2026-03-10",
        description   = "All-inclusive holiday package to Tenerife, Spain. Depart from Manchester. Travel May 2026.",
    ),
    Deal(
        deal_id       = make_id("Travelzoo", "test-05"),
        source        = "Travelzoo",
        title         = "Las Vegas 3-night hotel + resort credit $189/pp",
        url           = SOURCE_URLS["Travelzoo"],
        price         = "$189",
        region        = "USA",
        deal_type     = "Hotel",
        is_error_fare = False,
        published     = "2026-03-10",
        description   = "Stay at the MGM Grand Las Vegas. Includes $50 resort credit. Valid Apr–Jun 2026.",
    ),
    Deal(
        deal_id       = make_id("ThePointsGuy", "test-06"),
        source        = "ThePointsGuy",
        title         = "Chase Sapphire: 80,000 bonus miles offer",
        url           = SOURCE_URLS["ThePointsGuy"],
        price         = "80,000 miles",
        region        = "Global",
        deal_type     = "Miles",
        is_error_fare = False,
        published     = "2026-03-10",
        description   = "Limited-time 80k bonus miles after $4k spend in 3 months. Best-ever offer.",
    ),
    Deal(
        deal_id       = make_id("ViewFromTheWing", "test-07"),
        source        = "ViewFromTheWing",
        title         = "Flash sale: Sydney → Bali from AUD $289",
        url           = SOURCE_URLS["ViewFromTheWing"],
        price         = "AUD $289",
        region        = "Asia",
        deal_type     = "Flight",
        is_error_fare = False,
        published     = "2026-03-10",
        description   = "Incredible deal on Qantas, SYD→DPS. 60% off normal fares. Travel Mar–Apr 2026.",
    ),
    Deal(
        deal_id       = make_id("OneMilleAtATime", "test-08"),
        source        = "OneMilleAtATime",
        title         = "Business class NYC → Dubai from $1,299",
        url           = SOURCE_URLS["OneMilleAtATime"],
        price         = "$1,299",
        region        = "Middle East",
        deal_type     = "Flight",
        is_error_fare = False,
        published     = "2026-03-10",
        description   = "Emirates business class JFK→DXB, was normally $4,500. Flash sale ends tonight.",
    ),
    Deal(
        deal_id       = make_id("Airfarewatchdog", "test-09"),
        source        = "Airfarewatchdog",
        title         = "Chicago → Cancun from $178 round-trip",
        url           = SOURCE_URLS["Airfarewatchdog"],
        price         = "$178",
        region        = "Latin America",
        deal_type     = "Flight",
        is_error_fare = False,
        published     = "2026-03-10",
        description   = "Budget fare on Volaris, ORD→CUN. Travel Apr–May 2026. Was $420.",
    ),
    Deal(
        deal_id       = make_id("ManyFlights", "test-10"),
        source        = "ManyFlights",
        title         = "Amsterdam → Bangkok from €389 r/t",
        url           = SOURCE_URLS["ManyFlights"],
        price         = "€389",
        region        = "Asia",
        deal_type     = "Flight",
        is_error_fare = False,
        published     = "2026-03-10",
        description   = "Great deal AMS→BKK with KLM or Thai Airways. Travel May–Jul 2026.",
    ),
    Deal(
        deal_id       = make_id("Flightlist", "test-11"),
        source        = "Flightlist",
        title         = "Los Angeles → Tokyo from $498 round-trip",
        url           = SOURCE_URLS["Flightlist"],
        price         = "$498",
        region        = "Asia",
        deal_type     = "Flight",
        is_error_fare = False,
        published     = "2026-03-10",
        description   = "Cheap fares LAX→NRT on United or ANA. Solid deal at 45% off. Travel Jun–Aug 2026.",
    ),
    Deal(
        deal_id       = make_id("Wandr", "test-12"),
        source        = "Wandr",
        title         = "Marrakech city break from £179 incl. flights & hotel",
        url           = SOURCE_URLS["Wandr"],
        price         = "£179",
        region        = "Africa",
        deal_type     = "Package",
        is_error_fare = False,
        published     = "2026-03-10",
        description   = "3 nights in Marrakech, Morocco. Includes return flights from London. Travel Apr 2026.",
    ),
]


async def main() -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        for i, deal in enumerate(SAMPLES, 1):
            print(f"[{i:02d}/{len(SAMPLES)}] Sending {deal.source} sample…")
            try:
                await send_deal(client, deal)
                print(f"        ✓ sent")
            except Exception as exc:
                print(f"        ✗ failed: {exc}")
            await asyncio.sleep(1.2)   # stay within TG rate limits

    print("\nDone. Check Telegram.")


if __name__ == "__main__":
    asyncio.run(main())
