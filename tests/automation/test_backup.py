from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path
from typing import Any

from automation.backup import dump_database, prune
from core.interfaces import QueryResult, StoredObject, TableInfo


class FakeDB:
    def __init__(self, tables: dict[str, list[list[Any]]]) -> None:
        self.tables = tables
        self.statements: list[str] = []

    async def list_tables(self) -> list[TableInfo]:
        return [TableInfo(name=n) for n in self.tables]

    async def query(self, statement: str, params: Any = None, limit: int = 500) -> QueryResult:
        self.statements.append(statement)
        if statement.startswith("{"):
            cmd = json.loads(statement)
            rows, offset = self.tables[cmd["find"]], int(cmd.get("skip", 0))
        else:
            name = statement.split('"')[1]
            rows = self.tables[name]
            offset = int(statement.rsplit("OFFSET", 1)[1])
        page = rows[offset:offset + limit]
        return QueryResult(columns=["a", "b"], rows=page, row_count=len(page))


class FakeStorage:
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        self.deleted: list[str] = []

    async def list(self, prefix: str = "", limit: int = 1000) -> list[StoredObject]:
        return [StoredObject(key=k, size=1) for k in self.keys if k.startswith(prefix)]

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


async def test_dump_pages_through_big_tables_and_skips_sqlite_internals(tmp_path: Path) -> None:
    db = FakeDB({"notes": [[i, f"n{i}"] for i in range(1203)], "sqlite_sequence": [["x", 1]]})
    out = tmp_path / "turso.jsonl.gz"
    assert await dump_database(db, "turso", out) == 1203  # type: ignore[arg-type]
    lines = gzip.open(out, "rt", encoding="utf-8").read().splitlines()
    assert json.loads(lines[1]) == {"table": "notes", "columns": ["a", "b"]}
    assert len(lines) == 2 + 1203 and not any("sqlite_sequence" in s for s in db.statements)
    assert [s.rsplit("OFFSET", 1)[1].strip() for s in db.statements] == ["0", "500", "1000"]


async def test_mongo_uses_find_with_skip(tmp_path: Path) -> None:
    db = FakeDB({"items": [[1, 2]] * 3})
    assert await dump_database(db, "mongodb_atlas", tmp_path / "m.gz") == 3  # type: ignore[arg-type]
    assert json.loads(db.statements[0]) == {"find": "items", "skip": 0}


async def test_prune_removes_only_old_day_folders() -> None:
    st = FakeStorage(["backups/2026-10-01/neon.jsonl.gz", "backups/2026-10-10/neon.jsonl.gz",
                      "backups/2026-10-20/neon.jsonl.gz", "backups/readme.txt"])
    removed = await prune(st, keep_days=14, today=date(2026, 10, 20))  # type: ignore[arg-type]
    assert removed == ["backups/2026-10-01/neon.jsonl.gz"]
