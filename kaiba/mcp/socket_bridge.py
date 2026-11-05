"""Bridge the stdio MCP server to a protected Unix-domain socket.

The Python MCP SDK deliberately exposes stdio and HTTP transports, but not a Unix
domain socket transport.  Production Kaiba services run as separate Unix users, so
Hermes cannot open the core database and must reach the MCP process through the
``kaiba-mcp-ipc`` socket group.  This module keeps the SDK's well-tested stdio
transport and moves its bytes across that boundary without inspecting or rewriting
JSON-RPC messages.

There are two explicit modes:

``serve``
    The core-side listener.  Each client gets a fresh stdio MCP child, which keeps
    session state isolated and makes disconnect cleanup straightforward.

``client``
    The Hermes-side adapter.  Hermes still launches a normal stdio MCP command; this
    process connects that stdin/stdout pair to the core Unix socket.

The relay is intentionally opaque.  MCP stdio messages are newline-delimited JSON-RPC
but stream fragmentation is legal, so parsing here would add a second protocol parser
and create a framing/security mismatch.  Logs go to stderr only.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import stat
import sys
import threading
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO

log = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
DEFAULT_MAX_CLIENTS = 8
DEFAULT_SOCKET_MODE = 0o660


class BridgeError(RuntimeError):
    """A local bridge setup or lifecycle error."""


def resolve_socket_path(value: str | os.PathLike[str] | None = None) -> Path:
    """Resolve and validate an absolute Unix socket path.

    Relative paths are rejected because a service's working directory is mutable and
    would make the IPC endpoint ambiguous.  ``KAIBA_MCP_SOCKET`` is the only implicit
    source, which keeps the Hermes profile and the systemd unit auditable.
    """

    raw = value if value is not None else os.environ.get("KAIBA_MCP_SOCKET")
    if not raw:
        raise BridgeError("socket path is required (pass --socket or set KAIBA_MCP_SOCKET)")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise BridgeError("socket path must be absolute")
    return path


def _remove_existing_socket(path: Path) -> None:
    """Remove one stale socket, refusing to unlink any other filesystem object."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise BridgeError(f"refusing to replace non-socket endpoint: {path}")
    path.unlink()


def _apply_socket_permissions(path: Path, mode: int, group: str | None) -> None:
    if not 0 <= mode <= 0o777:
        raise BridgeError("socket mode must be between 0000 and 0777")
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise BridgeError("could not set socket mode") from exc
    if group is None:
        return
    if os.name != "posix":
        raise BridgeError("socket groups are only supported on POSIX hosts")
    import grp

    try:
        gid = grp.getgrnam(group).gr_gid
        os.chown(path, -1, gid)
    except (KeyError, OSError) as exc:
        raise BridgeError(f"could not set socket group: {group}") from exc


def _default_child_argv() -> tuple[str, ...]:
    """The only production child command; no shell or model-supplied command exists."""

    return (sys.executable, "-I", "-B", "-m", "kaiba.mcp.server")


