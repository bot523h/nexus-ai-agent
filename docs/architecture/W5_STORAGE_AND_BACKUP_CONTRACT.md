# W5 storage and local backup contract

## Governance & Multi-Stream Merge Isolation Note

The project owner has directed this W5 branch not to modify `.agents/board.json`
 or `docs/DECISION_LOG.md` while parallel W3-A, W4 and W6 streams are active.
This document records the W5 decision and its limitations for this branch; it
is not a claim that the normal repository decision-log requirement was met.
Reconciliation with that log remains necessary when the exclusive owner permits it.

## Status and threat boundary

This is a draft design and implementation record for PR #356, not a claim of
production readiness. Blob publication uses a private independent candidate,
verifies its digest, and uses a no-clobber hard link. Publishers cooperating
with the repository protocol serialize promotion and rollback using a thread
lock and an advisory file lock with a deadline. The candidate file and the
publication directory are fsynced. Rollback checks the inode under this lock.
Published candidates have owner-only read permissions (0400). This is not
immutability: the owner or a same-UID process can change the mode and modify
the bytes. Integrity verification detects modifications, but cannot prevent them.
The inode check and unlink are separate syscalls: a same-UID process which
ignores the lock can race them and delete another inode. The protocol cannot
fence such a process, and a private directory and file permissions do not
isolate processes with the same UID. All writers must cooperate. This is not
an operating-system-enforced mandatory lock.

## Local encrypted SQLite drill

The local drill checks the configuration separately from production backup.
Only a caller-supplied preflight can produce the status
`LOCAL_DRILL_VERIFIED_REMOTE_PREFLIGHT_OK`; this indicates structural checks,
not remote connectivity or a remote backup. With no preflight the receipt says
`LOCAL_DRILL_VERIFIED_REMOTE_NOT_CHECKED`.

The caller must supply an independently CSPRNG-generated 256-bit key. Simple
pattern rejection is not an entropy estimator. A factory-issued key wrapper can
label OS-CSPRNG or environment inputs; strict mode is the default and rejects raw
or unattested keys. Legacy callers must explicitly opt out. Environment provenance
does not prove how a secret was generated.
The wrapper binds the label and bytes with a process-local authenticator to reject
accidental relabeling via dataclass replacement. Neither Python-private values nor
that authenticator provide isolation against code in the same interpreter; key
wrappers must be reissued after a process restart. Version 2 manifests authenticate the key
source and a domain-separated key fingerprint as AAD; version 1 remains readable.
The drill uses AES-GCM with manifest metadata authenticated as AAD, limits
whole-file reads, checks SQLite
integrity, and uses a deadline during SQLite online backup. Source and work
folders must be trusted against path replacement: SQLite opens paths separately
from the no-follow file descriptor. This is a real limitation, not TOCTOU
protection for hostile directories.

Files are staged and verified in a private generation directory, then that
one directory is renamed into the work directory and the directory is fsynced.
Independent three-file replacements are not an atomic transaction. Reruns
create distinct generations, rather than a byte-identical idempotent result;
recovery after process or power failure remains unverified.

Optional local drill retention authenticates each candidate before counting it,
keeps corrupt or incomplete generations, and uses cooperative shared reader
leases and exclusive pruning locks. It only considers `drill-` generations and
uses an archive mtime plus name tie-break for ordering: this timestamp is not
authenticated and requires a trusted root. Pins and reader-lease snapshots must
be provided by a cooperating caller; an on-disk `.pin` also protects a generation.
Readers using other paths or not taking the reader lease are not protected.
Pruning renames to a private quarantine before descriptor-relative removal; a
crash during removal can leave that quarantine for operator review. Retention
never operates on remote production backups. The encrypted drill is not wired
into the production maintenance scheduler.
The separate structural preflight now gates non-dry-run remote backup and
remote restore before provider construction and I/O. Local restore does not
require cloud settings. SQLite source is accepted only when the canonical
backend policy permits it; configured PostgreSQL URLs must pass TLS checks. PostgreSQL defaults to
`sslmode=verify-full`; `verify-ca` requires an explicit preflight opt-out and does
not verify host identity. A valid preflight is configuration evidence, not remote backup verification.
Checks of
source-directory permissions and pre/post inode identity detect some path
replacements, but a swap-and-restore between checks remains possible. The
64 MiB absolute read cap is not a bound on total process RSS: encryption and
verification require multiple in-memory copies. A failed staging unlink records a durable cleanup hint when the marker write
succeeds. The startup coordinator acquires one exclusive POSIX admission lease,
commits recovery, snapshots all remaining STAGING keys from the database, then
sweeps at most the configured number of marked orphans under the same lease.
Snapshot errors propagate before the sweep. It never guesses that unmarked
staging is orphaned; marker-write failure or a crash before recording the marker
still requires ingest-state recovery. Direct sweeper callers must provide their
own authoritative snapshot under a live exclusive lease.

Stage writes and publication use shared admission. Recovery can borrow an outer
exclusive lease without reacquiring flock. For a threaded publication, a dup of
the same locked open-file description holds the kernel lock until the worker
actually finishes even if its coroutine and outer owner have exited. Cancelled
lock acquisition is cleaned up after it completes. This is cooperative: it is
not a boundary against non-cooperating processes or same-UID replacement of the
lock inode, and SQLite path checks do not prevent a trusted directory from being
swapped while SQLite opens it.

API, polling bot and webhook startup opt in with `asset_library_service` on the
corresponding state/bot_data, or the pair `asset_library_root` and
`asset_library_db_path` on state/bot_data (settings: `NEXUS_ASSET_LIBRARY_ROOT`
and `NEXUS_ASSET_LIBRARY_DB_PATH`). The fallback requires an existing SQLite
file containing the ingest table and uses a recovery-only deny-all authorizer.
A partial configuration fails startup; no configuration is a no-op. The
result is stored as `asset_library_startup_report`. The configured database and
root must refer to the same library and their parent directories must be trusted.
Recovery
compares staged bytes against the durable Tx2 digest and length before
publishing; a mismatch fails the ingest without deleting the staged bytes. A
crash before Tx2 has no durable measured identity to compare against, so this
check cannot establish authenticity of staging data in that earlier window.

## Cryptographic evidence

The existing Creative Artifact Passport uses HMAC-SHA256, not Ed25519. RFC 8032
Ed25519 tests in this branch apply to the repository's pack-signature verifier;
they are not evidence that the passport uses Ed25519. RFC 6962 and DSP test
vectors are local regression evidence, not external certification.

## Remaining gates

Per-module 98% line and branch coverage, the 15-mutation campaign, and full CI
must be run before merging this draft. Until those and the open review findings
are resolved, this document describes work in progress.
