"""Regenerate the checked-in, deterministic local-demo CSV fixtures.

The fixtures are deliberately internal business data only. External weather, FX,
and historic-event facts remain the responsibility of external research clients.
"""

from __future__ import annotations

import csv
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RNG = random.Random(20240719)
EVENT_RNG = random.Random(20260416)
START = date(2024, 1, 1)
END = date(2024, 12, 31)

CATEGORIES = [
    ("C01", "Outdoor"),
    ("C02", "Home"),
    ("C03", "Electronics"),
    ("C04", "Travel"),
    ("C05", "Fitness"),
]
SUPPLIERS = [
    ("S01", "Northern Trail Works", "Sweden", "EUR"),
    ("S02", "Baltic Home Goods", "Poland", "EUR"),
    ("S03", "Iberian Living", "Spain", "EUR"),
    ("S04", "Northstar Goods", "Italy", "EUR"),
    ("S05", "Horizon Fitness", "Netherlands", "EUR"),
    ("S06", "Atlas Travel Supply", "France", "EUR"),
    ("S07", "Cedar & Coast", "Portugal", "EUR"),
    ("S08", "Danube Digital", "Austria", "EUR"),
    ("S09", "Pacific Components", "United States", "USD"),
    ("S10", "Kansai Optics", "Japan", "JPY"),
    ("S11", "Milan Atelier", "Italy", "EUR"),
    ("S17", "Alpine Supply Co", "Germany", "EUR"),
]
STORES = [
    ("IT-MIL-01", "Milano Centro", "Milan", "Lombardy", "Italy", "store"),
    ("IT-BER-01", "Bergamo Porta Nuova", "Bergamo", "Lombardy", "Italy", "store"),
    ("IT-BRE-01", "Brescia Centro", "Brescia", "Lombardy", "Italy", "store"),
    ("IT-TUR-01", "Torino Porta", "Turin", "Piedmont", "Italy", "store"),
    ("IT-GEN-01", "Genova Porto", "Genoa", "Liguria", "Italy", "store"),
    ("IT-BOL-01", "Bologna Centro", "Bologna", "Emilia-Romagna", "Italy", "store"),
    ("IT-FIR-01", "Firenze Duomo", "Florence", "Tuscany", "Italy", "store"),
    ("IT-ROM-01", "Roma Termini", "Rome", "Lazio", "Italy", "store"),
    ("IT-NAP-01", "Napoli Centro", "Naples", "Campania", "Italy", "store"),
    ("IT-BAR-01", "Bari Vecchia", "Bari", "Apulia", "Italy", "store"),
    ("IT-PAL-01", "Palermo Teatro", "Palermo", "Sicily", "Italy", "store"),
    ("IT-VER-01", "Verona Arena", "Verona", "Veneto", "Italy", "store"),
    ("IT-WEB-01", "Italy Online", "Online", "National", "Italy", "online"),
    ("IT-AMZ-01", "Marketplace Italy", "Online", "National", "Italy", "online"),
    ("FR-WEB-01", "France Online", "Online", "National", "France", "online"),
    ("DE-WEB-01", "Germany Online", "Online", "National", "Germany", "online"),
]
DUBAI_STORES = [
    ("AE-DXB-01", "Dubai Marina", "Dubai", "Dubai", "United Arab Emirates", "store"),
    ("AE-DXB-02", "Dubai Mall", "Dubai", "Dubai", "United Arab Emirates", "store"),
    ("AE-DXB-03", "Deira City Centre", "Dubai", "Dubai", "United Arab Emirates", "store"),
    ("AE-WEB-01", "United Arab Emirates Online", "Online", "National", "United Arab Emirates", "online"),
]
NORTH_ITALY = {"IT-MIL-01", "IT-BER-01", "IT-BRE-01", "IT-TUR-01", "IT-GEN-01", "IT-VER-01"}
DUBAI_PHYSICAL_STORES = {"AE-DXB-01", "AE-DXB-02", "AE-DXB-03"}
DUBAI_ONLINE_STORE = "AE-WEB-01"
DUBAI_FLOOD_DAYS = {date(2024, 4, day) for day in range(16, 20)}
CONFLICT_BASELINE_START = date(2026, 2, 1)
CONFLICT_END = date(2026, 3, 31)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def days() -> list[date]:
    current = START
    values = []
    while current <= END:
        values.append(current)
        current += timedelta(days=1)
    return values


