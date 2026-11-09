"""SQLite persistence: runs, an append-only event log (drives the live view), and the PO ledger."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, created_at REAL, finished_at REAL, filename TEXT, source TEXT, sample_id TEXT,
  status TEXT, decision TEXT, final_decision TEXT, vendor_name TEXT, invoice_number TEXT, po_number TEXT,
  total REAL, currency TEXT, reason_codes TEXT, summary TEXT, duration_ms INTEGER, result_json TEXT,
  parent_run_id TEXT, reviewer_note TEXT, file_hash TEXT, file_path TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, type TEXT, stage TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS ev_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS run_ai(run_id TEXT, kind TEXT, sig TEXT, content TEXT, created_at REAL, PRIMARY KEY(run_id, kind));
CREATE TABLE IF NOT EXISTS run_docs(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, filename TEXT, text TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, vendor_id TEXT, invoice_number TEXT, inv_norm TEXT,
  amount REAL, invoice_date TEXT, descr TEXT, status TEXT, run_id TEXT, source TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS led_po ON ledger(po_number);
CREATE INDEX IF NOT EXISTS led_v ON ledger(vendor_id);
CREATE TABLE IF NOT EXISTS custom_vendors(
  id TEXT PRIMARY KEY, name TEXT, tax_id TEXT, bank_last4 TEXT, email TEXT, status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS custom_pos(
  number TEXT PRIMARY KEY, vendor_id TEXT, description TEXT, currency TEXT, total REAL,
  issue_date TEXT, expiry_date TEXT, status TEXT, created_at REAL);
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with tx() as con:
        con.executescript(SCHEMA)


def reset_all():
    """Clear runs, events and the ledger. Vendors and POs a person added are kept: they are master data, not history."""
    keep = {}
    if config.DB_PATH.exists():
        try:
            for t in ("custom_vendors", "custom_pos"):
                keep[t] = rows(f"SELECT * FROM {t}")
        except sqlite3.Error:
            keep = {}
        config.DB_PATH.unlink()
    for ext in ("-wal", "-shm"):
        p = config.DB_PATH.with_name(config.DB_PATH.name + ext)
        if p.exists():
            p.unlink()
    init()
    for t, rs in keep.items():
        for r in rs:
            execute(f"INSERT INTO {t}({', '.join(r)}) VALUES({', '.join('?' * len(r))})", tuple(r.values()))


def rows(sql: str, args: tuple = ()) -> list[dict]:
    with tx() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> Optional[dict]:
    r = rows(sql, args)
    return r[0] if r else None


def execute(sql: str, args: tuple = ()) -> int:
    with tx() as con:
        return con.execute(sql, args).lastrowid


# ------------------------------------------------------------------ runs
def create_run(run_id, filename, source, sample_id=None, parent=None, file_hash=None, file_path=None):
    execute("INSERT INTO runs(id, created_at, filename, source, sample_id, status, parent_run_id, file_hash, file_path)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, time.time(), filename, source, sample_id, "running", parent, file_hash, file_path))


def update_run(run_id: str, **cols):
    if not cols:
        return
    sets = ", ".join(f"{k}=?" for k in cols)
    execute(f"UPDATE runs SET {sets} WHERE id=?", (*cols.values(), run_id))


def get_run(run_id: str) -> Optional[dict]:
    r = one("SELECT * FROM runs WHERE id=?", (run_id,))
    if r:
        r["reason_codes"] = json.loads(r["reason_codes"]) if r["reason_codes"] else []
        r["result"] = json.loads(r["result_json"]) if r["result_json"] else None
        r.pop("result_json", None)
        r["effective_decision"] = r["final_decision"] or r["decision"]
    return r


def list_runs(limit=100) -> list[dict]:
    out = rows("SELECT id, created_at, finished_at, filename, source, sample_id, status, decision, final_decision,"
               " vendor_name, invoice_number, po_number, total, reason_codes, summary, duration_ms, reviewer_note"
               " FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
    for r in out:
        r["reason_codes"] = json.loads(r["reason_codes"]) if r["reason_codes"] else []
        r["effective_decision"] = r["final_decision"] or r["decision"]
    return out


# ------------------------------------------------------------------ events
def add_event(run_id: str, type_: str, stage: Optional[str], payload: Any) -> int:
    return execute("INSERT INTO events(run_id, ts, type, stage, payload) VALUES(?,?,?,?,?)",
                   (run_id, time.time(), type_, stage, json.dumps(payload, default=str)))


def events_since(run_id: str, after_id: int = 0) -> list[dict]:
    out = rows("SELECT id, ts, type, stage, payload FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after_id))
    for e in out:
        e["payload"] = json.loads(e["payload"])
    return out


# ------------------------------------------------------------------ ledger
def add_ledger(po_number, vendor_id, invoice_number, inv_norm, amount, invoice_date, descr, status, run_id, source="run"):
    execute("INSERT INTO ledger(po_number, vendor_id, invoice_number, inv_norm, amount, invoice_date, descr, status,"
            " run_id, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (po_number, vendor_id, invoice_number, inv_norm, amount, invoice_date, descr, status, run_id, source, time.time()))


def ledger_for_po(po_number: str, statuses=("committed",)) -> list[dict]:
    q = ",".join("?" * len(statuses))
    return rows(f"SELECT * FROM ledger WHERE po_number=? AND status IN ({q}) ORDER BY invoice_date", (po_number, *statuses))


def ledger_for_vendor(vendor_id: str, statuses=("committed", "pending")) -> list[dict]:
    q = ",".join("?" * len(statuses))
    return rows(f"SELECT * FROM ledger WHERE vendor_id=? AND status IN ({q}) ORDER BY invoice_date", (vendor_id, *statuses))


def set_ledger_status(run_id: str, status: str):
    execute("UPDATE ledger SET status=? WHERE run_id=?", (status, run_id))


# ------------------------------------------------------------------ master data added in the app
def add_custom_vendor(vid, name, tax_id, bank_last4, email, status="approved"):
    execute("INSERT INTO custom_vendors(id, name, tax_id, bank_last4, email, status, created_at) VALUES(?,?,?,?,?,?,?)",
            (vid, name, tax_id, bank_last4, email, status, time.time()))


def add_custom_po(number, vendor_id, description, currency, total, issue_date, expiry_date, status="open"):
    execute("INSERT INTO custom_pos(number, vendor_id, description, currency, total, issue_date, expiry_date, status, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)", (number, vendor_id, description, currency, total, issue_date, expiry_date, status, time.time()))


def delete_custom_po(number: str) -> bool:
    """Remove a PO added in the app, and any ledger entries booked against it."""
    if not rows("SELECT 1 FROM custom_pos WHERE number=?", (number,)):
        return False
    execute("DELETE FROM ledger WHERE po_number=?", (number,))
    execute("DELETE FROM custom_pos WHERE number=?", (number,))
    return True


def custom_vendors() -> list[dict]:
    return rows("SELECT * FROM custom_vendors ORDER BY created_at")


def custom_pos() -> list[dict]:
    return rows("SELECT * FROM custom_pos ORDER BY created_at")
