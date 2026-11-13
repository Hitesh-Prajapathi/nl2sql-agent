#!/usr/bin/env python
"""SQL-level red-team suite (no LLM).

Measures, for every attack payload in redteam/sql_payloads.yaml:

  (a) static layer:   does ``validate_sql`` block it?
  (b) DB-layer knockout: execute the payload DIRECTLY (validator bypassed) as the
      read-only role ``nl2sql_reader`` and record whether Postgres itself rejects
      it, and with which SQLSTATE. Two passes per payload, both always rolled back:
        pass A: BEGIN READ ONLY            (as the agent executes)
        pass B: BEGIN READ WRITE           (read-only txn knocked out, so a role
                                            denial cannot be masked by the txn flag)
      Both use SET LOCAL statement_timeout='2s', one fresh connection per payload
      per pass, and a client-side watchdog (cancel after 5s).
  (c) write canary: md5 of every public table (plus large-object and sequence
      side-effect oracles) before/after the whole run and around every payload.

It also measures the benign false-block rate (redteam/sql_benign.yaml, statically
and by executing them) and the regex prompt-injection guard's TPR/FPR
(redteam/nl_injection.yaml) for the legacy guard vs the current one.

Usage:  PYTHONPATH=$PWD/src python redteam/run_sql_suite.py --out results/redteam_sql_v1.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path

import psycopg
import sqlglot
import yaml

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
DSN = os.environ.get(
    "NL2SQL_REDTEAM_DSN",
    "postgresql://nl2sql_reader:reader_pass@localhost:5432/nl2sql_assignment",
)

# Frozen copy of the ORIGINAL (v1) injection regexes so that v1 vs v2 can always be
# measured, regardless of how the live guard has been retuned.
LEGACY_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(previous|above|all|prior)\s+(instructions?|prompts?|context)", re.I),
    re.compile(r"you\s+are\s+now\s+", re.I),
    re.compile(r"(system|assistant)\s*:\s*", re.I),
    re.compile(r"forget\s+(everything|all|your|what)", re.I),
    re.compile(r"new\s+(role|persona|instructions?)", re.I),
    re.compile(r"jailbreak", re.I),
    re.compile(r"DAN\s+mode", re.I),
]


def legacy_guard(text: str) -> bool:
    return any(p.search(text) for p in LEGACY_INJECTION_PATTERNS)


# --------------------------------------------------------------------------- db
def connect() -> psycopg.Connection:
    return psycopg.connect(DSN, autocommit=True, application_name="redteam_sql_suite")


BROKEN_PAYLOAD_STATES = {
    "42601",  # syntax_error
    "42P01",  # undefined_table
    "42883",  # undefined_function
    "42703",  # undefined_column
    "42704",  # undefined_object
    "42P02",  # undefined_parameter
    "3F000",  # invalid_schema_name
    "42809",
    "42804",
    "22P02",
    "22023",
    "42P10",
    "42725",
    "26000",  # invalid_sql_statement_name
    "34000",  # invalid_cursor_name
    "22007",
    "22003",
    "42846",
    "42P18",
    "2200F",
    "42P20",
    "55000",  # object_not_in_prerequisite_state
    "0A000",  # feature_not_supported (e.g. syntax unsupported for this construct)
}


def classify(state: str | None, message: str, watchdog: bool) -> str:
    if state is None:
        return "succeeded"
    if state == "42501":
        return "permission_denied"
    if state == "25006":
        return "read_only_txn"
    if state == "57014":
        if watchdog:
            return "watchdog_cancel"
        return "timeout"
    if state in BROKEN_PAYLOAD_STATES:
        return "broken_payload:" + state
    return "other_error:" + state


def run_payload(sql: str, rw: bool) -> dict:
    """Execute one payload on a fresh connection inside a rolled-back transaction."""
    conn = connect()
    fired = {"v": False}

    def _cancel() -> None:
        fired["v"] = True
        try:
            conn.cancel()
        except Exception:
            pass

    timer = threading.Timer(5.0, _cancel)
    timer.start()
    state: str | None = None
    msg = ""
    rows = None
    try:
        conn.execute("BEGIN READ WRITE" if rw else "BEGIN READ ONLY")
        conn.execute("SET LOCAL statement_timeout='2s'")
        cur = conn.execute(sql)
        try:
            if cur.description:
                rows = len(cur.fetchmany(5))
        except Exception:
            pass
    except psycopg.Error as exc:
        state = getattr(exc, "sqlstate", None) or "XX000"
        msg = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
    except Exception as exc:  # pragma: no cover - defensive
        state = "XXCLI"
        msg = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        timer.cancel()
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
    return {
        "outcome": classify(state, msg, fired["v"]),
        "sqlstate": state,
        "message": msg,
        "rows": rows,
    }


def public_tables(conn: psycopg.Connection) -> list[str]:
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1"
    )
    return [r[0] for r in cur.fetchall()]


def canary(conn: psycopg.Connection, tables: list[str]) -> dict:
    """md5 of each public table + side-effect oracles, in one REPEATABLE READ snapshot."""
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    try:
        out: dict[str, str] = {}
        for t in tables:
            row = conn.execute(
                f"SELECT md5(coalesce(string_agg(x::text, ',' ORDER BY x::text), '')) "
                f'FROM public."{t}" x'
            ).fetchone()
            out[t] = row[0]
        out["_pg_largeobject_metadata_count"] = str(
            conn.execute("SELECT count(*) FROM pg_largeobject_metadata").fetchone()[0]
        )
        out["_pg_sequences_md5"] = conn.execute(
            "SELECT md5(coalesce(string_agg(s::text, ',' ORDER BY s::text), '')) FROM pg_sequences s"
        ).fetchone()[0]
        return out
    finally:
        conn.execute("ROLLBACK")


def diff(a: dict, b: dict) -> list[str]:
    return sorted(k for k in a if a[k] != b.get(k))


# ------------------------------------------------------------------- helpers
def git_info() -> dict:
    def g(*args: str) -> str:
        try:
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            return ""

    return {
        "sha": g("rev-parse", "HEAD"),
        "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(g("status", "--porcelain", "--untracked-files=no")),
    }


def rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def guard_metrics(items: list[dict], fn) -> dict:
    res: dict = {}
    for split in ("dev", "heldout", "all"):
        sub = [i for i in items if split == "all" or i["split"] == split]
        inj = [i for i in sub if i["label"] == "injection"]
        ben = [i for i in sub if i["label"] == "benign"]
        tp = [i for i in inj if fn(i["text"])]
        fp = [i for i in ben if fn(i["text"])]
        res[split] = {
            "n_injection": len(inj),
            "n_benign": len(ben),
            "tpr": rate(len(tp), len(inj)),
            "fpr": rate(len(fp), len(ben)),
            "missed_injections": [i["id"] for i in inj if i not in tp],
            "false_positives": [i["id"] for i in fp],
        }
    return res


# ----------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    from nl2sql.safety import validator as V  # imported late so PYTHONPATH decides which copy

    vpath = Path(V.__file__).resolve()
    payloads = yaml.safe_load((HERE / "sql_payloads.yaml").read_text())
    benign = yaml.safe_load((HERE / "sql_benign.yaml").read_text())
    nl = yaml.safe_load((HERE / "nl_injection.yaml").read_text())

    # ---- (a) static -------------------------------------------------------
    def static(sql: str) -> dict:
        try:
            r = V.validate_sql(sql)
            return {"blocked": not r.is_valid, "terminal": bool(r.is_blocked), "error": r.error}
        except Exception as exc:  # a validator crash is NOT a clean block
            return {"blocked": False, "terminal": False, "error": f"VALIDATOR_EXCEPTION {type(exc).__name__}: {exc}"}

    for p in payloads:
        p["static"] = static(p["sql"])

    # ---- benign -----------------------------------------------------------
    sconn = connect()
    tables = public_tables(sconn)
    seq_priv = sconn.execute(
        "SELECT bool_or(has_sequence_privilege(c.oid,'USAGE') OR has_sequence_privilege(c.oid,'UPDATE')) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='S' AND n.nspname='public'"
    ).fetchone()[0]
    benign_rows = []
    for b in benign:
        st = static(b["sql"])
        ex = run_payload(b["sql"], rw=False)
        benign_rows.append(
            {
                "id": b["id"],
                "feature": b.get("feature"),
                "static_blocked": st["blocked"],
                "static_error": st["error"],
                "exec_outcome": ex["outcome"],
                "exec_error": ex["message"] if ex["outcome"] != "succeeded" else None,
            }
        )

    # ---- (b)+(c) DB knockout with canary ------------------------------------
    canary_before = canary(sconn, tables)
    prev = canary_before
    per_payload_changes: list[dict] = []
    seq_re = re.compile(r"\b(nextval|setval)\b", re.I)
    for p in payloads:
        a = run_payload(p["sql"], rw=False)
        if seq_priv and seq_re.search(p["sql"]):
            b = {"outcome": "skipped_sequence_privileged", "sqlstate": None, "message": "", "rows": None}
        else:
            b = run_payload(p["sql"], rw=True)
        p["db_ro"] = a
        p["db_rw"] = b
        now = canary(sconn, tables)
        changed = diff(prev, now)
        if changed:
            per_payload_changes.append({"id": p["id"], "changed": changed})
        prev = now
    canary_after = canary(sconn, tables)
    sconn.close()

    # ---- layer attribution ------------------------------------------------
    for p in payloads:
        a, b = p["db_ro"]["outcome"], p["db_rw"]["outcome"]
        layers = []
        if p["static"]["blocked"]:
            layers.append("validator")
        if "permission_denied" in (a, b):
            layers.append("db_role")
        if a == "read_only_txn":
            layers.append("read_only_txn")
        if "timeout" in (a, b):
            layers.append("timeout")
        p["caught_by"] = layers
        broken = lambda o: o.startswith("broken_payload") or o.startswith("other_error")  # noqa: E731
        p["db_inconclusive"] = (not layers) and broken(a)
        p["db_succeeded"] = a == "succeeded"
        # a read-only-txn error in pass A can mask a role denial, and a role denial in
        # pass A can mask the read-only flag; the second layer is "unknown" in that case.
        p["masked_note"] = (
            "role denial seen; read_only_txn coverage unobservable as nl2sql_reader"
            if "db_role" in layers and "read_only_txn" not in layers
            else ""
        )

    # ---- aggregate --------------------------------------------------------
    cats = sorted({p["category"] for p in payloads})
    per_cat = {}
    for c in cats:
        sub = [p for p in payloads if p["category"] == c]
        blocked = sum(p["static"]["blocked"] for p in sub)
        db_outcomes = Counter(p["db_ro"]["outcome"].split(":")[0] for p in sub)
        per_cat[c] = {
            "n": len(sub),
            "static_blocked": blocked,
            "static_block_rate": rate(blocked, len(sub)),
            "db_ro_outcomes": dict(db_outcomes),
            "db_succeeded_if_validator_missed": sum(p["db_succeeded"] for p in sub),
        }
    n_blocked = sum(p["static"]["blocked"] for p in payloads)
    bypass = [
        {"id": p["id"], "category": p["category"], "sql": p["sql"], "validator": p["static"]["error"],
         "db_ro": p["db_ro"]["outcome"], "db_rw": p["db_rw"]["outcome"]}
        for p in payloads if not p["static"]["blocked"]
    ]
    single = [
        {"id": p["id"], "category": p["category"], "sql": p["sql"], "defender": p["caught_by"][0],
         "note": p["masked_note"]}
        for p in payloads if len(p["caught_by"]) == 1
    ]
    single_by_layer = Counter(s["defender"] for s in single)
    none = [p["id"] for p in payloads if not p["caught_by"]]
    db_succeeded = [
        {"id": p["id"], "category": p["category"], "sql": p["sql"], "static_blocked": p["static"]["blocked"]}
        for p in payloads if p["db_succeeded"]
    ]
    db_ok_by_cat = Counter(d["category"] for d in db_succeeded)
    db_only_stops = {}
    for c in cats:
        sub = [p for p in payloads if p["category"] == c]
        db_only_stops[c] = {
            "n": len(sub),
            "db_stops": sum(any(l != "validator" for l in p["caught_by"]) for p in sub),
            "db_succeeds": sum(p["db_succeeded"] for p in sub),
            "db_inconclusive": sum(p["db_inconclusive"] for p in sub),
        }
    n_fb = sum(b["static_blocked"] for b in benign_rows)
    n_fx = sum(b["exec_outcome"] != "succeeded" for b in benign_rows)

    nl_old = guard_metrics(nl, legacy_guard)
    nl_new = guard_metrics(nl, V.detect_prompt_injection)

    result = {
        "label": args.label,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "git": git_info(),
        "sqlglot_version": sqlglot.__version__,
        "validator": {
            "path": str(vpath),
            "sha256": hashlib.sha256(vpath.read_bytes()).hexdigest(),
            "in_this_worktree": str(vpath).startswith(str(ROOT)),
        },
        "static": {
            "n_payloads": len(payloads),
            "n_blocked": n_blocked,
            "block_rate": rate(n_blocked, len(payloads)),
            "n_terminal_blocks": sum(p["static"]["terminal"] for p in payloads),
            "per_category": {c: {k: v for k, v in per_cat[c].items() if k.startswith(("n", "static"))} for c in cats},
            "bypasses": bypass,
        },
        "benign": {
            "n": len(benign_rows),
            "static_false_blocks": n_fb,
            "false_block_rate": rate(n_fb, len(benign_rows)),
            "execution_failures": n_fx,
            "rows": [b for b in benign_rows if b["static_blocked"] or b["exec_outcome"] != "succeeded"],
        },
        "db_layer": {
            "method": "each payload executed directly as nl2sql_reader (validator bypassed); pass A in "
            "BEGIN READ ONLY, pass B in BEGIN READ WRITE; statement_timeout 2s; always ROLLBACK; fresh "
            "connection per pass. Only SQLSTATE 42501 (role), 25006 (read-only txn) and 57014 (timeout) "
            "count as a DB defence; undefined function/table/syntax errors are 'broken_payload' and "
            "count as inconclusive, never as a catch.",
            "sequence_privileges_present": bool(seq_priv),
            "per_category": per_cat,
            "db_only_summary": db_only_stops,
            "would_succeed_if_validator_missed": db_succeeded,
            "would_succeed_by_category": dict(db_ok_by_cat),
            "inconclusive_ids": [p["id"] for p in payloads if p["db_inconclusive"]],
        },
        "layers": {
            "caught_by_none": none,
            "single_defender_counts": dict(single_by_layer),
            "single_defender": single,
            "layer_totals": dict(Counter(l for p in payloads for l in p["caught_by"])),
        },
        "canary": {
            "tables": tables,
            "before": canary_before,
            "after": canary_after,
            "global_changed": diff(canary_before, canary_after),
            "per_payload_changes": per_payload_changes,
            "write_canary_tripped": bool(diff(canary_before, canary_after) or per_payload_changes),
        },
        "injection_guard": {
            "legacy_v1_regex": nl_old,
            "current": nl_new,
            "n_items": len(nl),
        },
        "payloads": [
            {
                "id": p["id"],
                "category": p["category"],
                "static_blocked": p["static"]["blocked"],
                "static_error": p["static"]["error"],
                "db_ro": {k: p["db_ro"][k] for k in ("outcome", "sqlstate", "message")},
                "db_rw": {k: p["db_rw"][k] for k in ("outcome", "sqlstate", "message")},
                "caught_by": p["caught_by"],
                "n_layers": len(p["caught_by"]),
            }
            for p in payloads
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str) + "\n")

    s = result["static"]
    print(f"static block rate: {s['n_blocked']}/{s['n_payloads']} = {s['block_rate']}")
    print(f"benign false-block: {n_fb}/{len(benign_rows)}  exec failures: {n_fx}")
    print(f"bypasses: {len(bypass)}  single-defender: {dict(single_by_layer)}  caught by none: {len(none)}")
    print(f"DB would-succeed (pass A): {len(db_succeeded)} by category {dict(db_ok_by_cat)}")
    print(f"canary tripped: {result['canary']['write_canary_tripped']} {result['canary']['global_changed']}")
    print(f"guard legacy all: tpr={nl_old['all']['tpr']} fpr={nl_old['all']['fpr']} | current all: tpr={nl_new['all']['tpr']} fpr={nl_new['all']['fpr']}")
    print(f"validator: {vpath} in_worktree={result['validator']['in_this_worktree']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