def date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def build_catalog() -> tuple[list[dict[str, object]], dict[str, float], set[str]]:
    write_csv(DATA / "catalog/categories.csv", ["id", "name"], [dict(zip(["id", "name"], item)) for item in CATEGORIES])
    write_csv(
        DATA / "catalog/suppliers.csv",
        ["id", "name", "country", "base_currency"],
        [dict(zip(["id", "name", "country", "base_currency"], item)) for item in SUPPLIERS],
    )
    write_csv(
        DATA / "catalog/stores.csv",
        ["id", "name", "city", "region", "country", "channel"],
        [dict(zip(["id", "name", "city", "region", "country", "channel"], item)) for item in [*STORES, *DUBAI_STORES]],
    )

    descriptors = {
        "C01": ["trail backpack", "shell jacket", "trekking poles", "camp stove", "rain cover", "day pack"],
        "C02": ["lantern", "storage basket", "travel mug", "wool throw", "desk light", "kitchen scale"],
        "C03": ["action camera", "GPS watch", "wireless speaker", "charging hub", "headphones", "tablet stand"],
        "C04": ["packing cubes", "carry-on bag", "passport wallet", "neck pillow", "luggage scale", "toiletry bag"],
        "C05": ["resistance band", "yoga mat", "running belt", "foam roller", "water bottle", "training towel"],
    }
    product_rows: list[dict[str, object]] = []
    prices: dict[str, float] = {}
    shortage_products: set[str] = set()
    supplier_ids = [supplier[0] for supplier in SUPPLIERS if supplier[0] != "S17"]
    for number in range(1, 241):
        category_id = CATEGORIES[(number - 1) % len(CATEGORIES)][0]
        product_id = f"P{number:03d}"
        supplier_id = "S17" if category_id == "C01" and number <= 20 else supplier_ids[(number * 7) % len(supplier_ids)]
        unit_cost = round(9 + (number % 29) * 4.7 + RNG.random() * 5, 2)
        selling_price = round(unit_cost * (1.75 + (number % 4) * 0.1), 2)
        product_rows.append(
            {
                "id": product_id,
                "name": f"{descriptors[category_id][number % 6].title()} {number:03d}",
                "category_id": category_id,
                "supplier_id": supplier_id,
                "supplier_unit_cost": f"{unit_cost:.2f}",
            }
        )
        prices[product_id] = selling_price
        if supplier_id == "S17":
            shortage_products.add(product_id)
    write_csv(
        DATA / "catalog/products.csv",
        ["id", "name", "category_id", "supplier_id", "supplier_unit_cost"],
        product_rows,
    )

    promotion_rows = []
    for number in range(1, 49):
        product_id = f"P{((number * 11) % 240) + 1:03d}"
        start = START + timedelta(days=(number * 7) % 335)
        promotion_rows.append(
            {
                "id": f"PR-{number:03d}",
                "product_id": product_id,
                "starts_on": start.isoformat(),
                "ends_on": (start + timedelta(days=13)).isoformat(),
                "discount_pct": f"{[5, 10, 15, 20][number % 4]:.2f}",
            }
        )
    write_csv(
        DATA / "catalog/promotions.csv",
        ["id", "product_id", "starts_on", "ends_on", "discount_pct"],
        promotion_rows,
    )
    return product_rows, prices, shortage_products


def timestamp(day: date, hour: int, minute: int) -> str:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def append_dubai_conflict_sales(
    sales_rows: list[dict[str, object]], products: list[dict[str, object]], prices: dict[str, float]
) -> None:
    """Add a direct geopolitical-disruption benchmark with a February baseline.

    March 2026 physical retail is constrained by local security measures and
    reduced access. Online trade and inventory remain available, so the data
    can distinguish an operations/traffic effect from a stockout or pricing
    story without encoding the external event in application logic.
    """
    product_ids = [str(product["id"]) for product in products]
    stores = DUBAI_STORES
    sequence = 1
    for day in date_range(CONFLICT_BASELINE_START, CONFLICT_END):
        for store_id, _name, _city, _region, _country, channel in stores:
            if channel == "online":
                event_count = 20 + EVENT_RNG.randint(-3, 3)
            elif day.month == 2:
                event_count = 14 + EVENT_RNG.randint(-3, 3)
            elif day.day <= 14:
                event_count = 4 + EVENT_RNG.randint(0, 2)
            else:
                event_count = 8 + EVENT_RNG.randint(-2, 2)
            for _ in range(max(1, event_count)):
                product_id = EVENT_RNG.choice(product_ids)
                units = EVENT_RNG.choices([1, 2, 3, 4], weights=[55, 29, 11, 5])[0]
                sales_rows.append(
                    {
                        "occurred_at": timestamp(day, EVENT_RNG.randint(8, 20), EVENT_RNG.randint(0, 59)),
                        "event_id": f"EV-DXB-{day:%Y%m%d}-{store_id[-2:]}-{sequence:05d}",
                        "product_id": product_id,
                        "store_id": store_id,
                        "units": units,
                        "revenue": f"{prices[product_id] * units:.2f}",
                        "channel": channel,
                        "platform": "web" if channel == "online" else "",
                        "payment_method": EVENT_RNG.choices(["card", "wallet", "bank_transfer"], weights=[70, 24, 6])[0],
                        "payment_status": "completed",
                        "app_release": "5.0",
                    }
                )
                sequence += 1


