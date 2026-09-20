import csv
from collections import Counter
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "examples" / "data"


def read_csv(relative_path: str) -> list[dict[str, str]]:
    with (DATA / relative_path).open(newline="") as fixture:
        return list(csv.DictReader(fixture))


def test_catalog_fixtures_have_expected_columns_and_references() -> None:
    categories = read_csv("catalog/categories.csv")
    suppliers = read_csv("catalog/suppliers.csv")
    products = read_csv("catalog/products.csv")
    stores = read_csv("catalog/stores.csv")

    assert set(categories[0]) == {"id", "name"}
    assert set(suppliers[0]) == {"id", "name", "country", "base_currency"}
    assert set(products[0]) == {"id", "name", "category_id", "supplier_id", "supplier_unit_cost"}
    assert {product["category_id"] for product in products} <= {category["id"] for category in categories}
    assert {product["supplier_id"] for product in products} <= {supplier["id"] for supplier in suppliers}
    assert {store["channel"] for store in stores} == {"store", "online"}
    assert len(products) >= 200
    assert len(stores) >= 12


def test_analytics_fixtures_preserve_supplier_shortage_signal() -> None:
    inventory = read_csv("analytics/inventory_history.csv")
    sales = read_csv("analytics/sales_events.csv")
    products = read_csv("catalog/products.csv")

    assert set(sales[0]) == {
        "occurred_at", "event_id", "product_id", "store_id", "units", "revenue", "channel",
        "platform", "payment_method", "payment_status", "app_release",
    }
    milan_p001 = [row for row in inventory if row["product_id"] == "P001" and row["store_id"] == "IT-MIL-01"]
    july_shortage = [
        row for row in milan_p001
        if "2024-07-08" <= row["recorded_at"][:10] <= "2024-07-29"
    ]
    assert [int(row["available_units"]) for row in july_shortage] == [49, 34, 19, 4]
    assert all(int(row["incoming_units"]) == 0 for row in july_shortage)

    north_italy = {"IT-MIL-01", "IT-BER-01", "IT-BRE-01", "IT-TUR-01", "IT-GEN-01", "IT-VER-01"}
    shortage_products = {product["id"] for product in products if product["supplier_id"] == "S17"}

    def units_sold(start: date, end: date) -> int:
        return sum(
            int(row["units"])
            for row in sales
            if start <= date.fromisoformat(row["occurred_at"][:10]) <= end
            and row["store_id"] in north_italy
            and row["product_id"] in shortage_products
        )

    # The supplier shortage removes its products from Northern Italy sales after July 14.
    assert units_sold(date(2024, 7, 14), date(2024, 7, 31)) == 0
    assert units_sold(date(2024, 7, 1), date(2024, 7, 13)) > 0
    assert len(sales) > 80_000

    # A deliberately contained duplicate-ingestion incident supports data-quality demos.
    event_ids = Counter(row["event_id"] for row in sales)
    assert any(count > 1 for count in event_ids.values())
