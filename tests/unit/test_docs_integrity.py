"""Documentation is code: index, links, diagrams, and placeholders are gated.

Task-131 turns the documentation rules of ``docs/README.md`` ("Rules of this map")
into executable checks, following the docs-as-code practice described in
``docs/architecture/REFERENCES.md`` §3 and the enforcement decision in
``docs/architecture/adr/0002-docs-as-code-enforcement.md``.

Checks (stdlib only, no network):

1. **single index** — every Markdown file under ``docs/`` is listed in
   ``docs/README.md`` (grouped listings such as ``todo-v1.2.0.md … todo-v2.1.0.md``
   are accepted, because only the *basename* has to appear);
2. **no dead links** — every relative Markdown link inside ``docs/`` resolves to a
   real file (absolute URLs, ``mailto:`` and pure anchors are ignored);
3. **diagrams are real** — every ```mermaid fence is balanced, non-empty, starts
   with a supported diagram keyword, and every ``flowchart`` closes the
   ``subgraph`` blocks it opens (the one structural error Mermaid silently
   renders as a broken box);
4. **no placeholders** — the architecture pages contain no ``TODO``/``FIXME``/
   ``XXX``/``TBD``/``<placeholder>`` marker: an architecture page is a claim;
5. **ADR index agreement** — ``docs/architecture/adr/README.md`` lists exactly the
   ADR files that exist (plus the template, which is listed separately);
6. **living pages are titled** — each ``docs/architecture/*.md`` page starts with a
   single H1, so navigation and the docs index stay predictable;
7. **cited ADRs resolve** — every ``adr/NNNN-*.md`` path named from code or docs
   exists (a docstring promising "this ADR governs me" is a claim about a file);
8. **cited test files resolve** — every ``tests/*.py`` path named in *prose* (the
   living docs, plus Python comments and docstrings — never string literals, which
   are data) exists, because a cited test is the evidence for a claim;
9. **quoted counts are reproducible** — a living page that writes
   `` `tests/<dir>/` (N test modules) `` must agree with the filesystem.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
DOCS = REPO_ROOT / "docs"
INDEX = DOCS / "README.md"

#: Markdown link syntax, excluding images (``![alt](src)``).
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_MERMAID_FENCE = re.compile(r"^```mermaid\s*$", re.MULTILINE)
_FENCE = re.compile(r"^```", re.MULTILINE)

#: Diagram keywords accepted by the docs gate (broad Mermaid 10+ surface).
MERMAID_KEYWORDS = {
    "flowchart",
    "graph",
    "sequencediagram",
    "statediagram",
    "statediagram-v2",
    "classdiagram",
    "erdiagram",
    "journey",
    "gantt",
    "pie",
    "mindmap",
    "timeline",
    "quadrantchart",
    "xychart-beta",
    "gitgraph",
    "block-beta",
    "c4context",
    "c4container",
}

#: Placeholder markers in their *instruction* form (``TODO:``), not in the prose that
#: documents the rule ("no ``TODO`` marker"), which is why code spans are stripped first.
PLACEHOLDER_PATTERNS = (
    re.compile(r"\b(?:TODO|FIXME|XXX|TBD|HACK)\s*:"),
    re.compile(r"<placeholder", re.IGNORECASE),
)

ARCHITECTURE_PAGES = sorted((DOCS / "architecture").rglob("*.md")) + [DOCS / "architecture.md"]


def _strip_code(text: str) -> str:
    """Remove fenced blocks and inline code spans.

    Documentation legitimately *writes about* code: a shell comment inside a fence
    starts with ``# `` and a rule mention lives inside backticks. Neither may be
    mistaken for a heading or a leftover marker.
    """
    without_fences: list[str] = []
    inside_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside_fence = not inside_fence
            continue
        if not inside_fence:
            without_fences.append(line)
    return re.sub(r"`[^`\n]*`", "", "\n".join(without_fences))


def _docs_markdown_files() -> list[Path]:
    return sorted(path for path in DOCS.rglob("*.md") if path.is_file())


def _mermaid_blocks(text: str) -> list[list[str]]:
    """Return every ```mermaid block body as a list of lines (unclosed fence counts as the rest)."""
    blocks: list[list[str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() == "```mermaid":
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                body.append(lines[index])
                index += 1
            blocks.append(body)
        index += 1
    return blocks


