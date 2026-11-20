"""a16-ir-bridge: stream the A16's HM1092 IR sensor into a v4l2loopback node.

The HM1092 only works through the raw CAMSS path (libcamera rejects it), which
hands out packed 10-bit grey (Y10P) with no exposure control. howdy wants a
plain V4L2 node with 8-bit frames. While a session runs, this:

  1. takes libcamera's lock on the CAMSS media device (so an app using the
     RGB webcam blocks us, and we block it, instead of breaking each other);
  2. loads v4l2loopback (options from modprobe.d) if needed;
  3. routes csiphy0 (HM1092) -> csid0 -> vfe0 rdi0 with Y10_1X10 560x360
     (CAMSS here only has CSID0/VFE0, shared with the RGB camera on csiphy4);
  4. captures Y10P from the rdi0 video node (v4l2-ctl), keeps the high 8
     bits, runs auto exposure on the sensor's exposure/analogue_gain metered
     on the centre of the frame (where the face is), stretches levels, and
     writes GREY frames to the loopback node;
  5. with --emitter-led, lights the IR flood emitter (torch) while streaming,
     at most --emitter-seconds, off again at the end (emitter.py); auto
     exposure may dim it for a face too close to need the full current;
  6. stops once the consumer (howdy) has closed the loopback node, or nobody
     opened it, or a hard time limit; then restores the RGB route.

`serve` is the systemd service (socket-activated: clients waiting on the
socket get "ready" once frames flow, or "error: ..."). See trigger.py for the
client used from PAM.
"""

import argparse
import fcntl
import glob
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from . import v4l2
from .emitter import Emitter
from .image import AutoExposure, Levels, TemporalDenoise, histogram, meter, unpack_y10p_to_u8

WIDTH, HEIGHT = 560, 360
CAMSS_MODEL = "Qualcomm Camera Subsystem"
RDI_VIDEO_NAME = "msm_vfe0_video0"
SENSOR_PREFIX = "hm1092"
IR_LINK = '"msm_csiphy0":1 -> "msm_csid0":0'
RGB_LINK = '"msm_csiphy4":1 -> "msm_csid0":0'
IR_PADS = ['"msm_csiphy0":0', '"msm_csid0":0', '"msm_vfe0_rdi0":0']
STATE_FILE = "/run/a16-ir-bridge/ae"
DEFAULT_EXPOSURE, DEFAULT_GAIN = 729, 96
# A starting point with the emitter lit, without a saved state: a face at
# laptop distance under the 100 mA torch.
DEFAULT_EMITTER_EXPOSURE, DEFAULT_EMITTER_GAIN = 300, 0


def log(msg):
    print(f"a16-ir-bridge: {msg}", file=sys.stderr, flush=True)


class BridgeError(Exception):
    pass


