"""Focused tests for the MCP stdio/Unix-socket boundary.

The relay must preserve arbitrary stream fragmentation and must never turn the MCP
endpoint into a generic subprocess launcher.  Real AF_UNIX tests run on the Linux
deployment target; the Windows development runtime used for this checkout does not
expose AF_UNIX and skips those transport tests.
"""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import sys
import textwrap
from io import BytesIO
from pathlib import Path

import pytest

from kaiba.mcp import socket_bridge

HAS_AF_UNIX = hasattr(socket, "AF_UNIX") and hasattr(asyncio, "start_unix_server")
AF_UNIX_REQUIRED = pytest.mark.skipif(
    not HAS_AF_UNIX, reason="the active Python runtime has no AF_UNIX support"
)


def test_socket_path_is_absolute_and_explicit(tmp_path):
    with pytest.raises(socket_bridge.BridgeError, match="absolute"):
        socket_bridge.resolve_socket_path("relative.sock")
    path = tmp_path / "kaiba.sock"
    assert socket_bridge.resolve_socket_path(path) == path


def test_stale_path_must_already_be_a_socket(tmp_path):
    path = tmp_path / "endpoint"
    path.write_text("keep me", encoding="utf-8")
    with pytest.raises(socket_bridge.BridgeError, match="non-socket"):
        socket_bridge._remove_existing_socket(path)
    assert path.read_text(encoding="utf-8") == "keep me"


def test_production_child_command_is_closed_and_shell_free():
    argv = socket_bridge._default_child_argv()
    assert argv[-2:] == ("-m", "kaiba.mcp.server") or argv[-1:] == ("kaiba.mcp.server",)
    assert "-c" not in argv and "shell" not in argv
    assert all(isinstance(part, str) and part for part in argv)


def test_client_failure_never_writes_diagnostics_to_mcp_stdout(tmp_path):
    output = BytesIO()
    result = socket_bridge.run_client(
        tmp_path / "missing.sock", stdin=BytesIO(), stdout=output
    )
    assert result == 1
    assert output.getvalue() == b""


async def _wait_for_path(path: Path, task: asyncio.Task[None]) -> None:
    for _ in range(200):
        if path.exists():
            return
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    raise AssertionError("MCP socket did not appear")


@AF_UNIX_REQUIRED
@pytest.mark.asyncio
async def test_server_preserves_fragmentation_and_large_frames(tmp_path):
    child = tmp_path / "echo_child.py"
    child.write_text(
        textwrap.dedent(
            """
            import sys
            for line in sys.stdin.buffer:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
            """
        ),
        encoding="utf-8",
    )
    path = tmp_path / "mcp.sock"
    stop = asyncio.Event()
    server_task = asyncio.create_task(
        socket_bridge.serve_async(
            path,
            child_argv=(sys.executable, "-u", str(child)),
            socket_mode=0o660,
            stop_event=stop,
        )
    )
    try:
        await _wait_for_path(path, server_task)
        reader, writer = await asyncio.open_unix_connection(str(path))
        payload = (
            b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
            + b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"blob":"'
            + b"x" * (128 * 1024)
            + b'"}}\n'
        )
        # Deliberately split every request at awkward boundaries.  A framing-aware
        # proxy would be tempted to make assumptions here; a byte relay must not.
        for offset in range(0, len(payload), 137):
            writer.write(payload[offset : offset + 137])
            await writer.drain()
        got = await asyncio.wait_for(reader.readexactly(len(payload)), timeout=5)
        assert got == payload
        writer.close()
        await writer.wait_closed()
        stop.set()
        await asyncio.wait_for(server_task, timeout=5)
        assert not path.exists()
    finally:
        if not server_task.done():
            stop.set()
            await asyncio.wait_for(server_task, timeout=5)