def append_dubai_weather_sales(
    sales_rows: list[dict[str, object]], products: list[dict[str, object]], prices: dict[str, float]
) -> None:
    """Add an April 2024 Dubai baseline and short physical-access disruption."""
    product_ids = [str(product["id"]) for product in products]
    sequence = 1
    for day in date_range(date(2024, 4, 1), date(2024, 4, 30)):
        for store_id, _name, _city, _region, _country, channel in DUBAI_STORES:
            event_count = 20 + EVENT_RNG.randint(-3, 3) if channel == "online" else 14 + EVENT_RNG.randint(-3, 3)
            if store_id in DUBAI_PHYSICAL_STORES and day in DUBAI_FLOOD_DAYS:
                event_count = EVENT_RNG.randint(0, 5)
            for _ in range(max(0, event_count)):
                product_id = EVENT_RNG.choice(product_ids)
                units = EVENT_RNG.choices([1, 2, 3, 4], weights=[55, 29, 11, 5])[0]
                sales_rows.append(
                    {
                        "occurred_at": timestamp(day, EVENT_RNG.randint(8, 20), EVENT_RNG.randint(0, 59)),
                        "event_id": f"EV-DXB-WX-{day:%Y%m%d}-{store_id[-2:]}-{sequence:05d}",
                        "product_id": product_id,
                        "store_id": store_id,
                        "units": units,
                        "revenue": f"{prices[product_id] * units:.2f}",
                        "channel": channel,
                        "platform": "web" if channel == "online" else "",
                        "payment_method": EVENT_RNG.choices(["card", "wallet", "bank_transfer"], weights=[70, 24, 6])[0],
                        "payment_status": "completed",
                        "app_release": "4.17",
                    }
                )
                sequence += 1


def dubai_operation(day: date) -> tuple[str, int, str]:
    """Return a generic operational state for the two real-world disruption windows."""
    if day in DUBAI_FLOOD_DAYS:
        if day == date(2024, 4, 16):
            return "closed", 0, "severe weather"
        return "restricted", 5, "severe weather"
    if day.year == 2026 and day.month == 3:
        if day.day <= 7:
            return "restricted", 4, "security restriction"
        return "restricted", 8, "security restriction"
    return "open", 11, ""


