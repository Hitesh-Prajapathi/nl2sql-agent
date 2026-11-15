from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from nl2sql.models import SQLValidationResult
from nl2sql.safety.allowlist import (
    ALLOWED_FUNCTIONS,
    ALLOWED_TYPED_NODES,
    BLOCKED_TYPES,
    DISCLOSURE_NODES,
    KEYWORD_FUNCTIONS,
)

# -- Retained for reference and for informative error messages. Safety no longer depends
# -- on this list: any function that is not in the allowlist is blocked.
DANGEROUS_FUNCTIONS: frozenset[str] = frozenset(
    {
        "pg_sleep",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "lo_get",
        "lo_put",
        "lo_create",
        "lo_unlink",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "set_config",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "copy_file_range",
    }
)

# -- System schemas that must never be accessed ---------------------------------------
BLOCKED_SCHEMAS: frozenset[str] = frozenset(
    {"pg_catalog", "information_schema", "pg_toast", "pg_temp"}
)

# Only this schema may be referenced explicitly (``public.orders``).
ALLOWED_SCHEMAS: frozenset[str] = frozenset({"public"})

# -- Statement types that are always terminal refusals --------------------------------
BLOCKED_STATEMENT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.AlterColumn,
    exp.Grant,
    exp.Revoke,
    exp.Copy,
    exp.TruncateTable,
    exp.Command,  # DO, CALL, SET ROLE, RESET, LOCK, PREPARE, EXECUTE, EXPLAIN, ...
    exp.Set,
    exp.Transaction,  # BEGIN, COMMIT, ROLLBACK
    exp.Commit,
    exp.Rollback,
)

_ROOT_SELECT_TYPES = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)

# generate_series: total produced rows (product over all calls) must stay below this.
_MAX_SERIES_ROWS = 10_000_000


def _blocked(error: str) -> SQLValidationResult:
    return SQLValidationResult(is_valid=False, is_blocked=True, error=error)


# ---------------------------------------------------------------------------------------
# PostgreSQL-faithful lexical scan.
#
# sqlglot's tokenizer disagrees with PostgreSQL in places that matter for safety, e.g. it
# treats ``\'`` as an escaped quote inside an ordinary string, so
#     SELECT 'a\' || pg_sleep(5) || '...'
# parses as one string literal (the call is invisible to every AST check) while PostgreSQL
# sees a function call. This scanner follows PostgreSQL's rules, rejects the constructs
# whose meaning differs between the two parsers, and counts statements independently.
# ---------------------------------------------------------------------------------------
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_\u0080-￿][A-Za-z0-9_\u0080-￿]*)?\$")


@dataclass
class _Scan:
    statements: int = 0
    dollar_quote: bool = False
    escape_string: bool = False
    unicode_escape: bool = False
    backslash_in_literal: bool = False


def _skip_quoted(sql: str, start: int, quote: str, backslash_escapes: bool) -> tuple[int, bool]:
    """Return (index after the closing quote, saw_backslash) for a quoted token."""
    i = start + 1
    n = len(sql)
    saw_backslash = False
    while i < n:
        c = sql[i]
        if c == "\\":
            if backslash_escapes:
                i += 2
                continue
            saw_backslash = True
        elif c == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1, saw_backslash
        i += 1
    return n, saw_backslash


def _scan_sql(sql: str) -> _Scan:
    scan = _Scan()
    n = len(sql)
    i = 0
    has_token = False
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif c == ";":
            if has_token:
                scan.statements += 1
                has_token = False
            i += 1
        else:
            has_token = True
            if c == "'" or c == '"':
                i, bs = _skip_quoted(sql, i, c, backslash_escapes=False)
                scan.backslash_in_literal |= bs
            elif c == "$":
                m = _DOLLAR_TAG.match(sql, i)
                if m:
                    scan.dollar_quote = True
                    end = sql.find(m.group(0), m.end())
                    i = n if end < 0 else end + len(m.group(0))
                else:
                    i += 1  # positional parameter such as $1
            elif c.isalpha() or c == "_" or ord(c) > 127:
                j = i
                while j < n and (sql[j].isalnum() or sql[j] in "_$" or ord(sql[j]) > 127):
                    j += 1
                word = sql[i:j].lower()
                i = j
                if word == "e" and i < n and sql[i] == "'":
                    scan.escape_string = True
                    i, _ = _skip_quoted(sql, i, "'", backslash_escapes=True)
                elif word == "u" and sql.startswith("&", i) and i + 1 < n and sql[i + 1] in "'\"":
                    scan.unicode_escape = True
                    i, _ = _skip_quoted(sql, i + 1, sql[i + 1], backslash_escapes=False)
            else:
                i += 1
    if has_token:
        scan.statements += 1
    return scan


