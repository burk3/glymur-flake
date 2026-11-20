"""Kernel red flag patterns (a16_observer/redflags.py) on real A16 log lines
(tests/fixtures/redflags/lines.txt: expected id, tab, line)."""

import os
import re

import pytest

from a16_observer import redflags as rf

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "redflags", "lines.txt")


def fixture_lines():
    out = []
    with open(FIX) as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            want, _, text = line.partition("\t")
            out.append(pytest.param(want, text, id=f"{n}:{want}"))
    return out


@pytest.mark.parametrize("want,line", fixture_lines())
def test_fixture_line(want, line):
    p = rf.classify(line)
    assert (p.id if p else "-") == want, line


def test_every_pattern_has_a_fixture_line():
    with open(FIX) as f:
        seen = {ln.split("\t", 1)[0] for ln in f if "\t" in ln and not ln.startswith("#")}
    missing = [p.id for p in rf.PATTERNS if p.id not in seen]
    assert not missing, f"add a real (or printk-format) line for {missing} to {FIX}"


def test_pattern_table_is_well_formed():
    ids = [p.id for p in rf.PATTERNS]
    assert len(ids) == len(set(ids)), "duplicate ids"
    for p in rf.PATTERNS:
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", p.id), p.id
        assert p.sev in rf.SEVERITIES, p.id
        assert p.subsystem, p.id
        assert p.bead is None or re.fullmatch(r"[a-z0-9]{3}(\.\d+)*", p.bead), p.id


def test_regexes_are_portable():
    """VictoriaLogs (Go RE2) and journalctl -g (PCRE2) take the same regex:
    no lookaround, no backreferences, no global inline flags."""
    for p in rf.PATTERNS:
        r = p.regex
        assert not re.search(r"\(\?[=!<]", r), f"{p.id}: lookaround"
        assert not re.search(r"\\[1-9]", r), f"{p.id}: backreference"
        assert not re.search(r"\(\?[a-z]+\)", r), f"{p.id}: global inline flag; use (?i:...)"
    # the combined prefilter compiles and keeps every fixture red flag
    pre = re.compile(rf.combined_regex("info"))
    for param in fixture_lines():
        want, line = param.values
        if want != "-":
            assert pre.search(line), line


def test_combined_regex_min_severity():
    warn = re.compile(rf.combined_regex("warn"))
    assert warn.search("qcom-soundwire 6c80000.soundwire: qcom_swrm_irq_handler: SWR bus clash detected")
    assert not warn.search("dwc3-qcom a600000.usb: port-1 HS-PHY not in L2")
    # anchored patterns still match behind a console timestamp
    assert warn.search("[  123.456789] WARNING: CPU: 3 PID: 1 at foo.c:1 bar+0x0/0x4")


def test_split_ts():
    assert rf.split_ts("[ 1213.739046] usb 1-1: x") == (1213739046, "usb 1-1: x")
    assert rf.split_ts("[    0.5] y") == (500000, "y")
    assert rf.split_ts("no stamp") == (None, "no stamp")
    # raw extended netconsole record
    assert rf.split_ts("7.3.0,3,6812,2494284096,-;qcom-soundwire 6c80000.soundwire: x") == (2494284096, "qcom-soundwire 6c80000.soundwire: x")
    assert rf.split_ts("3,6812,2494284096,-,caller=T1;msg\n SUBSYSTEM=platform") == (2494284096, "msg")
    assert rf.split_ts("usb 1-1: speed 5,0,1;x") == (None, "usb 1-1: speed 5,0,1;x")


def test_tally():
    t = rf.Tally()
    lines = [
        "qcom-soundwire 6c80000.soundwire: qcom_swrm_irq_handler: SWR bus clash detected",
        "wsa884x-codec sdw:1:0:0217:0204:00:1: Parity error detected",
        "wsa884x-codec sdw:1:0:0217:0204:00:0: Parity error detected",
        "dwc3-qcom a600000.usb: port-1 HS-PHY not in L2",
        "usb 5-1: new high-speed USB device number 3 using xhci-hcd",
    ]
    for i, ln in enumerate(lines):
        t.add(ln, t=i, source="kernel")
    assert t.by_severity() == {"crit": 3, "warn": 0, "info": 1}
    r = t.ranked("warn")
    assert [h.pattern.id for h in r] == ["wsa-parity", "swr-bus-clash"]
    assert r[0].count == 2 and r[0].first_t == 1 and r[0].last_t == 2 and r[0].last.endswith("00:0: Parity error detected")
    assert rf.summary_line(t.by_severity(), [("wsa-parity", 2)]) == "crit 3, warn 0, info 1; top: wsa-parity 2"
