"""a16-snap core: the read helpers every collector uses, and the registry.

Safety rules (docs/a16-snap.md "Safety"), enforced here where they can be:
  - reads only: files are opened O_RDONLY, never written; paths outside the
    pseudo filesystems and a few system directories are refused;
  - files without a read permission bit are never opened (debugfs `command`
    nodes and the like are write-only);
  - every file read and every command has a hard timeout (default 5 s); a
    read that hangs in the kernel is abandoned on its worker thread;
  - register-backed debugfs (dwc3 regdump, xHCI PORTSC) goes through
    `ctx.read_regs(path, device)`, which reads only while the device's
    runtime PM status is "active" and otherwise records "skipped: suspended".
    Reading such a file on a runtime-suspended or unclocked Qualcomm block
    can raise an unclocked-access fault and reset the SoC.

A collector is a function `fn(ctx) -> dict` registered with
`@collector("topic")`. It reads through ctx, returns JSON-able data (dicts keyed
by stable names, so diffs line up) and may save verbatim text with
`ctx.raw(name, text)` (raw/<name>.txt in the snapshot).
"""

from __future__ import annotations

import concurrent.futures
import glob as _glob
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = 5.0

# Where reads may go. /dev/kmsg is the kernel log; /dev/input/event* is opened
# read-only for the lid switch state (EVIOCGSW).
READ_PREFIXES = ("/sys/", "/proc/", "/run/", "/etc/", "/nix/", "/var/lib/a16-snap", "/dev/kmsg", "/dev/input/")

# Port name -> controller (DT unit address). Everything else (xHCI device,
# buses, Type-C port, PHYs, repeaters) is derived from sysfs links, so a
# renumbered bus or typec port doesn't break the map. README "Hardware facts".
PORTS = {
    "c1": {"controller": "a600000.usb", "desc": "USB-C 1 (left, near hinge), combo PHY fd5000, connector@0"},
    "c2": {"controller": "a800000.usb", "desc": "USB-C 2 (left, front), combo PHY fde000, connector@1"},
    "a1": {"controller": "a400000.usb", "desc": "USB-A (right), multiport, PTN3222 repeaters"},
}


class ReadTimeout(Exception):
    pass


@dataclass
class CmdResult:
    argv: list[str]
    rc: int | None
    out: str
    err: str = ""
    timed_out: bool = False
    missing: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    def summary(self) -> dict:
        d: dict = {"rc": self.rc}
        if self.timed_out:
            d["timeout"] = True
        if self.missing:
            d["missing"] = True
        if self.err.strip() and not self.ok:
            d["stderr"] = self.err.strip()[-400:]
        return d


class _Pool:
    """Worker threads for file reads, so a read stuck in the kernel can be
    abandoned. After a timeout the pool is replaced (the stuck thread stays
    behind, daemonised by concurrent.futures)."""

    def __init__(self):
        self.ex = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="snapread")

    def call(self, fn, timeout):
        fut = self.ex.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            self.ex.shutdown(wait=False, cancel_futures=True)
            self.ex = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="snapread")
            raise ReadTimeout() from None


