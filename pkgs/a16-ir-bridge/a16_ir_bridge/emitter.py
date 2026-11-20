"""The A16's IR flood emitter (PMH0101 flash LED, leds-qcom-flash) in torch mode.

Torch has no hardware timer, so the bridge bounds it: on() once the route is
set, off() after `max_seconds` or when the session ends, whichever is first.
off() is idempotent and also runs at start (a previous bridge killed with
SIGKILL is covered by the unit's ExecStopPost as well).
"""

import os
import time

LEDS = "/sys/class/leds"


class Emitter:
    def __init__(self, led, brightness, max_seconds, log, root=LEDS, clock=time.monotonic):
        self.path = os.path.join(root, led)
        self.brightness = brightness
        self.max_seconds = max_seconds
        self.log = log
        self.clock = clock
        self.on_since = None
        self.top = None
        self.value = 0

    def _write(self, value):
        with open(os.path.join(self.path, "brightness"), "w") as f:
            f.write(f"{value}\n")

    def on(self):
        with open(os.path.join(self.path, "max_brightness")) as f:
            top = int(f.read())
        self.top = top
        value = max(0, min(self.brightness, top))
        self._write(value)
        self.value = value
        self.on_since = self.clock()
        self.log(f"IR emitter on (brightness {value}/{top}, cap {self.max_seconds:g} s)")

    @property
    def lit(self):
        return self.on_since is not None

    def set(self, value):
        """Change the brightness while lit (auto exposure dims it for a close
        face). Never above the configured brightness; ignored once off."""
        if not self.lit:
            return
        value = max(1, min(int(value), self.brightness, self.top))
        if value != self.value:
            self._write(value)
            self.value = value

    def tick(self):
        """Call per frame: turns the emitter off once its time is up."""
        if self.on_since is not None and self.clock() - self.on_since >= self.max_seconds:
            self.off(f"emitter cap ({self.max_seconds:g} s); going on with ambient light")

    def off(self, why=None):
        try:
            self._write(0)
        except OSError as e:
            self.log(f"IR emitter off failed: {e}")
            return
        self.value = 0
        if self.on_since is not None:
            self.log(f"IR emitter off after {self.clock() - self.on_since:.1f} s" + (f" ({why})" if why else ""))
        self.on_since = None
