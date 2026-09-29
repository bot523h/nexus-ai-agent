"""The offline fake embedder must be stable across persisted-store restarts."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
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
