"""Nightly database backups to the Drive (Backblaze B2), run by .github/workflows/backup.yml.

Every enabled database plugin is dumped table by table through its adapter (paged ``SELECT``/``find``)
into a portable gzip'd JSON-lines file: one header line per table (``{"table", "columns"}``) followed by
one line per row. Postgres databases (Neon, Supabase) additionally get a real ``pg_dump`` when the binary
is available (the workflow runs it from the postgres:17 image), which is the format to restore from.

Files land in ``backups/<YYYY-MM-DD>/<plugin>.jsonl.gz`` (and ``<plugin>.pg_dump.sql.gz``) on the
storage plugin chosen with ``--storage`` (default ``backblaze_b2``, the one with a real $0 spending cap).
Day folders older than ``--keep-days`` (default 14) are deleted afterwards.

    python -m automation.backup                         # all databases, keep 14 days
    python -m automation.backup --only neon,turso --keep-days 30
    python -m automation.backup --dry-run               # dump locally into data/backups, upload nothing

Exit code 0 when every database was backed up, 1 when at least one failed (the rest still run).
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.interfaces import DBAdapter, NotConfigured, ProviderKind, StorageAdapter
from core.registry import PluginRegistry

__all__ = ["BackupReport", "dump_database", "main", "prune", "run_backups"]

PAGE = 500
PREFIX = "backups"
POSTGRES = {"neon": "NEON_DATABASE_URL", "supabase": "SUPABASE_DB_URL"}
SQLITE_FAMILY = {"turso", "cloudflare_d1"}
SKIP = {"upstash_redis"}  # a cache: counters and events, nothing worth restoring


@dataclass
class BackupReport:
    ok: dict[str, int] = field(default_factory=dict)  # plugin -> rows dumped
    failed: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)  # not configured (no credentials yet)
    files: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _rows(adapter: DBAdapter, plugin: str, table: str) -> AsyncIterator[tuple[list[str], list[list[Any]]]]:
    """Yield (columns, rows) pages for one table/collection."""
    offset = 0
    while True:
        if plugin == "mongodb_atlas":
            result = await adapter.query(json.dumps({"find": table, "skip": offset}), limit=PAGE)
        else:
            result = await adapter.query(f"SELECT * FROM {_quote(table)} LIMIT {PAGE} OFFSET {offset}", limit=PAGE)
        rows = result.rows[:PAGE]
        yield result.columns, rows
        if len(rows) < PAGE:
            return
        offset += PAGE


async def dump_database(adapter: DBAdapter, plugin: str, out: Path) -> int:
    """Write ``out`` (gzip JSON lines) and return the number of rows dumped."""
    total = 0
    tables = await adapter.list_tables()
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"plugin": plugin, "dumped_at": datetime.now(timezone.utc).isoformat(),
                             "tables": [t.name for t in tables]}) + "\n")
        for info in tables:
            if plugin in SQLITE_FAMILY and (info.name.startswith("sqlite_") or info.name.startswith("_cf_")):
                continue
            header_written = False
            async for columns, rows in _rows(adapter, plugin, info.name):
                if not header_written:
                    fh.write(json.dumps({"table": info.name, "columns": columns}) + "\n")
                    header_written = True
                for row in rows:
                    fh.write(json.dumps(row, default=str) + "\n")
                total += len(rows)
    return total


def pg_dump(url: str, out: Path) -> bool:
    """Run pg_dump (plain SQL, no owners) into ``out`` gzip'd. Returns False when pg_dump is unavailable."""
    binary = shutil.which("pg_dump")
    if not binary:
        return False
    proc = subprocess.run([binary, "--no-owner", "--no-privileges", "--format=plain", url],
                          capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-400:])
    with gzip.open(out, "wb") as fh:
        fh.write(proc.stdout)
    return True


async def prune(storage: StorageAdapter, keep_days: int, today: date) -> list[str]:
    """Delete every object under backups/<day>/ older than ``keep_days``."""
    cutoff = today - timedelta(days=keep_days)
    removed: list[str] = []
    for obj in await storage.list(prefix=f"{PREFIX}/", limit=10_000):
        parts = obj.key.split("/")
        try:
            day = date.fromisoformat(parts[1])
        except (IndexError, ValueError):
            continue
        if day < cutoff:
            await storage.delete(obj.key)
            removed.append(obj.key)
    return removed


async def run_backups(registry: PluginRegistry, *, secret: Any, only: Sequence[str] = (), storage_name: str = "backblaze_b2",
                      keep_days: int = 14, workdir: Path, dry_run: bool = False,
                      today: date | None = None) -> BackupReport:
    today = today or datetime.now(timezone.utc).date()
    report = BackupReport()
    workdir.mkdir(parents=True, exist_ok=True)
    storage = None if dry_run else await registry.get(storage_name)
    names = [spec.name for spec in registry.specs.values()
             if spec.kind == ProviderKind.DB and spec.enabled and spec.name not in SKIP and (not only or spec.name in only)]
    for name in names:
        try:
            adapter = await registry.get(name)
        except NotConfigured as exc:  # e.g. card-only databases without credentials yet
            report.skipped[name] = str(exc)
            continue
        except Exception as exc:
            report.failed[name] = f"{type(exc).__name__}: {exc}"
            continue
        files: list[Path] = []
        try:
            out = workdir / f"{name}.jsonl.gz"
            report.ok[name] = await dump_database(adapter, name, out)
            files.append(out)
            url = secret(POSTGRES[name]) if name in POSTGRES else None
            if url:
                sql = workdir / f"{name}.pg_dump.sql.gz"
                if await asyncio.to_thread(pg_dump, url, sql):
                    files.append(sql)
        except NotConfigured as exc:  # credentials are checked lazily, on the first call
            report.skipped[name] = str(exc)
            report.ok.pop(name, None)
            continue
        except Exception as exc:
            report.failed[name] = f"{type(exc).__name__}: {exc}"[:400]
            report.ok.pop(name, None)
        for path in files:
            key = f"{PREFIX}/{today.isoformat()}/{path.name}"
            if storage is not None:
                await storage.put(key, path.read_bytes(), "application/gzip", {"source": "omnifree-backup"})
            report.files.append(key)
    if storage is not None:
        report.pruned = await prune(storage, keep_days, today)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m automation.backup")
    parser.add_argument("--only", default="", help="comma-separated database plugin names")
    parser.add_argument("--storage", default="backblaze_b2")
    parser.add_argument("--keep-days", type=int, default=14)
    parser.add_argument("--workdir", default="data/backups")
    parser.add_argument("--dry-run", action="store_true", help="dump locally, upload and prune nothing")
    args = parser.parse_args(argv)

    from core.config import get_settings

    settings = get_settings()

    async def go() -> BackupReport:
        registry = PluginRegistry.from_file(settings.providers_path, secret_lookup=settings.secret)
        try:
            return await run_backups(registry, secret=settings.secret,
                                     only=[x for x in args.only.split(",") if x], storage_name=args.storage,
                                     keep_days=args.keep_days, workdir=Path(args.workdir), dry_run=args.dry_run)
        finally:
            await registry.close()

    report = asyncio.run(go())
    for name, rows in report.ok.items():
        print(f"ok      {name}: {rows} rows")
    for name in report.skipped:
        print(f"skipped {name}: not configured")
    for name, err in report.failed.items():
        print(f"FAILED  {name}: {err}")
    verb = "wrote" if args.dry_run else "uploaded"
    print(f"{verb} {len(report.files)} files, pruned {len(report.pruned)}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