# ---------------------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------------------
def _number(node: exp.Expression | None) -> float | None:
    """Numeric value of a literal (optionally negated), else None."""
    if isinstance(node, exp.Neg):
        inner = _number(node.this)
        return None if inner is None else -inner
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return float(node.this)
        except ValueError:
            return None
    return None


def _check_series(nodes: list[exp.Func]) -> str | None:
    """Bound generate_series: numeric-literal spans must multiply to a small row count."""
    total = 1.0
    for node in nodes:
        start, stop = _number(node.args.get("start")), _number(node.args.get("end"))
        if start is None or stop is None:
            continue  # date series / data-driven bounds: statement_timeout is the backstop
        total *= max(abs(stop - start) + 1, 1)
        if total > _MAX_SERIES_ROWS:
            return (
                "generate_series() would produce more than "
                f"{_MAX_SERIES_ROWS:,} rows. Use a smaller literal range."
            )
    return None


def _function_error(node: exp.Func) -> str | None:
    """Return an error message when this function call is not allowlisted."""
    if isinstance(node, exp.Anonymous):
        name = (node.name or "").lower()
        if name in ALLOWED_FUNCTIONS:
            return None
    else:
        cls = type(node).__name__
        name = node.sql_name().lower()
        if cls in DISCLOSURE_NODES:
            return (
                f"Function {DISCLOSURE_NODES[cls]} is blocked: it discloses server or session "
                "details and is not on the allowed-function list."
            )
        if cls in ALLOWED_TYPED_NODES or name in ALLOWED_FUNCTIONS:
            return None
    if not name:
        return (
            "Function call with an unresolvable name is blocked (not on the allowed-function list)."
        )
    if name in DANGEROUS_FUNCTIONS:
        return f"Dangerous function blocked: {name}()"
    return (
        f"Function {name}() is not on the allowed-function list. Only aggregate, math, string, "
        "date/time, conditional, cast, JSON and window functions are permitted."
    )


def _check_table(table: exp.Table, allowed: set[str] | None, cte_names: set[str]) -> str | None:
    """Terminal table/schema problem (system schema, cross-database, non-public schema)."""
    db = (table.db or "").lower()
    catalog = (table.catalog or "").lower()
    name = (table.name or "").lower()
    shown = ".".join(p for p in (catalog, db, name) if p)

    if isinstance(table.this, exp.Func) and (db or catalog):
        return f"Schema-qualified function calls are blocked: {shown or 'function'}()"
    if (
        db in BLOCKED_SCHEMAS
        or catalog in BLOCKED_SCHEMAS
        or name.startswith(("pg_", "information_schema"))
    ):
        return f"Access to system schema/table blocked: {shown}"
    if catalog:
        return f"Cross-database references are blocked: {shown}"
    if db and db not in ALLOWED_SCHEMAS:
        return f"Access to schema '{db}' is blocked; only the public schema may be queried."
    return None


def _unknown_table(table: exp.Table, allowed: set[str] | None, cte_names: set[str]) -> str | None:
    """Correctable problem: a (public-schema or unqualified) table that is not in the schema.

    Only meaningful for tables that already passed ``_check_table``. A hallucinated table name is
    an ordinary model mistake, not an attack, so the caller returns a retryable error for it.
    """
    if allowed is None or isinstance(table.this, exp.Func):
        return None
    db = (table.db or "").lower()
    name = (table.name or "").lower()
    if not name or (not db and name in cte_names) or name in allowed:
        return None
    return (
        f"Table '{name}' does not exist in this database. Use only the tables listed in the schema."
    )


