"""File-level invariants for backend/migrations. No DB, no network.

Guards three things the schema-identity CI job can't see until later:
sequential numbering without gaps, a provenance line in every shipped
migration (lineage must be explicit), and the initdb stamp file agreeing
exactly with the migration set.
"""

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def numbered_files():
    return sorted(p.name for p in MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))


def test_migrations_sequential_no_gaps():
    names = numbered_files()
    assert names, "no numbered migrations found"
    ids = [int(n[:3]) for n in names]
    assert ids == list(range(1, len(ids) + 1)), f"non-sequential ids: {ids}"


def test_each_migration_names_its_lineage():
    for name in numbered_files():
        head = "\n".join((MIGRATIONS / name).read_text().splitlines()[:10])
        assert ("Ported from agi-memory" in head) or ("Ported from Sili" in head) or ("sill original" in head), (
            f"{name}: header needs a provenance line "
            "('Ported from agi-memory migration NNN (date)' or 'Ported from Sili migration NNN (date)' or 'sill original')"
        )


def test_initdb_stamp_matches_migration_set():
    stamp = (MIGRATIONS / "zzz_initdb_stamp.sql").read_text()
    stamped = set(re.findall(r"\('(\d{3})',\s*'initdb'\)", stamp))
    present = {n[:3] for n in numbered_files()}
    assert stamped == present, f"stamp {sorted(stamped)} != files {sorted(present)}"
