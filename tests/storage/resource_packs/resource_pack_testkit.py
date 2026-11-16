"""Shared helpers for the resource-pack suite (imported by bare module name).

``tests/`` is not a package (see ``tests/architecture/test_test_suite_hygiene.py``),
so the test modules import this file as ``resource_pack_testkit`` — the same
pattern as ``tests/unit/ed25519_testkit.py``.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from nexus_ai_agent.storage.resource_packs import (
    PackTier,
    ResourceBlobEntry,
    ResourcePackManifest,
    build_manifest,
    default_blob_key,
    describe_seed_entry,
)

#: Small but real recipes — each bakes in well under a second.
LUT_RECIPE: dict[str, object] = {
    "recipe": "lut3d_cube",
    "version": 1,
    "params": {
        "title": "NAGAR Teal Orange",
        "size": 9,
        "temperature_k": 5600,
        "contrast_milli": 3500,
        "midpoint_milli": 480,
        "chroma_milli": 1150,
        "hue_shift_mdeg": -4000,
    },
}
ATLAS_RECIPE: dict[str, object] = {
    "recipe": "skin_pore_normal_atlas",
    "version": 1,
    "params": {"width": 32, "height": 32, "seed": 20261115, "pore_cell_px": 8},
}
FORMANT_RECIPE: dict[str, object] = {
    "recipe": "vocal_formant_table",
    "version": 1,
    "params": {"languages": ["fa", "en"], "voice_scale_milli": 1000, "window_sizes": [64, 128]},
}


def blob(label: str, size: int) -> bytes:
    """Deterministic pseudo-random payload of *size* bytes."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{label}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:size])


def remote_entry(path: str, data: bytes) -> ResourceBlobEntry:
    return ResourceBlobEntry(
        relative_path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        acquisition_mode="remote_blob",
    )


@dataclass
class PackMirror:
    """A publisher-side mirror directory laid out like the CDN (``blobs/ab/ab…``)."""

    root: Path
    published: list[ResourceBlobEntry] = field(default_factory=list)

    def add_remote(self, path: str, data: bytes) -> ResourceBlobEntry:
        entry = remote_entry(path, data)
        target = self.root / default_blob_key(entry)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.published.append(entry)
        return entry

    def manifest(
        self,
        pack_id: str,
        version: str,
        entries: list[ResourceBlobEntry],
        *,
        tier: PackTier = "core",
        title_fa: str = "بسته منابع پایه",
        min_free_space_bytes: int | None = None,
    ) -> ResourcePackManifest:
        return build_manifest(
            pack_id=pack_id,
            title_fa=title_fa,
            tier=tier,
            version=version,
            entries=entries,
            min_free_space_bytes=min_free_space_bytes,
        )


def core_seed_entries() -> list[ResourceBlobEntry]:
    return [
        describe_seed_entry("luts/teal_orange.cube", LUT_RECIPE),
        describe_seed_entry("skin/pore_normal_atlas.bin", ATLAS_RECIPE),
        describe_seed_entry("voice/formants.bin", FORMANT_RECIPE),
    ]


class FakeClock:
    def __init__(self, step: float = 0.5) -> None:
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@dataclass
class ServerControl:
    """Mutable knobs and request log of :func:`range_http_server`."""

    root: Path
    range_headers: list[str | None] = field(default_factory=list)
    #: ``Host`` header of every request, in arrival order.
    request_hosts: list[str | None] = field(default_factory=list)
    #: when set, the next response sends only this many body bytes, then hangs up.
    cut_after_bytes: int | None = None
    ignore_range: bool = False
    bogus_content_range: bool = False
    #: when set, the next request is answered ``302 Location: <redirect_to><path>`` (once).
    redirect_to: str | None = None
    base_url: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


_RANGE_RE = re.compile(r"bytes=(\d+)-$")


def _handler_for(control: ServerControl) -> type[BaseHTTPRequestHandler]:
    class RangeHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib name
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
            header = self.headers.get("Range")
            with control.lock:
                control.range_headers.append(header)
                control.request_hosts.append(self.headers.get("Host"))
                cut, control.cut_after_bytes = control.cut_after_bytes, None
                redirect, control.redirect_to = control.redirect_to, None
            if redirect is not None:
                self.send_response(302)
                self.send_header("Location", f"{redirect}{self.path}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path = control.root / self.path.lstrip("/")
            if not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            match = _RANGE_RE.match(header or "")
            if match and not control.ignore_range:
                start = int(match.group(1))
                if start >= len(data):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(data)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = data[start:]
                self.send_response(206)
                shown = start + 1 if control.bogus_content_range else start
                self.send_header("Content-Range", f"bytes {shown}-{len(data) - 1}/{len(data)}")
            else:
                body = data
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if cut is not None:
                self.wfile.write(body[:cut])
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(body)

    return RangeHandler


@contextmanager
def range_http_server(root: Path) -> Iterator[ServerControl]:
    """A real ``http.server`` in a background thread that speaks ``Range``/206."""
    control = ServerControl(root=root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(control))
    control.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield control
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def tree_snapshot(root: Path) -> Mapping[str, bytes]:
    """Every file under *root* with its bytes — to prove nothing changed."""
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