def validate_sql(sql: str, allowed_tables: set[str] | None = None) -> SQLValidationResult:
    """
    Full AST-based SQL validation using sqlglot (Postgres dialect).

    Rules enforced:
      0. PostgreSQL-faithful lexical pre-scan: no dollar-quoted strings, ``E''`` / ``U&''``
         literals or backslashes inside ordinary literals (parser-differential tricks), and an
         independent statement count
      1. Exactly one statement
      2. Root node is SELECT (or WITH whose body resolves to SELECT)
      3. No DML/DDL anywhere in the AST, including inside CTEs
      4. No SELECT ... INTO or FOR UPDATE/SHARE
      5. Function ALLOWLIST: every function call must be on the allowed list; schema-qualified
         calls, session-keyword functions (``user``) and casts to ``reg*``/``oid`` types are blocked
      6. No access to system schemas (pg_catalog, information_schema, etc.) or non-public schemas
      7. Optional ``allowed_tables``: only those tables (plus the query's own CTEs) may be read;
         an unknown table is a retryable error (is_blocked=False), not a terminal block

    Returns SQLValidationResult:
      - is_valid=True  -> proceed to execution
      - is_blocked=True -> terminal refusal (do NOT retry)
      - is_valid=False, is_blocked=False -> retryable syntax error
    """
    sql = sql.strip()
    if not sql:
        return SQLValidationResult(is_valid=False, error="Empty SQL string.")

    # -- Rule 0: lexical pre-scan ------------------------------------------------------
    scan = _scan_sql(sql)
    if scan.dollar_quote:
        return _blocked("Dollar-quoted strings ($$...$$) are not allowed.")
    if scan.escape_string:
        return _blocked("Escape string literals (E'...') are not allowed; use plain '...' strings.")
    if scan.unicode_escape:
        return _blocked("Unicode-escape literals/identifiers (U&'...') are not allowed.")
    if scan.backslash_in_literal:
        return _blocked(
            "Backslashes inside string literals or quoted identifiers are not allowed "
            "(the SQL parser and PostgreSQL disagree about how they are escaped)."
        )
    multi = (
        "Multiple statements are not allowed. Expected exactly 1 statement, got "
        "{n}. Stacked statements are not allowed."
    )
    if scan.statements > 1:
        return _blocked(multi.format(n=scan.statements))

    # -- Parse ---------------------------------------------------------------------------
    try:
        parsed = sqlglot.parse(sql, dialect="postgres", error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.SqlglotError as exc:
        return SQLValidationResult(is_valid=False, error=f"SQL syntax error: {exc}")
    except RecursionError:
        return SQLValidationResult(
            is_valid=False, error="SQL syntax error: query is nested too deeply."
        )

    # -- Rule 1: exactly one statement -----------------------------------------------------
    statements = [s for s in parsed if s is not None and not isinstance(s, exp.Semicolon)]
    if len(statements) != 1:
        if not statements:
            return SQLValidationResult(is_valid=False, error="Empty SQL string.")
        return _blocked(multi.format(n=len(statements)))
    stmt = statements[0]

    # -- Rule 2: root must be SELECT (WITH bodies are part of the Select node) -------------
    if not isinstance(stmt, _ROOT_SELECT_TYPES):
        if isinstance(stmt, exp.Command):
            keyword = str(stmt.this).upper()
            return _blocked(
                f"Only SELECT statements are allowed. Got: {keyword} statement "
                f"({type(stmt).__name__})."
            )
        return _blocked(f"Only SELECT statements are allowed. Got: {type(stmt).__name__}")

    # -- Rule 3: no DML/DDL anywhere in the tree (including inside CTEs) -------------------
    for node in stmt.walk():
        if isinstance(node, BLOCKED_STATEMENT_TYPES):
            return _blocked(f"Blocked operation found in query tree: {type(node).__name__}")

    # -- Rule 4: no SELECT ... INTO or FOR UPDATE/SHARE ------------------------------------
    if stmt.find(exp.Into):
        return _blocked("SELECT ... INTO is not allowed.")
    if stmt.find(exp.Lock):
        return _blocked("FOR UPDATE / FOR SHARE is not allowed.")

    # -- Rule 5: function allowlist, keyword functions, catalog-resolving types ------------
    series: list[exp.Func] = []
    for node in stmt.walk():
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            return _blocked(
                f"Schema-qualified function calls are blocked: {node.sql(dialect='postgres')}"
            )
        if isinstance(node, exp.Func):
            error = _function_error(node)
            if error:
                return _blocked(error)
            if type(node).__name__ in ("ExplodingGenerateSeries", "GenerateSeries"):
                series.append(node)
        elif isinstance(node, exp.Column) and not node.table:
            ident = node.this
            if (
                isinstance(ident, exp.Identifier)
                and not ident.quoted
                and ident.name.lower() in KEYWORD_FUNCTIONS
            ):
                return _blocked(
                    f"{ident.name.lower()} is blocked: it discloses session details "
                    "and is not on the allowed-function list."
                )
        elif isinstance(node, exp.ObjectIdentifier):
            return _blocked(
                "Casts to reg*/oid types are blocked: "
                "they resolve names against the system catalogs."
            )
        elif (
            isinstance(node, exp.DataType)
            and str(node.args.get("kind") or "").lower() in BLOCKED_TYPES
        ):
            return _blocked(
                f"Cast to type {node.args.get('kind')} is blocked (system-catalog type)."
            )
    series_error = _check_series(series)
    if series_error:
        return _blocked(series_error)

    # -- Rules 6/7: schemas and tables ----------------------------------------------------
    allowed = {t.lower() for t in allowed_tables} if allowed_tables is not None else None
    cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
    tables = list(stmt.find_all(exp.Table))
    for table in tables:
        error = _check_table(table, allowed, cte_names)
        if error:
            return _blocked(error)
    # Only after every terminal check passed: an unknown table is retryable (correct_sql), so a
    # query that also touches a system schema is never downgraded to a correctable error.
    for table in tables:
        unknown = _unknown_table(table, allowed, cte_names)
        if unknown:
            return SQLValidationResult(is_valid=False, error=unknown)

    return SQLValidationResult(is_valid=True)


# -- Prompt-injection detection (input guard) -------------------------------------------
# v2 patterns. The v1 guard flagged any "system:", "assistant:", "you are now", "new role",
# "forget everything" ... which false-refused ordinary business questions such as
# "Show tickets where category is system: outage". These patterns require an *imperative
# structure* (an override verb followed by an instruction-like object, a role marker at the
# start of a line or sentence, a persona switch, a request to reveal the prompt) instead of
# a bare trigger word.
_FILLER = (
    r"(?:(?:all|any|the|your|my|these|those|previous|prior|above|earlier|preceding"
    r"|former|original|system)\s+)+"
)
_INSTRUCTION_NOUN = (
    r"(?:instructions?|prompts?|rules|guidelines|directions|context|polic(?:y|ies)"
    r"|constraints|restrictions|messages?)"
)
_REVEAL_VERBS = r"(?:reveal|print|repeat|output|leak|disclose|display|show|tell)"
_PROMPT_NAMES = r"(?:prompt|message|instructions?)"

_INJECTION_SOURCES: list[tuple[str, int]] = [
    # "ignore previous instructions", "disregard your system prompt", "override all rules"
    (rf"\b(?:ignore|disregard|forget|override|skip)\s+{_FILLER}{_INSTRUCTION_NOUN}", re.I),
    (
        r"\b(?:ignore|disregard)\s+(?:the\s+)?(?:above|previous|prior|preceding)\s*"
        r"(?:$|[.,;:!]|and\b|then\b|instead\b)",
        re.I,
    ),
    (
        r"\b(?:ignore|disregard)\s+(?:everything|anything|all)\s+"
        r"(?:above|before|previous|prior|you)\b",
        re.I,
    ),
    (
        r"\bforget\s+(?:everything|all|what)\s+"
        r"(?:you|i|that|above|before|previously|prior|earlier)\b",
        re.I,
    ),
    (
        r"\bforget\s+(?:all\s+)?(?:your|the|my)\s+(?:\w+\s+){0,2}"
        r"(?:instructions?|rules|training|prompt|guidelines)\b",
        re.I,
    ),
    # persona switches
    (
        r"\byou\s+are\s+now\s+(?:an?|my|no\s+longer"
        r"|in\s+(?:developer|maintenance|admin|god|unrestricted)"
        r"|dan\b|free\b|unrestricted|unfiltered|jailbroken)",
        re.I,
    ),
    (
        r"\b(?:from\s+now\s+on|henceforth|starting\s+now)\W+"
        r"(?:you\s+(?:are|will|must|shall)|act|behave|respond)\b",
        re.I,
    ),
    (r"\bpretend\s+(?:that\s+)?(?:you|to\s+be)\b", re.I),
    (
        r"\bact\s+as\s+(?:if\s+you"
        r"|an?\s+(?:unrestricted|unfiltered|uncensored|jailbroken|evil|hacker)"
        r"|unrestricted|unfiltered)",
        re.I,
    ),
    (r"\b(?:your|my)\s+new\s+(?:instructions?|role|persona|task|rules)\b", re.I),
    (r"\bnew\s+(?:instructions?|persona|system\s+prompt|policy|rules?|role)\s*:", re.I),
    (r"\b(?:follow|obey)\s+(?:only\s+)?my\s+(?:instructions?|commands?|orders)\b", re.I),
    (r"\binstead\s+of\s+the\s+(?:original|previous|above)\s+(?:ones|instructions?)\b", re.I),
    # role markers and chat-template delimiters (start of text, new line or new sentence)
    (r"(?:^|[\n.!?])\s*(?:system|assistant|developer)\s*:", re.I),
    (r"\b(?:SYSTEM|ASSISTANT)\s*:", 0),  # shouted role marker, case-sensitive
    (r"[\[<]\s*/?\s*(?:system|assistant|inst|s)\s*[\]>]|<\|\w+\|>|\[/?INST\]", re.I),
    (r"^\s*#{2,}\s*system\b", re.I),
    # jailbreak vocabulary
    (r"\bjailbreak", re.I),
    (r"\bDAN\s+mode\b|\bdo\s+anything\s+now\b", re.I),
    (r"\b(?:unrestricted|unfiltered|developer|maintenance|god)\s+mode\b", re.I),
    (
        r"\bwithout\s+(?:any\s+)?(?:restrictions|filters|safety|limits|guardrails)\b"
        r"|\bno\s+(?:restrictions|safety\s+rules|filters)\b",
        re.I,
    ),
    # prompt extraction
    (
        rf"\b{_REVEAL_VERBS}\b[^.?!]{{0,30}}\b(?:system|hidden|initial|original|developer)\s+"
        rf"{_PROMPT_NAMES}\b",
        re.I,
    ),
    (
        r"\b(?:reveal|print|repeat|output|leak|disclose|what\s+(?:is|are))\s+(?:me\s+)?"
        r"your\s+(?:\w+\s+){0,2}(?:prompt|instructions?|rules)\b",
        re.I,
    ),
    (r"\b(?:your|the)\s+(?:initial|original|hidden|secret)\s+(?:instructions?|prompt)\b", re.I),
    # disabling safeguards
    (
        r"\b(?:override|bypass|disable|circumvent|turn\s+off)\s+(?:\w+\s+){0,3}"
        r"(?:safety|security|validator|guardrails?|content\s+filters?|restrictions)\b",
        re.I,
    ),
]

INJECTION_PATTERNS: list[re.Pattern[str]] = [re.compile(p, f) for p, f in _INJECTION_SOURCES]

_ZERO_WIDTH = dict.fromkeys(range(0x200B, 0x2010)) | {0xFEFF: None, 0x2060: None}


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).split())


def detect_prompt_injection(text: str) -> bool:
    """Return True if the text looks like a prompt-injection attempt."""
    normalised = _normalise(text)
    return any(p.search(normalised) or p.search(text) for p in INJECTION_PATTERNS)
