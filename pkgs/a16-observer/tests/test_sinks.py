import json

from a16_observer import journal as jmod
from a16_observer.sinks import (
    HeartbeatSink,
    MetricsBridge,
    NetconsoleSink,
    SeqSet,
    heartbeat_samples,
    parse_heartbeat,
    parse_netconsole,
)

B1 = "3f2c8a0e-1d2b-4c5d-9e8f-0a1b2c3d4e5f"
B2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_parse_v1_and_v2():
    hb = parse_heartbeat(f"{B1} 123.45\n".encode())
    assert hb == {"boot_id": B1, "uptime": 123.45, "fields": {}}
    hb = parse_heartbeat(f"{B1} 5.0 v=2 temp_max_mc=48200 bat_uw=-6510000 state=run junk bad-key=1".encode())
    assert hb["fields"] == {"v": "2", "temp_max_mc": "48200", "bat_uw": "-6510000", "state": "run"}


def test_parse_rejects():
    assert parse_heartbeat(b"hello world") is None
    assert parse_heartbeat(f"{B1} nan-ish".encode()) is None
    assert parse_heartbeat(b"\xff\xfe") is None
    assert parse_heartbeat(f"{B1}".encode()) is None


def test_samples():
    hb = parse_heartbeat(f"{B1} 5.5 v=2 temp_max_mc=48200 load1=0.42 state=suspending".encode())
    lines = heartbeat_samples(hb, "192.0.2.165", 1000.0)
    assert 'a16_hb_uptime_seconds{host="zenbook-a16",src="192.0.2.165"} 5.5 1000000' in lines
    assert 'a16_hb_temp_max_mc{host="zenbook-a16",src="192.0.2.165"} 48200 1000000' in lines
    assert 'a16_hb_load1{host="zenbook-a16",src="192.0.2.165"} 0.42 1000000' in lines
    assert 'a16_hb_state{host="zenbook-a16",src="192.0.2.165",state="suspending"} 1 1000000' in lines
    assert f'a16_hb_boot_info{{host="zenbook-a16",src="192.0.2.165",boot_id="{B1}"}} 1 1000000' in lines
    assert not any(line.startswith("a16_hb_v{") for line in lines)


def test_heartbeat_events(tmp_path, journal):
    bridge = MetricsBridge(None, journal)
    s = HeartbeatSink(str(tmp_path), bridge, journal)
    s.receive(f"{B1} 10.0 v=2 iv=1 state=run".encode(), "192.0.2.165", 100.0)
    last = json.loads((tmp_path / "heartbeat.last").read_text())
    assert last["boot_id"] == B1 and last["src"] == "192.0.2.165" and last["state"] == "run" and last["t"] == 100.0
    s.receive(f"{B1} 11.0 v=2 iv=1 state=run".encode(), "192.0.2.165", 101.0)
    s.check(103.0)  # 2 s: not lost yet
    assert [e.get("A16_EVENT") for e in journal.entries] == ["boot"]
    s.receive(f"{B1} 12.0 v=2 iv=1 state=suspending susp=3".encode(), "192.0.2.167", 102.0)
    s.check(110.0)
    s.check(111.0)  # lost only once
    s.receive(f"{B1} 20.0 v=2 iv=1 state=resumed".encode(), "192.0.2.167", 150.0)
    s.receive(f"{B2} 3.0 v=2 iv=1 state=run".encode(), "192.0.2.165", 400.0)
    ev = [e.get("A16_EVENT") for e in journal.entries]
    assert ev == ["boot", "state", "route", "lost", "resumed", "state", "boot"], ev
    lost = [e for e in journal.entries if e.get("A16_EVENT") == "lost"][0]
    assert lost["A16_STATE"] == "suspending" and "uptime 12.0s" in lost["MESSAGE"]
    boot = journal.entries[-1]
    assert boot["A16_PREV_BOOT_ID"] == B1 and boot["A16_PREV_UPTIME"] == 20.0 and boot["A16_PREV_STATE"] == "resumed"


def test_heartbeat_malformed_logged_once_a_minute(tmp_path, journal):
    s = HeartbeatSink(str(tmp_path), MetricsBridge(None, journal), journal)
    for t in range(5):
        s.receive(b"garbage", "1.2.3.4", 1000.0 + t)
    assert len(journal.entries) == 1


def test_bridge_buffers_on_failure(journal):
    b = MetricsBridge("http://127.0.0.1:9/api/v1/import/prometheus", journal)
    b.add(["a 1 1", "b 2 2"])
    b.flush()
    assert list(b.lines) == ["a 1 1", "b 2 2"]
    assert "failed" in journal.entries[0]["MESSAGE"]
    b.flush()
    assert len(journal.entries) == 1  # failure logged once


