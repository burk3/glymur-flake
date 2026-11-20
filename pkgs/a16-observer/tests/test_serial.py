import asyncio
import json
import os
import pty
import tty

import pytest

from a16_observer.serial import LineAssembler, SerialDaemon


def test_line_assembler():
    out = []
    la = LineAssembler(lambda b, p: out.append((b, p)))
    la.feed(b"abc\r\nde")
    la.feed(b"f\nxy")
    assert out == [(b"abc\r", False), (b"def", False)]
    la.flush()
    assert out[-1] == (b"xy", True)
    la.feed(b"z" * 5000)
    assert out[-1] == (b"z" * 4096, True)


async def _client(path, hello):
    r, w = await asyncio.open_unix_connection(path)
    w.write((json.dumps(hello) + "\n").encode())
    await w.drain()
    reply = json.loads(await asyncio.wait_for(r.readline(), 5))
    return r, w, reply


async def _read_until(r, needle, timeout=5):
    buf = b""
    async def go():
        nonlocal buf
        while needle not in buf:
            d = await r.read(4096)
            if not d:
                break
            buf += d
        return buf
    return await asyncio.wait_for(go(), timeout)


@pytest.fixture
def pair(tmp_path):
    master, slave = pty.openpty()
    tty.setraw(master)
    name = os.ttyname(slave)
    os.close(slave)  # the daemon opens it by name
    yield master, name
    os.close(master)


def test_daemon(pair, tmp_path, journal):
    master, dev = pair
    sock = str(tmp_path / "serial.sock")

    async def scenario():
        d = SerialDaemon(dev, sock, journal=journal, partial_timeout=0.1, retry=0.2)
        task = asyncio.create_task(d.run())
        for _ in range(50):
            if os.path.exists(sock) and d.fd is not None:
                break
            await asyncio.sleep(0.05)
        assert d.fd is not None
        assert oct(os.stat(sock).st_mode & 0o777) == "0o660"
        # Exclusive: a second open of the tty fails (EBUSY) for non-root.
        if os.geteuid() != 0:
            with pytest.raises(OSError):
                os.open(dev, os.O_RDWR | os.O_NOCTTY)

        r1, w1, rep = await _client(sock, {"mode": "read"})
        assert rep["ok"]
        r2, w2, rep = await _client(sock, {"mode": "read"})
        assert rep["ok"]
        os.write(master, b"hello a16\r\nlogin: ")
        assert b"hello a16" in await _read_until(r1, b"login: ")
        assert b"hello a16" in await _read_until(r2, b"login: ")
        await asyncio.sleep(0.3)  # partial flush
        msgs = journal.messages()
        assert "hello a16" in msgs and "login: " in msgs
        partial = [e for e in journal.entries if e["MESSAGE"] == "login: "][0]
        assert partial["A16_SERIAL_PARTIAL"] == 1

        # Write lock: one writer; the second is refused naming the first.
        rw, ww, rep = await _client(sock, {"mode": "write", "holder": "alice"})
        assert rep["ok"] and rep["open"]
        _, w3, rep = await _client(sock, {"mode": "write", "holder": "bob"})
        assert not rep["ok"] and rep["writer"] == "alice" and "alice" in rep["error"]
        ww.write(b"uptime\r")
        await ww.drain()
        loop = asyncio.get_running_loop()
        got = await asyncio.wait_for(loop.run_in_executor(None, os.read, master, 100), 5)
        assert got == b"uptime\r"
        await asyncio.sleep(0.7)
        tx = [e for e in journal.entries if e["IDENT"] == "a16-serial-tx"]
        assert tx and tx[0]["MESSAGE"] == "uptime" and tx[0]["A16_HOLDER"] == "alice"
        # Readers can't write.
        w1.write(b"nope\r")
        await w1.drain()
        # Force steals the lock and closes the old writer.
        rb, wb, rep = await _client(sock, {"mode": "write", "holder": "bob", "force": True})
        assert rep["ok"]
        assert await asyncio.wait_for(rw.read(), 5) == b""
        wb.write(b"x")
        await wb.drain()
        got = await asyncio.wait_for(loop.run_in_executor(None, os.read, master, 100), 5)
        assert got == b"x"  # "nope" from the reader never arrived
        # Backlog for late joiners, status.
        r4, w4, rep = await _client(sock, {"mode": "read", "backlog": 9})
        assert await _read_until(r4, b"login: ") == b"\r\nlogin: "
        _, _, st = await _client(sock, {"mode": "status"})
        assert st["ok"] and st["open"] and st["writer"] == "bob" and st["clients"] >= 3
        # Writer gone: lock free again.
        wb.close()
        await asyncio.sleep(0.1)
        _, _, st = await _client(sock, {"mode": "status"})
        assert st["writer"] is None
        # SIGHUP-style reopen keeps clients connected.
        d.reopen()
        for _ in range(50):
            await asyncio.sleep(0.05)
            if d.fd is not None:
                break
        assert d.fd is not None
        os.write(master, b"after reopen\n")
        assert b"after reopen" in await _read_until(r1, b"after reopen")
        task.cancel()

    asyncio.run(scenario())


def test_missing_device_retries(tmp_path, journal):
    sock = str(tmp_path / "s.sock")
    dev = str(tmp_path / "nodev")

    async def scenario():
        d = SerialDaemon(dev, sock, journal=journal, retry=0.1)
        task = asyncio.create_task(d.run())
        await asyncio.sleep(0.5)
        assert d.fd is None
        # Logged once, not once per retry.
        assert sum("unavailable" in m for m in journal.messages()) == 1
        _, w, rep = await _client(sock, {"mode": "write", "holder": "a"})
        assert rep["ok"] and rep["open"] is False
        task.cancel()

    asyncio.run(scenario())