# --------------------------------------------------------------------------- #
# 1. single index
# --------------------------------------------------------------------------- #
def test_every_document_is_indexed_in_docs_readme() -> None:
    index_text = INDEX.read_text(encoding="utf-8")
    missing = [
        str(path.relative_to(REPO_ROOT))
        for path in _docs_markdown_files()
        if path != INDEX and path.name not in index_text
    ]
    assert not missing, (
        "documents exist that docs/README.md does not index: "
        f"{missing}. Add them to the right table (rule 1 of the docs map)."
    )


def test_index_points_only_at_existing_files() -> None:
    broken = [
        target
        for target in _relative_links(INDEX)
        if not (INDEX.parent / target.split("#")[0]).resolve().exists()
    ]
    assert not broken, f"docs/README.md links to missing paths: {broken}"


# --------------------------------------------------------------------------- #
# 2. no dead links
# --------------------------------------------------------------------------- #
def _relative_links(path: Path) -> list[str]:
    links = []
    for raw in _LINK.findall(path.read_text(encoding="utf-8")):
        target = raw.strip()
        if target.startswith(("http://", "https://", "mailto:", "#", "tel:")):
            continue
        if not target.split("#")[0]:
            continue
        links.append(target)
    return links


def test_no_dead_relative_links_anywhere_in_docs() -> None:
    dead: list[str] = []
    for path in _docs_markdown_files():
        for target in _relative_links(path):
            resolved = (path.parent / target.split("#")[0]).resolve()
            if not resolved.exists():
                dead.append(f"{path.relative_to(REPO_ROOT)} -> {target}")
    assert not dead, "dead relative links in docs/:\n" + "\n".join(dead)


# --------------------------------------------------------------------------- #
# 3. diagrams are real
# --------------------------------------------------------------------------- #
def test_mermaid_blocks_are_balanced_and_non_empty() -> None:
    problems: list[str] = []
    for path in _docs_markdown_files():
        text = path.read_text(encoding="utf-8")
        opens = len(_MERMAID_FENCE.findall(text))
        total_fences = len(_FENCE.findall(text))
        if total_fences % 2 != 0:
            problems.append(f"{path.relative_to(REPO_ROOT)}: odd number of ``` fences")
        if opens == 0:
            continue
        for block in _mermaid_blocks(text):
            body = [line for line in block if line.strip()]
            if not body:
                problems.append(f"{path.relative_to(REPO_ROOT)}: empty mermaid block")
                continue
            first = body[0].strip().split()[0].lower()
            if first not in MERMAID_KEYWORDS:
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}: unsupported diagram type {first!r}"
                )
            if first in {"flowchart", "graph"}:
                opened = sum(1 for line in body if line.strip().startswith("subgraph"))
                closed = sum(1 for line in body if line.strip() == "end")
                if opened != closed:
                    problems.append(
                        f"{path.relative_to(REPO_ROOT)}: flowchart opens {opened} "
                        f"subgraph(s) and closes {closed}"
                    )
    assert not problems, "mermaid problems:\n" + "\n".join(problems)


# --------------------------------------------------------------------------- #
# 4. no placeholders in architecture pages
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ARCHITECTURE_PAGES, ids=lambda p: p.name)
def test_architecture_pages_have_no_placeholder_tokens(path: Path) -> None:
    text = _strip_code(path.read_text(encoding="utf-8"))
    found = [pattern.pattern for pattern in PLACEHOLDER_PATTERNS if pattern.search(text)]
    assert not found, f"{path.relative_to(REPO_ROOT)} still contains placeholder markers {found}"


