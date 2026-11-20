"""Snapshot identity (host, boot, generation, kernel) and the kernel log."""

from __future__ import annotations

import errno
import os
import re
import socket

from . import parse
from .core import Ctx


def identity(ctx: Ctx) -> dict:
    gen_link = ctx.readlink("/nix/var/nix/profiles/system")
    m = re.match(r"system-(\d+)-link", gen_link or "")
    current = ctx.realpath("/run/current-system") if ctx.exists("/run/current-system") else None
    booted = ctx.realpath("/run/booted-system") if ctx.exists("/run/booted-system") else None
    uptime = ctx.read("/proc/uptime")
    return {
        "host": ctx.read("/proc/sys/kernel/hostname") or socket.gethostname(),
        "boot_id": ctx.read("/proc/sys/kernel/random/boot_id"),
        "generation": int(m.group(1)) if m else None,
        "system": current,
        "booted_system": booted,
        "booted_is_current": booted == current if booted and current else None,
        "kernel": ctx.read("/proc/sys/kernel/osrelease"),
        "kernel_version": ctx.read("/proc/version"),
        "cmdline": ctx.read("/proc/cmdline"),
        "uptime_s": float(uptime.split()[0]) if uptime else None,
        "soc": {k: ctx.read(f"/sys/devices/soc0/{k}") for k in ("machine", "soc_id", "revision", "family")},
        "dmi": {k: ctx.read(f"/sys/class/dmi/id/{k}") for k in ("product_name", "bios_version", "board_name")},
    }


def read_kmsg(ctx: Ctx) -> list[dict]:
    """Every record in the kernel ring buffer, oldest first. On the A16 this
    reads /dev/kmsg non-blocking (each open has its own cursor, nothing is
    consumed). Tests put the records one per line in <root>/dev/kmsg."""

    def load():
        recs = []
        path = ctx.p("/dev/kmsg")
        if ctx.root:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        r = parse.parse_kmsg_record(line.rstrip("\n"))
                        if r:
                            recs.append(r)
            except OSError:
                pass
            return recs
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:
            ctx.errors.append({"path": "/dev/kmsg", "error": str(e)})
            return recs
        try:
            while True:
                try:
                    b = os.read(fd, 16384)
                except BlockingIOError:
                    break
                except OSError as e:
                    if e.errno == errno.EPIPE:  # overwritten while reading; continue
                        continue
                    break
                if not b:
                    break
                r = parse.parse_kmsg_record(b.decode("utf-8", "replace").rstrip("\n"))
                if r:
                    recs.append(r)
        finally:
            os.close(fd)
        return recs

    return ctx.memo("kmsg", load)