def _child_environment() -> dict[str, str]:
    """Keep core credentials in the child while preventing recursive socket mode."""

    env = os.environ.copy()
    env.pop("KAIBA_MCP_SOCKET", None)
    env["KAIBA_MCP_TRANSPORT"] = "stdio"
    env["KAIBA_MCP_CHILD"] = "1"
    return env


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Stop a child without allowing a broken client to leak a process."""

    if process.returncode is not None:
        return
    with suppress(ProcessLookupError, OSError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        with suppress(ProcessLookupError, OSError):
            process.kill()
        with suppress(Exception):
            await process.wait()


async def _pump_client_to_child(
    reader: asyncio.StreamReader, stdin: asyncio.StreamWriter
) -> None:
    try:
        while True:
            chunk = await reader.read(CHUNK_SIZE)
            if not chunk:
                break
            stdin.write(chunk)
            await stdin.drain()
    finally:
        with suppress(Exception):
            stdin.close()
        with suppress(Exception):
            await stdin.wait_closed()


async def _pump_child_to_client(
    stdout: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        while True:
            chunk = await stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    finally:
        await _close_writer(writer)


async def _serve_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    child_argv: Sequence[str],
    children: set[asyncio.subprocess.Process],
) -> None:
    """Serve one opaque byte stream and tear down both ends together."""

    process: asyncio.subprocess.Process | None = None
    incoming: asyncio.Task[None] | None = None
    outgoing: asyncio.Task[None] | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *child_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,  # diagnostics belong in the service journal, never the MCP wire
            env=_child_environment(),
        )
        assert process.stdin is not None and process.stdout is not None
        children.add(process)
        incoming = asyncio.create_task(_pump_client_to_child(reader, process.stdin))
        outgoing = asyncio.create_task(_pump_child_to_client(process.stdout, writer))

        done, _ = await asyncio.wait(
            (incoming, outgoing), return_when=asyncio.FIRST_COMPLETED
        )
        # A client half-close is normal: the child may still flush its final response.
        if incoming in done and not outgoing.done():
            if incoming.exception() is None:
                with suppress(Exception):
                    await outgoing
            else:
                outgoing.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await outgoing
        else:
            # Child/server or socket output ended first.  Do not leave a stdin pump
            # blocked on a client that no longer has a server to talk to.
            if incoming is not None and not incoming.done():
                incoming.cancel()
                with suppress(asyncio.CancelledError):
                    await incoming
    except (BrokenPipeError, ConnectionError, asyncio.IncompleteReadError):
        log.debug("MCP bridge session closed", exc_info=False)
    except Exception as exc:  # noqa: BLE001 - a client must not crash the listener
        log.warning("MCP bridge session failed: %s", type(exc).__name__)
    finally:
        for task in (incoming, outgoing):
            if task is not None and not task.done():
                task.cancel()
        for task in (incoming, outgoing):
            if task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if process is not None:
            await _terminate(process)
            children.discard(process)
        await _close_writer(writer)


async def serve_async(
    socket_path: str | os.PathLike[str] | None = None,
    *,
    socket_group: str | None = None,
    socket_mode: int = DEFAULT_SOCKET_MODE,
    max_clients: int = DEFAULT_MAX_CLIENTS,
    child_argv: Sequence[str] | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the AF_UNIX listener until cancelled or ``stop_event`` is set.

    ``child_argv`` is an in-process test seam.  The CLI never accepts a command and
    always uses :func:`_default_child_argv`, preventing a client or model from turning
    this boundary into an arbitrary subprocess launcher.
    """

    if max_clients < 1:
        raise BridgeError("max_clients must be at least one")
    path = resolve_socket_path(socket_path)
    if not hasattr(socket, "AF_UNIX"):
        raise BridgeError("this Python build has no AF_UNIX support")

    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    _remove_existing_socket(path)
    owned_socket: tuple[int, int] | None = None
    try:
        argv = tuple(child_argv or _default_child_argv())
        children: set[asyncio.subprocess.Process] = set()
        active: set[asyncio.Task[None]] = set()
        active_count = 0

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal active_count
            if active_count >= max_clients:
                await _close_writer(writer)
                return
            active_count += 1
            task = asyncio.current_task()
            if task is not None:
                active.add(task)
            try:
                await _serve_session(reader, writer, argv, children)
            finally:
                active_count -= 1
                if task is not None:
                    active.discard(task)

        server = await asyncio.start_unix_server(handle, path=str(path), backlog=max_clients)
        bound = path.stat()
        owned_socket = (bound.st_dev, bound.st_ino)
        try:
            _apply_socket_permissions(path, socket_mode, socket_group)
            if stop_event is None:
                await server.serve_forever()
            else:
                await stop_event.wait()
        finally:
            server.close()
            await server.wait_closed()
            for task in tuple(active):
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            for process in tuple(children):
                await _terminate(process)
            children.clear()
    finally:
        # Do not remove a replacement endpoint created by another process.
        with suppress(FileNotFoundError, OSError):
            current = path.lstat()
            if (
                owned_socket is not None
                and (current.st_dev, current.st_ino) == owned_socket
                and stat.S_ISSOCK(current.st_mode)
            ):
                path.unlink()


def serve(
    socket_path: str | os.PathLike[str] | None = None,
    *,
    socket_group: str | None = None,
    socket_mode: int = DEFAULT_SOCKET_MODE,
    max_clients: int = DEFAULT_MAX_CLIENTS,
) -> None:
    """Synchronous CLI entry point for the core-side listener."""

    asyncio.run(
        serve_async(
            socket_path,
            socket_group=socket_group,
            socket_mode=socket_mode,
            max_clients=max_clients,
        )
    )


def _binary_stream(stream: BinaryIO | Any) -> Any:
    return getattr(stream, "buffer", stream)


def _write_all(stream: Any, data: bytes) -> None:
    """Write every byte and **always** flush before returning.

    The flush is not an optimisation, it is the protocol. MCP over stdio is a
    request/response stream on a pipe that the host holds open for the life of the
    server: the host writes a request and then blocks reading our stdout. Python
    block-buffers a pipe, so bytes merely handed to the buffer are invisible to the
    host until something flushes them -- and with stdin still open, nothing ever does.

    That is precisely the bug this replaced. The old version returned early when
    ``write()`` reported ``None``, on the reasoning that a buffered stream returns
    ``None`` once it has consumed the whole buffer. True, and beside the point:
    *consumed into a buffer* is not *delivered down the pipe*. It skipped the flush, so
    Hermes wrote `initialize`, waited, and timed out after exactly 30 s -- three times,
    then parked the server and ran with no Kaiba tools at all.

    It hid well. Every manual test closed stdin or let the process exit, and both flush
    on the way out, so the bridge looked perfect from the command line and failed only
    under the one caller that matters. Diagnosed 2026-09-21 by holding stdin open and
    watching nothing arrive for 20 s.
    """
    view = memoryview(data)
    try:
        while view:
            written = stream.write(view)
            if written is None:
                # A buffered stream reports None once it has taken the whole buffer.
                break
            if written <= 0:
                raise BrokenPipeError("stdio output closed")
            view = view[written:]
    finally:
        # In a `finally` so a partial write still delivers what it managed to hand over;
        # the host can act on a truncated frame, but never on bytes stuck in our buffer.
        flush = getattr(stream, "flush", None)
        if flush is not None:
            flush()


