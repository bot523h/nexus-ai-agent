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
pattern rejection is not an entropy estimator. The drill uses AES-GCM with
manifest metadata authenticated as AAD, limits whole-file reads, checks SQLite
integrity, and uses a deadline during SQLite online backup. Source and work
folders must be trusted against path replacement: SQLite opens paths separately
from the no-follow file descriptor. This is a real limitation, not TOCTOU
protection for hostile directories.

Files are staged and verified in a private generation directory, then that
one directory is renamed into the work directory and the directory is fsynced.
Independent three-file replacements are not an atomic transaction. Reruns
create distinct generations, rather than a byte-identical idempotent result;
retention and recovery after process or power failure remain unverified.
The drill is not wired into the production maintenance scheduler. Checks of
source-directory permissions and pre/post inode identity detect some path
replacements, but a swap-and-restore between checks remains possible. The
64 MiB absolute read cap is not a bound on total process RSS: encryption and
verification require multiple in-memory copies. Cleanup retry tracking for
blob staging is process-local and is lost on restart. Publication from the
shared ingestion and recovery path uses worker threads; cancellation does not
stop an already-running worker, so existing recovery remains necessary.

## Cryptographic evidence

The existing Creative Artifact Passport uses HMAC-SHA256, not Ed25519. RFC 8032
Ed25519 tests in this branch apply to the repository's pack-signature verifier;
they are not evidence that the passport uses Ed25519. RFC 6962 and DSP test
vectors are local regression evidence, not external certification.

## Remaining gates

Per-module 98% line and branch coverage, the 15-mutation campaign, and full CI
must be run before merging this draft. Until those and the open review findings
are resolved, this document describes work in progress.
