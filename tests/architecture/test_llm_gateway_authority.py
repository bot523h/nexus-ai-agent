"""W2 architecture gates: the Global LLM Gateway is the only path to a provider.

LAW 1 ("one authority") and LAW 2 ("no direct bypass") are *structural* claims:
they say which files may talk to which endpoints, not how one request behaves.
So they are enforced structurally. This module walks the AST of every file under
``src/nexus_ai_agent`` and answers three questions:

1. **Who names a provider?** A file that holds a provider endpoint literal
   (``generativelanguage.googleapis.com``, ``api.openai.com``,
   ``localhost:11434``, …), a provider wire path (``:generateContent``,
   ``/chat/completions``, ``/v1/messages``, …) or a provider SDK import
   (``litellm``, ``openai``, ``google.generativeai``, …) is part of the LLM call
   graph. Everything outside ``llm/gateway/`` that is part of it must appear in
   :data:`LLM_CALL_GRAPH` below, with a role and a reason.

2. **Is the claimed role still true?** Roles are checked, not trusted:
   ``gateway-bound`` must import the gateway *and* resolve it by name;
   ``provider-implementation`` must be imported from inside the gateway package
   (i.e. wrapped by one of its adapters); ``config-default`` must perform no I/O
   at all. A role that stops being true fails here instead of quietly becoming a
   bypass.

3. **Does anybody classify errors by substring?** The banned shape
   (``if "429" in str(error):``) is searched for mechanically across the whole
   package, gateway included (LAW 3: typed truth).

:data:`LLM_CALL_GRAPH` is a ratchet in *both* directions: a new entry fails
until somebody writes down what it is and why it is allowed, and a stale entry
fails too, so a migrated bypass cannot leave its permission behind.
:data:`MAX_PINNED_BYPASSES` is the count of files that genuinely perform LLM
egress outside the authority; it may only go down.

Tests and scripts are out of scope: a test may name an endpoint to assert
against it (``httpx.MockTransport`` doubles), which is not egress.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "nexus_ai_agent"

#: The authority. Files inside it are the *only* ones allowed to dereference a
#: provider endpoint, and they are excluded from the call-graph inventory.
GATEWAY_DIR = "llm/gateway"

#: Host/authority markers for every provider NEXUS can be configured to use.
PROVIDER_ENDPOINTS: tuple[str, ...] = (
    "generativelanguage.googleapis.com",
    "api.openai.com",
    "api.groq.com",
    "openrouter.ai",
    "api.anthropic.com",
    "api.deepseek.com",
    "api.mistral.ai",
    "api.together.xyz",
    "api.perplexity.ai",
    "api.x.ai",
    "api.cohere.ai",
    "localhost:11434",
    "127.0.0.1:11434",
)

#: Wire paths that only exist on an LLM provider's API surface.
PROVIDER_WIRE_PATHS: tuple[str, ...] = (
    ":generateContent",
    ":streamGenerateContent",
    ":embedContent",
    "/chat/completions",
    "/completions",
    "/api/chat",
    "/api/generate",
    "/api/embeddings",
    "/v1/messages",
    "/v1/embeddings",
    "/v1beta/models",
)

#: Provider SDKs. Importing one is how a file reaches a provider without naming
#: an endpoint at all, so it counts as call-graph evidence on its own.
PROVIDER_SDKS: tuple[str, ...] = (
    "litellm",
    "google.generativeai",
    "genai",
    "openai",
    "anthropic",
    "ollama",
    "groq",
)

#: HTTP clients — used to prove a ``config-default`` performs no I/O.
HTTP_CLIENT_MODULES: frozenset[str] = frozenset(
    {"httpx", "aiohttp", "requests", "urllib.request", "urllib3", "http.client"}
)

EVIDENCE_ENDPOINT = "endpoint"
EVIDENCE_WIRE_PATH = "wire-path"
EVIDENCE_SDK = "provider-sdk"

# ── roles in the call graph ────────────────────────────────────────────────
#: The authority itself (never appears in the inventory; it is the exclusion).
ROLE_GATEWAY = "gateway"
#: Names an endpoint only to hand it to the gateway, which performs the call.
ROLE_GATEWAY_BOUND = "gateway-bound"
#: A provider client that the gateway wraps in one of its own adapters (LAW 9).
ROLE_PROVIDER_IMPLEMENTATION = "provider-implementation"
#: Declares an endpoint as configuration data and performs no I/O.
ROLE_CONFIG_DEFAULT = "config-default"
#: Real egress outside the authority: named, justified, and ratcheted.
ROLE_PINNED_BYPASS = "pinned-bypass"


@dataclass(frozen=True)
class CallGraphNode:
    """One file in the LLM call graph: what it is, and why that is allowed."""

    role: str
    reason: str


#: The real inventory (Phase 3 of the mission), mechanically kept honest.
LLM_CALL_GRAPH: dict[str, CallGraphNode] = {
    "config/settings.py": CallGraphNode(
        ROLE_CONFIG_DEFAULT,
        "declares ollama_base_url (http://localhost:11434) and the provider "
        "credentials as settings; performs no I/O — the gateway's registry reads "
        "these values and builds the adapters",
    ),
    "creative/image_gen/gemini_adapter.py": CallGraphNode(
        ROLE_PINNED_BYPASS,
        "image *generation* (predict endpoint, inline base64 image parts, paid-tier "
        "guard, cost estimate per image). The gateway's contract and adapters are "
        "text/modality-scoped for chat and embeddings; migrating image generation "
        "needs a binary-output adapter contract, which is a separate mission. "
        "Fail-closed on paid tier, typed ImageGenerationError, no substring "
        "classification",
    ),
    "creative/slideshow/analysis.py": CallGraphNode(
        ROLE_PINNED_BYPASS,
        "synchronous httpx vision scoring for slide ordering; default provider is "
        "local_heuristic (no network at all) and the hosted leg is fail-closed "
        "behind NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD. Typed AnalysisError, no "
        "substring classification. Migration needs a sync-entry story in the "
        "gateway (its engine is async), which is a separate mission",
    ),
    "features/ai_chat.py": CallGraphNode(
        ROLE_GATEWAY_BOUND,
        "BASE_URL exists only to be passed to gateway_for_credentials(); every "
        "request goes through LLMGateway.execute with a Caller, purpose and "
        "operation",
    ),
    "features/summarizer.py": CallGraphNode(
        ROLE_GATEWAY_BOUND,
        "the Gemini leg goes through gateway_for_credentials(base_url=...); its "
        "own httpx client is the SSRF-guarded fetcher for user-supplied URLs, "
        "which is not LLM egress (pinned by tests/unit/test_summarizer_ssrf.py)",
    ),
    "llm/litellm_provider.py": CallGraphNode(
        ROLE_PROVIDER_IMPLEMENTATION,
        "builds the litellm Router that the gateway wraps in LitellmRouterAdapter "
        "(llm/gateway/registry.py imports it); the Router is a deployment selector "
        "inside one route and is registered with max_attempts=1 so no retry nests",
    ),
    "llm/local_server_provider.py": CallGraphNode(
        ROLE_PROVIDER_IMPLEMENTATION,
        "the llama.cpp/Ollama HTTP provider; llm/gateway/registry.py wraps it in "
        "LegacyProviderAdapter with an explicit error-kind map, so its typed "
        "LlamaServerError becomes TRANSIENT_PROVIDER at the gateway boundary",
    ),
}

#: Files that genuinely perform LLM egress outside the authority. May only shrink.
MAX_PINNED_BYPASSES = 2

#: Words an error-classification-by-substring bug would have to use.
ERROR_KEYWORDS: tuple[str, ...] = (
    "429",
    "400",
    "401",
    "402",
    "403",
    "500",
    "502",
    "503",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "quota",
    "overloaded",
    "resource_exhausted",
    "internal server error",
    "unavailable",
    "timeout",
    "timed out",
    "unauthorized",
    "invalid api key",
    "safety",
    "blocked",
)


# ═══════════════════════════════════════════════════════════════════════════
# Discovery
# ═══════════════════════════════════════════════════════════════════════════


def _source_files(root: Path = SRC) -> Iterator[Path]:
    yield from sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _relative(path: Path, root: Path = SRC) -> str:
    return path.relative_to(root).as_posix()


def imported_modules(tree: ast.AST) -> set[str]:
    """Every module name a file imports (``import a.b`` and ``from a.b import c``)."""

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def call_graph_evidence(source: str) -> frozenset[str]:
    """Why *source* belongs to the LLM call graph (empty when it does not)."""

    tree = ast.parse(source)
    evidence: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal = node.value
            if any(marker in literal for marker in PROVIDER_ENDPOINTS):
                evidence.add(EVIDENCE_ENDPOINT)
            if any(marker in literal for marker in PROVIDER_WIRE_PATHS):
                evidence.add(EVIDENCE_WIRE_PATH)
    modules = imported_modules(tree)
    if any(
        module == sdk or module.startswith(f"{sdk}.") for module in modules for sdk in PROVIDER_SDKS
    ):
        evidence.add(EVIDENCE_SDK)
    return frozenset(evidence)


def discover_call_graph(root: Path = SRC) -> dict[str, frozenset[str]]:
    """Every file that names a provider, keyed by path relative to ``src/``.

    The gateway package itself is excluded: it *is* the authority, so its
    endpoint literals are the point, not a bypass.
    """

    found: dict[str, frozenset[str]] = {}
    for path in _source_files(root):
        relative = _relative(path, root)
        if relative.startswith(f"{GATEWAY_DIR}/"):
            continue
        evidence = call_graph_evidence(path.read_text(encoding="utf-8"))
        if evidence:
            found[relative] = evidence
    return found


def _reads_gateway_authority(source: str) -> bool:
    """True when a file resolves the authority by name, not just by import."""

    return "gateway_for_credentials" in source or "LLMGateway" in source


def _imports_gateway(source: str) -> bool:
    modules = imported_modules(ast.parse(source))
    return any(module.startswith("nexus_ai_agent.llm.gateway") for module in modules)


def _imported_by_the_gateway(module_stem: str) -> bool:
    """True when some file inside ``llm/gateway/`` imports *module_stem*."""

    gateway_root = SRC / GATEWAY_DIR
    pattern = re.compile(rf"\b{re.escape(module_stem)}\b")
    for path in _source_files(gateway_root):
        if pattern.search(path.read_text(encoding="utf-8")):
            return True
    return False


def _nodes(role: str) -> dict[str, CallGraphNode]:
    return {name: node for name, node in LLM_CALL_GRAPH.items() if node.role == role}


# ═══════════════════════════════════════════════════════════════════════════
# Gate 1 — the inventory is exactly the truth (ratchet, both directions)
# ═══════════════════════════════════════════════════════════════════════════


def test_the_llm_call_graph_is_exactly_the_pinned_inventory() -> None:
    """No undocumented path to a provider, and no permission left behind.

    Both directions matter. A file that starts naming an endpoint without being
    written down here is a new bypass; an entry whose file no longer names one is
    a stale permission that would let a future bypass walk straight in.
    """

    discovered = set(discover_call_graph())
    pinned = set(LLM_CALL_GRAPH)
    undocumented = sorted(discovered - pinned)
    stale = sorted(pinned - discovered)
    assert not undocumented, (
        "these files reach a provider and are not in LLM_CALL_GRAPH: "
        f"{undocumented}. Route them through nexus_ai_agent.llm.gateway, or add "
        "them with a role and a reason — and if the reason is 'pinned-bypass', "
        "MAX_PINNED_BYPASSES must be raised in review, which is the point."
    )
    assert not stale, (
        f"these LLM_CALL_GRAPH entries no longer name a provider: {stale}. "
        "Remove them so the ratchet cannot be reused by a future bypass."
    )


#: Method names that put a request on the wire, or build one to be sent.
REQUEST_METHODS: frozenset[str] = frozenset(
    {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "request",
        "stream",
        "send",
        "build_request",
        "acompletion",
        "completion",
        "generate_content",
        "agenerate_content",
        "embed_content",
        "batch_embed_content",
    }
)


def _mentions_provider(node: ast.AST) -> bool:
    """True when an expression holds a provider endpoint or wire-path literal."""

    return any(
        isinstance(sub, ast.Constant)
        and isinstance(sub.value, str)
        and (
            any(marker in sub.value for marker in PROVIDER_ENDPOINTS)
            or any(marker in sub.value for marker in PROVIDER_WIRE_PATHS)
        )
        for sub in ast.walk(node)
    )


def _names_used(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return names


def _provider_tainted_names(tree: ast.AST) -> set[str]:
    """Names whose value is, or is built from, a provider endpoint.

    Assignment-only, iterated to a fixpoint: ``GEMINI_ENDPOINT = "https://…"``
    taints ``GEMINI_ENDPOINT``; ``url = GEMINI_ENDPOINT.format(model=…)`` then
    taints ``url``. That is what lets the gate survive indirection — a call that
    hides its endpoint behind a variable is still a call.
    """

    assignments: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assignments.extend((name, node.value) for name in _names_used(target))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.extend((name, node.value) for name in _names_used(node.target))

    tainted: set[str] = set()
    for _ in range(8):  # a fixpoint; real chains are two or three hops
        before = len(tainted)
        for name, value in assignments:
            if _mentions_provider(value) or (_names_used(value) & tainted):
                tainted.add(name)
        if len(tainted) == before:
            break
    return tainted


def provider_request_sites(source: str) -> list[int]:
    """Line numbers of the requests this file itself sends to an LLM provider.

    A *request* is a call to a transport or SDK method whose target is a provider
    endpoint — the literal is in the arguments, or an argument names something
    tainted by one. Naming an endpoint is deliberately not enough: ``BASE_URL``
    handed to ``gateway_for_credentials`` is a reference, and fetching a
    user-supplied URL over httpx is not LLM egress at all.
    """

    tree = ast.parse(source)
    tainted = _provider_tainted_names(tree)
    sites: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in REQUEST_METHODS:
            continue
        arguments = list(node.args) + [keyword.value for keyword in node.keywords]
        if any(_mentions_provider(argument) for argument in arguments):
            sites.append(node.lineno)
            continue
        used: set[str] = set()
        for argument in arguments:
            used |= _names_used(argument)
        if used & tainted:
            sites.append(node.lineno)
    return sites


def test_only_the_gateway_and_its_wrapped_providers_make_provider_requests() -> None:
    """LAW 2, mechanically: who actually puts a request on the wire.

    Three roles may. The authority (``llm/gateway/``); a provider implementation
    the authority wraps in one of its own adapters, because an adapter is exactly
    where the wire is supposed to live (LAW 9); and a pinned bypass with a
    written reason. Everybody else may *name* an endpoint only to hand it to the
    gateway — which is what ``gateway-bound`` and ``config-default`` mean, and
    what the gates below verify.
    """

    allowed = {ROLE_PINNED_BYPASS, ROLE_PROVIDER_IMPLEMENTATION}
    offenders: list[str] = []
    for path in _source_files():
        relative = _relative(path)
        if relative.startswith(f"{GATEWAY_DIR}/"):
            continue
        sites = provider_request_sites(path.read_text(encoding="utf-8"))
        if not sites:
            continue
        node = LLM_CALL_GRAPH.get(relative)
        if node is None or node.role not in allowed:
            offenders.append(f"{relative}:{sites}")
    assert not offenders, (
        "these files send requests to an LLM provider themselves, around the "
        f"authority: {offenders}. Route the call through nexus_ai_agent.llm.gateway "
        "(LAW 1, LAW 2), or wrap the provider in a gateway adapter (LAW 9). A "
        "bypass needs a written reason here and a raised MAX_PINNED_BYPASSES, "
        "which is a review, not an edit."
    )


def test_a_provider_sdk_import_only_lives_behind_an_adapter() -> None:
    """Importing a provider SDK *is* the wire, so only an adapter may hold it.

    A caller that imports ``litellm``, ``openai`` or ``google.generativeai`` can
    reach a provider without ever writing an endpoint literal, which the request
    detector above cannot see. The role can: an SDK import is allowed only in a
    provider implementation the gateway wraps (LAW 9) or in a pinned bypass with
    a written reason.
    """

    allowed = {ROLE_PROVIDER_IMPLEMENTATION, ROLE_PINNED_BYPASS}
    offenders = [
        f"{relative} (role={LLM_CALL_GRAPH[relative].role})"
        for relative, evidence in sorted(discover_call_graph().items())
        if EVIDENCE_SDK in evidence and LLM_CALL_GRAPH[relative].role not in allowed
    ]
    assert not offenders, (
        f"these files import a provider SDK without being an adapter or a pinned "
        f"bypass: {offenders}. Wrap the SDK in a gateway adapter so policy, "
        "observability and typed errors apply (LAW 1, LAW 9)."
    )


def test_the_request_detector_is_live() -> None:
    """The gate above must be able to fire on a bypass nobody has written yet."""

    direct = """
