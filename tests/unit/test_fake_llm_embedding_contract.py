"""The offline fake embedder must be stable across persisted-store restarts."""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


_PERSISTENCE_SCRIPT = r"""
import asyncio
import json
import struct
import sys
from pathlib import Path

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.llm.litellm_provider import build_llm_provider
from nexus_ai_agent.memory.long_term import LongTermMemory

mode, db_path = sys.argv[1:]
settings = Settings(
    _env_file=None,
    llm_routing_enabled=True,
    ollama_model="",
    groq_api_key=None,
    gemini_api_key=None,
    openrouter_api_key=None,
    llama_server_base_url="",
    model_path=str(Path(db_path).with_name("missing-model.gguf")),
)
provider, label = build_llm_provider(settings)
assert isinstance(provider, FakeLLMProvider), label
memory = LongTermMemory(db_path, provider)

async def run():
    if mode == "write":
        await memory.store("thread-A", "target-0")
        await memory.store("thread-A", "I prefer coffee before coding")
        await memory.store("thread-B", "thread-B-only-secret")
        await memory.store("thread-B", "thread-B-noise")
        assert memory._use_vec, "the declared sqlite-vec runtime must be active"
        print(
            json.dumps(
                {
                    "provider": type(provider).__name__,
                    "label": label,
                    "sqlite_vec": memory._use_vec,
                }
            )
        )
        return

    assert mode == "read"
    thread_a = await memory.search("thread-A", "target-0", top_k=1)
    thread_b = await memory.search("thread-B", "thread-B-only-secret", top_k=1)
    thread_a_cannot_read_b = await memory.search(
        "thread-A", "thread-B-only-secret", top_k=10
    )
    thread_b_cannot_read_a = await memory.search("thread-B", "target-0", top_k=10)
    connection = memory._conn_()

    async def stored_vector_matches(thread_id, text):
        row = connection.execute(
            "SELECT embedding FROM memories WHERE thread_id=? AND content=?",
            (thread_id, text),
        ).fetchone()
        assert row is not None
        current = await provider.embed(text)
        return row[0] == struct.pack(f"{len(current)}f", *current)

    matches_a = await stored_vector_matches("thread-A", "target-0")
    matches_b = await stored_vector_matches("thread-B", "thread-B-only-secret")
    print(json.dumps({
        "thread_a": thread_a,
        "thread_b": thread_b,
        "thread_a_cannot_read_b": thread_a_cannot_read_b,
        "thread_b_cannot_read_a": thread_b_cannot_read_a,
        "thread_a_vector_matches": matches_a,
        "thread_b_vector_matches": matches_b,
        "sqlite_vec": memory._use_vec,
    }))

asyncio.run(run())
"""


def _run_in_fresh_process(script: str, hash_seed: str, *args: str) -> str:
    """Run repository code in a new interpreter with a controlled hash salt."""
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = hash_seed
    src_path = str(ROOT / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (src_path, env.get("PYTHONPATH", "")) if part
    )
    result = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return result.stdout


def _printed_payload(stdout: str, key: str) -> dict[str, object]:
    """Find this script's JSON result even when provider logs share stdout."""
    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and key in payload:
            return payload
    raise AssertionError(f"subprocess did not print a JSON payload containing {key!r}")


def _embed_in_fresh_process(text: str, hash_seed: str) -> list[float]:
    script = (
        "import asyncio, json\n"
        "from nexus_ai_agent.llm.fake_llm import FakeLLMProvider\n"
        f"vector = asyncio.run(FakeLLMProvider().embed({text!r}))\n"
        'print(json.dumps(vector, allow_nan=False, separators=(",", ":")))\n'
    )
    return json.loads(_run_in_fresh_process(script, hash_seed))


