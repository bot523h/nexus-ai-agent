"""SQL-aware table extraction from a plain-text ``pg_dump`` artifact.

Arena finding F-007: ``_expected_dump_tables`` decided "which tables does this
dump create?" with a line-by-line ``line.startswith("CREATE TABLE ")`` test.
That is wrong for real PostgreSQL dumps in several ways, and each one changes
the post-restore proof:

* a ``CREATE TABLE`` preceded by leading whitespace or emitted on one line with
  its body is missed, so the target's table is never required — the proof
  passes while a table is missing;
* ``CREATE TABLE public.t (...)``, ``CREATE TABLE "My Table"`` and
  ``CREATE TABLE ONLY public.t`` need real identifier handling, not
  ``split(".")[-1]``;
* a ``CREATE TABLE`` *inside a function body* (PL/pgSQL dollar-quoting) is not a
  table creation and must not be counted;
* ``CREATE TABLE ... PARTITION OF`` creates no new table here and must be
  excluded; ``CREATE UNLOGGED TABLE``/``CREATE TEMP TABLE`` must be included;
* comments (``--``) and string literals containing the words "CREATE TABLE"
  are not statements.

This module implements a small, purpose-built scanner rather than a full SQL
parser: it strips comments and dollar-quoted/string bodies, then finds real
``CREATE [UNLOGGED|TEMP|TEMPORARY] TABLE [IF NOT EXISTS] [ONLY] <name>``
statements at statement boundaries. It is deliberately conservative — when in
doubt it *includes* a name (a false positive fails a drill that should pass,
which an operator can see and fix; a false negative silently weakens the proof).
"""

from __future__ import annotations

import re
from pathlib import Path

#: A qualified identifier: one or more parts joined by dots, where a part is
#: either a double-quoted name (``""`` escapes a quote, spaces allowed) or a
#: bare identifier. Matching this as one unit is why ``public."My Table"`` is
#: read as a single name instead of stopping at the space.
_IDENT_PART = r'(?:"(?:[^"]|"")*"|[^\s(;."]+)'
_QUALIFIED_NAME = rf"{_IDENT_PART}(?:\.{_IDENT_PART})*"

#: Statement keywords that create a *new* table we must verify on the target.
_CREATE_TABLE = re.compile(
    r"""\bCREATE\s+
        (?:GLOBAL\s+|LOCAL\s+)?            # partition-scope qualifiers
        (?:UNLOGGED\s+|TEMP\s+|TEMPORARY\s+)?
        TABLE\s+
        (?:IF\s+NOT\s+EXISTS\s+)?
        (?:ONLY\s+)?
        (?P<name>"""
    + _QUALIFIED_NAME
    + r")",
    re.IGNORECASE | re.VERBOSE,
)

#: ``CREATE TABLE ... PARTITION OF parent`` attaches to an existing table; it is
#: not a new table and pg_dump emits the parent's CREATE TABLE separately.
_PARTITION_OF = re.compile(r"\bPARTITION\s+OF\b", re.IGNORECASE)

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _strip_noise(sql: str) -> str:
    """Remove comments, string literals and dollar-quoted bodies.

    What remains is statement structure: the words "CREATE TABLE" inside a
    comment, a ``'...'`` literal or a ``$body$ ... $body$`` function body are
    gone, so they cannot be mistaken for a real statement. Line comments
    (``--``) and block comments (``/* */``) are both handled; single quotes are
    un-doubled per the SQL standard (``''`` is an escaped quote).
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # line comment
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            out.append("\n")
            continue
        # block comment (PostgreSQL block comments nest)
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            depth = 1
            i += 2
            while i < n and depth:
                if sql[i] == "/" and i + 1 < n and sql[i + 1] == "*":
                    depth += 1
                    i += 2
                elif sql[i] == "*" and i + 1 < n and sql[i + 1] == "/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            out.append(" ")
            continue
        # single-quoted string
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        # dollar-quoted body (function bodies, etc.)
        if ch == "$":
            match = _DOLLAR_TAG.match(sql, i)
            if match:
                tag = match.group(0)
                end = sql.find(tag, match.end())
                if end != -1:
                    i = end + len(tag)
                    out.append(" ")
                    continue
            i += 1
            out.append(ch)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _normalize_identifier(raw: str) -> str:
    """``public."My Table"`` → ``My Table``; ``"a"."b"`` → ``b``.

    The schema is dropped: the post-restore proof reads ``public`` only, which
    is what the drill's target query filters on. Quoted identifiers keep their
    spaces and case; ``""`` is an escaped double quote inside a quoted name.
    """
    name = raw.strip().rstrip(";").strip()
    if not name:
        return ""
    # Split on dots that are not inside double quotes.
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    i = 0
    while i < len(name):
        ch = name[i]
        if ch == '"':
            if in_quotes and i + 1 < len(name) and name[i + 1] == '"':
                current.append('"')
                i += 2
                continue
            in_quotes = not in_quotes
            i += 1
            continue
        if ch == "." and not in_quotes:
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts[-1].strip()


def expected_dump_tables(sql_text: str) -> set[str]:
    """Table names a plain-text ``pg_dump`` artifact creates.

    Returns the *unqualified* table names (schema stripped), matching what the
    drill reads back from the target's ``public`` schema.
    """
    cleaned = _strip_noise(sql_text)
    names: set[str] = set()
    for match in _CREATE_TABLE.finditer(cleaned):
        # Look at the statement tail to exclude ``PARTITION OF``.
        tail = cleaned[match.end() : match.end() + 40]
        if _PARTITION_OF.search(tail):
            continue
        identifier = _normalize_identifier(match.group("name"))
        if identifier:
            names.add(identifier)
    return names


#: ``COPY <name> (cols) FROM stdin;`` — the data-bearing section of a text dump.
_COPY_START = re.compile(
    r"^COPY\s+(?P<name>" + _QUALIFIED_NAME + r")\s*(?:\([^)]*\))?\s*FROM\s+stdin\s*;",
    re.IGNORECASE,
)


def copy_row_counts(path: Path, *, max_lines: int = 5_000_000) -> tuple[dict[str, int], bool]:
    """Count data rows per table from a text ``pg_dump``, streaming the file.

    F-007E: ``RESTORE_PROVEN`` after an apply previously proved only that the
    *schema* landed; a dump whose data never arrived would still pass. This
    gives the per-table row counts the artifact claims, so the drill can compare
    them to the target. It reads line by line and never holds a row in memory,
    so a multi-gigabyte dump costs a streaming pass, not its own size in RAM
    (F-012). Returns ``({table: rows}, truncated)``; ``truncated`` is ``True``
    when *max_lines* was exceeded, so the caller can label the proof honestly
    rather than claim a comparison it did not finish.
    """
    counts: dict[str, int] = {}
    current: str | None = None
    seen = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            seen += 1
            if seen > max_lines:
                return counts, True
            if current is not None:
                if line.startswith("\\."):
                    current = None
                    continue
                if line.strip():
                    counts[current] += 1
                continue
            match = _COPY_START.match(line)
            if match:
                name = _normalize_identifier(match.group("name"))
                if name:
                    counts.setdefault(name, 0)
                    current = name
    return counts, False
