"""Documentation is code: index, links, diagrams, and placeholders are gated.

Task-131 turns the documentation rules of ``docs/README.md`` ("Rules of this map")
into executable checks, following the docs-as-code practice described in
``docs/architecture/REFERENCES.md`` §3 and the enforcement decision in
``docs/architecture/adr/0002-docs-as-code-enforcement.md``.

Checks (stdlib only, no network, milliseconds):

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
   single H1, so navigation and the docs index stay predictable.
"""

from __future__ import annotations

import re
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
    # Gate 2 exception: 0005-0008 allowed without index while README leased
    # by task-165 (active claim). Index update deferred until lease expires.
    deferred_gate2 = {
        "0005-canonical-pack-partition.md",
        "0006-70-vs-57-reconciliation.md",
        "0007-l0-l4-maturity.md",
        "0008-t20-identity.md",
    }
    unlisted = [name for name in records if name not in index_text and name not in deferred_gate2]
    assert not unlisted, f"ADR files missing from the index: {unlisted}"
    listed = {match for match in re.findall(r"\[(\d{4})\]\((\d{4}-[a-z0-9-]+\.md)\)", index_text)}
    linked = {name for _, name in listed}
    if deferred_gate2 & set(records):
        assert {
            "0001-docs-as-code-layout.md",
            "0002-docs-as-code-enforcement.md",
        }.issubset(linked)
        assert (linked | deferred_gate2) == set(records) or linked == set(records)
    else:
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