def test_fake_embedding_is_stable_across_process_hash_seeds() -> None:
    text = "same persisted memory 🔒 café"
    first = _embed_in_fresh_process(text, "1")
    second = _embed_in_fresh_process(text, "2")
    different = _embed_in_fresh_process("different persisted memory 東京", "1")

    assert len(first) == len(second) == len(different) == 384
    assert all(
        math.isfinite(value) and -0.1 <= value <= 0.1
        for vector in (first, second, different)
        for value in vector
    )
    assert first == second, (
        "the same stored text must reconstruct the same fake vector after restart, "
        f"independent of PYTHONHASHSEED; got {first[:3]!r} vs {second[:3]!r}"
    )
    assert first != different, "the embedding must remain content-derived, not constant"


def test_fake_vector_bytes_match_geminis_documented_synthetic_embedding() -> None:
    """The offline fallback shares Gemini's current pseudo-vector contract."""
    from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
    from nexus_ai_agent.llm.gemini_provider import GeminiProvider

    fake = FakeLLMProvider()
    gemini = GeminiProvider(api_key="")

    async def compare_vectors() -> None:
        for text in ("", "same text", "same persisted memory 🔒 café"):
            fake_vector = await fake.embed(text)
            gemini_vector = await gemini.embed(text)
            assert len(fake_vector) == len(gemini_vector) == 384
            assert fake_vector == gemini_vector
            assert struct.pack(f"{len(fake_vector)}f", *fake_vector) == struct.pack(
                f"{len(gemini_vector)}f", *gemini_vector
            )

    asyncio.run(compare_vectors())


def test_fake_embedding_calls_are_thread_independent() -> None:
    first_text = "concurrent synthetic vector"
    second_text = "separate concurrent memory"
    script = f"""\
import asyncio, hashlib, json, struct
from concurrent.futures import ThreadPoolExecutor
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
text_a = {first_text!r}
text_b = {second_text!r}
provider = FakeLLMProvider()
def embed_digest(text):
    vector = asyncio.run(provider.embed(text))
    payload = struct.pack(str(len(vector)) + "f", *vector)
    return text, len(vector), hashlib.sha256(payload).hexdigest()
inputs = [text_a if i % 2 else text_b for i in range(16)]
with ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(embed_digest, inputs))
print(json.dumps(results))
"""
    results = json.loads(_run_in_fresh_process(script, "3"))

    assert len(results) == 16
    assert all(dimension == 384 for _, dimension, _ in results)
    digests_by_text: dict[str, set[str]] = {}
    for text, _, digest in results:
        digests_by_text.setdefault(text, set()).add(digest)
    assert set(digests_by_text) == {first_text, second_text}
    assert all(len(digests) == 1 for digests in digests_by_text.values())
    assert digests_by_text[first_text] != digests_by_text[second_text]


_CONCURRENT_WRITER_SCRIPT = r"""
import asyncio
import json
import sys
import time
from pathlib import Path

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.llm.litellm_provider import build_llm_provider
from nexus_ai_agent.memory.long_term import LongTermMemory

db_path, thread_id, ready_path, start_path = sys.argv[1:]
settings = Settings(
    _env_file=None,
    llm_routing_enabled=True,
    ollama_model="",
    groq_api_key=None,
    gemini_api_key=None,
    openrouter_api_key=None,
    llama_server_base_url="",
    model_path=str(Path(db_path).with_name("missing-model.gguf")),
)
provider, label = build_llm_provider(settings)
assert isinstance(provider, FakeLLMProvider), label
memory = LongTermMemory(db_path, provider)
Path(ready_path).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 15
while not Path(start_path).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("concurrent writer start barrier timed out")
    time.sleep(0.005)

async def write_rows():
    for index in range(20):
        await memory.store(thread_id, f"{thread_id}-memory-{index:02d}")

asyncio.run(write_rows())
assert memory._use_vec, "the declared sqlite-vec runtime must be active"
print(json.dumps({
    "provider": type(provider).__name__,
    "thread": thread_id,
    "rows": 20,
    "sqlite_vec": memory._use_vec,
}))
"""