import httpx

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent"


async def ask(prompt: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(ENDPOINT, json={"prompt": prompt})
    return response.text
"""
    assert provider_request_sites(direct), "an endpoint constant used as a request target"

    hidden = """
import httpx


async def ask(prompt: str) -> str:
    url = "https://api.openai.com/v1" + "/chat/completions"
    async with httpx.AsyncClient() as client:
        return (await client.request("POST", url, json={})).text
"""
    assert provider_request_sites(hidden), "indirection through a variable must not hide a call"

    sdk = """
import litellm


async def ask(prompt: str) -> str:
    return str(await litellm.acompletion(model="gpt-4o", messages=[]))
"""
    assert not provider_request_sites(sdk), (
        "an SDK call with no provider literal is call-graph evidence (the SDK "
        "import), not a proven request site — the inventory gate owns that case"
    )

    innocent = """
import httpx


async def fetch(url: str) -> str:
    async with httpx.AsyncClient() as client:
        return (await client.get(url)).text
"""
    assert not provider_request_sites(innocent), "fetching a user URL is not LLM egress"

    handed_over = """
from nexus_ai_agent.llm.gateway.registry import gateway_for_credentials

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def build(key: str):
    return gateway_for_credentials(key, "gemini-2.0-flash", base_url=BASE_URL)
"""
    assert not provider_request_sites(handed_over), (
        "naming an endpoint and handing it to the authority is not a call"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Gate 2 — the pinned bypasses are named, justified and ratcheted
# ═══════════════════════════════════════════════════════════════════════════


def test_the_pinned_bypass_ratchet_has_not_grown() -> None:
    """Two files egress outside the authority, and that number may only fall."""

    bypasses = _nodes(ROLE_PINNED_BYPASS)
    assert len(bypasses) == MAX_PINNED_BYPASSES, (
        f"{len(bypasses)} pinned bypasses but MAX_PINNED_BYPASSES is "
        f"{MAX_PINNED_BYPASSES}. If a bypass was migrated, lower the constant — "
        "that is the ratchet paying off. If one was added, this gate is telling "
        "you that NEXUS now has another path around its own LLM authority."
    )
    assert sorted(bypasses) == [
        "creative/image_gen/gemini_adapter.py",
        "creative/slideshow/analysis.py",
    ], "the pinned bypass set changed; every entry must be reviewed by name"


def test_every_pinned_bypass_carries_a_real_justification() -> None:
    """A bypass without a written reason is just an undocumented call."""

    for relative, node in _nodes(ROLE_PINNED_BYPASS).items():
        assert len(node.reason) >= 120, f"{relative}: the reason is too thin to review"
        assert (SRC / relative).exists(), f"{relative} is pinned but missing"
        source = (SRC / relative).read_text(encoding="utf-8")
        # Typed truth still applies outside the authority: a bypass must fail
        # with its own error type, not with a string somebody has to parse.
        assert re.search(r"\bclass \w*Error\b|\w+Error\b", source), (
            f"{relative} raises no typed error of its own"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Gate 3 — claimed roles are verified, not trusted
# ═══════════════════════════════════════════════════════════════════════════


def test_a_gateway_bound_caller_really_routes_through_the_gateway() -> None:
    """``gateway-bound`` means it resolves the authority, not that it mentions it."""

    for relative in _nodes(ROLE_GATEWAY_BOUND):
        source = (SRC / relative).read_text(encoding="utf-8")
        assert _imports_gateway(source), f"{relative} no longer imports the gateway package"
        assert _reads_gateway_authority(source), (
            f"{relative} imports the gateway but never resolves LLMGateway or "
            "gateway_for_credentials — the endpoint constant it holds is not "
            "being handed to the authority (LAW 1)"
        )


def test_a_provider_implementation_is_wrapped_by_a_gateway_adapter() -> None:
    """A provider client is legal only because the gateway owns it (LAW 9).

    The check is the import edge itself: if ``llm/gateway/`` stops importing a
    provider module, that module is no longer behind an adapter — it is either
    dead code or a bypass, and both need an answer.
    """

    for relative in _nodes(ROLE_PROVIDER_IMPLEMENTATION):
        stem = Path(relative).stem
        assert _imported_by_the_gateway(stem), (
            f"{relative} is classified as a provider implementation, but nothing "
            "inside llm/gateway/ imports it: no adapter wraps it, so its calls "
            "would not carry gateway policy (LAW 9)"
        )


def test_a_config_default_performs_no_io() -> None:
    """A settings file may name an endpoint; it may not call one."""

    for relative in _nodes(ROLE_CONFIG_DEFAULT):
        source = (SRC / relative).read_text(encoding="utf-8")
        modules = imported_modules(ast.parse(source))
        assert not (modules & HTTP_CLIENT_MODULES), (
            f"{relative} imports an HTTP client: configuration must stay data"
        )
        assert not (modules & set(PROVIDER_SDKS)), (
            f"{relative} imports a provider SDK: configuration must stay data"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Gate 4 — LAW 3: nobody classifies an error by substring
# ═══════════════════════════════════════════════════════════════════════════


def _wraps_a_stringification(node: ast.AST) -> bool:
    """True when *node* turns something into text (``str(x)``, ``f"{x}"``, …)."""

    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "str":
            return True
        if isinstance(sub, ast.JoinedStr):
            return True
    return False


def substring_classifications(source: str) -> list[tuple[int, str]]:
    """The banned shape: an error keyword literal tested against stringified data.

    ``if "429" in str(error):`` and its cousins (``"rate limit" in
    str(exc).lower()``, ``"quota" in f"{error}"``) are how a typed error taxonomy
    rots: the classification depends on a provider's prose, which changes without
    notice and says nothing about retryability.
    """

    tree = ast.parse(source)
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            continue
        pairs = [(node.left, node.comparators[0])]
        pairs += list(zip(node.comparators, node.comparators[1:], strict=False))
        for literal, other in pairs:
            if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
                literal, other = other, literal
            if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
                continue
            if not any(keyword in literal.value.lower() for keyword in ERROR_KEYWORDS):
                continue
            if _wraps_a_stringification(other):
                findings.append((node.lineno, literal.value))
    return findings


def test_nothing_in_the_package_classifies_an_error_by_substring() -> None:
    """LAW 3, structurally: no ``if "429" in str(error)`` anywhere in ``src/``.

    The gateway is included. An authority that scans messages is worse than a
    caller that does, because every caller inherits the guess.
    """

    offenders: list[str] = []
    for path in _source_files():
        findings = substring_classifications(path.read_text(encoding="utf-8"))
        for lineno, literal in findings:
            offenders.append(f"{_relative(path)}:{lineno} ({literal!r})")
    assert not offenders, (
        "substring-based error classification: "
        + ", ".join(offenders)
        + ". Classify on the typed error (kind/status/retry-after), never on prose."
    )


def test_the_substring_detector_is_live() -> None:
    """A gate that cannot fire is not a gate.

    These are the shapes the ban exists for; if a refactor of the detector makes
    them pass, the gate above has silently stopped protecting anything.
    """

    hostile = [
        'if "429" in str(error):\n    retry = True\n',
        'if "rate limit" in str(exc).lower():\n    kind = "throttle"\n',
        'if "RESOURCE_EXHAUSTED" in f"{error}":\n    sleep(60)\n',
        'if "too many requests" in str(response.text):\n    backoff()\n',
        'if "quota" not in str(detail):\n    raise\n',
    ]
    for snippet in hostile:
        assert substring_classifications(snippet), f"detector missed: {snippet!r}"

    benign = [
        "if kind is LLMErrorKind.RATE_LIMITED:\n    retry = True\n",
        "if error.status_code == 429:\n    retry = True\n",
        'if "rate limit" in allowed_keywords:\n    pass\n',
        "if retry_after is not None and retry_after > 300:\n    pass\n",
    ]
    for snippet in benign:
        assert not substring_classifications(snippet), f"detector false positive: {snippet!r}"


def test_the_call_graph_detector_is_live() -> None:
    """Discovery must flag a brand-new direct call, not just the ones we know."""

    fresh_bypass = '''
"""A caller that decided to phone a provider itself."""

import httpx


async def ask(prompt: str, key: str) -> str:
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json={"contents": [{"parts": [{"text": prompt}]}]})
    return response.text
'''
    evidence = call_graph_evidence(fresh_bypass)
    assert EVIDENCE_ENDPOINT in evidence
    assert EVIDENCE_WIRE_PATH in evidence

    sdk_only = """
import litellm


async def ask(prompt: str) -> str:
    return await litellm.acompletion(model="gpt-4o", messages=[{"role": "user", "content": p}])
"""
    assert EVIDENCE_SDK in call_graph_evidence(sdk_only)

    innocent = """
import httpx


async def fetch_page(url: str) -> str:
    async with httpx.AsyncClient() as client:
        return (await client.get(url)).text
"""
    assert not call_graph_evidence(innocent), "an unrelated HTTP caller is not LLM egress"


# Constructor ratchet is separate from endpoint detection. These entries are
# inventory, NOT proof that every entry is a single authority. The LiteLLM
# compatibility constructor remains a release blocker in the golden report.
GATEWAY_CONSTRUCTION_SITES = {
    ("llm/gateway/engine.py", "GatewayBuilder.build"),
    ("llm/gateway/registry.py", "build_gateway_from_settings"),
    ("llm/gateway/registry.py", "gateway_for_credentials"),
    ("llm/litellm_provider.py", "LiteLLMRoutingProvider.gateway"),
}


def _gateway_constructors(source: str) -> set[str]:
    tree = ast.parse(source)
    aliases = {"LLMGateway"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name == "LLMGateway"
            )
    found: set[str] = set()

    def walk(node: ast.AST, scope: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope += (node.name,)
        if isinstance(node, ast.Call):
            called = node.func
            if (isinstance(called, ast.Name) and called.id in aliases) or (
                isinstance(called, ast.Attribute) and called.attr == "LLMGateway"
            ):
                found.add(".".join(scope) or "<module>")
        for child in ast.iter_child_nodes(node):
            walk(child, scope)

    walk(tree)
    return found


def test_no_new_gateway_constructor_authority_can_hide_behind_an_alias():
    found = {
        (str(path.relative_to(SRC)), scope)
        for path in SRC.rglob("*.py")
        for scope in _gateway_constructors(path.read_text(encoding="utf-8"))
    }
    assert found == GATEWAY_CONSTRUCTION_SITES, (
        "Unreviewed constructor or stale exception: " + repr(found ^ GATEWAY_CONSTRUCTION_SITES)
    )


def test_gateway_constructor_detector_is_live():
    assert _gateway_constructors("from gateway import LLMGateway as Hidden\nHidden()") == {
        "<module>"
    }
    assert _gateway_constructors("def bypass():\n return engine.LLMGateway()") == {"bypass"}
    assert not _gateway_constructors("def pure():\n return 'LLMGateway()'")
