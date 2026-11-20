"""a16-ir-bridge-trigger: ask the bridge for IR frames and wait until they flow.

Run from PAM (pam_exec, `optional`) just before pam_howdy, as whatever user
the PAM application runs as. Connects to the bridge's socket (systemd starts
the bridge on the first connection) and waits for "ready" or "error". Exits 0
once frames are flowing, 1 otherwise; PAM ignores the result either way, and
howdy then simply fails (falling through to the password) if there are no
frames. Users without a howdy face model don't start the camera at all.

Keep this module free of heavy imports: it runs on every PAM auth.
"""

import argparse
import os
import socket
import sys
import time

SOCKET_PATH = "/run/a16-ir-bridge.sock"
MODELS_DIR = "/var/lib/howdy/models"


def request(path=SOCKET_PATH, timeout=6.0):
    deadline = time.monotonic() + timeout
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(path)
        data = b""
        while b"\n" not in data:
            s.settimeout(max(0.05, deadline - time.monotonic()))
            chunk = s.recv(256)
            if not chunk:
                break
            data += chunk
        return data.decode(errors="replace").strip() or "error: no reply"
    except OSError as e:
        return f"error: {e}"
    finally:
        s.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="a16-ir-bridge-trigger", description=__doc__.splitlines()[0])
    p.add_argument("--user", default=os.environ.get("PAM_USER"), help="skip unless this user has a face model (default: $PAM_USER)")
    p.add_argument("--any-user", action="store_true", help="don't check for a face model")
    p.add_argument("--socket", default=SOCKET_PATH)
    p.add_argument("--timeout", type=float, default=6.0)
    p.add_argument("--models-dir", default=MODELS_DIR)
    args = p.parse_args(argv)

    if not args.any_user:
        if not args.user or "/" in args.user:
            print("no user", file=sys.stderr)
            return 1
        if not os.path.exists(os.path.join(args.models_dir, args.user + ".dat")):
            print(f"no face model for {args.user}; not starting the IR camera", file=sys.stderr)
            return 1

    reply = request(args.socket, args.timeout)
    print(reply)
    return 0 if reply == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