# --------------------------------------------------------------------------- #
# 5. ADR index agreement
# --------------------------------------------------------------------------- #
def test_adr_index_lists_exactly_the_existing_records() -> None:
    adr_dir = DOCS / "architecture" / "adr"
    index_text = (adr_dir / "README.md").read_text(encoding="utf-8")
    records = sorted(p.name for p in adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert records, "no ADR records found"
    unlisted = [name for name in records if name not in index_text]
    assert not unlisted, f"ADR files missing from the index: {unlisted}"
    listed = {match for match in re.findall(r"\[(\d{4})\]\((\d{4}-[a-z0-9-]+\.md)\)", index_text)}
    linked = {name for _, name in listed}
    assert linked == set(records), f"ADR index/files disagree: {sorted(linked ^ set(records))}"


# --------------------------------------------------------------------------- #
# 6. living pages start with a single H1
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ARCHITECTURE_PAGES, ids=lambda p: p.name)
def test_architecture_pages_start_with_a_single_h1(path: Path) -> None:
    body = _strip_code(path.read_text(encoding="utf-8"))
    # MADR records open with YAML front matter — the H1 follows it.
    if body.lstrip().startswith("---"):
        parts = body.split("---", 2)
        body = parts[2] if len(parts) == 3 else body
    lines = [line for line in body.splitlines() if line.strip()]
    assert lines and lines[0].startswith("# "), f"{path.name} does not start with an H1"
    h1_count = sum(1 for line in lines if line.startswith("# "))
    assert h1_count == 1, f"{path.name} has {h1_count} H1 headings (expected exactly 1)"


# --------------------------------------------------------------------------- #
# 7. ADR references made *from code* resolve
# --------------------------------------------------------------------------- #
#: Where an ADR path may be cited.  Code counts: a module docstring saying
#: "this ADR governs me" is the strongest form of the documentation contract,
#: and until this check existed nothing verified that the record it names was
#: even present.  It was not: ``creative/packs/trust.py`` cited the record
#: numbered ``0009-pack-trust-root.md`` while the record it shipped is ``0006``.
_CITATION_ROOTS = ("src", "tests", "scripts", "docs")
_ADR_PATH = re.compile(r"(?:docs/architecture/)?adr/(\d{4}-[a-z0-9-]+\.md)")
#: A citation is *outbound* only if it names a path; ``ADR 0006`` / ``ADR-0006``
#: prose form is checked by the index test above.
_SELF_REFERENCE_CONTEXT = ("template.md",)


def _citation_files() -> list[Path]:
    files: list[Path] = []
    for root in _CITATION_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for pattern in ("*.py", "*.md"):
            files.extend(p for p in base.rglob(pattern) if "__pycache__" not in p.parts)
    files.extend(REPO_ROOT.glob("*.md"))
    return sorted(set(files))


def _dangling_adr_citations(text: str, existing: set[str]) -> list[str]:
    """Citation numbers in *text* that name no record in *existing*.

    Kept pure so the red-proof below can run it against a fixture rather than
    having to break the repository to prove the guard can fail.
    """
    dangling: list[str] = []
    for match in _ADR_PATH.finditer(text):
        cited = match.group(1)
        if cited in _SELF_REFERENCE_CONTEXT:
            continue
        if cited not in existing:
            line = text[: match.start()].count("\n") + 1
            dangling.append(f"line {line} -> adr/{cited}")
    return dangling


def test_every_cited_adr_record_exists() -> None:
    """A dangling ADR citation is a documentation claim that cannot be checked.

    The ADR index test (check 5) only ties ``adr/README.md`` to the files in
    ``adr/``.  A reference from a Python docstring to a record number that was
    never written therefore drifted undetected — exactly the docs-as-code
    failure mode ADR 0002 exists to prevent.  It had already happened:
    ``creative/packs/trust.py`` cited record 0009 for three days while the
    record it shipped was 0006.
    """
    adr_dir = DOCS / "architecture" / "adr"
    existing = {path.name for path in adr_dir.glob("*.md")}
    assert existing, "no ADR records found"
    dangling: list[str] = []
    for path in _citation_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for problem in _dangling_adr_citations(text, existing):
            dangling.append(f"{path.relative_to(REPO_ROOT)}:{problem.removeprefix('line ')}")
    assert not dangling, "ADR citations that resolve to nothing:\n" + "\n".join(dangling)


#: A record that has never existed, and the path prefix the citation regex
#: anchors on.  Both are assembled at run time on purpose: this module is inside
#: the guard's own scan scope, so a literal citation written here would make the
#: guard report its own red-proof fixture as a defect.
_MISSING_RECORD = "0009-pack-trust-root.md"
_ADR_PREFIX = "adr/"


