import csv
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


def test_analytics_fixtures_preserve_supplier_shortage_signal() -> None:
    inventory = read_csv("analytics/inventory_history.csv")
    sales = read_csv("analytics/sales_events.csv")

    assert set(sales[0]) == {
        "occurred_at", "event_id", "product_id", "store_id", "units", "revenue", "channel",
        "platform", "payment_method", "payment_status", "app_release",
    }
    milan_p001 = [row for row in inventory if row["product_id"] == "P001" and row["store_id"] == "IT-MIL-01"]
    assert [int(row["available_units"]) for row in milan_p001] == [84, 46, 12, 4]
    milan_p001_sales = [row for row in sales if row["product_id"] == "P001" and row["store_id"] == "IT-MIL-01"]
    assert [int(row["units"]) for row in milan_p001_sales] == [18, 17, 5, 2]