def test_default_provider_persists_and_reloads_fake_vectors_across_processes(
    tmp_path: Path,
) -> None:
    """Exercise the no-provider factory path and durable vector consumer end to end."""
    db_path = str(tmp_path / "persistent-vector.sqlite")
    writer = _printed_payload(
        _run_in_fresh_process(_PERSISTENCE_SCRIPT, "1", "write", db_path), "provider"
    )
    reader = _printed_payload(
        _run_in_fresh_process(_PERSISTENCE_SCRIPT, "2", "read", db_path), "thread_a"
    )

    assert writer == {
        "provider": "FakeLLMProvider",
        "label": "FakeLLM (no providers configured, no GGUF model found)",
        "sqlite_vec": True,
    }
    assert reader["sqlite_vec"] is True
    assert reader["thread_a"] == ["target-0"]
    assert reader["thread_b"] == ["thread-B-only-secret"]
    assert set(reader["thread_a_cannot_read_b"]) == {
        "target-0",
        "I prefer coffee before coding",
    }
    assert set(reader["thread_b_cannot_read_a"]) == {
        "thread-B-only-secret",
        "thread-B-noise",
    }
    assert "thread-B-only-secret" not in reader["thread_a_cannot_read_b"]
    assert "target-0" not in reader["thread_b_cannot_read_a"]
    assert reader["thread_a_vector_matches"] is True
    assert reader["thread_b_vector_matches"] is True


def test_concurrent_factory_processes_preserve_sqlite_vectors_and_thread_scope(
    tmp_path: Path,
) -> None:
    """Two real factory processes concurrently write and reload one SQLite store."""
    db_path = tmp_path / "concurrent-vector.sqlite"
    start_path = tmp_path / "start"
    threads = ("thread-A", "thread-B")
    workers: list[tuple[str, Path, subprocess.Popen[str]]] = []

    try:
        for index, thread_id in enumerate(threads, start=1):
            ready_path = tmp_path / f"ready-{index}"
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = str(index)
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(ROOT / "src"), env.get("PYTHONPATH", "")) if part
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _CONCURRENT_WRITER_SCRIPT,
                    str(db_path),
                    thread_id,
                    str(ready_path),
                    str(start_path),
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            workers.append((thread_id, ready_path, process))

        deadline = time.monotonic() + 20
        while not all(ready_path.is_file() for _, ready_path, _ in workers):
            for thread_id, ready_path, process in workers:
                if process.poll() is not None and not ready_path.is_file():
                    stdout, stderr = process.communicate()
                    raise AssertionError(
                        f"{thread_id} writer exited before the barrier: {stdout}\n{stderr}"
                    )
            if time.monotonic() >= deadline:
                raise AssertionError("concurrent writers did not reach the start barrier")
            time.sleep(0.01)

        start_path.touch()
        worker_payloads = []
        for thread_id, _, process in workers:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, f"{thread_id} writer failed: {stderr}"
            worker_payloads.append(_printed_payload(stdout, "thread"))
    finally:
        for _, _, process in workers:
            if process.poll() is None:
                process.kill()
            process.communicate()

    assert {payload["thread"] for payload in worker_payloads} == set(threads)
    assert all(payload["provider"] == "FakeLLMProvider" for payload in worker_payloads)
    assert all(
        payload["rows"] == 20 and payload["sqlite_vec"] is True for payload in worker_payloads
    )

    from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
    from nexus_ai_agent.memory.long_term import LongTermMemory

    memory = LongTermMemory(str(db_path), FakeLLMProvider())

    async def read_rows() -> tuple[list[str], list[str]]:
        thread_a = await memory.search("thread-A", "thread-A-memory-00", top_k=100)
        thread_b = await memory.search("thread-B", "thread-B-memory-00", top_k=100)
        return thread_a, thread_b

    thread_a, thread_b = asyncio.run(read_rows())
    expected_a = {f"thread-A-memory-{index:02d}" for index in range(20)}
    expected_b = {f"thread-B-memory-{index:02d}" for index in range(20)}

    assert memory._use_vec is True
    assert set(thread_a) == expected_a
    assert set(thread_b) == expected_b
    assert not any(item.startswith("thread-B-") for item in thread_a)
    assert not any(item.startswith("thread-A-") for item in thread_b)
