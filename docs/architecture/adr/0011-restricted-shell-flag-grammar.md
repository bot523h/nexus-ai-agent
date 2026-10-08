---
status: accepted
date: 2026-09-27
deciders: arena/p0-1-shell-grammar-successor (security boundary + tool substrate; successor of #105/#119)
consulted: GNU coreutils 9.12 manual (date/ls/cat), GNU grep 3.12 manual, GNU findutils 4.9 manual, POSIX.1-2017 Utility Conventions (getopt), OWASP "OS Command Injection Defence Cheat Sheet"
informed: tool substrate owners, agent-runtime lane, security-boundary lane
---

# 0011. The restricted shell validates an argument *grammar*, not a list of forbidden options

## Context and Problem Statement

`tools/system_shell.py` runs an allowlisted command with `shell=False` and
`cwd` set to the workspace root, and its module contract promised that "any
argument that is (or resolves to) a path outside the workspace is rejected".
The implementation of that promise was an **enumeration of dangerous spellings**:
a set of positionally-detected path arguments per command, plus two literal
flag names (`find -fprintf`, `grep -f`). Nothing in the test suite exercised
`date`, so the claim was never falsified — and it was false.

Measured against `main@8de0edd7d6bd182be2f53ccba8df45cc9fbd09a8`, with a secret file outside the workspace:

| Command | Observed |
|---|---|
| `date -f /tmp/outside_secret.txt` | prints `date: invalid date 'THIS_IS_A_SECRET_LINE_1'` — GNU `date` parses each line of the file and echoes the offending line back |
| `date --file=/tmp/outside_secret.txt` | identical; the `=` spelling was never inspected |
| `date -r /tmp/outside_secret.txt` | prints the file's mtime — an existence/metadata oracle for any host path |
| `grep -f/tmp/outside_secret.txt must_match.txt` | the attached-value spelling bypassed validation entirely; grep matched using outside patterns |
| `grep --file=/…` , `grep --exclude-from=/…` | same class |
| `grep -R TOP_SECRET .` | with `ws/escape_link -> outside`, printed `./escape_link:TOP_SECRET=42` |
| `ls -L sub` , `find -L .` | follow in-workspace symlinks out of the workspace |

Two independent defects are visible here, and they need different fixes.

1. **The forbidden-list model is unbounded.** The validator only recognised the
   spellings its author thought of. `--file=`, `-fFILE`, `--reference=` and any
   option added by a future coreutils release are all outside that set, and the
   path-shaped-argument check that existed could not see a *bare relative name*
   (`-f secret`) because a relative name is indistinguishable from a pattern
   without knowing what the flag consumes.
2. **Some escapes are not path arguments at all.** `grep -R`, `ls -L`,
   `find -L` take no path. They change how the *command* walks the filesystem,
   so no amount of argument validation contains them: one symlink inside the
   workspace redirects the read outside it.

## Decision Drivers

- **Fail closed.** An argument the validator does not understand must be
  refused, not passed to a program that might.
- **Bounded attack surface over time.** Adding a new coreutils option must not
  silently widen the sandbox.
- **No false security.** Where the boundary truly cannot be enforced by
  validation, refuse the capability instead of documenting a promise that the
  implementation cannot keep.
- **Keep the tool useful.** A sandbox that refuses `ls -la` is a sandbox nobody
  runs; the guard must not become the reason the feature is disabled.
- **Executable evidence.** Both mechanisms must be provably load-bearing, and
  the symlink-following rule must be an assertion, not a comment.

## Considered Options

1. **Extend the forbidden list** with the newly discovered spellings
   (`-f`, `--file`, `-r`, `--reference`, `-R`, `-L`, …) and add
   positionally-independent absolute-path detection.
2. **Declare an explicit flag grammar per command** — every accepted flag is
   listed with the number and kind of value tokens it consumes; anything
   unlisted is refused — plus an independent path-shaped-argument net and a
   raw assertion that no symlink-following flag is ever declared safe.
3. **Reimplement the commands in-process** (`ls`/`cat`/`grep`/`find`/`date` over
   `WorkspaceFilesystem`) and delete `subprocess` from this module.

## Decision Outcome

Chosen option: **2**, because it is the only option that makes the boundary
*closed* rather than *enumerated*, and it can be proven closed by a sweep.

Option 1 was rejected as the actual root cause rather than a symptom: it
re-duplicates the same unbounded enumeration, and the repository already has
evidence that this class of list goes stale (`grep -fFILE` was missed on the
first pass; `--exclude-from` was missed on the second). Option 3 removes the
subprocess but replaces `grep`/`find` with a reimplementation whose behaviour
would drift from the real tools — a much larger correctness surface for the
same security property, and a worse trade under the "minimal diff, maximum
integrity" rule.

The grammar is `dict[str, FlagKind]` per command, with `bool`, `value`,
`path`, `optional_value` and `tuple` kinds. `path` is the only kind whose value
is validated as a workspace path. `optional_value` (`ls --color[=WHEN]`,
`date -I[timespec]`) deliberately never consumes the following token, because a
following token may be a real path — auto-consuming it was a live
misinterpretation bug during development.

Two mechanisms are kept deliberately independent, so that defeating the sandbox
requires defeating both:

- **M1, the grammar**, decides *which tokens a command may receive at all*.
- **M2, a syntactic net**, requires any token that lexically denotes an absolute
  or traversing path — `/etc/passwd`, `../x`, `--file=/etc/x`, `~/.ssh` — to
  resolve inside the workspace, in any position and any spelling.

### Consequences

- (+) The sandbox is closed under "flags nobody has written yet": an unknown
  option is refused, so a coreutils upgrade cannot add a new file-opening path.
- (+) The same walk now also catches malformed vectors (`ls -w` with no width,
  `--all=yes` on a boolean, a stray token in a `find` expression) which
  previously reached the subprocess to fail on its own terms.
- (+) Symlink-following options are refused by *absence from the table*, which
  is a property a test can assert directly, instead of a rule someone must
  remember while editing.
- (−) Some legitimate spellings are now refused. `date -d`, `date -f`,
  `ls -L`, `grep -R` and `find -L` are unavailable by design; `echo /etc/passwd`
  and `date /etc/passwd` are refused by M2 even though echoing a path is
  harmless, because M2 cannot tell "echoed" from "opened" and the tool is
  deliberately restricted.
- (−) The tables must be maintained when a *wanted* flag is added, and a table
  entry of kind `path` is a security-relevant declaration, not documentation.
- (~) `find` is validated without short-flag clustering (`-name` is one option,
  not `-n -a -m -e`), so it carries a separate `BUNDLES_SHORT_FLAGS` path.

## Confirmation

- `tests/unit/test_shell_sandbox.py` — the adversarial half. **Each attack test
  was run against the pre-decision implementation and fails there** (8 of 8
  behavioural tests red on `main@8de0edd`); the same file also pins the
  legitimate surface (`_LEGITIMATE_COMMANDS`, 53 parametrised cases) so the
  guard cannot be tightened into uselessness.
- `tests/unit/test_shell_sandbox.py::test_every_declared_path_flag_is_validated`
  — an executable completeness proof: it iterates the live `ShellTool.FLAGS`
  table and requires every `path`-kind flag to refuse an outside value, so a
  table entry added without wiring fails the suite. It also asserts a floor on
  the number of flags exercised, so the sweep cannot silently shrink to nothing.
- `tests/unit/test_shell_sandbox.py::test_no_declared_flag_follows_symlinks` —
  the symlink rule as data, not prose.
- `tests/unit/test_filesystem_boundary.py::test_symlink_is_refused_even_when_its_target_is_inside_the_workspace`
  — the pre-existing tests only covered links that *escape*, which the
  `is_relative_to` check catches on its own; this one covers the case only the
  symlink-component rule provides, and which the shell tool needs because
  validation and `execve` are separate syscalls.
- `scripts/shell_sandbox_mutations.py` — 11 adversarial mutations; the suite
  must turn red for each (11/11 killed). Mutations are anchored on the table
  *definition* lines, not on list items, because `ruff format` rewrites list
  items and a mutation that silently stops applying still reports success.
  Runs as the blocking `shell-mutations` CI job.

## Pros and Cons of the Options

### 1. Extend the forbidden list

- (+) Smallest possible patch; the existing structure is preserved.
- (−) Same unbounded model that already produced two misses in this file.
- (−) Every future coreutils option is a new vulnerability until someone notices.
- (−) Provides no way to prove completeness — the test can only sample.

### 2. Declared flag grammar + independent net (chosen)

- (+) Refusal is the default, so unknown options are safe by construction.
- (+) Completeness is checkable: iterate the table and assert every `path` flag
  is validated.
- (+) `optional_value` / `tuple` kinds make the real getopt semantics explicit,
  removing the class of "the validator's model of the flag differs from the
  program's".
- (−) A larger, security-relevant data table that must be reviewed like code.
- (−) Over-refusal is a real risk, mitigated by pinning the legitimate surface.

### 3. Reimplement the commands in-process

- (+) Removes `subprocess`, `shlex` and the whole flag surface from the module.
- (−) Reimplements `grep`/`find` semantics in Python; correctness drift becomes
  a new, larger class of defect.
- (−) Loses features operators currently rely on (`grep -r`, `-A/-B`,
  `find -newer`) or requires reimplementing them too.
- (−) Evidence does not support a rewrite this size: the limited fix is
  sufficient and provable.

## More Information

- Defect discovery and ablation results: this record's Confirmation section;
  the pre-fix behaviour is reproducible by running the adversarial tests against
  `git show 8de0edd7:src/nexus_ai_agent/tools/system_shell.py`.
- [`../SECURITY.md`](../SECURITY.md) — the boundary inventory this control belongs to.
- [ADR 0006](0006-capability-pack-trust-root.md) — the sibling decision that a
  claim inside an artifact can never be its own authority; 0011 applies the same
  reasoning to an argument vector.
- POSIX.1-2017 §12.2 "Utility Syntax Guidelines" (getopt conventions) — the
  basis for short-flag clustering and for the rule that an option's argument
  belongs to that option.
- GNU coreutils manual, `date` invocation: `-f`/`--file`, `-r`/`--reference`
  are the two file-opening options this record turns from allowed into refused.
- GNU grep manual: `-r` follows symlinks only on the command line, `-R` follows
  all of them — the distinction that makes allowing `-r` while refusing `-R`
  defensible rather than arbitrary.