def run_client(
    socket_path: str | os.PathLike[str] | None = None,
    *,
    stdin: BinaryIO | Any = sys.stdin,
    stdout: BinaryIO | Any = sys.stdout,
) -> int:
    """Relay the current process's stdio to the core socket, byte-for-byte."""

    if not hasattr(socket, "AF_UNIX"):
        print("kaiba MCP socket unavailable: AF_UNIX is unsupported", file=sys.stderr)
        return 1
    path = resolve_socket_path(socket_path)
    input_stream = _binary_stream(stdin)
    output_stream = _binary_stream(stdout)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except OSError as exc:
        with suppress(Exception):
            sock.close()
        print(f"kaiba MCP socket unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1

    stop = threading.Event()
    input_done = threading.Event()
    output_done = threading.Event()
    errors: list[BaseException] = []

    # `read(n)` on a buffered binary stream is read-*exactly*-n: it blocks until it has n
    # bytes or hits EOF. An MCP request is a couple of hundred bytes on a pipe the host
    # keeps open forever, so `read(65536)` waited for 64 KiB that was never coming and the
    # request was never forwarded to the socket at all. `read1` returns whatever one
    # underlying read produces, which is the framing MCP actually has.
    #
    # This is why every manual test passed: `printf ... | client` closes stdin, EOF makes
    # the short read return, and the whole exchange completes. Hold stdin open -- which is
    # exactly what Hermes does -- and it deadlocks until the 30 s connect timeout.
    # Diagnosed 2026-09-21 after proving the server streamed fine over a raw socket.
    read_available = getattr(input_stream, "read1", None) or input_stream.read

    def input_pump() -> None:
        try:
            while not stop.is_set():
                data = read_available(CHUNK_SIZE)
                if not data:
                    break
                sock.sendall(data)
            with suppress(OSError):
                sock.shutdown(socket.SHUT_WR)
        except BaseException as exc:  # noqa: BLE001 - pass the failure to the owner
            errors.append(exc)
        finally:
            input_done.set()

    def output_pump() -> None:
        try:
            while not stop.is_set():
                data = sock.recv(CHUNK_SIZE)
                if not data:
                    break
                _write_all(output_stream, data)
        except BaseException as exc:  # noqa: BLE001 - pass the failure to the owner
            errors.append(exc)
        finally:
            output_done.set()

    in_thread = threading.Thread(target=input_pump, name="kaiba-mcp-stdin", daemon=True)
    out_thread = threading.Thread(target=output_pump, name="kaiba-mcp-stdout", daemon=True)
    in_thread.start()
    out_thread.start()
    try:
        while not output_done.is_set():
            if input_done.is_set():
                # The server should close after stdin EOF.  Waiting here preserves any
                # final response bytes instead of dropping them on an eager exit.
                out_thread.join()
                break
            output_done.wait(0.05)
    finally:
        stop.set()
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        sock.close()
    return 1 if errors else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kaiba MCP stdio/Unix-socket bridge")
    sub = parser.add_subparsers(dest="mode", required=True)
    serve_parser = sub.add_parser("serve", help="serve MCP children on an AF_UNIX socket")
    serve_parser.add_argument("--socket", dest="socket_path")
    serve_parser.add_argument("--socket-group")
    serve_parser.add_argument("--socket-mode", type=lambda value: int(value, 8), default=DEFAULT_SOCKET_MODE)
    serve_parser.add_argument("--max-clients", type=int, default=DEFAULT_MAX_CLIENTS)
    client_parser = sub.add_parser("client", help="connect stdio to an MCP Unix socket")
    client_parser.add_argument("--socket", dest="socket_path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; diagnostics never share stdout with the MCP stream."""

    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        if args.mode == "serve":
            serve(
                args.socket_path,
                socket_group=args.socket_group,
                socket_mode=args.socket_mode,
                max_clients=args.max_clients,
            )
            return 0
        return run_client(args.socket_path)
    except (BridgeError, OSError, ValueError) as exc:
        print(f"kaiba MCP bridge failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess contract tests
    raise SystemExit(main())