@AF_UNIX_REQUIRED
@pytest.mark.asyncio
async def test_client_mode_round_trips_stdio_bytes(tmp_path):
    path = tmp_path / "mcp.sock"
    payload = b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
    done = asyncio.Event()

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while chunk := await reader.read(socket_bridge.CHUNK_SIZE):
                writer.write(chunk)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            done.set()

    listener = await asyncio.start_unix_server(echo, path=str(path))
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "kaiba.mcp.socket_bridge",
            "client",
            "--socket",
            str(path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        out, err = await asyncio.wait_for(proc.communicate(payload), timeout=5)
        assert proc.returncode == 0, err.decode(errors="replace")
        assert out == payload
        assert err == b""
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        listener.close()
        await listener.wait_closed()
        if "proc" in locals() and proc.returncode is None:
            proc.kill()
            await proc.wait()


@AF_UNIX_REQUIRED
@pytest.mark.asyncio
async def test_socket_mode_is_applied(tmp_path):
    path = tmp_path / "mcp.sock"
    stop = asyncio.Event()
    task = asyncio.create_task(
        socket_bridge.serve_async(path, child_argv=(sys.executable, "-c", ""), stop_event=stop)
    )
    try:
        await _wait_for_path(path, task)
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o660
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


@AF_UNIX_REQUIRED
@pytest.mark.asyncio
async def test_shutdown_does_not_unlink_a_replacement_socket(tmp_path):
    path = tmp_path / "mcp.sock"
    stop = asyncio.Event()
    task = asyncio.create_task(socket_bridge.serve_async(path, stop_event=stop))
    replacement = None
    try:
        await _wait_for_path(path, task)
        path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(path))
        replacement.listen(1)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert path.exists()
    finally:
        if not task.done():
            stop.set()
            await asyncio.wait_for(task, timeout=5)
        if replacement is not None:
            replacement.close()
        with suppress_os_error(path):
            path.unlink()


class suppress_os_error:
    """Small synchronous cleanup context for a platform-dependent socket path."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is OSError:
            return True
        return False


# --------------------------------------------------------------------------------------
# stdout flushing
# --------------------------------------------------------------------------------------


class _CountingBuffer:
    """A stream that buffers like a pipe and reports how often it was flushed.

    ``write`` returns ``None`` deliberately: that is what a Python buffered binary
    stream does once it has taken the whole buffer, and it is the case the old
    ``_write_all`` used as an excuse to return early without flushing.
    """

    def __init__(self) -> None:
        self.buffered = bytearray()
        self.delivered = bytearray()
        self.flushes = 0

    def write(self, data) -> None:  # noqa: ANN001 - mirrors the stdlib signature
        self.buffered.extend(bytes(data))
        return None

    def flush(self) -> None:
        self.flushes += 1
        self.delivered.extend(self.buffered)
        self.buffered.clear()


def test_write_all_flushes_even_when_write_reports_none():
    """Bytes must reach the far side without waiting for the process to exit.

    MCP over stdio is request/response on a pipe the host holds open for the life of
    the server. If a response is only *buffered*, the host sees nothing and blocks
    until its connect timeout. That is exactly what happened on the VPS on 2026-09-21:
    Hermes wrote `initialize`, waited, timed out after 30s three times, parked the
    server, and ran with zero Kaiba tools.

    It was invisible to every manual test because closing stdin or exiting flushes.
    """
    stream = _CountingBuffer()
    socket_bridge._write_all(stream, b'{"jsonrpc":"2.0","id":1,"result":{}}\n')

    assert stream.flushes == 1, "the response was never flushed to the pipe"
    assert stream.buffered == b"", "bytes were left sitting in the buffer"
    assert stream.delivered == b'{"jsonrpc":"2.0","id":1,"result":{}}\n'


def test_write_all_flushes_what_it_managed_to_write_when_the_pipe_breaks():
    """A partial write still delivers. A truncated frame the host can see and reject
    beats a complete frame stuck in our buffer, which it can only time out on."""

    class _BreaksAfterFirstChunk(_CountingBuffer):
        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        def write(self, data):  # noqa: ANN001, ANN202
            self._calls += 1
            if self._calls == 1:
                self.buffered.extend(bytes(data)[:4])
                return 4
            raise BrokenPipeError("stdio output closed")

    stream = _BreaksAfterFirstChunk()
    with pytest.raises(BrokenPipeError):
        socket_bridge._write_all(stream, b"0123456789")

    assert stream.flushes == 1, "the partial write was not flushed on the way out"
    assert stream.delivered == b"0123"