def run(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise BridgeError(f"{what}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def sysfs_find(pattern, name_pred):
    for d in sorted(glob.glob(pattern)):
        try:
            with open(os.path.join(d, "name" if "video4linux" in pattern else "model")) as f:
                if name_pred(f.read().strip()):
                    return os.path.basename(d)
        except OSError:
            pass
    return None


def find_hardware():
    media = sysfs_find("/sys/bus/media/devices/media*", lambda n: n == CAMSS_MODEL)
    video = sysfs_find("/sys/class/video4linux/video*", lambda n: n == RDI_VIDEO_NAME)
    subdev = sysfs_find("/sys/class/video4linux/v4l-subdev*", lambda n: n.startswith(SENSOR_PREFIX))
    if not (media and video and subdev):
        raise BridgeError(f"camera not found (media={media} video={video} sensor={subdev})")
    return f"/dev/{media}", f"/dev/{video}", f"/dev/{subdev}"


def openers(path, exclude):
    """PIDs (other than `exclude`) with `path` open."""
    found = set()
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        pid = int(fd_dir.split("/")[2])
        if pid in exclude:
            continue
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(f"{fd_dir}/{fd}") == path:
                        found.add(pid)
                        break
                except OSError:
                    pass
        except OSError:
            pass
    return found


class Clients:
    """Answers connections on the activation socket: "ready" once frames
    flow, "error: ..." on failure."""

    def __init__(self, sock):
        self.sock = sock
        self.state = None
        self.waiting = []
        self.lock = threading.Lock()
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    @staticmethod
    def _send(conn, msg):
        try:
            conn.sendall((msg + "\n").encode())
        except OSError:
            pass
        conn.close()

    def _loop(self):
        while self.running:
            r, _, _ = select.select([self.sock], [], [], 0.1)
            if not r or not self.running:
                continue
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with self.lock:
                if self.state is not None:
                    self._send(conn, self.state)
                else:
                    self.waiting.append(conn)

    def resolve(self, state):
        with self.lock:
            if self.state is None:
                self.state = state
            for c in self.waiting:
                self._send(c, self.state)
            self.waiting = []

    def stop(self, drain=False):
        """Stop accepting. Connections still in the backlog start a new
        session (systemd re-activates); with drain=True (after an error)
        answer them instead so a persistent failure can't loop."""
        self.running = False
        self.thread.join()
        if drain:
            self.sock.setblocking(False)
            while True:
                try:
                    conn, _ = self.sock.accept()
                except OSError:
                    break
                self._send(conn, self.state or "error: stopped")
        with self.lock:
            for c in self.waiting:
                self._send(c, self.state or "error: stopped")
            self.waiting = []


def load_ae_state(emitter):
    """Last session's exposure and gain, kept per lighting (emitter lit or
    ambient only), as a quick start."""
    try:
        with open(STATE_FILE + (".emitter" if emitter else "")) as f:
            e, g = f.read().split()
            return int(e), int(g)
    except (OSError, ValueError):
        return (DEFAULT_EMITTER_EXPOSURE, DEFAULT_EMITTER_GAIN) if emitter else (DEFAULT_EXPOSURE, DEFAULT_GAIN)


def save_ae_state(emitter, exposure, gain):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE + (".emitter" if emitter else ""), "w") as f:
            f.write(f"{exposure} {gain}\n")
    except OSError:
        pass


class Session:
    def __init__(self, args, stop):
        self.args = args
        self.stop = stop
        self.media_fd = None
        self.routed = False
        self.sensor_fd = None
        self.loop_fd = None
        self.proc = None
        self.errlog = None
        self.media = None
        self.emitter = None
        self.ae = None
        self.fixed = args.fixed_exposure is not None

    # --- setup ---------------------------------------------------------
    def open(self):
        a = self.args
        self.media, video, sensor = find_hardware()
        self.media_fd = os.open(self.media, os.O_RDWR | os.O_CLOEXEC)
        try:
            # The same POSIX lock libcamera takes when an app acquires a camera.
            fcntl.lockf(self.media_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BridgeError("camera in use (CAMSS media device locked by a libcamera app)")

        if not os.path.exists(a.loopback):
            run([a.modprobe, "v4l2loopback"], "modprobe v4l2loopback")
            for _ in range(40):
                if os.path.exists(a.loopback):
                    break
                time.sleep(0.05)
            else:
                raise BridgeError(f"{a.loopback} did not appear after loading v4l2loopback")

        mc = [a.media_ctl, "-d", self.media]
        self.routed = True  # from here on, always try to restore the RGB route
        run(mc + ["-l", f"{RGB_LINK} [0]"], "disable RGB link (RGB camera streaming?)")
        run(mc + ["-l", f"{IR_LINK} [1]"], "enable IR link")
        for pad in IR_PADS:
            run(mc + ["-V", f"{pad} [fmt:Y10_1X10/{WIDTH}x{HEIGHT}]"], f"format {pad}")

        self.sensor_fd = os.open(sensor, os.O_RDWR | os.O_CLOEXEC)
        if self.fixed:
            exp, gain = a.fixed_exposure, a.fixed_gain
        else:
            exp, gain = load_ae_state(bool(a.emitter_led))
        self.ae = AutoExposure(
            exp,
            gain,
            target=a.ae_target,
            brightness=a.emitter_brightness if a.emitter_led else None,
        )
        self.apply_controls(self.ae.exposure, self.ae.gain)

        run(
            [a.v4l2_ctl, "-d", video, f"--set-fmt-video=width={WIDTH},height={HEIGHT},pixelformat=Y10P"],
            "set capture format",
        )
        fmt = run([a.v4l2_ctl, "-d", video, "--get-fmt-video"], "get capture format")
        self.bpl = self.sizeimage = None
        for line in fmt.splitlines():
            k, _, v = line.partition(":")
            k = k.strip()
            if k == "Bytes per Line" and self.bpl is None:
                self.bpl = int(v.split()[0])
            elif k == "Size Image" and self.sizeimage is None:
                self.sizeimage = int(v.split()[0])
        if not self.bpl or not self.sizeimage or self.bpl < WIDTH * 5 // 4:
            raise BridgeError(f"unexpected capture format:\n{fmt}")

        self.loop_fd = os.open(a.loopback, os.O_RDWR | os.O_CLOEXEC)
        v4l2.set_output_format(self.loop_fd, WIDTH, HEIGHT)

        if a.emitter_led:
            self.emitter = Emitter(a.emitter_led, a.emitter_brightness, a.emitter_seconds, log)
            self.emitter.off()  # a known state, in case a previous bridge died
            self.emitter.on()
            if self.ae.brightness is not None:
                # max_brightness may be below the configured value
                self.ae.brightness_start = self.ae.brightness = self.emitter.value
                self.ae.brightness_min = max(1, self.emitter.value // 8)

        self.errlog = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [a.v4l2_ctl, "-d", video, "--stream-mmap=4", "--stream-count=0", "--stream-to=-"],
            stdout=subprocess.PIPE,
            stderr=self.errlog,
            bufsize=0,
        )

    def apply_controls(self, exposure, gain, brightness=None):
        v4l2.set_ctrl(self.sensor_fd, v4l2.V4L2_CID_EXPOSURE, exposure)
        v4l2.set_ctrl(self.sensor_fd, v4l2.V4L2_CID_ANALOGUE_GAIN, gain)
        if brightness is not None and self.emitter is not None:
            self.emitter.set(brightness)

    def settings(self):
        ae = self.ae
        mean, sat = ae.last
        s = f"exposure {ae.exposure}, gain {ae.gain}"
        if self.emitter is not None:
            s += f", emitter {self.emitter.value}"
        if mean is not None:
            s += f"; centre mean {mean:.0f}, {sat * 100:.1f}% clipped"
        return s

    # --- streaming -----------------------------------------------------
    def read_frame(self, timeout):
        buf = bytearray(self.sizeimage)
        view = memoryview(buf)
        got = 0
        deadline = time.monotonic() + timeout
        fd = self.proc.stdout.fileno()
        while got < self.sizeimage:
            left = deadline - time.monotonic()
            if left <= 0 or self.stop.is_set():
                return None
            r, _, _ = select.select([fd], [], [], min(left, 0.2))
            if not r:
                continue
            n = os.readv(fd, [view[got:]])
            if n == 0:
                self.errlog.seek(0)
                err = self.errlog.read().decode(errors="replace").strip()[-300:]
                raise BridgeError(f"capture stopped: {err}")
            got += n
        return buf

    def pump(self, on_ready):
        a = self.args
        start = time.monotonic()
        ready_at = None
        last_consumer = None
        last_scan = 0.0
        frames = 0
        own = {os.getpid(), self.proc.pid}
        while not self.stop.is_set():
            buf = self.read_frame(timeout=3.0 if frames == 0 else 1.0)
            if buf is None:
                if self.stop.is_set():
                    break
                raise BridgeError("no frames from the IR sensor")
            img = unpack_y10p_to_u8(buf, WIDTH, HEIGHT, self.bpl)
            hist = histogram(img)
            frames += 1
            if self.emitter is not None:
                lit = self.emitter.lit
                self.emitter.tick()
                if lit and not self.emitter.lit:
                    self.save_state(emitter=True)
                    self.ae.emitter_lost()
            mean, sat, hi = meter(img)
            if self.fixed:
                self.ae.last = (mean, sat)
            else:
                was = self.ae.converged
                change = self.ae.update(mean, sat, hi)
                if change:
                    self.apply_controls(*change)
                    if a.ae_debug:
                        log(f"AE frame {frames}: centre mean {mean:.0f}, {sat * 100:.1f}% clipped -> exposure {change[0]}, gain {change[1]}" + (f", emitter {change[2]}" if change[2] is not None else ""))
                if self.ae.converged and not was:
                    log(f"AE in band after {frames} frames: {self.settings()}")
            if a.smooth < 1.0:
                img = self.denoise.apply(img)
                hist = histogram(img)
            out = self.levels.apply(img, hist) if a.levels else img
            os.write(self.loop_fd, out.tobytes())

            now = time.monotonic()
            if ready_at is None and frames >= a.ready_frames:
                ready_at = now
                log(f"streaming ({frames} frames, {self.settings()})")
                on_ready()
            if ready_at is not None and now - last_scan >= 0.5:
                last_scan = now
                if openers(a.loopback, own):
                    last_consumer = now
                if last_consumer is None and now - ready_at > a.first_open_timeout:
                    log("nobody opened the IR node; stopping")
                    break
                if last_consumer is not None and now - last_consumer > a.idle_timeout:
                    log("IR node closed; stopping")
                    break
            if now - start > a.max_seconds:
                log(f"time limit ({a.max_seconds} s); stopping")
                break
        return frames

    # --- teardown --------------------------------------------------------
    def save_state(self, emitter):
        if self.ae is not None and not self.fixed and self.ae.frames:
            save_ae_state(emitter, self.ae.exposure, self.ae.gain)

    def close(self):
        self.save_state(emitter=self.emitter is not None and self.emitter.lit)
        if self.emitter is not None:
            self.emitter.off("session end")
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for fd in (self.loop_fd, self.sensor_fd):
            if fd is not None:
                os.close(fd)
        if self.routed:
            mc = [self.args.media_ctl, "-d", self.media]
            for cmd in (["-l", f"{IR_LINK} [0]"], ["-l", f"{RGB_LINK} [1]"]):
                r = subprocess.run(mc + cmd, capture_output=True, text=True)
                if r.returncode != 0:
                    log(f"restoring the RGB route ({cmd[1]}) failed: {r.stderr.strip()}")
        if self.media_fd is not None:
            os.close(self.media_fd)  # drops the lock


def listen_socket(args):
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
        return socket.socket(fileno=3)
    if args.socket:
        try:
            os.unlink(args.socket)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(args.socket)
        os.chmod(args.socket, 0o666)
        s.listen(8)
        return s
    return None


def serve(args):
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    sock = listen_socket(args)
    clients = Clients(sock) if sock else None
    session = Session(args, stop)
    session.levels = Levels()
    session.denoise = TemporalDenoise(args.smooth)
    t0 = time.monotonic()
    ok = False
    try:
        session.open()
        frames = session.pump(lambda: clients and clients.resolve("ready"))
        ok = True
        ae = session.ae
        conv = f"in band at frame {ae.converged_frame}" if ae.converged_frame else "never in band"
        log(f"session done: {frames} frames in {time.monotonic() - t0:.1f} s, AE {conv} after {ae.changes} changes; last {session.settings()}")
    except BridgeError as e:
        log(f"error: {e}")
        if clients:
            clients.resolve(f"error: {e}")
    except Exception as e:  # never leave a PAM client hanging
        log(f"unexpected error: {e!r}")
        if clients:
            clients.resolve(f"error: {e!r}")
        raise
    finally:
        if clients:
            clients.stop(drain=not ok)
        session.close()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="a16-ir-bridge", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run one IR session (systemd socket activation, or --socket)")
    s.add_argument("--loopback", default="/dev/video42", help="v4l2loopback node to write to")
    s.add_argument("--socket", help="listen here when not socket-activated")
    s.add_argument("--ready-frames", type=int, default=3, help="frames to write before reporting ready")
    s.add_argument("--first-open-timeout", type=float, default=6.0, help="stop if nobody opens the node within this many seconds")
    s.add_argument("--idle-timeout", type=float, default=2.0, help="stop this long after the last consumer closed the node")
    s.add_argument("--max-seconds", type=float, default=90.0, help="hard limit for one session")
    s.add_argument("--smooth", type=float, default=0.4, help="weight of the newest frame in the running average (1 = no temporal denoise)")
    s.add_argument("--no-levels", dest="levels", action="store_false", help="write the raw high 8 bits, no contrast stretch")
    s.add_argument("--media-ctl", default="media-ctl")
    s.add_argument("--v4l2-ctl", default="v4l2-ctl")
    s.add_argument("--modprobe", default="modprobe")
    s.add_argument("--emitter-led", help="/sys/class/leds name of the IR flood emitter to light (torch) while streaming")
    s.add_argument("--emitter-brightness", type=int, default=255, help="emitter brightness (0..max_brightness, linear in current)")
    s.add_argument("--emitter-seconds", type=float, default=15.0, help="hard cap on emitter on-time per session")
    s.add_argument("--ae-target", type=float, default=115.0, help="auto exposure target: mean of the frame's centre (8-bit, before levels)")
    s.add_argument("--ae-debug", action="store_true", help="log every auto exposure change")
    s.add_argument("--fixed-exposure", type=int, help="no auto exposure: this exposure (lines), with --fixed-gain")
    s.add_argument("--fixed-gain", type=int, default=0, help="analogue gain with --fixed-exposure")
    args = p.parse_args(argv)
    if args.cmd == "serve":
        return serve(args)
    return 2
