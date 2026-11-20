"""Frame processing: Y10P unpacking, auto exposure, and 8-bit levels.

Pure functions and small state objects, unit-tested on the build host.
"""

import numpy as np


def unpack_y10p_to_u8(buf, width, height, bytesperline):
    """Packed 10-bit grey (MIPI RAW10: 4 pixels in 5 bytes, the first four
    bytes holding each pixel's 8 high bits) to 8-bit grey by keeping the high
    bits. Returns a (height, width) uint8 array."""
    if width % 4:
        raise ValueError("Y10P width must be a multiple of 4")
    packed = width * 5 // 4
    rows = np.frombuffer(buf, np.uint8, count=bytesperline * height)
    rows = rows.reshape(height, bytesperline)[:, :packed]
    return rows.reshape(height, width // 4, 5)[:, :, :4].reshape(height, width)


def histogram(img):
    return np.bincount(img.ravel(), minlength=256)


def hist_mean(hist):
    total = hist.sum()
    return float((hist * np.arange(256)).sum() / total) if total else 0.0


def hist_percentile(hist, pct):
    cum = np.cumsum(hist)
    if cum[-1] == 0:
        return 0
    return int(np.searchsorted(cum, cum[-1] * pct / 100.0))


def meter(img, x=(0.25, 0.75), y=(0.2, 0.8), saturated=248, pct=98.5):
    """Centre-weighted metering over the region where a face sits at laptop
    distance, the middle half of the width and the middle 60% of the
    height; the background (a dark room, ceiling lights, a window) doesn't
    count. Returns (mean, saturated fraction, `pct` percentile)."""
    h, w = img.shape
    roi = img[int(h * y[0]) : int(h * y[1]), int(w * x[0]) : int(w * x[1])]
    hist = np.bincount(roi.ravel(), minlength=256)
    n = roi.size
    mean = float((hist * np.arange(256)).sum()) / n
    sat = float(hist[saturated:].sum()) / n
    return mean, sat, hist_percentile(hist, pct)


def default_gain_factor(gain):
    """The HM1092's analogue gain register (0x0205) as a multiplier: bits
    6:4 a coarse gain of 2^n, bits 3:0 a fine 1 + n/16, bit 7 ignored
    (measured on the A16 2026-10-27 at fixed exposure: 0x20 = 4.4x 0x00,
    0x28 = 1.5x 0x20, 0x40 = 4.0x 0x20, 0x80 = 0x00, 0xa0 = 0x20). So
    0x00..0x7f is monotonic, 1x to 128 * 31/16 = 248x."""
    return float(2 ** ((gain >> 4) & 7)) * (1.0 + (gain & 15) / 16.0)


class AutoExposure:
    """Drives the sensor's exposure (lines) and analogue gain (register),
    and optionally the IR emitter's brightness, toward a target mean over
    the centre of the 8-bit image, but no further than puts the brightest
    `sat_max` of the centre at `highlight` (a face lit by the emitter is
    much brighter than the room: a mean target alone clips it).

    The three are treated as one total, exposure * gain_factor(gain) *
    brightness / brightness_start, and split in a fixed order: exposure
    first (up to its maximum, at gain 0 and full emitter), then gain;
    below `dim_exposure` lines the emitter is dimmed instead of shortening
    the exposure further (a face close to the emitter).

    Fast first, then damped: until the centre first lands in band (or after
    `fast_updates` changes) a step may change the total up to 4x either way;
    after that a step moves part of the way (ratio ** damping, within
    0.7..1.4x). Controls take a frame or two to reach the image, so after a
    change the next `settle` frames are not metered."""

    def __init__(
        self,
        exposure,
        gain,
        exposure_min=2,
        exposure_max=729,
        gain_max=0x7F,
        target=115.0,
        highlight=220.0,
        black=8.0,
        tolerance=0.15,
        sat_max=0.015,
        settle=2,
        fast_updates=8,
        damping=0.6,
        gain_factor=default_gain_factor,
        brightness=None,
        brightness_min=None,
        dim_exposure=8,
    ):
        self.exposure_min = exposure_min
        self.exposure_max = exposure_max
        self.gain_max = gain_max
        self.exposure = min(max(int(exposure), exposure_min), exposure_max)
        self.gain = min(max(int(gain), 0), gain_max)
        self.target = target
        self.highlight = highlight
        self.black = black
        self.tolerance = tolerance
        self.sat_max = sat_max
        self.settle = settle
        self.fast_updates = fast_updates
        self.damping = damping
        self.gain_table = np.array([gain_factor(g) for g in range(gain_max + 1)], np.float64)
        self.brightness_start = brightness
        self.brightness = brightness
        self.brightness_min = brightness_min if brightness_min is not None else (max(1, brightness // 8) if brightness else None)
        self.dim_exposure = max(dim_exposure, exposure_min)
        self.wait = settle  # the first frames may predate the controls
        self.changes = 0
        self.converged = False
        self.converged_frame = None
        self.frames = 0
        self.last = (None, None)

    # --- the total and its split ---------------------------------------
    def _efac(self, brightness):
        if not self.brightness_start or brightness is None:
            return 1.0
        return brightness / self.brightness_start

    def total(self):
        return self.exposure * self.gain_table[self.gain] * self._efac(self.brightness)

    def split(self, total):
        """(exposure, gain, brightness) closest to `total`, in priority order."""
        b = self.brightness_start
        if total <= self.dim_exposure and b:
            bright = int(round(b * total / self.dim_exposure))
            bright = min(max(bright, self.brightness_min), b)
            return self.dim_exposure, 0, bright
        if total <= self.exposure_max:
            return max(self.exposure_min, int(round(total))), 0, b
        want = total / self.exposure_max
        gain = int(np.searchsorted(self.gain_table, want))
        if gain > 0 and abs(self.gain_table[gain - 1] - want) < abs(self.gain_table[min(gain, self.gain_max)] - want):
            gain -= 1
        return self.exposure_max, min(gain, self.gain_max), b

    def emitter_lost(self):
        """The emitter went off (its time cap): stop accounting for it."""
        self.brightness_start = self.brightness = None

    # --- the loop ---------------------------------------------------------
    def update(self, mean, sat, hi):
        """Feed the centre's mean, saturated fraction and highlight
        (meter()) of each frame. Returns (exposure, gain, brightness) when
        they should change, else None; brightness is None without an
        emitter."""
        self.frames += 1
        self.last = (mean, sat)
        if self.wait > 0:
            self.wait -= 1
            return None
        k = (self.target - self.black) / max(mean - self.black, 1.0)
        # Highlights: the brightest (sat_max) of the centre should sit near
        # `highlight`, below the clip. Once clipped, the percentile says
        # nothing about how far over, so step down by how much is clipped.
        if sat > self.sat_max:
            k_hi = 0.25 if sat > 0.2 else 0.5 if sat > 0.05 else 0.8 if sat > 2 * self.sat_max else 0.9
        else:
            k_hi = (self.highlight - self.black) / max(hi - self.black, 1.0)
        k = min(k, k_hi)
        if abs(np.log(k)) <= np.log1p(self.tolerance):
            if not self.converged:
                self.converged = True
                self.converged_frame = self.frames
            return None
        if not self.converged and self.changes < self.fast_updates:
            k = min(max(k, 0.25), 4.0)
        else:
            k = min(max(k**self.damping, 0.7), 1.4)
        new = self.split(self.total() * k)
        cur = (self.exposure, self.gain, self.brightness)
        if new == cur:
            new = self._nudge(k > 1)
            if new is None:
                return None  # at the limit
        self.exposure, self.gain, self.brightness = new
        self.changes += 1
        self.wait = self.settle
        return new

    def _nudge(self, up):
        """One quantum in the wanted direction, when rounding ate the step."""
        e, g, b = self.exposure, self.gain, self.brightness
        if up:
            if b is not None and b < self.brightness_start:
                return e, g, b + 1
            if e < self.exposure_max:
                return e + 1, g, b
            if g < self.gain_max:
                return e, g + 1, b
        else:
            if g > 0:
                return e, g - 1, b
            if e > self.dim_exposure or (b is None and e > self.exposure_min):
                return e - 1, g, b
            if b is not None and b > self.brightness_min:
                return e, g, b - 1
        return None


class Levels:
    """Per-frame contrast stretch (smoothed low/high percentiles to 0..255),
    with the stretch factor capped so noise in a dark scene isn't blown up
    into a full-range image."""

    def __init__(self, low_pct=0.5, high_pct=99.5, smoothing=0.3, min_span=48):
        self.low_pct = low_pct
        self.high_pct = high_pct
        self.smoothing = smoothing
        self.min_span = min_span
        self.lo = None
        self.hi = None
        self._lut_key = None
        self._lut = None

    def lut(self, hist):
        lo = hist_percentile(hist, self.low_pct)
        hi = hist_percentile(hist, self.high_pct)
        if self.lo is None:
            self.lo, self.hi = float(lo), float(hi)
        else:
            a = self.smoothing
            self.lo += a * (lo - self.lo)
            self.hi += a * (hi - self.hi)
        lo_i = int(round(self.lo))
        hi_i = max(int(round(self.hi)), lo_i + self.min_span)
        key = (lo_i, hi_i)
        if key != self._lut_key:
            x = (np.arange(256, dtype=np.float32) - lo_i) * (255.0 / (hi_i - lo_i))
            self._lut = np.clip(x, 0, 255).astype(np.uint8)
            self._lut_key = key
        return self._lut

    def apply(self, img, hist):
        return self.lut(hist)[img]


class TemporalDenoise:
    """Running average over frames (exponential, `weight` for the newest).
    At max analogue gain in a dim room the HM1092 is very noisy; at 30 fps
    averaging ~3 frames costs little motion blur for a face held still."""

    def __init__(self, weight=0.4):
        self.weight = weight
        self.acc = None

    def apply(self, img):
        if self.acc is None:
            self.acc = img.astype(np.float32)
        else:
            self.acc += self.weight * (img - self.acc)
        return (self.acc + 0.5).astype(np.uint8)
