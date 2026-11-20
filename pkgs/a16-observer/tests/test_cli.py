import json
import os

import pytest

from a16_observer import cli


def test_fmt_entry():
    e = {
        "__REALTIME_TIMESTAMP": "1792800000123456",
        "MESSAGE": "uptime",
        "SYSLOG_IDENTIFIER": "a16-serial-tx",
        "A16_HOLDER": "alice",
    }
    s = cli.fmt_entry(e)
    assert s.endswith("tx         [alice] uptime")
    assert ".123 " in s
    e = {"__REALTIME_TIMESTAMP": "0", "MESSAGE": [104, 105], "SYSLOG_IDENTIFIER": "a16-serial", "A16_SERIAL_PARTIAL": "1"}
    assert cli.fmt_entry(e, show_source=False).endswith("(partial) hi")


def test_parse_duration():
    assert cli.parse_duration("90") == 90
    assert cli.parse_duration("2h") == 7200
    with pytest.raises(SystemExit):
        cli.parse_duration("soon")


def test_pause_resume(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "RUN", str(tmp_path))
    d = tmp_path / "vmagent"
    (d / "active").mkdir(parents=True)
    (d / "a16.json.default").write_text('[{"targets": ["x:9100"]}]\n')
    (d / "active" / "a16.json").write_text('[{"targets": ["x:9100"]}]\n')
    with pytest.raises(SystemExit) as e:
        cli.main(["metrics", "pause", "--reason", "idle power"])
    assert e.value.code == 0
    assert not (d / "active" / "a16.json").exists() and (d / "paused").exists()
    assert cli.metrics_paused()
    with pytest.raises(SystemExit):
        cli.main(["metrics", "resume"])
    assert json.loads((d / "active" / "a16.json").read_text())[0]["targets"] == ["x:9100"]
    assert not (d / "paused").exists()


def test_status_without_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "RUN", str(tmp_path))
    monkeypatch.setattr(cli, "VM", "http://127.0.0.1:9")
    st = cli.gather_status(probe=False)
    assert st["verdict"] == "silent" and st["code"] == 2 and st["serial"] is None


def test_status_heartbeat(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(cli, "RUN", str(tmp_path))
    monkeypatch.setattr(cli, "VM", "http://127.0.0.1:9")
    (tmp_path / "heartbeat.last").write_text(json.dumps({"t": time.time(), "boot_id": "b", "uptime": 5, "state": "run"}))
    assert cli.gather_status(probe=False)["code"] == 0
    (tmp_path / "heartbeat.last").write_text(json.dumps({"t": time.time() - 60, "boot_id": "b", "uptime": 5, "state": "suspending"}))
    assert cli.gather_status(probe=False)["verdict"] == "suspended"


def test_redflags_scan_dedupes_sources():
    """The 10-27 23:58 hed clash as the journal, netconsole (parsed and raw
    record) and serial each deliver it: one event, three sources."""
    clash = "qcom-soundwire 6c80000.soundwire: qcom_swrm_irq_handler: SWR bus clash detected"
    t = "1793084323283032"
    entries = [
        {"MESSAGE": clash, "SYSLOG_IDENTIFIER": "kernel", "_SOURCE_MONOTONIC_TIMESTAMP": "8045181370", "__REALTIME_TIMESTAMP": t},
        {"MESSAGE": clash, "SYSLOG_IDENTIFIER": "a16-netconsole", "A16_KTIME_USEC": "8045181370", "__REALTIME_TIMESTAMP": t},
        {"MESSAGE": "7.3.0,3,1456,8045181370,-;" + clash, "SYSLOG_IDENTIFIER": "a16-netconsole", "__REALTIME_TIMESTAMP": t},
        {"MESSAGE": "[ 8045.181370] " + clash, "SYSLOG_IDENTIFIER": "a16-serial", "__REALTIME_TIMESTAMP": t},
        {"MESSAGE": "wsa884x-codec sdw:1:0:0217:0204:00:1: Parity error detected", "SYSLOG_IDENTIFIER": "kernel",
         "_SOURCE_MONOTONIC_TIMESTAMP": "8045181618", "__REALTIME_TIMESTAMP": t},
        {"MESSAGE": "dwc3-qcom a600000.usb: port-1 HS-PHY not in L2", "SYSLOG_IDENTIFIER": "kernel",
         "_SOURCE_MONOTONIC_TIMESTAMP": "8000000000", "__REALTIME_TIMESTAMP": t},
    ]
    tally, events = cli.redflags_scan(entries, "warn")
    assert [(p.id, sorted(s)) for _, p, _, s in events] == [
        ("swr-bus-clash", ["kernel", "netconsole", "serial"]),
        ("wsa-parity", ["kernel"]),
    ]
    assert tally.by_severity() == {"crit": 2, "warn": 0, "info": 0}
    tally, _ = cli.redflags_scan(entries, "info")
    assert tally.hits["usb-hsphy-not-l2"].count == 1
