"""The CLI's log backends (LogsQL / journalctl), boots with PON diffs, pstore."""

import http.server
import json
import threading
import time
import urllib.parse

import pytest

from a16_observer import cli
from a16_observer.telemetry import PON_MESSAGE_ID, PSTORE_MESSAGE_ID

B1 = "3f2c8a0e-1d2b-4c5d-9e8f-0a1b2c3d4e5f"
B2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
B3 = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_terms_and_logsql():
    t = cli.terms_for(["serial", "mark", "kernel", "serial"])
    assert t == [
        [("SYSLOG_IDENTIFIER", "a16-serial")],
        [("SYSLOG_IDENTIFIER", "a16-mark")],
        [("_HOSTNAME", "zenbook-a16"), ("_TRANSPORT", "kernel")],
    ]
    f = cli.logsql_filter(t, grep="panic")
    assert f == (
        '((SYSLOG_IDENTIFIER:="a16-serial") or (SYSLOG_IDENTIFIER:="a16-mark") or '
        '(_HOSTNAME:="zenbook-a16" _TRANSPORT:="kernel")) _msg:~"(?i)panic"'
    )
    assert cli.logsql_filter([[("A", 'q"x')]], grep="GLYMUR-PON") == '((A:="q\\"x")) _msg:~"GLYMUR-PON"'
    assert cli.logsql_query(t[:1], lines=5).endswith("| sort by (_time desc) | limit 5")
    assert cli.source_terms("a16:NetworkManager") == [[("_HOSTNAME", "zenbook-a16"), ("SYSLOG_IDENTIFIER", "NetworkManager")]]
    assert cli.source_terms("boot") == [[("MESSAGE_ID", PON_MESSAGE_ID)]]


def test_journal_cmd(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "JOURNAL", str(tmp_path))
    cmd = cli.journal_cmd(cli.terms_for(["serial", "kernel"]), since=100.0, lines=3, grep="x")
    assert cmd[:3] == ["journalctl", "-D", str(tmp_path)]
    assert cmd[cmd.index("--since") + 1] == "@100.000000"
    assert cmd[-4:] == ["SYSLOG_IDENTIFIER=a16-serial", "+", "_HOSTNAME=zenbook-a16", "_TRANSPORT=kernel"]
    assert "-a" in cmd and "-g" in cmd


def test_parse_time():
    now = 1_800_000_000.0
    assert cli.parse_time("10m", now) == now - 600
    assert cli.parse_time("-2h", now) == now - 7200
    assert cli.parse_time("@123.5", now) == 123.5
    assert cli.parse_time(None) is None
    assert cli.parse_time("2026-10-26T10:00:00+00:00") == 1793008800.0
    assert cli.parse_time("yesterday", now) == cli.parse_time("today", now) - 86400
    with pytest.raises(ValueError):
        cli.parse_time("not a time at all, really")


def test_rfc3339_us():
    assert cli.rfc3339_us("2026-10-26T16:04:33.542835123Z") == 1793030673542835
    assert cli.rfc3339_us("2026-10-26T16:04:33Z") == 1793030673000000
    assert cli.rfc3339_us("2026-10-26T09:04:33.5-07:00") == 1793030673500000


def test_fmt_entry_a16_journal():
    e = {"__REALTIME_TIMESTAMP": "0", "MESSAGE": "usb 1-1: new device", "SYSLOG_IDENTIFIER": "kernel", "_HOSTNAME": "zenbook-a16"}
    assert " a16/kernel " in cli.fmt_entry(e)


