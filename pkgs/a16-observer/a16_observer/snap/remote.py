"""the observer host side of a16-snap: `a16 snap TOPIC ...` runs a16-snap on the A16 over
ssh and pulls the snapshot into the local store (~/a16-snaps by default);
`a16 snap ls|show|diff` query that store.

Which a16-snap runs on the A16: $A16_SNAP_BIN if set; else the system's
`a16-snap` if its version matches ours; else this flake's aarch64 build
(`nix build <flake>#packages.aarch64-linux.a16-observer`, built on the builder or
substituted), copied into the A16's store with `nix copy` and run from its store
path. So a new collector is usable without a redeploy or reboot."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

from . import SNAP_VERSION, store

STORE = os.path.expanduser(os.environ.get("A16_SNAP_STORE", "~/a16-snaps"))
ATTR = "packages.aarch64-linux.a16-observer"


def flake_ref() -> str:
    if os.environ.get("A16_SNAP_FLAKE"):
        return os.environ["A16_SNAP_FLAKE"]
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        top = ""
    if top and os.path.exists(os.path.join(top, "pkgs/a16-observer/a16_observer/snap/main.py")):
        return top
    return os.path.expanduser("~/src/snapx2ee-bringup")


def ssh_base(host: str, alias: str) -> list[str]:
    return ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "-o", f"HostKeyAlias={alias}", f"root@{host}"]


def log(msg: str) -> None:
    print(f"a16 snap: {msg}", file=sys.stderr)


def remote_bin(host: str, alias: str, force_copy: bool = False) -> str:
    if os.environ.get("A16_SNAP_BIN"):
        return os.environ["A16_SNAP_BIN"]
    if not force_copy:
        r = subprocess.run(ssh_base(host, alias) + ["a16-snap --version 2>/dev/null"], capture_output=True, text=True, timeout=30)
        if r.stdout.strip() == f"a16-snap {SNAP_VERSION}":
            return "a16-snap"
        log(f"A16 has {r.stdout.strip() or 'no a16-snap'}, want {SNAP_VERSION}: copying this flake's build")
    flake = flake_ref()
    log(f"nix build {flake}#{ATTR}")
    r = subprocess.run(["nix", "build", "--no-link", "--print-out-paths", f"{flake}#{ATTR}"], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"nix build failed:\n{r.stderr[-2000:]}")
    path = r.stdout.strip().splitlines()[-1]
    env = {**os.environ, "NIX_SSHOPTS": f"-o ConnectTimeout=5 -o BatchMode=yes -o HostKeyAlias={alias}"}
    log(f"nix copy {path} -> {host}")
    r = subprocess.run(["nix", "copy", "--to", f"ssh://root@{host}", path], capture_output=True, text=True, timeout=900, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"nix copy failed:\n{r.stderr[-2000:]}")
    return f"{path}/bin/a16-snap"


def take(host: str, alias: str, topic: str, args: list[str], force_copy: bool = False) -> tuple[str, dict]:
    """Runs the snapshot on the A16 and copies its directory into STORE.
    Returns (local dir, summary)."""
    exe = remote_bin(host, alias, force_copy)
    cmd = " ".join(shlex.quote(x) for x in [exe, topic, *args, "--json"])
    r = subprocess.run(ssh_base(host, alias) + [cmd], capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"a16-snap failed on the A16 (rc {r.returncode}):\n{r.stderr[-2000:]}")
    summ = json.loads(r.stdout.strip().splitlines()[-1])
    sid = summ["id"]
    os.makedirs(STORE, exist_ok=True)
    rd = os.path.dirname(os.path.dirname(summ["dir"].rstrip("/")))  # <store>/<topic>/<name>
    tar = subprocess.Popen(ssh_base(host, alias) + [f"tar -C {shlex.quote(rd)} -cf - {shlex.quote(sid)}"], stdout=subprocess.PIPE)
    x = subprocess.run(["tar", "-C", STORE, "-xf", "-"], stdin=tar.stdout, timeout=300)
    tar.stdout.close()
    if tar.wait(timeout=300) != 0 or x.returncode != 0:
        raise RuntimeError("copying the snapshot from the A16 failed")
    local = os.path.join(STORE, sid)
    snap = store.load(local)
    store.append_index(STORE, store.index_entry(snap) | {"source": f"{host}:{summ['dir']}"})
    return local, summ
