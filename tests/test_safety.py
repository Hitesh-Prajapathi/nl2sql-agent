from __future__ import annotations

import pytest
from nl2sql.safety.validator import validate_sql


def test_select_queries_allowed() -> None:
    valid_queries = [
        "SELECT * FROM customers;",
        "SELECT count(*), status FROM orders GROUP BY status;",
        "SELECT c.name, sum(o.total_amount) FROM customers c JOIN orders o ON c.customer_id = o.customer_id GROUP BY c.name;",
        "WITH delivered_orders AS (SELECT * FROM orders WHERE status = 'delivered') SELECT * FROM delivered_orders;",
    ]
    for sql in valid_queries:
        result = validate_sql(sql)
        assert result.is_valid, f"Expected valid query for '{sql}', got: {result.error}"


def test_mutation_statements_blocked() -> None:
    blocked_queries = [
        "DELETE FROM orders WHERE status = 'cancelled';",
        "UPDATE customers SET tier = 'Gold' WHERE customer_id = 1;",
        "INSERT INTO products (name, unit_price) VALUES ('Hacked', 0);",
        "DROP TABLE customers;",
        "TRUNCATE TABLE payments;",
        "ALTER TABLE orders ADD COLUMN test INT;",
        "GRANT ALL PRIVILEGES ON DATABASE nl2sql_assignment TO public;",
    ]
    for sql in blocked_queries:
        result = validate_sql(sql)
        assert not result.is_valid, f"Expected '{sql}' to be blocked"
        assert result.is_safety_block, f"Expected safety block for '{sql}'"


def test_multiple_statements_blocked() -> None:
    stacked = "SELECT 1; DROP TABLE orders;"
    result = validate_sql(stacked)
    assert not result.is_valid
    assert result.is_safety_block
    assert "Multiple statements" in (result.error or "")


def test_dangerous_functions_blocked() -> None:
    dangerous = [
        "SELECT pg_sleep(10);",
        "SELECT pg_read_file('/etc/passwd');",
        "SELECT pg_catalog.pg_sleep(5);",
        "SELECT * FROM orders WHERE order_id = 1 AND (SELECT pg_sleep(2)) IS NULL;",
    ]
    for sql in dangerous:
        result = validate_sql(sql)
        assert not result.is_valid, f"Expected dangerous function '{sql}' to be blocked"
        assert result.is_safety_block


def test_disallowed_system_tables_blocked() -> None:
    blocked_sys = [
        "SELECT * FROM pg_shadow;",
        "SELECT * FROM pg_authid;",
    ]
    for sql in blocked_sys:
        result = validate_sql(sql)
        assert not result.is_valid
        assert result.is_safety_block
