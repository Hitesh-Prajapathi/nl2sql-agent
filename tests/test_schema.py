from __future__ import annotations

import pytest

from nl2sql.db.connection import get_pool, close_pool
from nl2sql.schema.introspect import introspect_schema, get_catalog_hash
from nl2sql.schema.provider import FullSchemaProvider


@pytest.mark.asyncio
async def test_schema_introspection_and_profiling() -> None:
    pool = await get_pool()
    try:
        context = await introspect_schema(pool)
        
        # Verify all 8 tables are present in context
        expected_tables = [
            "customers", "products", "orders", "order_items",
            "payments", "refunds", "support_tickets", "agents"
        ]
        for table in expected_tables:
            assert f"TABLE: {table}" in context, f"Table {table} missing from schema context"
            
        # Verify data profiling elements are present
        assert "rows)" in context
        assert "Distinct values:" in context  # Low cardinality values e.g. status, tier
        assert "Sample rows" in context
        
        # Verify catalog hash is deterministic
        h1 = await get_catalog_hash(pool)
        h2 = await get_catalog_hash(pool)
        assert h1 == h2
        assert len(h1) == 32
        
        # Verify FullSchemaProvider caching
        provider = FullSchemaProvider(pool)
        c1 = await provider.get_context("How many orders?")
        c2 = await provider.get_context("List all customers")
        assert c1 == c2
        assert c1 == context
    finally:
        await close_pool()
