#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
copy_db_to_local.py
Copy all user tables from a SQL Server database to a local SQLite file.

Usage:
    python copy_db_to_local.py
    python copy_db_to_local.py --source db_1405
    python copy_db_to_local.py --source db_1405 --output local_db_1405.db
    python copy_db_to_local.py --list
    python copy_db_to_local.py --source db_1405 --user sa --password '...'

Requirements:
    pip install pyodbc pandas sqlalchemy
"""

import argparse
import os
import sys
import time
from decimal import Decimal

try:
    import pyodbc
    import pandas as pd
    from sqlalchemy import create_engine
except ImportError as e:
    print(f"Missing dependency: {e.name}\n"
          f"Install with:  pip install pyodbc pandas sqlalchemy", file=sys.stderr)
    sys.exit(1)


# ==============================================================
#  Connection helpers
# ==============================================================
def build_mssql_connection(server=".", database="master",
                           driver="{SQL Server}", trusted=True,
                           username=None, password=None):
    parts = [
        f"DRIVER={driver}",
        f"SERVER={server}",
        f"DATABASE={database}",
    ]
    if trusted:
        parts.append("Trusted_Connection=yes;")
    else:
        parts.append(f"UID={username};")
        parts.append(f"PWD={password};")
    return pyodbc.connect(";".join(parts), timeout=15)


def list_databases(server=".", driver="{SQL Server}",
                   trusted=True, username=None, password=None):
    conn = build_mssql_connection(server, "master", driver, trusted,
                                  username, password)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT name FROM sys.databases ORDER BY name")
        except pyodbc.Error:
            cur.execute("SELECT name FROM sysdatabases ORDER BY name")
        dbs = [r[0] for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()

    print("📋 Available databases:")
    for db in dbs:
        print(f"   - {db}")
    return dbs


def list_user_tables(conn):
    cur = conn.cursor()
    try:
        try:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
            )
        except pyodbc.Error:
            cur.execute(
                "SELECT name FROM sysobjects WHERE xtype='U' ORDER BY name"
            )
        return [r[0] for r in cur.fetchall()]
    finally:
        cur.close()


# ==============================================================
#  Data type normalisation
# ==============================================================
def normalize_dataframe(df):
    """Make the DataFrame safe to write to SQLite."""
    for col in df.columns:
        if df[col].dtype != object:
            continue

        # Collapse memoryview -> bytes (SQLite BLOB)
        df[col] = df[col].apply(
            lambda v: bytes(v) if isinstance(v, memoryview) else v
        )

        # Convert Decimal -> float so numeric comparisons survive
        sample = df[col].dropna()
        if not sample.empty and isinstance(sample.iloc[0], Decimal):
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def safe_read_table(conn, table, chunk_size=None):
    sql = f"SELECT * FROM [{table}]"
    if chunk_size:
        frames = [chunk for chunk in pd.read_sql(sql, conn,
                                                chunksize=chunk_size)]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return pd.read_sql(sql, conn)


# ==============================================================
#  Copy
# ==============================================================
def copy_db(source_db, output_file, server=".", driver="{SQL Server}",
            trusted=True, username=None, password=None, chunk_size=None):
    print(f"\n🚚 Copying DB '{source_db}' → '{output_file}'")
    print("=" * 70)

    src_conn = build_mssql_connection(server, source_db, driver,
                                      trusted, username, password)
    dest_engine = create_engine(f"sqlite:///{output_file}")

    t0 = time.time()
    try:
        tables = list_user_tables(src_conn)
        print(f"📋 Found {len(tables)} tables\n")

        ok, fail = [], []
        total_rows = 0

        for i, table in enumerate(tables, 1):
            prefix = f"[{i:>2}/{len(tables)}] {table:<38}"
            print(f"{prefix} ... ", end="", flush=True)
            t1 = time.time()
            try:
                df = safe_read_table(src_conn, table, chunk_size)
                df = normalize_dataframe(df)
                df.to_sql(table, dest_engine, if_exists="replace",
                          index=False)
                elapsed = time.time() - t1
                print(f"✅ {len(df):>8,} rows  ({elapsed:.1f}s)")
                ok.append(table)
                total_rows += len(df)
            except Exception as e:
                print(f"❌ {type(e).__name__}: {e}")
                fail.append((table, str(e)))
    finally:
        src_conn.close()

    total_time = time.time() - t0
    print("=" * 70)
    print(f"✅ Copied {len(ok)} tables, {total_rows:,} rows "
          f"in {total_time:.1f}s")
    if fail:
        print(f"❌ Failed: {len(fail)} tables")
        for t, err in fail:
            print(f"   - {t}: {err[:120]}")
    print(f"📁 Output: {os.path.abspath(output_file)}")
    return ok, fail


# ==============================================================
#  Compare
# ==============================================================
def compare_counts(source_db, sqlite_file, server=".",
                   driver="{SQL Server}", trusted=True,
                   username=None, password=None):
    import sqlite3
    print(f"\n📊 Row counts: {source_db} vs {sqlite_file}")
    print("=" * 70)

    src_conn = build_mssql_connection(server, source_db, driver,
                                      trusted, username, password)
    src_cur = src_conn.cursor()
    lite_conn = sqlite3.connect(sqlite_file)
    lite_cur = lite_conn.cursor()

    try:
        tables = list_user_tables(src_conn)
        total_src = total_lite = 0
        diffs = []

        for table in tables:
            try:
                src_cur.execute(f"SELECT COUNT(*) FROM [{table}]")
                c_src = src_cur.fetchone()[0] or 0
                lite_cur.execute(f"SELECT COUNT(*) FROM [{table}]")
                c_lite = lite_cur.fetchone()[0] or 0
                total_src += c_src
                total_lite += c_lite
                if c_src == c_lite:
                    print(f"✅ {table:<38} {c_src:>10,}")
                else:
                    print(f"⚠️  {table:<38} src={c_src:>10,}  lite={c_lite:>10,}")
                    diffs.append(table)
            except Exception as e:
                print(f"❌ {table:<38} {e}")
    finally:
        src_cur.close(); src_conn.close()
        lite_cur.close(); lite_conn.close()

    print("=" * 70)
    print(f"📊 SQL Server : {total_src:,} rows")
    print(f"📊 SQLite     : {total_lite:,} rows")
    if diffs:
        print(f"⚠️  Mismatched tables: {', '.join(diffs)}")
    else:
        print("✅ All row counts match.")
    return diffs


# ==============================================================
#  CLI
# ==============================================================
def main():
    ap = argparse.ArgumentParser(description="Copy SQL Server DB → SQLite.")
    ap.add_argument("--source", default="db_1405")
    ap.add_argument("--output", default=None)
    ap.add_argument("--server", default=".")
    ap.add_argument("--driver", default="{SQL Server}")
    ap.add_argument("--user", default=None,
                    help="SQL user (omit for Trusted_Connection)")
    ap.add_argument("--password", default=None)
    ap.add_argument("--chunk", type=int, default=None,
                    help="Read large tables in chunks")
    ap.add_argument("--list", action="store_true", help="List DBs and exit")
    ap.add_argument("--skip-compare", action="store_true")
    args = ap.parse_args()

    trusted = args.user is None

    if args.list:
        list_databases(args.server, args.driver,
                       trusted, args.user, args.password)
        return

    output = args.output or f"local_{args.source}.db"

    copy_db(
        source_db=args.source, output_file=output,
        server=args.server, driver=args.driver, trusted=trusted,
        username=args.user, password=args.password,
        chunk_size=args.chunk,
    )

    if not args.skip_compare:
        compare_counts(
            source_db=args.source, sqlite_file=output,
            server=args.server, driver=args.driver, trusted=trusted,
            username=args.user, password=args.password,
        )


if __name__ == "__main__":
    main()