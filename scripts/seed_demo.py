"""Idempotent Demo data seeding through the application Data Source boundary.

Demo sales/procurement records are replaceable fixtures (PROJECT_SPEC §16). They are
loaded with the application account and read exclusively through the read-only account.

Usage (same environment as the backend, see README):

    uv run python -m scripts.seed_demo
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from backend.infrastructure.config import Settings
from backend.infrastructure.database import MySQLConnectionPool, PoolSettings

logger = logging.getLogger(__name__)

DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS products (
      product_id VARCHAR(20) NOT NULL PRIMARY KEY,
      name VARCHAR(120) NOT NULL,
      category VARCHAR(60) NOT NULL,
      list_price DECIMAL(12,2) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS sales_orders (
      order_id VARCHAR(20) NOT NULL PRIMARY KEY,
      product_id VARCHAR(20) NOT NULL,
      region VARCHAR(40) NOT NULL,
      customer_contact VARCHAR(120) NOT NULL,
      quantity INT NOT NULL,
      amount DECIMAL(12,2) NOT NULL,
      order_date DATE NOT NULL,
      KEY ix_sales_product (product_id),
      KEY ix_sales_date (order_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS procurement_orders (
      procurement_id VARCHAR(20) NOT NULL PRIMARY KEY,
      product_id VARCHAR(20) NOT NULL,
      supplier VARCHAR(120) NOT NULL,
      quantity INT NOT NULL,
      amount DECIMAL(12,2) NOT NULL,
      order_date DATE NOT NULL,
      KEY ix_procurement_product (product_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)

PRODUCTS: tuple[tuple[str, str, str, float], ...] = (
    ("P-1001", "Enterprise Gateway", "Infrastructure", 42000.00),
    ("P-1002", "Analytics Suite", "Software", 15800.00),
    ("P-1003", "Support Contract", "Service", 9600.00),
    ("P-1004", "Edge Sensor Kit", "Hardware", 2400.00),
    ("P-1005", "Training Package", "Service", 3200.00),
)

REGIONS: tuple[str, ...] = ("North", "South", "East", "West")

SALES_ROWS: tuple[tuple[str, str, str, str, int, float, str], ...] = tuple(
    (
        f"SO-{2000 + index}",
        PRODUCTS[index % len(PRODUCTS)][0],
        REGIONS[index % len(REGIONS)],
        f"contact{index}@example.internal",
        1 + (index % 12),
        round(1500.0 + (index * 733.17) % 40000, 2),
        (date(2026, 1, 1) + timedelta(days=index * 3)).isoformat(),
    )
    for index in range(48)
)

PROCUREMENT_ROWS: tuple[tuple[str, str, str, int, float, str], ...] = tuple(
    (
        f"PO-{3000 + index}",
        PRODUCTS[index % len(PRODUCTS)][0],
        f"Supplier {chr(65 + (index % 6))}",
        1 + (index % 9),
        round(800.0 + (index * 411.53) % 22000, 2),
        (date(2026, 1, 5) + timedelta(days=index * 4)).isoformat(),
    )
    for index in range(30)
)


def seed(settings: Settings) -> dict[str, int]:
    pool = MySQLConnectionPool(PoolSettings.application(settings, pool_size=1))
    try:
        with pool.cursor() as cursor:
            for statement in DDL:
                cursor.execute(statement)
        with pool.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO products (product_id, name, category, list_price) "
                "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE name = VALUES(name), "
                "category = VALUES(category), list_price = VALUES(list_price)",
                PRODUCTS,
            )
            cursor.executemany(
                "INSERT INTO sales_orders (order_id, product_id, region, customer_contact, "
                "quantity, "
                "amount, order_date) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE amount = VALUES(amount), quantity = VALUES(quantity)",
                SALES_ROWS,
            )
            cursor.executemany(
                "INSERT INTO procurement_orders (procurement_id, product_id, supplier, quantity, "
                "amount, order_date) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE amount = VALUES(amount), quantity = VALUES(quantity)",
                PROCUREMENT_ROWS,
            )
        _record_version(pool, settings.schema_version)
        return {
            "products": len(PRODUCTS),
            "sales_orders": len(SALES_ROWS),
            "procurement_orders": len(PROCUREMENT_ROWS),
        }
    finally:
        pool.close()


def _record_version(pool: MySQLConnectionPool, version: str) -> None:
    from backend.domain.identifiers import utc_now

    pool.execute(
        "INSERT INTO demo_metadata (metadata_key, metadata_value, updated_at) "
        "VALUES ('demo_schema_version', %s, %s) ON DUPLICATE KEY UPDATE "
        "metadata_value = VALUES(metadata_value), updated_at = VALUES(updated_at)",
        (version, utc_now()),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    counts = seed(settings)
    print(f"demo data seeded: {counts}")


if __name__ == "__main__":
    main()
