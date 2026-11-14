"""Function / type allowlists for the SQL safety validator.

The validator is allowlist-based: a function that is not listed here is a terminal
safety block, no matter what it is called. A denylist can never be complete
(``query_to_xml``, ``current_setting``, ``pg_ls_logdir`` ... all slipped through the old
one), whereas this list only grows when someone deliberately adds a function.

Two namespaces have to be covered because sqlglot parses *known* functions into typed
``exp.Func`` subclasses (named after the sqlglot class, e.g. ``ArraySize`` for PostgreSQL's
``array_length``) and everything else into ``exp.Anonymous`` (named after the SQL text):

* ``ALLOWED_FUNCTIONS``   lower-case SQL names. Checked against ``exp.Anonymous`` names and
  against ``sql_name()`` of typed nodes.
* ``ALLOWED_TYPED_NODES`` sqlglot class names of typed function nodes.

Both are deliberately conservative. Excluded on purpose: anything that reads or writes
server state (``pg_*``, ``lo_*``, ``dblink*``, ``set_config``, ``current_setting``,
``nextval``, ``txid_*``), anything that executes a SQL string (``query_to_xml*``,
``cursor_to_xml``, ``ts_stat``, ``xpath_table``, ``crosstab``), anything that discloses the
environment (``version``, ``current_user``, ``inet_*``, ``current_database``) and memory
bombs (``repeat``).
"""

from __future__ import annotations

# --------------------------------------------------------------------------- SQL names
AGGREGATE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "stddev",
        "stddev_pop",
        "stddev_samp",
        "variance",
        "var_pop",
        "var_samp",
        "string_agg",
        "array_agg",
        "json_agg",
        "jsonb_agg",
        "json_object_agg",
        "jsonb_object_agg",
        "bool_and",
        "bool_or",
        "every",
        "bit_and",
        "bit_or",
        "corr",
        "covar_pop",
        "covar_samp",
        "regr_slope",
        "regr_intercept",
        "regr_r2",
        "any_value",
        "percentile_cont",
        "percentile_disc",
        "mode",
    }
)

MATH_FUNCTIONS: frozenset[str] = frozenset(
    {
        "abs",
        "ceil",
        "ceiling",
        "floor",
        "round",
        "trunc",
        "sqrt",
        "cbrt",
        "power",
        "pow",
        "exp",
        "ln",
        "log",
        "log10",
        "mod",
        "sign",
        "div",
        "width_bucket",
        "sin",
        "cos",
        "tan",
        "pi",
        "degrees",
        "radians",
        "random",
        "gcd",
        "lcm",
        "scale",
        "min_scale",
    }
)

STRING_FUNCTIONS: frozenset[str] = frozenset(
    {
        "lower",
        "upper",
        "initcap",
        "length",
        "char_length",
        "character_length",
        "octet_length",
        "bit_length",
        "substring",
        "substr",
        "left",
        "right",
        "trim",
        "ltrim",
        "rtrim",
        "btrim",
        "lpad",
        "rpad",
        "replace",
        "translate",
        "concat",
        "concat_ws",
        "split_part",
        "strpos",
        "position",
        "starts_with",
        "reverse",
        "regexp_replace",
        "regexp_match",
        "regexp_matches",
        "regexp_split_to_array",
        "regexp_split_to_table",
        "regexp_count",
        "to_char",
        "to_number",
        "to_date",
        "to_timestamp",
        "md5",
        "format",
        "chr",
        "ascii",
        "overlay",
        "quote_literal",
        "quote_nullable",
    }
)

ARRAY_FUNCTIONS: frozenset[str] = frozenset(
    {
        "unnest",
        "array_length",
        "cardinality",
        "array_to_string",
        "string_to_array",
        "array_append",
        "array_prepend",
        "array_cat",
        "array_position",
        "array_positions",
        "array_remove",
        "array_replace",
        "array_upper",
        "array_lower",
        "array_ndims",
        "array_dims",
        "num_nonnulls",
        "num_nulls",
        "row",
        "all",
        "any",
    }
)