def test_the_dangling_citation_detector_can_actually_fire() -> None:
    """Red-proof: a citation guard that cannot fire is not a guard."""
    existing = {"0006-capability-pack-trust-root.md"}
    dangling = f"see {_ADR_PREFIX}{_MISSING_RECORD}"
    assert _dangling_adr_citations(dangling, existing) == [
        f"line 1 -> {_ADR_PREFIX}{_MISSING_RECORD}"
    ]
    assert (
        _dangling_adr_citations(f"see {_ADR_PREFIX}0006-capability-pack-trust-root.md", existing)
        == []
    )
    # A bare record *number* is prose, not a path citation, and must not fire.
    assert _dangling_adr_citations("governed by ADR 0009 and ADR-0012", existing) == []


# --------------------------------------------------------------------------- #
# 8. Evidence citations resolve: a named test file must exist
# --------------------------------------------------------------------------- #
#: Where a claim about *today* may live.  ``docs/README.md`` rule 2 splits the
#: tree into **living** pages (``architecture/``, updated in place) and **dated**
#: records (``audits/``, ``history/``, and the dated ``DECISION_LOG.md``).  A
#: dated record may legitimately name a file that no longer exists —
#: ``DECISION_LOG.md`` line 418 narrates replacing the manual ``test_rtl.py``
#: script, and rewriting that sentence to satisfy a lint would destroy the only
#: record of why the file is gone.  The same split decides what counts as prose
#: in code, below.
_LIVING_DOC_DIRS = ("docs/architecture",)
_LIVING_DOC_FILES = ("docs/README.md", "README.md", "AGENTS.md", "CHANGELOG.md")
_LIVING_CODE_ROOTS = ("src", "scripts", "tests")

#: A path such as ``tests/<unit>/<module>.py`` as cited in prose.  The leading
#: guard keeps a deeper ``…/tests/`` segment (``scripts/tests/…``) out of scope;
#: the trailing ``.py`` keeps directory mentions (``tests/unit/``) — which cite a
#: tree, not a file — out of scope.
_TEST_PATH = re.compile(r"(?<![\w/.-])(tests/[A-Za-z0-9_./-]+\.py)")

#: ``@pytest.mark.parametrize``-style lists, ``::test_name`` selectors and
#: trailing punctuation are not part of the path.
_PATH_TRAILERS = ".,;:)" + "]"