def build_analytics(products: list[dict[str, object]], prices: dict[str, float], shortage_products: set[str]) -> None:
    all_days = days()
    physical_stores = [store[0] for store in STORES if store[-1] == "store"]
    product_ids = [str(product["id"]) for product in products]
    online_stores = [store[0] for store in STORES if store[-1] == "online"]

    sales_rows: list[dict[str, object]] = []
    sequence = 1
    for day in all_days:
        for store in STORES:
            store_id, channel = store[0], store[-1]
            base_events = 14 if channel == "store" else 20
            for _ in range(base_events + RNG.randint(-3, 3)):
                product_id = RNG.choice(product_ids)
                shortage_window = date(2024, 7, 14) <= day <= date(2024, 7, 31)
                if shortage_window and store_id in NORTH_ITALY and product_id in shortage_products:
                    product_id = RNG.choice([item for item in product_ids if item not in shortage_products])
                units = RNG.choices([1, 2, 3, 4], weights=[55, 29, 11, 5])[0]
                price = prices[product_id]
                sales_rows.append(
                    {
                        "occurred_at": timestamp(day, RNG.randint(8, 20), RNG.randint(0, 59)),
                        "event_id": f"EV-{day:%Y%m%d}-{sequence:07d}",
                        "product_id": product_id,
                        "store_id": store_id,
                        "units": units,
                        "revenue": f"{price * units:.2f}",
                        "channel": channel,
                        "platform": "web" if channel == "online" else "",
                        "payment_method": RNG.choices(["card", "wallet", "bank_transfer"], weights=[70, 24, 6])[0],
                        "payment_status": "completed",
                        "app_release": "4.16" if day < date(2024, 3, 18) else "4.17",
                    }
                )
                sequence += 1
    # A contained duplicate-ingestion event for later data-quality investigation.
    april_events = [row for row in sales_rows if row["occurred_at"].startswith("2024-04-04")][:120]
    sales_rows.extend(dict(row) for row in april_events)
    append_dubai_weather_sales(sales_rows, products, prices)
    append_dubai_conflict_sales(sales_rows, products, prices)
    write_csv(
        DATA / "analytics/sales_events.csv",
        ["occurred_at", "event_id", "product_id", "store_id", "units", "revenue", "channel", "platform", "payment_method", "payment_status", "app_release"],
        sales_rows,
    )

    inventory_rows: list[dict[str, object]] = []
    tracked_products = product_ids[:120]
    week_starts = [day for day in all_days if day.weekday() == 0]
    for day in week_starts:
        for store_id in physical_stores + online_stores[:1]:
            for product_id in tracked_products:
                store_number = sum(ord(character) for character in store_id)
                product_number = int(product_id[1:])
                available = 70 + (
                    (store_number * 31 + product_number * 17 + day.timetuple().tm_yday * 13) % 160
                )
                incoming = 20 + ((product_number * 7 + day.month * 11) % 70)
                if date(2024, 7, 8) <= day <= date(2024, 7, 29) and store_id in NORTH_ITALY and product_id in shortage_products:
                    weeks = (day - date(2024, 7, 8)).days // 7
                    available = max(0, 48 - weeks * 15 + (product_number % 7))
                    incoming = 0
                inventory_rows.append(
                    {
                        "recorded_at": timestamp(day, 0, 0),
                        "product_id": product_id,
                        "store_id": store_id,
                        "available_units": available,
                        "incoming_units": incoming,
                    }
                )
    for day in date_range(CONFLICT_BASELINE_START, CONFLICT_END):
        if day.weekday() != 0:
            continue
        for store_id in DUBAI_PHYSICAL_STORES:
            for product_id in tracked_products:
                product_number = int(product_id[1:])
                inventory_rows.append(
                    {
                        "recorded_at": timestamp(day, 0, 0),
                        "product_id": product_id,
                        "store_id": store_id,
                        "available_units": 115 + (product_number * 11 + day.day * 3) % 90,
                        "incoming_units": 25 + (product_number * 5 + day.month) % 55,
                    }
                )
    for day in [item for item in week_starts if item.month == 4]:
        for store_id in DUBAI_PHYSICAL_STORES:
            for product_id in tracked_products:
                product_number = int(product_id[1:])
                inventory_rows.append(
                    {
                        "recorded_at": timestamp(day, 0, 0),
                        "product_id": product_id,
                        "store_id": store_id,
                        "available_units": 120 + (product_number * 13 + day.day * 5) % 85,
                        "incoming_units": 24 + (product_number * 3 + day.month) % 50,
                    }
                )
    write_csv(DATA / "analytics/inventory_history.csv", ["recorded_at", "product_id", "store_id", "available_units", "incoming_units"], inventory_rows)

    traffic_rows = []
    for day in all_days:
        weekend_lift = 95 if day.weekday() in (5, 6) else 0
        for store in STORES:
            baseline = 720 if store[-1] == "store" else 1050
            traffic_rows.append({"recorded_at": timestamp(day, 0, 0), "store_id": store[0], "visits": baseline + weekend_lift + RNG.randint(-85, 85)})
    for day in date_range(date(2024, 4, 1), date(2024, 4, 30)):
        for store_id, _name, _city, _region, _country, channel in DUBAI_STORES:
            baseline = 1050 if channel == "online" else 720
            if store_id in DUBAI_PHYSICAL_STORES and day in DUBAI_FLOOD_DAYS:
                baseline = 130 if day == date(2024, 4, 16) else 260
            traffic_rows.append(
                {"recorded_at": timestamp(day, 0, 0), "store_id": store_id, "visits": baseline + EVENT_RNG.randint(-45, 45)}
            )
    for day in date_range(CONFLICT_BASELINE_START, CONFLICT_END):
        for store in DUBAI_STORES:
            if store[-1] == "online":
                baseline = 1050
            elif day.month == 2:
                baseline = 720
            elif day.day <= 14:
                baseline = 155
            else:
                baseline = 360
            traffic_rows.append(
                {"recorded_at": timestamp(day, 0, 0), "store_id": store[0], "visits": baseline + EVENT_RNG.randint(-45, 45)}
            )
    write_csv(DATA / "analytics/store_traffic.csv", ["recorded_at", "store_id", "visits"], traffic_rows)

    conversion_rows = []
    for day in all_days:
        for platform in ("ios", "android", "web"):
            sessions = 880 + RNG.randint(-65, 65)
            checkout_sessions = round(sessions * (0.34 + RNG.uniform(-0.02, 0.02)))
            failure_window = date(2024, 3, 18) <= day <= date(2024, 3, 31) and platform == "ios"
            completed = round(checkout_sessions * (0.38 if failure_window else 0.78 + RNG.uniform(-0.025, 0.025)))
            conversion_rows.append(
                {
                    "recorded_at": timestamp(day, 12, 0),
                    "platform": platform,
                    "sessions": sessions,
                    "checkout_sessions": checkout_sessions,
                    "completed_orders": completed,
                    "payment_failures": checkout_sessions - completed if failure_window else RNG.randint(7, 20),
                    "app_release": "4.16" if day < date(2024, 3, 18) else "4.17",
                }
            )
    write_csv(DATA / "analytics/conversion_metrics.csv", ["recorded_at", "platform", "sessions", "checkout_sessions", "completed_orders", "payment_failures", "app_release"], conversion_rows)

    price_rows = []
    for day in all_days:
        if day.day != 1:
            continue
        for product in products:
            product_id = str(product["id"])
            base_cost = float(product["supplier_unit_cost"])
            imported = str(product["supplier_id"]) in {"S09", "S10"}
            local_cost = base_cost
            if imported and day >= date(2024, 9, 1):
                local_cost = base_cost * (1.0 + ((day.month - 8) * 0.055))
            price_rows.append(
                {
                    "recorded_at": timestamp(day, 0, 0),
                    "product_id": product_id,
                    "selling_price": f"{prices[product_id]:.2f}",
                    "local_unit_cost": f"{local_cost:.2f}",
                    "supplier_base_price": f"{base_cost:.2f}",
                    "currency": "USD" if str(product["supplier_id"]) == "S09" else "JPY" if str(product["supplier_id"]) == "S10" else "EUR",
                }
            )
    for day in (date(2026, 2, 1), date(2026, 3, 1)):
        for product in products:
            product_id = str(product["id"])
            base_cost = float(product["supplier_unit_cost"])
            price_rows.append(
                {
                    "recorded_at": timestamp(day, 0, 0),
                    "product_id": product_id,
                    "selling_price": f"{prices[product_id]:.2f}",
                    "local_unit_cost": f"{base_cost:.2f}",
                    "supplier_base_price": f"{base_cost:.2f}",
                    "currency": "USD" if str(product["supplier_id"]) == "S09" else "JPY" if str(product["supplier_id"]) == "S10" else "EUR",
                }
            )
    write_csv(DATA / "analytics/price_history.csv", ["recorded_at", "product_id", "selling_price", "local_unit_cost", "supplier_base_price", "currency"], price_rows)

    operation_rows: list[dict[str, object]] = []
    for day in [*all_days, *date_range(CONFLICT_BASELINE_START, CONFLICT_END)]:
        for store_id in DUBAI_PHYSICAL_STORES:
            status, hours, reason = dubai_operation(day)
            operation_rows.append(
                {
                    "recorded_at": timestamp(day, 0, 0),
                    "store_id": store_id,
                    "operating_status": status,
                    "open_hours": hours,
                    "restriction_reason": reason,
                }
            )
    write_csv(
        DATA / "analytics/store_operations.csv",
        ["recorded_at", "store_id", "operating_status", "open_hours", "restriction_reason"],
        operation_rows,
    )


if __name__ == "__main__":
    product_rows, product_prices, supplier_shortage_products = build_catalog()
    build_analytics(product_rows, product_prices, supplier_shortage_products)
