"""update_memory_importance on a disposable store (migration 016).

Before 016 the function wrote its audit row to ag_catalog.importance_updates,
a table the baseline creates in public, so every call raised and the MCP
update_importance tool could not work on a Sill install.
"""
from uuid import uuid4

import pytest


@pytest.mark.parametrize("search_path", ["ag_catalog, public", "public", "public, ag_catalog"])
def test_update_importance_changes_the_row_and_records_why(db, search_path):
    with db.cursor() as cur:
        cur.execute("SELECT memory_id FROM public.mint_memory('semantic',%s)", ("importance " + uuid4().hex,))
        memory_id = cur.fetchone()[0]
        cur.execute("SELECT importance FROM public.memories WHERE id=%s", (memory_id,))
        before = cur.fetchone()[0]
        cur.execute(f"SET LOCAL search_path = {search_path}")
        cur.execute("SELECT update_memory_importance(%s, 0.9, 'reviewed')", (memory_id,))
        assert cur.fetchone()[0] is True
        cur.execute("SELECT importance FROM public.memories WHERE id=%s", (memory_id,))
        assert cur.fetchone()[0] == pytest.approx(0.9)
        cur.execute("SELECT old_importance, new_importance, reason FROM importance_updates WHERE memory_id=%s",
                    (memory_id,))
        assert cur.fetchall() == [(pytest.approx(before), pytest.approx(0.9), "reviewed")]


def test_update_importance_reports_an_unknown_memory(db):
    with db.cursor() as cur:
        cur.execute("SELECT update_memory_importance(%s, 0.9, 'nobody')", (str(uuid4()),))
        assert cur.fetchone()[0] is False