def _prose_lines(path: Path) -> dict[int, str]:
    """1-based line number -> the *prose* text on it.

    In Markdown every line is prose.  In Python a citation is a claim only where
    a human reads it as a claim: a docstring or a comment.  A path inside a
    string literal is **data** — ``tests/unit/test_ci_extras_parity.py`` builds
    synthetic pytest log lines (``"SKIPPED [1] tests/…/x.py:12: …"``) and
    ``tests/unit/test_agent_board_active_in_review.py`` feeds a deliberately
    non-matching path to the overlap referee.  Treating those as claims would
    force tests to stop exercising the very strings they exist to exercise,
    which is the kind of "fix" that makes a guard worse than no guard.

    The value stored is the docstring's own segment or the comment's own text —
    *not* the whole line — so a path literal that shares a line with a trailing
    comment stays data.  Multi-line docstrings report their first line.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if path.suffix != ".py":
        return dict(enumerate(lines, start=1))
    source = "\n".join(lines)
    prose: dict[int, str] = {}
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        segment = ast.get_source_segment(source, first.value)
        if segment is not None:
            prose.setdefault(first.lineno, segment)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            prose.setdefault(token.start[0], token.string)
    return prose


def _dangling_test_paths(prose: dict[int, str], existing: set[str]) -> list[str]:
    """Citations in *prose* that name no existing file.

    Kept pure so the red-proof below can run it against a fixture instead of
    having to break the repository to prove the guard can fail.
    """
    dangling: list[str] = []
    for lineno in sorted(prose):
        for match in _TEST_PATH.finditer(prose[lineno]):
            cited = match.group(1).rstrip(_PATH_TRAILERS)
            if cited not in existing:
                dangling.append(f"line {lineno} -> {cited}")
    return dangling


def _evidence_files() -> list[Path]:
    files: list[Path] = []
    for name in _LIVING_DOC_DIRS:
        files.extend(sorted((REPO_ROOT / name).rglob("*.md")))
    files.extend(REPO_ROOT / name for name in _LIVING_DOC_FILES)
    for name in _LIVING_CODE_ROOTS:
        base = REPO_ROOT / name
        files.extend(sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts))
    return [path for path in files if path.is_file()]


def test_every_cited_test_file_exists() -> None:
    """A cited test file is evidence; evidence that is not there is narrative.

    Measured 2026-09-27 on ``main@e6b06e0``: 199 test-path citations in the
    living surfaces, six of them pointing at files that do not exist — three in
    architecture pages (``SECURITY.md`` T3, ``DATA_AND_STORAGE.md`` §6,
    ``OVERVIEW.md`` Q2) and three synthetic fixtures that this rule deliberately
    ignores (see :func:`_prose_lines`).
    """
    existing = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "tests").rglob("*.py")
        if "__pycache__" not in path.parts
    }
    assert existing, "no test files found — the guard has lost its subject"
    dangling: list[str] = []
    for path in _evidence_files():
        for problem in _dangling_test_paths(_prose_lines(path), existing):
            dangling.append(f"{path.relative_to(REPO_ROOT)}:{problem.removeprefix('line ')}")
    assert not dangling, "citations that resolve to nothing:\n" + "\n".join(dangling)


#: A path that has never existed, assembled at run time: this module is inside
#: the guard's own scan scope, so a literal citation written here would make the
#: guard report its own red-proof fixture as a defect (same trick as
#: ``_MISSING_RECORD`` above).
_ABSENT_TEST = "tests/unit/" + "test_absent_evidence_probe.py"


def test_the_test_citation_detector_can_actually_fire() -> None:
    """Red-proof: a citation guard that cannot fire is not a guard."""
    existing = {"tests/unit/test_docs_integrity.py"}
    assert _dangling_test_paths({1: f"see {_ABSENT_TEST} for the guard"}, existing) == [
        f"line 1 -> {_ABSENT_TEST}"
    ]
    assert _dangling_test_paths({1: "see tests/unit/test_docs_integrity.py"}, existing) == []
    # A directory is not a file claim, and a prose selector is not a path.
    assert _dangling_test_paths({1: "everything in tests/unit/ is guarded"}, existing) == []
    assert _dangling_test_paths({1: "tests/unit/test_docs_integrity.py::test_x"}, existing) == []


def test_string_literals_are_data_not_claims(tmp_path: Path) -> None:
    """The prose reader must see docstrings and comments, and nothing else.

    This is the discriminator that keeps the guard honest: without it, the
    "obvious" implementation (scan every line) would have flagged three
    legitimate test fixtures and the cheapest way to green would have been to
    delete the fixtures.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        f'"""Module docstring citing {_ABSENT_TEST}."""\n'
        f"# comment citing {_ABSENT_TEST}\n"
        f'LOG = "SKIPPED [1] {_ABSENT_TEST}:1: synthetic pytest line"\n',
        encoding="utf-8",
    )
    prose = _prose_lines(probe)
    assert sorted(lineno for lineno, text in prose.items() if _ABSENT_TEST in text) == [1, 2]
    assert _dangling_test_paths(prose, set()) == [
        f"line 1 -> {_ABSENT_TEST}",
        f"line 2 -> {_ABSENT_TEST}",
    ]


# --------------------------------------------------------------------------- #
# 9. A quoted count is a claim: ``tests/architecture/`` (N test modules)
# --------------------------------------------------------------------------- #
#: ``docs/README.md`` rule 4 requires that quoted numbers be reproducible.  This
#: is the one count shape the living pages quote about the suite, and it had
#: already drifted: ``OVERVIEW.md`` and ``REFERENCES.md`` both advertised
#: "15 files" while ``tests/architecture/`` held 26 modules, because a
#: hand-maintained number has no maintainer.
_DIR_COUNT = re.compile(r"`(tests/[A-Za-z0-9_/]+/)` \((\d+) test modules?\)")


def test_quoted_test_module_counts_are_true() -> None:
    """A count in a living page must match the tree it describes."""
    problems: list[str] = []
    for path in sorted((DOCS / "architecture").rglob("*.md")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            for match in _DIR_COUNT.finditer(line):
                directory, quoted = match.group(1), int(match.group(2))
                target = REPO_ROOT / directory
                if not target.is_dir():
                    problems.append(f"{path.name}:{lineno} -> {directory} does not exist")
                    continue
                actual = len(list(target.glob("*.py")))
                if actual != quoted:
                    problems.append(
                        f"{path.name}:{lineno} -> {directory} has {actual} test modules, "
                        f"page says {quoted}"
                    )
    assert not problems, "counts that are not reproducible:\n" + "\n".join(problems)
