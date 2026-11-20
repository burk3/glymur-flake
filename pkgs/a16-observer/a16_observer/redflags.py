"""Kernel red flags: known-bad (and known-benign) kernel log signatures.

The one list every tool uses: a16-snap's `redflags` collector, the
`a16-telemetry redflags` counter on the A16 (a16_redflag_total), and
the observer host's `a16 redflags` / `a16 status` (docs/a16-snap.md "Red flags").
It exists because on 2026-10-26 dozens of "SWR bus clash detected" and
"wsa884x ... Parity error detected" lines went by during a 63-cycle suspend
test and nobody saw them until an amp died and the A16 reset (bead hed).

Each pattern has an id (stable: it is a metric label and a diff key), a
severity, a subsystem, the bead that tracks it, and a regex over one kernel
log message (the text after the "[    1.234567] " timestamp, which `message`
strips). The first pattern in list order that matches wins, so specific
entries (known-benign ones, the rtl8159 firmware) come before the general
ones they would otherwise fall into.

Severities:
  crit  a device, data or the machine can be lost: oops/BUG/panic, lockups,
        stalls, SMMU faults, DSP crashes, the APM wedge, the SoundWire faults
        that preceded the hed amp loss, MHI power-on failures, NVMe timeouts.
  warn  something failed or was retried and recovered (or not): USB -71 and
        enumeration failures, WARN splats, UBSAN, DPU timeouts, missing firmware.
  info  known and understood noise, kept so a count is still visible but out of
        the way: "HS-PHY not in L2", the pcie 1bf0000 L2 entry timeout, sync_state
        timeouts, dummy regulators.

Regexes must work unchanged in Python `re`, VictoriaLogs (Go RE2) and
journalctl -g (PCRE2): no lookaround, no backreferences; case-insensitive
parts use a scoped (?i:...) group. tests/test_redflags.py checks this and
matches every entry against real A16 lines (tests/fixtures/redflags/).

Adding one: when a test shows a new failure signature, add an entry here
(before any broader entry that would catch it), a real line for it in
tests/fixtures/redflags/lines.txt, and bump SNAP_VERSION
(a16_observer/snap/__init__.py) so `a16 snap` ships the new list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SEVERITIES = ("info", "warn", "crit")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
BEAD_PREFIX = "snapx2ee-bringup-"


@dataclass(frozen=True)
class Pattern:
    id: str
    sev: str
    subsystem: str
    regex: str
    bead: str | None = None  # short bead id ("hed"), BEAD_PREFIX + it is the full one
    note: str = ""
    rx: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "rx", re.compile(self.regex))

    @property
    def bead_id(self) -> str | None:
        return BEAD_PREFIX + self.bead if self.bead else None


P = Pattern
PATTERNS: list[Pattern] = [
    # --- known benign (info) first, so the broader entries below don't take them
    P("usb-hsphy-not-l2", "info", "usb", r"HS-PHY not in L2", None,
      "dwc3-qcom on every suspend; FixItFoundry's message, harmless so far"),
    P("pcie-1bf0000-l2-timeout", "info", "pcie", r"qcom-pcie 1bf0000\.pcie: Timeout waiting for L2 entry", "v2c",
      "every s2idle entry; the empty slot's L2 handshake"),
    P("sync-state-timeout", "info", "boot", r"Timed out\. Forcing sync_state\(\)", None),
    P("dummy-regulator", "info", "boot", r"supply \S+ not found, using dummy regulator", None),
    P("pm-runtime-unbalanced", "info", "boot", r"Unbalanced pm_runtime_enable!", None, "coresight tpdm/tpda/funnel"),
    P("pm-parent-sleeping", "info", "pm", r"PM: parent \S+ should not be sleeping", None),
    P("edp-link-ok", "info", "display", r"^eDP link ok: ", "hu0", "our eDP retry patch's status line (logged at err)"),
    P("msm-no-gpu", "info", "display", r"msm_dpu \S+: no GPU device was found", None),
    P("edp-lt-retry", "info", "display", r"eDP-\d+: link training attempt \d+ failed", "hu0",
      "eDP first-attempt failure; the retry patch recovers it"),
    P("swr-spurious-irq", "info", "audio", r"no slave alert found\.?\s*spurious interrupt", "hed"),
    P("psci-pc-mode", "info", "firmware", r"psci: \[Firmware Bug\]: failed to set PC mode", None, "every boot"),
    P("fw-rtl8159-missing", "info", "usb", r"rtl_nic/rtl8159-1\.fw", "i25", "not in linux-firmware; r8152 runs without it"),
    P("wdt-did-not-stop", "info", "watchdog", r"watchdog\d*: watchdog did not stop!", None, "systemd closing /dev/watchdog0"),
    P("wdt-timeout-einval", "info", "watchdog", r"Failed to set watchdog hardware timeout to", "1wm"),
    P("scmi-raw-buffers", "info", "firmware", r"arm-scmi \S+: RAW\[\d+\] - Buffers exhausted", None),
    # --- kernel core (crit); the watchdog pretimeout panic first, so it is not "oops"
    P("wdt-pretimeout", "crit", "watchdog", r"pretimeout event|qcom_wdt.*bark|Watchdog bark", "157",
      "qcom_wdt pretimeout (panic governor) before the hardware watchdog resets"),
    P("soft-lockup", "crit", "kernel", r"soft lockup - CPU#\d+ stuck|hard LOCKUP|Watchdog detected hard LOCKUP", None),
    P("rcu-stall", "crit", "kernel",
      r"rcu: INFO: \S+ (self-)?detected stalls? on|self-detected stall on CPU|rcu_\w+ kthread (starved|timer wakeup)", None),
    P("hung-task", "crit", "kernel", r"INFO: task \S+ blocked for more than \d+ seconds", None),
    P("oops", "crit", "kernel",
      r"Internal error: |Unable to handle kernel |Kernel panic - not syncing|kernel BUG at |^BUG: |^Oops", None),
    P("ubsan", "warn", "kernel", r"^UBSAN: ", "rrj", "rrj is the CAMSS one; sound/soc ones are the jack/SDCA work"),
    P("warn-splat", "warn", "kernel", r"^WARNING: ", None, "a WARN_ON; the call trace follows"),
    P("fw-bug", "warn", "firmware", r"\[Firmware Bug\]", None),
    # --- watchdog / DSPs / SMMU -------------------------------------------------
    # SoCCP stopped by TZ: on 10-29 04:02 (reset-B3) TZ force-stopped SoCCP right
    # after an s2idle resume, then sent err-fatal to the CDSP and ADSP; before the
    # generic rproc-crash, which would file them under fb5
    P("soccp-tz-force-stop", "crit", "remoteproc",
      r"fatal error received: \S+:TZ force stop|crash detected in soccp: type", "81s",
      "TZ force-stopped SoCCP (or SoCCP crashed); the DSPs get err-fatal next"),
    P("rproc-tz-err-fatal", "crit", "remoteproc",
      r"fatal error received: \S+:err fatal notification received from TZ", "81s",
      "a DSP taken down by TZ, usually after soccp-tz-force-stop"),
    P("rproc-crash", "crit", "remoteproc",
      r"crash detected in \S+: type|handling crash #\d+ in|remoteproc\d+: recovering|(watchdog|fatal error) received: ",
      "fb5", "ADSP/CDSP SSR; never restart the DSPs by hand (CLAUDE.md)"),
    P("rproc-boot-fail", "crit", "remoteproc", r"remoteproc\d+: Boot failed|q6v5\S* .*(start|handover) timed out", None),
    P("smmu-context-fault", "crit", "iommu",
      r"Unhandled context fault|Unexpected global fault|arm-smmu \S+: GFSR ", "fb5"),
    P("iommu-fault", "warn", "iommu", r"iommu fault: da 0x", "fb5", "the client's side of an SMMU fault"),
    # --- audio (hed, 99a) -------------------------------------------------------
    P("swr-bus-clash", "crit", "audio", r"SWR bus clash detected", "hed",
      "preceded the left WSA884x pair dropping out (Alert) and the 10-28 00:38 reset"),
    P("wsa-parity", "crit", "audio", r"sdw:\S+: Parity error detected|wsa88\w+.*Parity error", "hed"),
    P("swr-port-collision", "warn", "audio", r"SWR Port collision detected", "hed"),
    P("swr-unknown-irq", "warn", "audio", r"SWR unknown interrupt value", "hed"),
    P("wsa-port-fail", "warn", "audio", r"wsa88\w+-codec \S+: (Test fail for port|Reached MAX_RETRY on)", "hed"),
    P("apm-cmd-timeout", "crit", "audio", r"CMD timeout for \[\w+\]|gprsvc:\S+ .*timeout", "99a",
      "the APM wedge (docs/audio-apm-wedge.md): only a reboot recovers"),
    P("apm-port-fail", "crit", "audio",
      r"Failed to (start|prepare|open|stop) APM|APM is not ready|(?i:q6apm)\S* .*(fail|error|timed? ?out)|qcom-prm.*(timeout|fail)",
      "99a"),
    # rnf: an HP stream held over s2idle (gens 71, 90): on resume every HP stream
    # start fails the codec's PDE power-up; the jack stays silent until a reboot
    P("sdca-pde-timeout", "crit", "audio",
      r"PDE \d+: pde transition \d+ -> \d+ failed|ASoC: \S+: PDE \d+ event failed", "rnf",
      "SDCA codec power domain stuck after s2idle: headphones dead until reboot"),
    # 8cq: v7.4-rc2's devm_kzalloc_flex() preset rtd->num_components, so no
    # component probed and every DT route failed: no sound card at all (gen 109)
    P("asoc-route-fail", "crit", "audio", r"ASoC: Failed to add route ", "8cq",
      "card routes failed: no sound card (widgets missing)"),
    P("asoc-error", "warn", "audio", r"ASoC error \(-\d+\)|ASoC: .*(failed|error) -?\d+", "rnf"),
    # --- USB (i25) --------------------------------------------------------------
    P("usb-overcurrent", "crit", "usb", r"(?i:over-?current)", "i25"),
    P("xhci-died", "crit", "usb", r"xHCI host (controller )?not responding|HC died|Host System Error|xHCI host controller .*dying",
      None),
    P("usb-enumerate", "warn", "usb",
      r"unable to enumerate USB device|cannot reset \(err|Cannot enable\. Maybe the USB cable is bad|connect-debounce failed|cannot disable",
      "i25"),
    P("usb-err-71", "warn", "usb",
      r"device descriptor read/\S+, error -\d+|not accepting address \d+, error|Device not responding to setup address|usb \S+: .*error -71",
      "i25", "protocol errors (-71) during enumeration"),
    P("xhci-ctx-state", "warn", "usb", r"WARN: invalid context state for evaluate context", None),
    P("r8152-tx-status", "warn", "net", r"r8152 \S+ \S+: [TR]x status -?\d+", "i25"),
    P("r8152-cfgselector-reset", "warn", "usb", r"r8152-cfgselector \S+: reset ", "i25"),
    P("r8152-error", "warn", "net", r"r8152 \S+.*(USB reset failed|Get ether addr fail)", "i25"),
    P("typec-altmode-fail", "warn", "usb", r"pmic_glink_altmode.*(failed to switch mux|configuration failed)", "920"),
    # --- Wi-Fi (157, b82.8) -----------------------------------------------------
    P("mhi-power-fail", "crit", "wifi",
      r"failed to set mhi state|failed to start mhi|mhi\S* .*(timed? ?out|Failed to)|timeout while waiting for restart complete",
      "157", "the s2idle resume MHI POWER_ON stall"),
    P("ath12k-mlo-peer-leak", "warn", "wifi",
      r"ath12k\S* \S+: (unable to create ath12k_dp_peer for sta \S+, ret: -17|failed to verify assoc link setting)",
      "b82.8", "stale MLO dp_peer after a failed roam; MLO reconnects fail until ath12k reload"),
    P("ath12k-error", "warn", "wifi",
      r"ath12k\S* \S+: .*(fail|timeout|error)|Hardware became unavailable upon resume", "157"),
    P("pm-resume-fail", "warn", "pm",
      r"PM: (dpm_run_callback\(\): .* returns -\d+|failed to (suspend|resume|freeze|thaw)\S*( async)?( early| late| noirq)?: error)|PM: Some devices failed to suspend",
      None),
    # --- PCIe / NVMe ------------------------------------------------------------
    P("pcie-link-down", "crit", "pcie",
      r"qcom-pcie \S+: .*(Link down|link down|Phy link never came up|link is down)|PCIe Bus Error|AER: (Corrected|Uncorrected|Multiple)",
      None),
    P("pcie-l2-timeout", "warn", "pcie", r"Timeout waiting for L2 entry", "v2c"),
    P("nvme-timeout", "crit", "storage",
      r"nvme\S* \S+: (I/O .*timeout|controller is down|Removing after probe failure|resetting controller|Device not ready)|I/O error, dev nvme",
      None),
    # --- thermal ----------------------------------------------------------------
    P("thermal-critical", "crit", "thermal",
      r"(?i:critical temperature reached|thermal.*(shutdown|emergency)|overheat|over-temperature)", "baw"),
    P("thermal-throttle", "warn", "thermal", r"thermal\S* \S+: .*(?i:throttl)|lmh\S* .*(?i:throttl)|(?i:throttling (enabled|activated|started))",
      "fci"),
    # --- display / GPU (920, dso) -------------------------------------------------
    P("gpu-hang", "crit", "display", r"hangcheck (detected|recover)|\*\*\* gpu fault|GPU hang|adreno \S+: .*fault", None),
    P("dpu-error", "warn", "display", r"\[dpu error\]|\[drm:dpu_\S+.*\*ERROR\*", "920", "frame done / vblank / commit timeouts"),
    P("dp-lt-fail", "warn", "display", r"link training failed after|link training .*failed", "920"),
    P("drm-error", "warn", "display", r"\[drm[:\]].*\*ERROR\*", "920"),
    P("lab-diag", "info", "display", r"LAB-DIAG", "dso",
      "the qmp-combo dp-poweron diagnostic dumps; its *ERROR* lines are drm-error above"),
    # --- firmware / probe / buses -------------------------------------------------
    P("firmware-missing", "warn", "firmware",
      r"Direct firmware load for \S+ failed|unable to load firmware|failed to load firmware|firmware: failed to load", None),
    P("deferred-probe", "warn", "boot", r"deferred probe pending", None),
    P("i2c-gpi-error", "warn", "bus", r"GPI transfer failed|DMA txn failed|gpi \S+: Error in Transaction", None),
    P("camss-error", "warn", "camera", r"qcom-camss \S+: .*(unmapped index|fail|error)", "rrj"),
]
del P

BY_ID = {p.id: p for p in PATTERNS}
_TS = re.compile(r"^\s*(?:<\d+>)?\[\s*(\d+)\.(\d+)\]\s?")
# A raw extended netconsole/kmsg record: "[release,]prio,seq,usec,flags;message".
_EXT = re.compile(r"^(?:[^,;\s]+,)?\d+,\d+,(\d+),[^;]*;")


def split_ts(line: str) -> tuple[int | None, str]:
    """('[ 1213.739046] usb 1-1: ...') -> (1213739046, 'usb 1-1: ...'): the
    kernel timestamp in microseconds (None if absent) and the message. Also
    takes a raw extended record ('7.3.0,3,6812,2494284096,-;msg'), as some
    netconsole entries carry."""
    m = _TS.match(line)
    if m:
        frac = m.group(2)[:6].ljust(6, "0")
        return int(m.group(1)) * 1_000_000 + int(frac), line[m.end():]
    m = _EXT.match(line)
    if m:
        return int(m.group(1)), line[m.end():].split("\n", 1)[0]
    return None, line


def message(line: str) -> str:
    return split_ts(line)[1]


def patterns(min_sev: str = "info") -> list[Pattern]:
    r = SEV_RANK[min_sev]
    return [p for p in PATTERNS if SEV_RANK[p.sev] >= r]


def combined_regex(min_sev: str = "info") -> str:
    """One alternation of the patterns at or above min_sev, for a server-side
    prefilter (LogsQL `_msg:~`, journalctl -g). Lines it returns are still
    classified with `classify`, which may file them under an info pattern
    listed earlier; such lines are dropped by the caller's severity filter.
    The `^` anchors become start-of-text, a console timestamp or a raw
    record's ';', so serial and netconsole lines match too."""
    parts = []
    for p in patterns(min_sev):
        parts.append("(?:" + p.regex.replace("^", r"(?:^|\] |;)") + ")")
    return "|".join(parts)


