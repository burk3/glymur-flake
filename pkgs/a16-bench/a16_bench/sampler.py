"""1 Hz trace of temperatures, clocks, fans and battery power during a test.

The column set is fixed when the sampler is created, so every row of a CSV
has the same columns. summarize() is pure (tests feed it rows).
"""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path
from statistics import mean
from typing import Any

from .sysfs import Probe, read, read_int


class Channels:
    """What to sample: (column, path, scale, kind) discovered once."""

    def __init__(self, probe: Probe):
        self.cols: list[tuple[str, Path, float]] = []
        self.zone_kind: dict[str, str] = {}  # column -> cpu|gpu|soc|other
        self.cpu_hw_max: dict[str, float] = {}  # policy -> MHz
        self.gpu_hw_max: float | None = None  # MHz
        seen: set[str] = set()
        for z in probe.zones():
            key = z.type if z.type not in seen else f"{z.type}#{z.name}"
            seen.add(z.type)
            col = f"tz.{key}"
            self.cols.append((col, z.path / "temp", 1e-3))
            self.zone_kind[col] = z.kind
        for pol in probe.cpu_policies():
            self.cols.append((f"cpu.{pol.name}.mhz", pol.path / "scaling_cur_freq", 1e-3))
            self.cols.append((f"cpu.{pol.name}.cap_mhz", pol.path / "scaling_max_freq", 1e-3))
            if pol.max_khz:
                self.cpu_hw_max[pol.name] = pol.max_khz / 1000
        gpu = probe.gpu_devfreq()
        if gpu:
            self.cols.append(("gpu.mhz", gpu.path / "cur_freq", 1e-6))
            self.cols.append(("gpu.cap_mhz", gpu.path / "max_freq", 1e-6))
            avail = read(gpu.path / "available_frequencies", "") or ""
            freqs = [int(x) for x in avail.split() if x.isdigit()]
            if freqs:
                self.gpu_hw_max = max(freqs) / 1e6
        for name, path in probe.fans():
            self.cols.append((f"fan.{name}", path, 1.0))
        bat = probe.battery()
        if bat:
            self.cols.append(("bat.power_mw", bat[1] / "power_now", 1e-3))
            self.cols.append(("bat.capacity", bat[1] / "capacity", 1.0))
            self._bat_status = bat[1] / "status"
        else:
            self._bat_status = None

    @property
    def names(self) -> list[str]:
        return ["t"] + [c for c, _, _ in self.cols] + ["bat.discharging"]

    def sample(self, t: float) -> dict[str, float | None]:
        row: dict[str, float | None] = {"t": round(t, 2)}
        for col, path, scale in self.cols:
            v = read_int(path)
            row[col] = None if v is None else round(v * scale, 3)
        if self._bat_status is not None:
            row["bat.discharging"] = 1.0 if read(self._bat_status) == "Discharging" else 0.0
        else:
            row["bat.discharging"] = None
        return row

    def die_max(self, row: dict[str, Any]) -> float | None:
        vals = [row[c] for c, k in self.zone_kind.items() if k != "other" and row.get(c) is not None]
        return max(vals) if vals else None


class Sampler:
    """Background sampler. Use as a context manager around a workload."""

    def __init__(self, channels: Channels, csv_path: Path | None, interval: float = 1.0, abort_c: float | None = None):
        self.ch = channels
        self.csv_path = csv_path
        self.interval = interval
        self.abort_c = abort_c
        self.rows: list[dict[str, Any]] = []
        self.over_limit = threading.Event()
        self.over_limit_c: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._t0 = time.monotonic()
        self._fh = open(self.csv_path, "w", newline="") if self.csv_path else None
        self._w = csv.DictWriter(self._fh, fieldnames=self.ch.names) if self._fh else None
        if self._w:
            self._w.writeheader()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._fh:
            self._fh.close()
        return False

    def _loop(self):
        while not self._stop.is_set():
            row = self.ch.sample(time.monotonic() - self._t0)
            self.rows.append(row)
            if self._w:
                self._w.writerow(row)
                self._fh.flush()
            if self.abort_c is not None:
                m = self.ch.die_max(row)
                if m is not None and m >= self.abort_c:
                    self.over_limit_c = m
                    self.over_limit.set()
            self._stop.wait(self.interval)

    def summary(self) -> dict[str, float]:
        return summarize(self.rows, self.ch.zone_kind, self.ch.cpu_hw_max, self.ch.gpu_hw_max, self.interval)


def summarize(
    rows: list[dict[str, Any]],
    zone_kind: dict[str, str],
    cpu_hw_max: dict[str, float],
    gpu_hw_max: float | None,
    interval: float = 1.0,
) -> dict[str, float]:
    """Trace rows -> flat summary (temps max, clock mean/min, time capped, power)."""
    out: dict[str, float] = {}
    if not rows:
        return out
    out["samples"] = len(rows)

    def col(name):
        return [r[name] for r in rows if r.get(name) is not None]

    for kind in ("cpu", "gpu"):
        vals = [v for c, k in zone_kind.items() if k == kind for v in col(c)]
        if vals:
            out[f"temp_{kind}_max_c"] = max(vals)
    die_cols = [c for c, k in zone_kind.items() if k != "other"]
    per_row_max = [max(vs) for r in rows if (vs := [r[c] for c in die_cols if r.get(c) is not None])]
    if per_row_max:
        out["temp_die_max_c"] = max(per_row_max)
        out["temp_die_mean_c"] = round(mean(per_row_max), 2)
        out["temp_die_start_c"] = per_row_max[0]
        out["temp_die_end_c"] = per_row_max[-1]

    capped_rows = 0
    for pol, hw in cpu_hw_max.items():
        f = col(f"cpu.{pol}.mhz")
        if f:
            out[f"cpu_{pol}_mhz_mean"] = round(mean(f), 1)
            out[f"cpu_{pol}_mhz_min"] = min(f)
    for r in rows:
        if any(
            r.get(f"cpu.{pol}.cap_mhz") is not None and r[f"cpu.{pol}.cap_mhz"] < hw - 1
            for pol, hw in cpu_hw_max.items()
        ):
            capped_rows += 1
    if cpu_hw_max:
        out["cpu_capped_s"] = capped_rows * interval

    g = col("gpu.mhz")
    if g:
        out["gpu_mhz_mean"] = round(mean(g), 1)
        out["gpu_mhz_min"] = min(g)
        out["gpu_mhz_max"] = max(g)
    if gpu_hw_max is not None:
        out["gpu_capped_s"] = sum(1 for v in col("gpu.cap_mhz") if v < gpu_hw_max - 1) * interval

    fans = [v for c in (rows[0].keys()) if c.startswith("fan.") for v in col(c)]
    if fans:
        out["fan_rpm_max"] = max(fans)

    dis = [abs(r["bat.power_mw"]) for r in rows if r.get("bat.discharging") == 1.0 and r.get("bat.power_mw") is not None]
    if dis and len(dis) == len(rows):
        out["bat_power_mw_mean"] = round(mean(dis), 1)
        out["bat_power_mw_max"] = max(dis)
    return out