def test_netconsole_extended():
    r = parse_netconsole(b"6,1234,5678901,-;hello world\n SUBSYSTEM=usb\n boot=" + B1.encode() + b"\n")
    assert r == [
        {
            "level": 6,
            "facility": 0,
            "seq": 1234,
            "usec": 5678901,
            "flags": {"-": ""},
            "text": "hello world",
            "meta": {"SUBSYSTEM": "usb", "boot": B1},
        }
    ]
    r = parse_netconsole(b"4,7,8,c,ncfrag=0/2000;part")
    assert r[0]["flags"]["ncfrag"] == "0/2000" and r[0]["level"] == 4


def test_netconsole_plain():
    assert parse_netconsole(b"[  1.0] a\r\n[  2.0] b\n\n") == [{"text": "[  1.0] a"}, {"text": "[  2.0] b"}]


def test_netconsole_dedup_and_gap(journal):
    s = NetconsoleSink(journal)
    for seq in (1, 2, 2, 3, 6, 4):
        s.receive(f"6,{seq},{seq * 1000},-;msg {seq}\n boot={B1}\n".encode(), "192.0.2.165")
    msgs = journal.messages()
    assert msgs[:3] == ["msg 1", "msg 2", "msg 3"]
    assert "2 message(s) missing (seq 4..5)" in msgs[3]
    assert msgs[4:] == ["msg 6", "msg 4"]
    assert journal.entries[0]["A16_BOOT_ID"] == B1 and journal.entries[0]["A16_SEQ"] == 1
    s.receive(b"plain line\n", "192.0.2.165")
    assert journal.entries[-1]["MESSAGE"] == "plain line"


def test_journal_encode_binary_field():
    d = jmod.encode("x", "two\nlines", 6, {"A16_SEQ": 3, "skip": None})
    assert b"MESSAGE\n" in d and b"A16_SEQ=3\n" in d and b"SKIP" not in d
    assert b"SYSLOG_IDENTIFIER=x\n" in d


def test_escape():
    assert jmod.escape(b"login: \x1b[1mroot\r") == "login: \\x1b[1mroot"
    assert jmod.escape("ünï\tx".encode()) == "ünï\tx"
    assert jmod.escape(b"\xff") == "\\xff"


def test_netconsole_release_prefix():
    r = parse_netconsole(b"7.3.0-a16+,6,42,99,-;with release\n boot=" + B1.encode() + b"\n")
    assert r[0]["release"] == "7.3.0-a16+" and r[0]["seq"] == 42 and r[0]["text"] == "with release"
    assert r[0]["meta"]["boot"] == B1
    # A plain line with commas is not mistaken for a release prefix.
    assert parse_netconsole(b"foo,1,2,3 bar\n") == [{"text": "foo,1,2,3 bar"}]


def test_seqset():
    s = SeqSet()
    for x in (5, 7, 6, 1, 2, 10):
        assert s.add(x)
    assert s.r == [[1, 2], [5, 7], [10, 10]]
    assert not s.add(6) and 6 in s and 3 not in s and 10 in s and 11 not in s
    s.add(3)
    s.add(4)
    assert s.r == [[1, 7], [10, 10]]


def test_netconsole_replay_merges_without_gaps(journal):
    s = NetconsoleSink(journal)
    rel = b"7.3.0,"
    # Live stream starts at seq 100 (netconsole attached late) ...
    for seq in (100, 101):
        s.receive(rel + f"6,{seq},{seq},-;live {seq}\n boot={B1}\n".encode(), "192.0.2.165")
    # ... the replay of the ring buffer brings 98..101: 98 and 99 are new,
    # 100 and 101 are duplicates, and nothing is reported as a gap.
    for seq in range(98, 102):
        s.receive(rel + f"6,{seq},{seq},-;replay {seq}\n boot={B1}\n replay=1\n".encode(), "192.0.2.165")
    s.receive(rel + f"6,102,102,-;live 102\n boot={B1}\n".encode(), "192.0.2.165")
    assert journal.messages() == ["live 100", "live 101", "replay 98", "replay 99", "live 102"]
    assert journal.entries[2]["A16_REPLAY"] == "1" and journal.entries[0]["A16_REPLAY"] is None
    assert journal.entries[0]["A16_KERNEL_RELEASE"] == "7.3.0"


def test_netconsole_fragments_kept(journal):
    s = NetconsoleSink(journal)
    s.receive(f"4,9,9,-,ncfrag=0/1500;first half\n boot={B1}\n".encode(), "x")
    s.receive(f"4,9,9,-,ncfrag=1000/1500;second half\n boot={B1}\n".encode(), "x")
    s.receive(f"4,9,9,-,ncfrag=1000/1500;second half\n boot={B1}\n".encode(), "x")
    assert journal.messages() == ["first half", "second half"]
