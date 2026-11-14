"""F-007: table detection must understand real ``pg_dump`` output.

Arena finding F-007: ``_expected_dump_tables`` used
``line.strip().startswith("CREATE TABLE ")`` plus ``split(".")[-1]``. A real
PG17 dump defeats that in both directions:

* it *misses* statements that are indented, on one line with their body, or
  qualified as ``ONLY``/``UNLOGGED``/``TEMP``/``IF NOT EXISTS`` — so the
  post-restore proof no longer requires those tables and a missing table passes;
* it *invents* tables named inside a PL/pgSQL function body or a comment — so a
  proof can demand a table the dump never creates.

Each case below is checked against the scanner directly and, where it matters,
against the end-to-end ``_expected_dump_tables`` used by the drill.
"""

from __future__ import annotations

from pathlib import Path

from nexus_ai_agent.maintenance.dump_sql import expected_dump_tables
from nexus_ai_agent.maintenance.restore import _expected_dump_tables

REAL_PG17_HEAD = """\
--
-- PostgreSQL database dump
--

-- Dumped from database version 17.0
SET statement_timeout = 0;
SET default_tablespace = '';

-- Name: jobs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.jobs (
    id uuid NOT NULL,
    payload text
);

-- Name: "My Table"; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public."My Table" (
    id integer
);

CREATE UNLOGGED TABLE public.cache_entries (
    k text
);

CREATE TABLE IF NOT EXISTS public.audit_log (
    id bigint
);

-- PostgreSQL database dump complete
"""


def test_real_pg17_dump_tables_are_all_found() -> None:
    assert expected_dump_tables(REAL_PG17_HEAD) == {
        "jobs",
        "My Table",
        "cache_entries",
        "audit_log",
    }


def test_one_line_create_table_with_body_is_found() -> None:
    sql = "CREATE TABLE public.t (id int);"
    assert expected_dump_tables(sql) == {"t"}


def test_indented_create_table_is_found() -> None:
    sql = "\n    CREATE TABLE public.indented (id int);\n"
    assert expected_dump_tables(sql) == {"indented"}


def test_only_and_quoted_and_qualified_names() -> None:
    sql = (
        "CREATE TABLE ONLY public.parent (id int);\n"
        'CREATE TABLE "Weird Schema"."Odd Name" (id int);\n'
        "CREATE TABLE app.qualified (id int);\n"
    )
    assert expected_dump_tables(sql) == {"parent", "Odd Name", "qualified"}


def test_temp_and_temporary_are_found() -> None:
    sql = "CREATE TEMP TABLE t1 (id int);\nCREATE TEMPORARY TABLE t2 (id int);\n"
    assert expected_dump_tables(sql) == {"t1", "t2"}


def test_create_table_inside_a_plpgsql_body_is_not_a_table() -> None:
    """A ``$body$`` function that creates a table on the fly must not count."""
    sql = """\
CREATE FUNCTION public.make_tmp() RETURNS void AS $body$
BEGIN
    CREATE TABLE public.ephemeral (id int);
    INSERT INTO public.ephemeral VALUES (1);
END;
$body$ LANGUAGE plpgsql;

CREATE TABLE public.real_one (id int);
"""
    assert expected_dump_tables(sql) == {"real_one"}


def test_dollar_quoted_body_with_named_tag() -> None:
    sql = """\
CREATE FUNCTION f() RETURNS void AS $func$
BEGIN
  CREATE TABLE not_a_table (id int);
END
$func$ LANGUAGE plpgsql;
CREATE TABLE yes_a_table (id int);
"""
    assert expected_dump_tables(sql) == {"yes_a_table"}


def test_create_table_in_a_comment_is_ignored() -> None:
    sql = """\
-- CREATE TABLE public.commented_out (id int);
/* CREATE TABLE public.block_commented (id int); */
CREATE TABLE public.real (id int);
"""
    assert expected_dump_tables(sql) == {"real"}


def test_create_table_in_a_string_literal_is_ignored() -> None:
    sql = """\
INSERT INTO public.notes(body) VALUES ('CREATE TABLE public.fake (id int);');
CREATE TABLE public.real (id int);
"""
    assert expected_dump_tables(sql) == {"real"}


def test_partition_of_is_not_a_new_table() -> None:
    """``CREATE TABLE ... PARTITION OF`` attaches to an existing table."""
    sql = """\
CREATE TABLE public.measurements (
    city_id int,
    logdate date
) PARTITION BY RANGE (logdate);

CREATE TABLE public.measurements_y2026m01 PARTITION OF public.measurements
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
"""
    assert expected_dump_tables(sql) == {"measurements"}


def test_nested_block_comment_is_handled() -> None:
    sql = """\
/* outer /* inner */ still a comment
   CREATE TABLE public.hidden (id int);
*/
CREATE TABLE public.visible (id int);
"""
    assert expected_dump_tables(sql) == {"visible"}


def test_views_and_matviews_are_not_tables() -> None:
    sql = """\
CREATE VIEW public.v AS SELECT 1;
CREATE MATERIALIZED VIEW public.mv AS SELECT 1;
CREATE TABLE public.real (id int);
"""
    assert expected_dump_tables(sql) == {"real"}


def test_restore_helper_reads_the_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "dump.sql"
    artifact.write_text(REAL_PG17_HEAD)
    assert _expected_dump_tables(artifact) == {
        "jobs",
        "My Table",
        "cache_entries",
        "audit_log",
    }
