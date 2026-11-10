"""The licence / dependency ledger of a composition.

The ledger keeps **twelve** categories apart, because conflating them is how a
runtime ends up claiming a commercial permission it never had:

======================================  ==========================================
category                                what it describes
======================================  ==========================================
``adapter_code``                        the code this repository adds
``runtime_software``                    the executable that does the work
``library``                             the libraries that executable links
``model``                               a model *architecture/artifact*
``model_weights``                       the trained weights of that model
``dataset``                             the data a model was trained/built on
``input_asset``                         the media the composition consumes
``distribution_restrictions``           where the output may be shipped
``commercial_permission``               whether use is commercially allowed
``attribution_notices``                 the notices that must be reproduced
``copyleft``                            whether obligations propagate
``network_use``                         obligations created by network use
======================================  ==========================================

Two rules the ledger enforces, both of them false-assumption killers:

1. **The tool's licence is not the output's rights.**  FFmpeg being GPL (or
   not) says nothing about whether *the produced media* may be used
   commercially — that is a property of the input asset and of the
   distribution terms, recorded in their own categories.
2. **``UNKNOWN`` stays unknown.**  A category with no record is reported by
   :attr:`LicenseLedger.unknown_categories`, and
   :attr:`LicenseLedger.effective_allow_commercial` is ``False`` unless
   *every* record is both permitted and **verified** (``commercial_use_verified``).

The FFmpeg facts below are *measured*, not remembered: :func:`ffmpeg_ledger`
runs the resolved binary and reads ``-version`` / ``-buildconf`` /
``-encoders`` at runtime, so the ledger states the build that actually
produced the artifact.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin

from .contracts import CATEGORIES, LicenseLedger, LicenseRecord

_VERSION_RE = re.compile(r"ffmpeg version (\S+)")
_CONFIG_LINE_RE = re.compile(r"^configuration:\s*(.*)$", re.MULTILINE)
_ENABLE_RE = re.compile(r"--enable-([a-z0-9_]+)")


def _run(binary: str, args: list[str], *, timeout: int = 60) -> str:
    """Run the allow-listed binary with an argv list and no shell."""
    from nexus_ai_agent.creative.slideshow.ffmpeg import encoder_env

    proc = subprocess.run(  # noqa: S603 - argv list, shell=False, allow-listed binary
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=encoder_env(),
    )
    return f"{proc.stdout or ''}\n{proc.stderr or ''}"


def ffmpeg_build_facts(binary: str | None = None) -> dict[str, Any]:
    """Measure the FFmpeg build that actually runs here.

    Nothing here is assumed: the version string, the configure flags and the
    encoder list are all read back from the binary.
    """
    resolved = binary or resolve_ffmpeg_bin()
    version_text = _run(resolved, ["-version"])
    version_match = _VERSION_RE.search(version_text)
    config_match = _CONFIG_LINE_RE.search(version_text)
    configuration = config_match.group(1) if config_match else ""
    enabled = sorted(set(_ENABLE_RE.findall(configuration)))
    encoders_text = _run(resolved, ["-hide_banner", "-encoders"])
    encoders = {
        "libx264": "libx264" in encoders_text,
        "libx265": "libx265" in encoders_text,
        "libvpx_vp9": "libvpx-vp9" in encoders_text,
        "mpeg4": re.search(r"\bmpeg4\b", encoders_text) is not None,
    }
    return {
        "binary": resolved,
        "version": version_match.group(1) if version_match else "unknown",
        "version_line": version_text.splitlines()[0].strip() if version_text.strip() else "",
        "configuration": configuration,
        "buildconf": configuration,
        "gpl": "--enable-gpl" in configuration,
        "version3": "--enable-version3" in configuration,
        "nonfree": "--enable-nonfree" in configuration,
        "static": "--pkg-config-flags=--static" in configuration,
        "enabled_components": enabled,
        "encoders": encoders,
    }


def ffmpeg_ledger(binary: str | None = None) -> LicenseLedger:
    """The runtime-software / library facts, measured from the binary.

    The *licence of FFmpeg itself* is recorded as ``runtime_software`` and
    ``library``; it deliberately does **not** grant anything about the
    produced media.  The GPL flag is a fact about the tool, and it is
    reported as such.
    """
    facts = ffmpeg_build_facts(binary)
    gpl = bool(facts["gpl"])
    records = (
        LicenseRecord(
            category="runtime_software",
            spdx_id="GPL-2.0-or-later" if gpl else "LGPL-2.1-or-later",
            copyleft=gpl,
            allow_commercial=True,
            commercial_use_verified=True,
            distribution_scope="binary distribution of the tool; the produced media is "
            "governed by the input asset's own terms, not by this record",
            notice=(
                "FFmpeg is invoked as an external binary; the tool's own licence "
                "obligations attach to redistributing the binary, not to the "
                "media it produces."
            ),
            attribution_required=False,
            network_use_obligations="none",
            evidence=(
                f"measured: {facts['version_line']} | "
                f"gpl={facts['gpl']} version3={facts['version3']} "
                f"nonfree={facts['nonfree']} static={facts['static']}"
            ),
        ),
        LicenseRecord(
            category="library",
            spdx_id="mixed: per-component licences of the linked libraries",
            copyleft=gpl,
            allow_commercial=True,
            commercial_use_verified=True,
            distribution_scope="per-library; see the configure flags of the build",
            notice=(
                "The linked libraries carry their own terms; the build's "
                "--enable-* flags are the authoritative list."
            ),
            attribution_required=True,
            network_use_obligations="none",
            evidence=("enabled components: " + ", ".join(facts["enabled_components"]) or "unknown"),
        ),
        LicenseRecord(
            category="adapter_code",
            spdx_id="Repository license (see LICENSE)",
            copyleft=False,
            allow_commercial=True,
            commercial_use_verified=False,
            distribution_scope="this repository's own terms",
            notice="",
            attribution_required=True,
            network_use_obligations="none",
            evidence="in-repository source of nexus_ai_agent.creative.composition",
        ),
    )
    return LicenseLedger(records=records)


def unknown_ledger() -> LicenseLedger:
    """A ledger that states nothing — every category stays unknown."""
    return LicenseLedger(records=())


def input_asset_record(name: str, sha256: str) -> LicenseRecord:
    """One consumed input asset, whose own licence is honestly **unknown**.

    The runtime can measure *what* it consumed (the digest), but it cannot
    establish *what the requester may do* with the result of consuming it.
    The record therefore says ``UNKNOWN`` and is counted as an unresolved
    category by :attr:`LicenseLedger.unknown_categories`, which keeps
    ``effective_allow_commercial`` false.
    """
    return LicenseRecord(
        category="input_asset",
        spdx_id="UNKNOWN",
        copyleft=False,
        allow_commercial=None,
        commercial_use_verified=False,
        distribution_scope="unknown — the requester owns the input's terms",
        notice="",
        attribution_required=False,
        network_use_obligations="none",
        evidence=f"consumed input {name!r}, sha256={sha256}",
    )


def compose_ledger(base: LicenseLedger, input_assets: dict[str, str]) -> LicenseLedger:
    """Add the consumed input assets to the measured tool ledger."""
    records = list(base.records)
    records.extend(
        input_asset_record(name, digest) for name, digest in sorted(input_assets.items())
    )
    return LicenseLedger(records=tuple(records))


def ledger_report(ledger: LicenseLedger) -> dict[str, Any]:
    """A JSON-ready report of the ledger, with the unknowns named."""
    return {
        "categories": list(CATEGORIES),
        "records": [record.model_dump(mode="json") for record in ledger.records],
        "unknown_categories": list(ledger.unknown_categories),
        "effective_allow_commercial": ledger.effective_allow_commercial,
        "commercial_use_verified": all(record.commercial_use_verified for record in ledger.records),
        "note": (
            "The tool's licence never implies the output's rights; the input "
            "asset and the distribution restrictions own that question."
        ),
    }


__all__ = [
    "CATEGORIES",
    "compose_ledger",
    "ffmpeg_build_facts",
    "ffmpeg_ledger",
    "input_asset_record",
    "ledger_report",
    "unknown_ledger",
]
