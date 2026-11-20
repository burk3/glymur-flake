"""Structured logging to the systemd journal's native socket.

Every entry carries SYSLOG_IDENTIFIER and any A16_* fields the caller passes.
Without a journal socket (tests, a shell outside systemd) entries go to stdout
as `IDENT[PRI]: MESSAGE KEY=VALUE ...`, which is also what the tests read.
"""

from __future__ import annotations

import os
import socket
import struct
import sys

SOCKET = os.environ.get("A16_OBS_JOURNAL_SOCKET", "/run/systemd/journal/socket")

# syslog priorities
ERR, WARNING, NOTICE, INFO, DEBUG = 3, 4, 5, 6, 7


def _field(key: str, value) -> bytes:
    k = key.upper().encode()
    if isinstance(value, bytes):
        v = value
    else:
        v = str(value).encode("utf-8", "replace")
    if b"\n" in v:
        return k + b"\n" + struct.pack("<Q", len(v)) + v + b"\n"
    return k + b"=" + v + b"\n"


def encode(identifier: str, message: str, priority: int = INFO, fields: dict | None = None) -> bytes:
    """One native-protocol datagram. Field names are upper-cased; None values are dropped."""
    out = [
        _field("MESSAGE", message),
        _field("PRIORITY", int(priority)),
        _field("SYSLOG_IDENTIFIER", identifier),
    ]
    for k, v in (fields or {}).items():
        if v is None:
            continue
        out.append(_field(k, v))
    return b"".join(out)


class Journal:
    def __init__(self, identifier: str, path: str | None = None):
        self.identifier = identifier
        self.path = path or SOCKET
        self.sock = None
        if os.environ.get("A16_OBS_JOURNAL_STDOUT") != "1":
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                s.connect(self.path)
                self.sock = s
            except OSError:
                self.sock = None

    def send(self, message: str, priority: int = INFO, identifier: str | None = None, **fields) -> None:
        ident = identifier or self.identifier
        if self.sock is not None:
            data = encode(ident, message, priority, fields)
            try:
                self.sock.send(data)
                return
            except OSError:
                # Too large for one datagram or journald restarting: fall back
                # to stdout, which systemd also sends to the journal.
                pass
        extra = " ".join(f"{k.upper()}={v}" for k, v in fields.items() if v is not None)
        sys.stdout.write(f"{ident}[{priority}]: {message}{' ' + extra if extra else ''}\n")
        sys.stdout.flush()


def escape(data: bytes) -> str:
    """Printable text for one serial line: UTF-8 kept, control bytes as \\xNN, CR dropped."""
    text = data.replace(b"\r", b"").decode("utf-8", "backslashreplace")
    out = []
    for ch in text:
        o = ord(ch)
        if ch == "\t" or o >= 0x20 and o != 0x7F:
            out.append(ch)
        else:
            out.append(f"\\x{o:02x}")
    return "".join(out)
