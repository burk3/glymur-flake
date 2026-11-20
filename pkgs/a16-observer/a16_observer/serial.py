"""a16-serial: the one owner of the observer host's end of the A16 serial cable.

- Opens the tty 115200 8N1 raw and sets TIOCEXCL, so any other non-root open()
  fails with EBUSY (no more split bytes between two `cat`s).
- Logs every received line to the journal (SYSLOG_IDENTIFIER=a16-serial); a
  partial line is flushed after 200 ms of silence with A16_SERIAL_PARTIAL=1.
- Serves a unix stream socket. A client sends one JSON line:
    {"mode": "read", "backlog": 4096}
    {"mode": "write", "holder": "agent-x", "force": false, "backlog": 0}
    {"mode": "status"}
  and gets one JSON line back ({"ok": true, ...} or {"ok": false, "error": ...}).
  Readers and the writer then receive every byte from the A16 (after the
  requested backlog). At most one client holds the write lock; its bytes go to
  the A16 and are logged as SYSLOG_IDENTIFIER=a16-serial-tx with A16_HOLDER.
  "force" takes the lock from the current writer, whose connection is closed.
- Survives the device going away (unplug, EIO): it closes, retries the open
  every second, and reopens on SIGHUP (sent by the host's attach service after
  a replug). Clients stay connected through that.
"""

from __future__ import annotations

import asyncio
import fcntl
import grp
import json
import os
import signal
import socket
import struct
import termios
import time

from .journal import ERR, INFO, NOTICE, WARNING, Journal, escape

BAUDS = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
    460800: termios.B460800,
    921600: termios.B921600,
}

MAX_LINE = 4096
CLIENT_BUFFER_LIMIT = 1 << 20


def configure_tty(fd: int, baud: int) -> None:
    attrs = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = attrs
    # cfmakeraw
    iflag &= ~(
        termios.IGNBRK
        | termios.BRKINT
        | termios.PARMRK
        | termios.ISTRIP
        | termios.INLCR
        | termios.IGNCR
        | termios.ICRNL
        | termios.IXON
        | termios.IXOFF
    )
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    cflag &= ~termios.HUPCL
    cc = list(cc)
    cc[termios.VMIN] = 1
    cc[termios.VTIME] = 0
    speed = BAUDS[baud]
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc])


def peer_uid(sock: socket.socket) -> int | None:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    except OSError:
        return None


class LineAssembler:
    """Splits a byte stream into lines; a partial line is emitted by flush()."""

    def __init__(self, emit):
        self.emit = emit  # emit(bytes, partial: bool)
        self.buf = bytearray()

    def feed(self, data: bytes) -> None:
        self.buf += data
        while True:
            i = self.buf.find(b"\n")
            if i < 0:
                break
            line = bytes(self.buf[:i])
            del self.buf[: i + 1]
            self.emit(line, False)
        while len(self.buf) >= MAX_LINE:
            line = bytes(self.buf[:MAX_LINE])
            del self.buf[:MAX_LINE]
            self.emit(line, True)

    def flush(self) -> None:
        if self.buf:
            line = bytes(self.buf)
            self.buf.clear()
            self.emit(line, True)


class Client:
    def __init__(self, daemon: "SerialDaemon", reader, writer):
        self.d = daemon
        self.reader = reader
        self.writer = writer
        self.mode = None
        self.holder = None
        sock = writer.get_extra_info("socket")
        self.uid = peer_uid(sock) if sock is not None else None
        self.closed = False
        self.tx = LineAssembler(self._log_tx)
        self.tx_timer = None

    def name(self) -> str:
        return f"{self.holder or '?'} (uid {self.uid})"

    def send(self, data: bytes) -> None:
        if self.closed:
            return
        transport = self.writer.transport
        if transport.get_write_buffer_size() > CLIENT_BUFFER_LIMIT:
            self.d.journal.send(f"dropping slow serial client {self.name()}", WARNING)
            self.close()
            return
        self.writer.write(data)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.tx.flush()
        try:
            self.writer.close()
        except Exception:
            pass

    def _log_tx(self, line: bytes, partial: bool) -> None:
        self.d.journal.send(
            escape(line),
            INFO,
            identifier="a16-serial-tx",
            A16_HOLDER=self.holder or "",
            A16_CLIENT_UID=self.uid,
            A16_SERIAL_PARTIAL=1 if partial else None,
        )

    def written(self, data: bytes) -> None:
        # Log what was typed, a line per CR/LF (or 500 ms of quiet).
        self.tx.feed(data.replace(b"\r", b"\n"))
        if self.tx_timer:
            self.tx_timer.cancel()
        self.tx_timer = asyncio.get_running_loop().call_later(0.5, self.tx.flush)