DATETIME_FUNCTIONS: frozenset[str] = frozenset(
    {
        "now",
        "current_date",
        "current_time",
        "current_timestamp",
        "localtime",
        "localtimestamp",
        "date_trunc",
        "date_part",
        "extract",
        "age",
        "make_date",
        "make_interval",
        "make_timestamp",
        "make_timestamptz",
        "make_time",
        "date_bin",
        "justify_days",
        "justify_hours",
        "justify_interval",
        "timezone",
        "isfinite",
        "date",
    }
)

CONDITIONAL_FUNCTIONS: frozenset[str] = frozenset(
    {"coalesce", "nullif", "greatest", "least", "case", "exists"}
)

CASTING_FUNCTIONS: frozenset[str] = frozenset({"cast"})

JSON_FUNCTIONS: frozenset[str] = frozenset(
    {
        "json_build_object",
        "jsonb_build_object",
        "json_build_array",
        "jsonb_build_array",
        "to_json",
        "to_jsonb",
        "row_to_json",
        "json_extract_path",
        "jsonb_extract_path",
        "json_extract_path_text",
        "jsonb_extract_path_text",
        "jsonb_array_elements",
        "json_array_elements",
        "jsonb_array_elements_text",
        "json_array_elements_text",
        "jsonb_array_length",
        "json_array_length",
        "jsonb_object_keys",
        "json_object_keys",
        "jsonb_each",
        "jsonb_each_text",
        "json_each",
        "json_each_text",
        "jsonb_typeof",
        "json_typeof",
        "jsonb_pretty",
        "jsonb_strip_nulls",
        "json_strip_nulls",
        "jsonb_set",
        "jsonb_insert",
        "jsonb_concat",
        "jsonb_path_exists",
        "jsonb_path_query",
        "jsonb_path_query_first",
    }
)

WINDOW_FUNCTIONS: frozenset[str] = frozenset(
    {
        "row_number",
        "rank",
        "dense_rank",
        "percent_rank",
        "cume_dist",
        "ntile",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "nth_value",
    }
)

# generate_series is allowed only with bounded literal arguments (see validator).
BOUNDED_FUNCTIONS: frozenset[str] = frozenset({"generate_series"})

ALLOWED_FUNCTIONS: frozenset[str] = (
    AGGREGATE_FUNCTIONS
    | MATH_FUNCTIONS
    | STRING_FUNCTIONS
    | ARRAY_FUNCTIONS
    | DATETIME_FUNCTIONS
    | CONDITIONAL_FUNCTIONS
    | CASTING_FUNCTIONS
    | JSON_FUNCTIONS
    | WINDOW_FUNCTIONS
    | BOUNDED_FUNCTIONS
)