@dataclass
class Ctx:
    root: str = ""  # tests: a synthetic tree; "" on the A16
    timeout: float = DEFAULT_TIMEOUT
    port: str = "all"
    regs: bool = True  # register-backed debugfs allowed (still gated on runtime PM)
    smart: bool = False
    runner: Callable | None = None  # tests: fake command runner
    prev_seq: int | None = None  # kmsg seq at the previous snapshot of this boot
    raws: dict[str, str] = field(default_factory=dict)
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    _pool: _Pool | None = None
    _cache: dict = field(default_factory=dict)

    # --- paths ------------------------------------------------------------
    def p(self, path: str) -> str:
        return self.root + path if self.root else path

    def unp(self, path: str) -> str:
        return path[len(self.root) :] if self.root and path.startswith(self.root) else path

    def exists(self, path: str) -> bool:
        return os.path.lexists(self.p(path))

    def isdir(self, path: str) -> bool:
        return os.path.isdir(self.p(path))

    def glob(self, pattern: str) -> list[str]:
        return sorted(self.unp(x) for x in _glob.glob(self.p(pattern)))

    def listdir(self, path: str) -> list[str]:
        try:
            return sorted(os.listdir(self.p(path)))
        except OSError:
            return []

    def readlink(self, path: str) -> str | None:
        try:
            return os.readlink(self.p(path))
        except OSError:
            return None

    def realpath(self, path: str) -> str:
        return self.unp(os.path.realpath(self.p(path)))

    def link_name(self, path: str) -> str | None:
        t = self.readlink(path)
        return os.path.basename(t) if t else None

    # --- reads ------------------------------------------------------------
    def _check(self, path: str) -> str:
        if not path.startswith(READ_PREFIXES):
            raise PermissionError(f"a16-snap reads only under {READ_PREFIXES}: {path}")
        return self.p(path)

    def read_bytes(self, path: str, limit: int = 4 << 20) -> bytes | None:
        """Bytes of a file, or None if missing/unreadable/write-only. Raises
        ReadTimeout if the read hangs past ctx.timeout."""
        real = self._check(path)
        try:
            st = os.stat(real)
        except OSError:
            return None
        if stat.S_ISDIR(st.st_mode) or not (st.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)):
            return None

        def rd():
            fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                chunks, n = [], 0
                while n < limit:
                    b = os.read(fd, 65536)
                    if not b:
                        break
                    chunks.append(b)
                    n += len(b)
                return b"".join(chunks)
            finally:
                os.close(fd)

        if self._pool is None:
            self._pool = _Pool()
        try:
            return self._pool.call(rd, self.timeout)
        except ReadTimeout:
            self.errors.append({"path": path, "error": f"read timed out after {self.timeout}s"})
            return None
        except OSError:
            return None

    def read(self, path: str, default=None) -> str | None:
        b = self.read_bytes(path)
        if b is None:
            return default
        return b.decode("utf-8", "replace").rstrip("\n")

    def read_int(self, path: str):
        v = self.read(path)
        try:
            return int(v) if v is not None else None
        except ValueError:
            return v

    def attrs(self, d: str, names) -> dict:
        """Named attribute files of a directory (missing ones omitted)."""
        out = {}
        for n in names:
            v = self.read(f"{d}/{n}")
            if v is not None:
                out[n] = v
        return out

    def tree(self, d: str, depth: int = 3, skip=("power", "subsystem", "device", "driver", "firmware_node", "of_node", "hwmon", "wakeup")) -> dict:
        """All readable files under d, recursively, symlinks to directories
        not followed. Only for directories known to hold cached software state
        (typec, usb_power_delivery, power_supply); never for debugfs."""
        out: dict = {}
        for name in self.listdir(d):
            if name in skip or name.startswith(("supplier:", "consumer:")):
                continue
            path = f"{d}/{name}"
            real = self.p(path)
            if os.path.islink(real):
                continue
            if os.path.isdir(real):
                if depth > 0:
                    sub = self.tree(path, depth - 1, skip)
                    if sub:
                        out[name] = sub
            else:
                v = self.read(path)
                if v is not None:
                    out[name] = v
        return out

    def runtime_status(self, dev: str) -> str | None:
        return self.read(f"{dev}/power/runtime_status")

    def read_regs(self, path: str, devices: list[str]) -> str | dict:
        """A register-backed debugfs file, read only if every device in
        `devices` (sysfs dirs: the block's controller and its children whose
        clocks the read needs) reports runtime_status "active". Returns the
        text, or {"skipped": reason}."""
        if not self.regs:
            return {"skipped": "register reads disabled (--no-regs)"}
        for d in devices:
            rs = self.runtime_status(d)
            if rs != "active":
                why = f"skipped: {os.path.basename(d)} runtime_status={rs or 'unknown'}"
                self.skipped.append({"path": path, "reason": why})
                return {"skipped": why}
        v = self.read(path)
        if v is None:
            return {"skipped": "unreadable"}
        return v

    # --- commands ---------------------------------------------------------
    def run(self, argv: list[str], timeout: float | None = None, input: bytes | None = None, raw: str | None = None) -> CmdResult:
        """Runs a read-only command with a hard timeout. `raw` saves its
        stdout (and stderr if it failed) as raw/<raw>.txt."""
        t = timeout or self.timeout
        if self.runner is not None:
            r = self.runner(argv)
            if r is None:
                r = CmdResult(argv, None, "", "not available in test", missing=True)
        else:
            try:
                p = subprocess.run(
                    argv,
                    input=input,
                    stdin=None if input is not None else subprocess.DEVNULL,
                    capture_output=True,
                    timeout=t,
                    env={**os.environ, "LC_ALL": "C"},
                )
                r = CmdResult(argv, p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))
            except FileNotFoundError:
                r = CmdResult(argv, None, "", f"{argv[0]}: not found", missing=True)
            except subprocess.TimeoutExpired as e:
                out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
                r = CmdResult(argv, None, out, f"timed out after {t}s", timed_out=True)
        if r.timed_out:
            self.errors.append({"cmd": " ".join(argv), "error": r.err})
        if raw:
            text = f"$ {' '.join(argv)}\n{r.out}"
            if not r.ok:
                text += f"\n[rc={r.rc}{' timeout' if r.timed_out else ''}] {r.err}"
            self.raw(raw, text)
        return r

    def raw(self, name: str, text: str) -> None:
        name = re.sub(r"[^A-Za-z0-9_.@+-]+", "_", name)
        if name in self.raws:
            self.raws[name] += "\n" + text
        else:
            self.raws[name] = text

    def memo(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]


# --- registry -----------------------------------------------------------------
@dataclass
class Collector:
    topic: str
    name: str
    fn: Callable[[Ctx], dict]
    doc: str = ""


COLLECTORS: list[Collector] = []
# Topics whose collectors run in every snapshot (kernel red flags).
ALWAYS = ("redflags",)


def collector(topic: str, name: str | None = None):
    def deco(fn):
        doc = " ".join((fn.__doc__ or "").strip().split("\n\n")[0].split())
        COLLECTORS.append(Collector(topic, name or fn.__name__, fn, doc))
        return fn

    return deco


def topics() -> list[str]:
    seen = []
    for c in COLLECTORS:
        if c.topic not in seen:
            seen.append(c.topic)
    return seen


def run_collectors(ctx: Ctx, topic: str) -> dict:
    """Runs every collector of `topic` ("all": every topic) and of the ALWAYS
    topics. One failing collector never stops the others; its error is
    recorded instead."""
    results = {}
    for c in COLLECTORS:
        if topic != "all" and c.topic != topic and c.topic not in ALWAYS:
            continue
        key = f"{c.topic}.{c.name}"
        t0 = time.monotonic()
        try:
            data = c.fn(ctx)
            results[key] = {"ok": True, "data": data}
        except Exception as e:  # a broken collector is data too
            results[key] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        results[key]["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
    return results