class SerialDaemon:
    def __init__(
        self,
        device: str,
        socket_path: str,
        baud: int = 115200,
        group: str | None = None,
        backlog_size: int = 65536,
        partial_timeout: float = 0.2,
        retry: float = 1.0,
        journal: Journal | None = None,
    ):
        self.device = device
        self.socket_path = socket_path
        self.baud = baud
        self.group = group
        self.backlog_size = backlog_size
        self.partial_timeout = partial_timeout
        self.retry = retry
        self.journal = journal or Journal("a16-serial")
        self.fd = None
        self.clients: set[Client] = set()
        self.writer_client: Client | None = None
        self.backlog = bytearray()
        self.rx = LineAssembler(self._log_rx)
        self.partial_timer = None
        self.rx_bytes = 0
        self.last_line = ""
        self.last_line_time = None
        self.last_rx_time = None
        self.device_error = None
        self.reopen_event = None

    # --- device -----------------------------------------------------------
    def _open(self) -> bool:
        try:
            fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            self._device_state(f"{self.device}: {e.strerror}")
            return False
        try:
            configure_tty(fd, self.baud)
            fcntl.ioctl(fd, termios.TIOCEXCL)
        except OSError as e:
            os.close(fd)
            self._device_state(f"{self.device}: setup failed: {e.strerror}")
            return False
        self.fd = fd
        asyncio.get_running_loop().add_reader(fd, self._readable)
        self.device_error = None
        st = os.fstat(fd)
        self.journal.send(
            f"opened {self.device} ({os.major(st.st_rdev)}:{os.minor(st.st_rdev)}) at {self.baud} baud, exclusive",
            NOTICE,
            A16_EVENT="open",
        )
        return True

    def _device_state(self, err: str) -> None:
        if err != self.device_error:
            self.journal.send(f"serial device unavailable: {err}", WARNING, A16_EVENT="unavailable")
        self.device_error = err

    def _close(self, why: str) -> None:
        if self.fd is None:
            return
        loop = asyncio.get_running_loop()
        loop.remove_reader(self.fd)
        try:
            # Drop exclusivity first: a pty keeps the flag past the last close.
            fcntl.ioctl(self.fd, termios.TIOCNXCL)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None
        self.rx.flush()
        self.journal.send(f"closed {self.device}: {why}", NOTICE, A16_EVENT="close")
        self.device_error = why

    def _readable(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError as e:
            self._close(e.strerror or str(e))
            self.reopen_event.set()
            return
        if not data:
            self._close("hangup")
            self.reopen_event.set()
            return
        self._received(data)

    def _received(self, data: bytes) -> None:
        now = time.time()
        self.rx_bytes += len(data)
        self.last_rx_time = now
        self.backlog += data
        if len(self.backlog) > self.backlog_size:
            del self.backlog[: len(self.backlog) - self.backlog_size]
        for c in list(self.clients):
            if c.mode in ("read", "write"):
                c.send(data)
        self.rx.feed(data)
        if self.partial_timer:
            self.partial_timer.cancel()
        self.partial_timer = asyncio.get_running_loop().call_later(self.partial_timeout, self.rx.flush)

    def _log_rx(self, line: bytes, partial: bool) -> None:
        text = escape(line)
        self.last_line = text
        self.last_line_time = time.time()
        self.journal.send(text, INFO, A16_SERIAL_PARTIAL=1 if partial else None)

    def write(self, data: bytes) -> bool:
        if self.fd is None:
            return False
        view = memoryview(data)
        deadline = time.monotonic() + 2.0
        while view:
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.005)
            except OSError as e:
                self._close(e.strerror or str(e))
                self.reopen_event.set()
                return False
        return True

    async def _pause(self) -> None:
        try:
            await asyncio.wait_for(self.reopen_event.wait(), self.retry)
        except asyncio.TimeoutError:
            pass
        self.reopen_event.clear()

    async def device_loop(self) -> None:
        while True:
            if self.fd is None and not self._open():
                await self._pause()
                continue
            await self.reopen_event.wait()
            self.reopen_event.clear()
            if self.fd is None:
                # Closed after an error or a SIGHUP: give a replugged device
                # (and the host's attach service) a moment before reopening.
                await asyncio.sleep(min(self.retry, 0.5))

    def reopen(self) -> None:
        self._close("SIGHUP: reopening")
        self.reopen_event.set()

    # --- clients ----------------------------------------------------------
    def status(self) -> dict:
        now = time.time()
        return {
            "device": self.device,
            "open": self.fd is not None,
            "error": self.device_error,
            "writer": self.writer_client.holder if self.writer_client else None,
            "clients": sum(1 for c in self.clients if c.mode in ("read", "write")),
            "rx_bytes": self.rx_bytes,
            "last_line": self.last_line,
            "last_line_age": None if self.last_line_time is None else round(now - self.last_line_time, 3),
            "last_rx_age": None if self.last_rx_time is None else round(now - self.last_rx_time, 3),
        }

    async def handle(self, reader, writer) -> None:
        c = Client(self, reader, writer)
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            try:
                hello = json.loads(line or b"{}")
                if not isinstance(hello, dict):
                    raise ValueError
            except ValueError:
                await self._reply(c, {"ok": False, "error": "bad hello: want one JSON object line"})
                return
            mode = hello.get("mode", "read")
            c.holder = str(hello.get("holder") or "")[:64] or None
            if mode == "status":
                await self._reply(c, {"ok": True, **self.status()})
                return
            if mode not in ("read", "write"):
                await self._reply(c, {"ok": False, "error": f"unknown mode {mode!r}"})
                return
            if mode == "write":
                cur = self.writer_client
                if cur is not None and not cur.closed:
                    if not hello.get("force"):
                        await self._reply(
                            c,
                            {"ok": False, "error": f"write lock held by {cur.name()}", "writer": cur.holder},
                        )
                        return
                    self.journal.send(
                        f"write lock taken from {cur.name()} by {c.name()} (force)", WARNING, A16_EVENT="steal"
                    )
                    cur.close()
                self.writer_client = c
            c.mode = mode
            self.clients.add(c)
            await self._reply(c, {"ok": True, "mode": mode, "open": self.fd is not None})
            n = int(hello.get("backlog") or 0)
            if n > 0 and self.backlog:
                c.send(bytes(self.backlog[-n:]))
            while not c.closed:
                data = await reader.read(4096)
                if not data:
                    break
                if c.mode != "write" or self.writer_client is not c:
                    continue
                c.written(data)
                if not self.write(data):
                    self.journal.send(
                        f"write from {c.name()} dropped: serial device not open", ERR, A16_EVENT="tx-dropped"
                    )
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            if self.writer_client is c:
                self.writer_client = None
            self.clients.discard(c)
            c.close()

    async def _reply(self, c: Client, obj: dict) -> None:
        c.writer.write((json.dumps(obj) + "\n").encode())
        try:
            await c.writer.drain()
        except ConnectionError:
            pass

    async def run(self) -> None:
        self.reopen_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self.reopen)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        old = os.umask(0o117)
        try:
            server = await asyncio.start_unix_server(self.handle, path=self.socket_path)
        finally:
            os.umask(old)
        if self.group:
            os.chown(self.socket_path, -1, grp.getgrnam(self.group).gr_gid)
        os.chmod(self.socket_path, 0o660)
        self.journal.send(f"listening on {self.socket_path}", INFO)
        async with server:
            await self.device_loop()


def main(args) -> None:
    d = SerialDaemon(
        device=args.device,
        socket_path=args.socket,
        baud=args.baud,
        group=args.group,
    )
    try:
        asyncio.run(d.run())
    except KeyboardInterrupt:
        pass