class FakeVL(http.server.BaseHTTPRequestHandler):
    rows: list = []
    queries: list = []
    fail = False

    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        q = urllib.parse.parse_qs(body)
        FakeVL.queries.append(q)
        if FakeVL.fail:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"cannot parse query")
            return
        self.send_response(200)
        self.end_headers()
        for r in FakeVL.rows:
            self.wfile.write((json.dumps(r) + "\n").encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def vl(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeVL)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    monkeypatch.setattr(cli, "VL", f"http://127.0.0.1:{srv.server_port}")
    FakeVL.rows, FakeVL.queries, FakeVL.fail = [], [], False
    yield FakeVL
    srv.shutdown()


def test_log_via_victorialogs(vl, capsys):
    vl.rows = [
        {"_time": "2026-10-26T16:00:01Z", "_msg": "newest", "SYSLOG_IDENTIFIER": "a16-serial"},
        {"_time": "2026-10-26T16:00:00Z", "_msg": "older", "SYSLOG_IDENTIFIER": "a16-mark", "A16_HOLDER": "carol"},
    ]
    assert cli.backend() == "vl"
    with pytest.raises(SystemExit) as e:
        cli.main(["log", "--since", "10m", "-n", "2", "-g", "old"])
    assert e.value.code == 0
    out = capsys.readouterr().out.splitlines()
    # -n queries newest first and prints them in time order.
    assert out[0].endswith("mark       [carol] older") and out[1].endswith("serial     newest")
    q = vl.queries[0]
    assert '_msg:~"(?i)old"' in q["query"][0] and "limit 2" in q["query"][0]
    assert abs(float(q["start"][0]) - (time.time() - 600)) < 5


def test_log_falls_back_to_journalctl(vl, monkeypatch, capsys):
    vl.fail = True
    called = {}

    def fake_journal(terms, since=None, until=None, lines=None, grep=None):
        called["terms"] = terms
        return [{"__REALTIME_TIMESTAMP": "0", "MESSAGE": "from the journal", "SYSLOG_IDENTIFIER": "a16-serial"}]

    monkeypatch.setattr(cli, "journal_entries", fake_journal)
    with pytest.raises(SystemExit):
        cli.main(["log", "--source", "serial"])
    cap = capsys.readouterr()
    assert "from the journal" in cap.out and "falling back to journalctl" in cap.err
    assert called["terms"] == [[("SYSLOG_IDENTIFIER", "a16-serial")]]


def rec(boot, prev_end, hlos, t, prev_boot=None):
    r = {
        "__REALTIME_TIMESTAMP": str(int(t * 1e6)),
        "MESSAGE_ID": PON_MESSAGE_ID,
        "A16_BOOT_ID": boot,
        "A16_GENERATION": "56",
        "A16_KERNEL": "7.3.0",
        "A16_PREVIOUS_END": prev_end,
        "A16_PON_HLOS": hlos,
        "A16_PON_PBS": "0008",
    }
    if prev_boot:
        r["A16_PREVIOUS_BOOT_ID"] = prev_boot
    return r


def test_pon_diff():
    assert cli.pon_diff({"hlos": "000102"}, {"hlos": "000102"}) == "hlos =ref"
    assert cli.pon_diff({"hlos": "000402", "pbs": "00"}, {"hlos": "000102", "pbs": "01"}) == "hlos +01=04(01); pbs +00=00(01)"
    assert cli.pon_diff({"hlos": "00"}, {}) == "hlos missing"
    many = cli.pon_diff({"hlos": "ff" * 12}, {"hlos": "00" * 12}, limit=2)
    assert many.endswith("+10 more")


def test_boots_with_pon(monkeypatch):
    t0 = 1_793_000_000.0
    events = [
        {"__REALTIME_TIMESTAMP": str(int((t0 + 30) * 1e6)), "A16_EVENT": "boot", "A16_BOOT_ID": B1, "A16_UPTIME": "30"},
        {
            "__REALTIME_TIMESTAMP": str(int((t0 + 1030) * 1e6)),
            "A16_EVENT": "boot",
            "A16_BOOT_ID": B2,
            "A16_UPTIME": "30",
            "A16_PREV_BOOT_ID": B1,
            "A16_PREV_UPTIME": "900",
            "A16_PREV_STATE": "run",
        },
    ]
    records = [
        rec(B1, "reboot", "000102", t0 + 20),
        # B2's record: B1 ended uncleanly, hlos byte 1 differs.
        rec(B2, "unclean", "000402", t0 + 1020, prev_boot=B1.replace("-", "")),
        # B3: only a record (no heartbeats), after a clean reboot of B2.
        rec(B3, "reboot", "000102", t0 + 2020),
    ]
    ref = cli.pick_reference(records, None)
    assert ref is records[2]
    boots = cli.build_boots(events, records, ref)
    assert [b["boot_id"] for b in boots] == [B1, B2, B3]
    assert boots[0]["ended"] == "unclean" and boots[0]["end_pon_diff"] == "hlos +01=04(01); pbs =ref"
    assert boots[1]["ended"] == "reboot" and boots[1]["end_pon_diff"] == "reference"
    assert "end_pon" not in boots[2]
    assert cli.pick_reference(records, B2[:8]) is records[1]


def test_boots_cli(vl, capsys, monkeypatch):
    t0 = time.time() - 3600
    vl.rows = []
    monkeypatch.setattr(cli, "VM", "http://127.0.0.1:9")

    def fake_fetch(terms, since=None, until=None, lines=None, grep=None, want=None):
        if terms == cli.terms_for(["boot"]):
            return [rec(B1, "reboot", "0001", t0), rec(B2, "unclean", "0101", t0 + 600)]
        return []

    monkeypatch.setattr(cli, "fetch", fake_fetch)
    with pytest.raises(SystemExit):
        cli.main(["boots"])
    out = capsys.readouterr().out
    assert "PON reference: dump read at boot 3f2c8a0e" in out
    assert "next boot found its journal ending: unclean" in out
    assert "PON at end: hlos +00=01(00); pbs =ref" in out


def test_pstore_from_journal(monkeypatch, capsys):
    recs = [
        {
            "__REALTIME_TIMESTAMP": "1793030000000000",
            "MESSAGE": "pstore record 1792815680 from 2026-10-23 21:21:20: 2468 bytes; Oops",
            "MESSAGE_ID": PSTORE_MESSAGE_ID,
            "A16_PSTORE_ID": "1792815680",
            "A16_PSTORE_TIME": "2026-10-23 21:21:20",
            "A16_PSTORE_FILES": "001/dmesg.txt",
            "A16_PSTORE_TEXT": list(b"Oops#1 Part1\nline\n"),
            "_BOOT_ID": "f392f8e4cca147299a8e622898fb3959",
        }
    ]
    monkeypatch.setattr(cli, "fetch", lambda *a, **k: recs)
    with pytest.raises(SystemExit):
        cli.main(["pstore"])
    assert "1792815680   crash 2026-10-23 21:21:20" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["pstore", "1792815680"])
    out = capsys.readouterr().out
    assert out.startswith("== pstore record 1792815680 (found at boot f392f8e4") and "Oops#1 Part1\nline\n" in out
