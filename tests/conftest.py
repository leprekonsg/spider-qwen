"""Make the repo root importable so `import spider_qwen` works under pytest."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Loopback hosts (lowercase); 0.0.0.0 is the unspecified/all-interfaces address,
# NOT loopback, so it is deliberately excluded.
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(host) -> bool:
    return isinstance(host, str) and host.lower() in _LOOPBACK


@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch, tmp_path):
    """Redirect the default agent state dir at a per-test tmp dir.

    Code paths that resolve state via ``$SPIDER_QWEN_STATE_DIR`` (the CLI, HTTP
    server, MCP handlers) otherwise fall back to the repo-local ``.spider_qwen``.
    Tests invoking ``main(["run", ...])`` would then share the SAME files; under
    a parallel run those interleave into a corrupt JSON store that crashes
    unrelated tests. Isolating per test makes that impossible. Tests that pass
    ``state_dir`` explicitly are unaffected (the env var is not consulted), and a
    test setting the var itself simply overrides this default.
    """
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))


@pytest.fixture
def no_network(monkeypatch):
    """Block outbound network for the duration of a test.

    Guardrail #5 (offline = zero network) was previously true only "by
    construction" (the mock providers happen not to call out). Requesting this
    fixture PROVES it: any INET connect to a non-loopback host raises, so an
    offline run that completes under it demonstrably reached no remote. Loopback
    connects and loopback name resolution are allowed (asyncio's Windows event
    loop opens a local socketpair -- IPC, not network egress -- and a local mock
    server would resolve 'localhost'); blocking those would be a false positive.
    monkeypatch restores the originals at teardown.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _remote(sock, address) -> bool:
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return False  # AF_UNIX etc. is local IPC, not network egress
        host = address[0] if isinstance(address, (tuple, list)) else address
        return not _is_loopback(host)

    def _make_guard(real_fn, verb):
        def _guard(self, address, *a, **k):
            if _remote(self, address):
                raise RuntimeError(f"network disabled by `no_network`: blocked {verb} to {address!r}")
            return real_fn(self, address, *a, **k)
        return _guard

    def _guard_getaddrinfo(host, *a, **k):
        if _is_loopback(host):  # local name resolution is not egress
            return real_getaddrinfo(host, *a, **k)
        raise RuntimeError(f"network disabled by `no_network`: blocked DNS resolution of {host!r}")

    monkeypatch.setattr(socket.socket, "connect", _make_guard(real_connect, "connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", _make_guard(real_connect_ex, "connect_ex"))
    monkeypatch.setattr(socket, "getaddrinfo", _guard_getaddrinfo)
    yield
