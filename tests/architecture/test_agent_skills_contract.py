"""Repo-native contract guard for the repository agent skills (`.agents/skills/`).

`AGENTS.md` treats `.agents/skills/` as repository constitution, but nothing in the
repository enforced its structure: the external skill-creator `quick_validate.py`
is a develop-time tool, not a CI gate, and it cannot see the README index, the
helper-script executable bit, or the local file references. This module is the
permanent, repo-owned regression guard for those properties.

It complements (does not replace) `quick_validate.py`: that tool validates the
frontmatter shape; this gate keeps the *discoverability* and *internal
consistency* of the set from silently decaying when a skill is renamed, added,
or has a reference moved.

Two honest limits, stated up front:

* Frontmatter is parsed with a deliberately minimal flat `key: value` reader, not
  a full YAML parser — the repository does not depend on a YAML library and this
  guard must not add one. It is sufficient for the flat single-line frontmatter
  every skill here uses; `quick_validate.py` remains the schema authority.
* A local reference is accepted when it resolves in the skill directory *or* at
  the repository root, because several skills legitimately cite repo-root scripts
  (e.g. `scripts/agent_board.py`). The gate therefore catches a reference that
  resolves *nowhere*, not one that merely points at the wrong root.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"
INDEX = SKILLS_ROOT / "README.md"

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_LOCAL_REF = re.compile(r"`((?:references|scripts|assets)/[^`\s]+)`")
_INDEX_LINK = re.compile(r"\]\(([^)]+/SKILL\.md)\)")


def _skill_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _frontmatter(skill_md: Path) -> dict[str, str] | None:
    match = _FRONTMATTER.match(skill_md.read_text(encoding="utf-8"))
    if match is None:
        return None
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line[0] in (" ", "\t", "#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            return None
        fields[key.strip()] = value.strip().strip("\"'")
    return fields


def _check_index(root: Path) -> list[str]:
    """Every skill directory is linked from README, and every link exists."""
    present = {d.name for d in _skill_dirs(root)}
    index = root / "README.md"
    if not index.is_file():
        return [f"{index}: index missing"]
    linked = {Path(p).parent.name for p in _INDEX_LINK.findall(index.read_text(encoding="utf-8"))}
    offenders = [f"{name}: skill dir not indexed in README" for name in sorted(present - linked)]
    offenders += [
        f"{name}: README links a skill that does not exist" for name in sorted(linked - present)
    ]
    return offenders


def _check_names(root: Path) -> list[str]:
    offenders: list[str] = []
    for d in _skill_dirs(root):
        fm = _frontmatter(d / "SKILL.md")
        if fm is None:
            offenders.append(f"{d.name}: missing or malformed frontmatter")
        elif not fm.get("name"):
            offenders.append(f"{d.name}: frontmatter has no name")
        elif fm["name"] != d.name:
            offenders.append(f"{d.name}: frontmatter name {fm['name']!r} != directory")
    return offenders


def _check_descriptions(root: Path) -> list[str]:
    offenders: list[str] = []
    for d in _skill_dirs(root):
        fm = _frontmatter(d / "SKILL.md")
        if fm is None or not fm.get("description"):
            offenders.append(f"{d.name}: missing or empty description")
    return offenders


def _check_references(root: Path, repo_root: Path) -> list[str]:
    offenders: list[str] = []
    for d in _skill_dirs(root):
        for md in sorted(d.rglob("*.md")):
            for ref in _LOCAL_REF.findall(md.read_text(encoding="utf-8")):
                target = ref.split("::", 1)[0].rstrip("/")
                candidates = (d / target, repo_root / target)
                if not any(c.is_file() or c.is_dir() for c in candidates):
                    offenders.append(f"{md.relative_to(repo_root)}: `{ref}` resolves nowhere")
    return offenders


def _check_executables(root: Path) -> list[str]:
    offenders: list[str] = []
    for d in _skill_dirs(root):
        scripts = d / "scripts"
        if not scripts.is_dir():
            continue
        for entry in sorted(scripts.iterdir()):
            if entry.is_file() and not entry.stat().st_mode & 0o111:
                offenders.append(f"{entry.relative_to(root)}: helper script is not executable")
    return offenders


# --------------------------------------------------------------------------- #
# Real-tree checks
# --------------------------------------------------------------------------- #
def test_skill_index_is_complete_and_bidirectional() -> None:
    assert _skill_dirs(SKILLS_ROOT), "no skill directories found under .agents/skills/"
    assert _check_index(SKILLS_ROOT) == []


def test_frontmatter_name_matches_directory() -> None:
    assert _check_names(SKILLS_ROOT) == []


def test_every_skill_has_a_description() -> None:
    assert _check_descriptions(SKILLS_ROOT) == []


def test_local_references_resolve() -> None:
    assert _check_references(SKILLS_ROOT, REPO_ROOT) == []


def test_helper_scripts_are_executable_and_present() -> None:
    # Vacuity guard: the executable check is meaningless if no skill ships a script.
    assert any((d / "scripts").is_dir() for d in _skill_dirs(SKILLS_ROOT))
    assert _check_executables(SKILLS_ROOT) == []


# --------------------------------------------------------------------------- #
# Positive control — the checks are real, not vacuous
# --------------------------------------------------------------------------- #
def _fixture(root: Path, name: str, **kw: object) -> None:
    d = root / name
    (d / "references").mkdir(parents=True)
    (d / "references" / "r.md").write_text("see nothing\n", encoding="utf-8")
    front_name = kw.get("front_name", name)
    description = kw.get("description", "a fixture skill")
    ref = kw.get("ref")
    body = f"\nSee `{ref}`.\n" if ref else "\n"
    (d / "SKILL.md").write_text(
        f"---\nname: {front_name}\ndescription: {description}\n---\n{body}", encoding="utf-8"
    )
    if kw.get("script_mode") is not None:
        (d / "scripts").mkdir()
        helper = d / "scripts" / "h.py"
        helper.write_text("print()\n", encoding="utf-8")
        helper.chmod(int(kw["script_mode"]))  # type: ignore[arg-type]


def _index(root: Path, *names: str) -> None:
    lines = [f"- [`{n}`]({n}/SKILL.md)" for n in names]
    (root / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_baseline_fixture_is_green(tmp_path: Path) -> None:
    _fixture(tmp_path, "good", ref="references/r.md", script_mode=0o755)
    _index(tmp_path, "good")
    assert _check_index(tmp_path) == []
    assert _check_names(tmp_path) == []
    assert _check_descriptions(tmp_path) == []
    assert _check_references(tmp_path, tmp_path) == []
    assert _check_executables(tmp_path) == []


def test_detects_skill_missing_from_index(tmp_path: Path) -> None:
    _fixture(tmp_path, "good")
    _fixture(tmp_path, "orphan")
    _index(tmp_path, "good")
    assert _check_index(tmp_path) != []


def test_detects_index_entry_without_a_skill(tmp_path: Path) -> None:
    _fixture(tmp_path, "good")
    _index(tmp_path, "good", "ghost")
    assert _check_index(tmp_path) != []


def test_detects_frontmatter_name_directory_mismatch(tmp_path: Path) -> None:
    _fixture(tmp_path, "good", front_name="wrong-name")
    assert _check_names(tmp_path) != []


def test_detects_dangling_local_reference(tmp_path: Path) -> None:
    _fixture(tmp_path, "good", ref="references/does-not-exist.md")
    assert _check_references(tmp_path, tmp_path) != []


def test_detects_non_executable_helper_script(tmp_path: Path) -> None:
    _fixture(tmp_path, "good", script_mode=0o644)
    assert _check_executables(tmp_path) != []


def test_detects_missing_description(tmp_path: Path) -> None:
    _fixture(tmp_path, "good", description="")
    assert _check_descriptions(tmp_path) != []