# ------------------------------------------------------------------ sqlglot typed nodes
# Class names of typed ``exp.Func`` nodes that the postgres parser produces for the SQL
# functions/operators above. Derived by parsing a sample call of every allowed function;
# a new sqlglot release that introduces a new class fails closed (blocked, never allowed).
ALLOWED_TYPED_NODES: frozenset[str] = frozenset(
    {
        # aggregates
        "AnyValue",
        "ArrayAgg",
        "Avg",
        "BitwiseAndAgg",
        "BitwiseOrAgg",
        "Corr",
        "Count",
        "CovarPop",
        "CovarSamp",
        "GroupConcat",
        "JSONArrayAgg",
        "JSONBObjectAgg",
        "JSONObjectAgg",
        "LogicalAnd",
        "LogicalOr",
        "Max",
        "Min",
        "Mode",
        "PercentileCont",
        "PercentileDisc",
        "RegrIntercept",
        "RegrR2",
        "RegrSlope",
        "Stddev",
        "StddevPop",
        "StddevSamp",
        "Sum",
        "Variance",
        "VariancePop",
        # math
        "Abs",
        "Cbrt",
        "Ceil",
        "Cos",
        "Degrees",
        "Exp",
        "Floor",
        "Ln",
        "Log",
        "Pi",
        "Pow",
        "Radians",
        "Rand",
        "Round",
        "Sign",
        "Sin",
        "Sqrt",
        "Tan",
        "Trunc",
        "WidthBucket",
        # string
        "Ascii",
        "BitLength",
        "Chr",
        "Concat",
        "ConcatWs",
        "Format",
        "Initcap",
        "Left",
        "Length",
        "Lower",
        "MD5",
        "Overlay",
        "Pad",
        "RegexpCount",
        "RegexpLike",
        "RegexpReplace",
        "Replace",
        "Reverse",
        "Right",
        "SplitPart",
        "StartsWith",
        "StrPosition",
        "Substring",
        "ToNumber",
        "Translate",
        "Trim",
        "Upper",
        # arrays
        "Array",
        "ArrayAppend",
        "ArrayConcat",
        "ArrayContainsAll",
        "ArrayPosition",
        "ArrayPrepend",
        "ArrayRemove",
        "ArraySize",
        "ArrayToString",
        "Explode",
        "StringToArray",
        "Unnest",
        # date / time
        "CurrentDate",
        "CurrentTime",
        "CurrentTimestamp",
        "Date",
        "DateBin",
        "Extract",
        "JustifyDays",
        "JustifyHours",
        "JustifyInterval",
        "Localtime",
        "Localtimestamp",
        "MakeInterval",
        "StrToDate",
        "StrToTime",
        "TimeFromParts",
        "TimeToStr",
        "TimestampFromParts",
        "TimestampTrunc",
        # conditional / casting / boolean operators that sqlglot models as functions
        "And",
        "Case",
        "Cast",
        "Coalesce",
        "Exists",
        "Greatest",
        "If",
        "Least",
        "Nullif",
        "Or",
        # json accessors (-> ->> #> #>> ? and friends)
        "JSONBContainsTopKey",
        "JSONBExists",
        "JSONBExtract",
        "JSONBExtractScalar",
        "JSONExtract",
        "JSONExtractScalar",
        "JSONStripNulls",
        # window
        "CumeDist",
        "DenseRank",
        "FirstValue",
        "Lag",
        "LastValue",
        "Lead",
        "Ntile",
        "NthValue",
        "PercentRank",
        "Rank",
        "RowNumber",
        # bounded set-returning function
        "ExplodingGenerateSeries",
        "GenerateSeries",
    }
)

# Typed nodes that deserve a specific, helpful error message (all are excluded above).
DISCLOSURE_NODES: dict[str, str] = {
    "CurrentUser": "current_user",
    "SessionUser": "session_user",
    "CurrentDatabase": "current_database()",
    "CurrentSchema": "current_schema()",
    "CurrentSchemas": "current_schemas()",
    "CurrentCatalog": "current_catalog",
    "CurrentVersion": "version()",
}

# Zero-argument SQL keywords that behave like functions but parse as plain columns.
KEYWORD_FUNCTIONS: frozenset[str] = frozenset(
    {
        "user",
        "current_user",
        "session_user",
        "system_user",
        "current_role",
        "current_catalog",
        "current_schema",
    }
)

# Types whose casts resolve names against the system catalogs (``'pg_roles'::regclass``).
BLOCKED_TYPES: frozenset[str] = frozenset(
    {
        "regclass",
        "regproc",
        "regprocedure",
        "regoper",
        "regoperator",
        "regtype",
        "regconfig",
        "regdictionary",
        "regnamespace",
        "regrole",
        "regcollation",
        "oid",
        "xid",
        "xid8",
        "cid",
        "tid",
        "pg_lsn",
        "pg_snapshot",
        "txid_snapshot",
        "refcursor",
    }
)