_PREFILTER: dict[str, re.Pattern] = {}


def classify(line: str) -> Pattern | None:
    """The first pattern matching this kernel log line (console timestamp
    allowed), or None."""
    msg = message(line)
    pre = _PREFILTER.get("info")
    if pre is None:
        pre = _PREFILTER["info"] = re.compile(combined_regex("info"))
    if not pre.search(msg):
        return None
    for p in PATTERNS:
        if p.rx.search(msg):
            return p
    return None


@dataclass
class Hit:
    pattern: Pattern
    count: int = 0
    first: str | None = None
    last: str | None = None
    first_t: float | None = None  # caller's time (kmsg usec, epoch s, ...)
    last_t: float | None = None
    sources: set = field(default_factory=set)


class Tally:
    """Counts per pattern with the first and last matching line."""

    def __init__(self):
        self.hits: dict[str, Hit] = {}

    def add(self, line: str, t=None, source: str | None = None, pattern: Pattern | None = None, n: int = 1) -> Pattern | None:
        p = pattern or classify(line)
        if p is None:
            return None
        h = self.hits.get(p.id)
        if h is None:
            h = self.hits[p.id] = Hit(p)
        h.count += n
        if h.first is None:
            h.first, h.first_t = line, t
        h.last, h.last_t = line, t
        if source:
            h.sources.add(source)
        return p

    def by_severity(self) -> dict[str, int]:
        out = {s: 0 for s in reversed(SEVERITIES)}
        for h in self.hits.values():
            out[h.pattern.sev] += h.count
        return out

    def ranked(self, min_sev: str = "info") -> list[Hit]:
        """Hits at or above min_sev: worst severity first, then most frequent."""
        r = SEV_RANK[min_sev]
        hs = [h for h in self.hits.values() if SEV_RANK[h.pattern.sev] >= r]
        return sorted(hs, key=lambda h: (-SEV_RANK[h.pattern.sev], -h.count, h.pattern.id))


def summary_line(by_sev: dict[str, int], top: list[tuple[str, int]] | None = None) -> str:
    """'crit 3, warn 12, info 40; top: swr-bus-clash 2, usb-err-71 9'."""
    s = ", ".join(f"{k} {by_sev.get(k, 0)}" for k in reversed(SEVERITIES))
    if top:
        s += "; top: " + ", ".join(f"{i} {n}" for i, n in top)
    return s
