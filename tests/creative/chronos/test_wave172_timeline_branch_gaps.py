from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.creative.chronos import timeline
from nexus_ai_agent.creative.chronos.contracts import KernelError


def _payload(duration: str, count: int, *, time_base: str | None = "1/48000") -> dict[str, Any]:
    stream: dict[str, Any] = {
        "index": 0,
        "codec_type": "audio",
        "codec_name": "pcm_s16le",
        "sample_fmt": "s16",
        "sample_rate": "48000",
        "channels": 1,
        "channel_layout": "mono",
        "extradata_hash": "sha256:stable",
    }
    if time_base is not None:
        stream["time_base"] = time_base
    return {
        "format": {"duration": duration},
        "streams": [stream],
        "packets": [{"stream_index": 0, "pts": index, "dts": index} for index in range(count)],
    }


def _install_concat_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: str | None = None,
    time_base: str | None = "1/48000",
) -> tuple[Path, list[Path]]:
    inputs = [tmp_path / "left.wav", tmp_path / "right.wav"]
    for path in inputs:
        path.touch()
    output = tmp_path / "assembled.wav"
    source = _payload("0.1", 1, time_base=time_base)
    assembled = _payload("0.2", 2, time_base=time_base)
    monkeypatch.setattr(
        timeline,
        "_probe",
        lambda path, *_args, **_kwargs: (
            assembled if Path(path).name.startswith("assembled") else source
        ),
    )
    calls = 0

    def run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        is_concat = "concat" in command
        is_boundary = "-ss" in command
        if is_concat:
            Path(command[-1]).write_bytes(b"assembled bytes")
        if (failure == "decode" and calls == 2) or (failure == "boundary" and is_boundary):
            return SimpleNamespace(returncode=1, stderr="injected decode failure", stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(subprocess, "run", run)
    return output, inputs


@pytest.mark.parametrize(
    ("failure", "message"),
    [("decode", "full output decode"), ("boundary", "splice boundary decode")],
)
def test_concat_copy_decode_refusals(
    failure: str, message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, inputs = _install_concat_fixture(tmp_path, monkeypatch, failure=failure)
    with pytest.raises(KernelError, match=message):
        timeline.concat_copy(inputs, output)
    assert not output.exists()


def test_concat_copy_refuses_missing_timebase_after_output_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, inputs = _install_concat_fixture(tmp_path, monkeypatch, time_base=None)
    monkeypatch.setattr(timeline, "_stream_key", lambda _stream: ("audio", "compatible"))
    with pytest.raises(KernelError, match="no packet time base"):
        timeline.concat_copy(inputs, output)


def test_concat_copy_removes_publication_on_post_rename_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, inputs = _install_concat_fixture(tmp_path, monkeypatch)
    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(
        hashlib,
        "sha256",
        lambda *_args: SimpleNamespace(hexdigest=lambda: next(digests)),
    )
    with pytest.raises(KernelError, match="published hash mismatch"):
        timeline.concat_copy(inputs, output)
    assert not output.exists()
