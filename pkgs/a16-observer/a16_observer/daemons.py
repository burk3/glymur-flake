"""`a16-obs <daemon>`: the services in the a16-obs container on the observer host."""

from __future__ import annotations

import argparse
import asyncio

from . import serial, sinks


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="a16-obs", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serial", help="own the FTDI, log lines, serve the socket")
    p.add_argument("--device", required=True)
    p.add_argument("--socket", required=True)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--group", help="group for the socket (mode 0660)")

    p = sub.add_parser("heartbeat", help="UDP heartbeat sink + VictoriaMetrics bridge")
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("--run-dir", required=True, help="where heartbeat.last goes")
    p.add_argument("--import-url", default="", help="VictoriaMetrics /api/v1/import/prometheus URL")

    p = sub.add_parser("netconsole", help="UDP netconsole sink")
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--port", type=int, default=6667)

    p = sub.add_parser("marks", help="unix socket for `a16 mark`")
    p.add_argument("--socket", required=True)
    p.add_argument("--group")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "serial":
            serial.main(args)
        elif args.cmd == "heartbeat":
            asyncio.run(sinks.run_heartbeat(args))
        elif args.cmd == "netconsole":
            asyncio.run(sinks.run_netconsole(args))
        elif args.cmd == "marks":
            asyncio.run(sinks.run_marks(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
