import numpy as np

from a16_ir_bridge.image import AutoExposure, Levels, default_gain_factor, hist_mean, hist_percentile, histogram, meter, unpack_y10p_to_u8


def pack_y10p(px10, bytesperline):
    """Reference packer: MIPI RAW10, 4 pixels -> 4 high bytes + 1 byte of low bits."""
    h, w = px10.shape
    out = np.zeros((h, bytesperline), np.uint8)
    g = px10.reshape(h, w // 4, 4)
    packed = np.zeros((h, w // 4, 5), np.uint8)
    packed[:, :, :4] = g >> 2
    packed[:, :, 4] = (g[:, :, 0] & 3) | ((g[:, :, 1] & 3) << 2) | ((g[:, :, 2] & 3) << 4) | ((g[:, :, 3] & 3) << 6)
    out[:, : w * 5 // 4] = packed.reshape(h, -1)
    out[:, w * 5 // 4 :] = 0xEE  # padding must be ignored
    return out.tobytes()


def test_unpack_keeps_high_bits():
    rng = np.random.default_rng(1)
    px = rng.integers(0, 1024, size=(360, 560), dtype=np.uint16)
    buf = pack_y10p(px, 704)
    img = unpack_y10p_to_u8(buf, 560, 360, 704)
    assert img.shape == (360, 560)
    assert img.dtype == np.uint8
    assert np.array_equal(img, (px >> 2).astype(np.uint8))


def test_hist_helpers():
    img = np.full((10, 10), 50, np.uint8)
    img[0, :] = 200
    h = histogram(img)
    assert h.sum() == 100
    assert abs(hist_mean(h) - 65.0) < 1e-9
    assert hist_percentile(h, 50) == 50
    assert hist_percentile(h, 99.5) == 200


def test_meter_centre_and_clipping():
    img = np.zeros((360, 560), np.uint8)  # dark room
    img[72:288, 140:420] = 100  # the centre region
    mean, sat, hi = meter(img)
    assert abs(mean - 100) < 1 and sat == 0 and hi == 100
    img[:, :] = 255  # clipped background outside the centre doesn't count...
    img[72:288, 140:420] = 100
    assert meter(img) == (100.0, 0.0, 100)
    img[72:180, 140:420] = 255  # ...a clipped half of the centre does
    mean, sat, hi = meter(img)
    assert abs(sat - 0.5) < 0.01 and hi == 255


class Scene:
    """A face (reflectance spread 0.6..1.4) under a torch plus a little
    ambient, seen through a sensor with a two-frame control latency and a
    clip at 255."""

    def __init__(self, face=0.5, ambient=0.02, gain_factor=default_gain_factor, lights=0):
        self.lights = lights
        rng = np.random.default_rng(3)
        self.refl = rng.uniform(0.6, 1.4, 4000)
        self.face = face
        self.ambient = ambient
        self.gf = gain_factor
        self.pipe = []

    def frame(self, exposure, gain, brightness, start=255):
        light = self.face * (brightness if brightness is not None else start) / start + self.ambient
        self.pipe.append(exposure * self.gf(gain) * light)
        level = self.pipe[-3] if len(self.pipe) >= 3 else self.pipe[0]
        px = np.minimum(8 + self.refl * level, 255)
        if self.lights:  # small light sources, clipped at any setting
            px = np.concatenate([px, np.full(self.lights, 255.0)])
        return float(px.mean()), float((px >= 248).mean()), float(np.percentile(px, 98.5))


def run(ae, scene, frames=60):
    history = []
    for _ in range(frames):
        mean, sat, hi = scene.frame(ae.exposure, ae.gain, ae.brightness)
        ae.update(mean, sat, hi)
        history.append((mean, sat, ae.exposure, ae.gain, ae.brightness))
    return history


def test_ae_saturated_face_converges_fast():
    # main's 2026-10-27 frame: max exposure, gain 76, face clipped
    ae = AutoExposure(729, 76, brightness=255)
    h = run(ae, Scene(face=0.5))
    assert ae.converged and ae.converged_frame <= 30  # ~1 s at 30 fps
    mean, sat = h[-1][:2]
    assert 98 <= mean <= 132 and sat <= 0.015
    assert ae.gain == 0 and ae.brightness == 255  # exposure alone did it
    assert len({x[2:] for x in h[-20:]}) == 1  # settled, no hunting


def test_ae_dark_raises_exposure_then_gain():
    ae = AutoExposure(50, 0, brightness=255)
    run(ae, Scene(face=0.02, ambient=0.0))
    assert ae.converged and ae.exposure == 729 and ae.gain > 0


def test_ae_close_face_dims_the_emitter():
    ae = AutoExposure(300, 0, brightness=255)
    h = run(ae, Scene(face=40.0, ambient=0.0), frames=90)
    assert ae.converged
    assert ae.exposure == ae.dim_exposure and ae.brightness < 255
    assert h[-1][1] <= 0.015
    # and back up, emitter first, once the face moves away
    scene = Scene(face=0.5)
    run(ae, scene, frames=90)
    assert ae.brightness == 255 and ae.exposure > ae.dim_exposure


def test_ae_without_emitter_and_limits():
    ae = AutoExposure(729, 255)
    assert ae.brightness is None and ae.gain == 0x7F  # bit 7 does nothing
    run(ae, Scene(face=0.0, ambient=0.0001))
    assert (ae.exposure, ae.gain) == (729, 0x7F)  # dark: at both maxima, no error
    ae = AutoExposure(2, 0)
    run(ae, Scene(face=0.0, ambient=1000.0))
    assert (ae.exposure, ae.gain) == (2, 0)  # blinding: at both minima


def test_ae_in_band_holds():
    ae = AutoExposure(729, 0)
    ae.wait = 0
    assert ae.update(115, 0.0, 180) is None and ae.converged
    assert ae.update(128, 0.0, 200) is None  # within 15% of the target
    assert ae.update(80, 0.0, 215) is None  # darker, but the highlights are there
    assert ae.update(100, 0.02, 255) is None  # barely clipped: hold
    assert ae.update(100, 0.04, 255)  # clipped: down


def test_ae_lights_in_the_centre_dont_hunt():
    # no face, ceiling lights (1% of the centre) that clip at any setting
    ae = AutoExposure(729, 76, brightness=255)
    h = run(ae, Scene(face=0.01, ambient=0.0, lights=40), frames=120)
    assert len({x[2:] for x in h[-60:]}) == 1


def test_gain_factor_is_monotonic_up_to_0x7f():
    f = [default_gain_factor(g) for g in range(256)]
    assert f[0] == 1 and f[0x20] == 4 and f[0x28] == 6 and f[0x7F] == 248
    assert all(a < b for a, b in zip(f[:127], f[1:128]))
    assert f[0x80] == f[0] and f[0xA0] == f[0x20]


def test_ae_split_order():
    ae = AutoExposure(729, 0, brightness=200)
    assert ae.split(100) == (100, 0, 200)
    assert ae.split(729 * 2) == (729, 16, 200)
    assert ae.split(729 * 6) == (729, 0x28, 200)
    assert ae.split(4) == (8, 0, 100)  # below dim_exposure: dim the emitter
    assert ae.split(0.001) == (8, 0, 25)  # never below brightness_min


def test_levels_stretch_and_cap():
    lv = Levels(smoothing=1.0)
    img = np.linspace(20, 120, 1000).astype(np.uint8).reshape(10, 100)
    out = lv.apply(img, histogram(img))
    assert out.min() <= 2 and out.max() >= 250
    flat = np.full((10, 100), 9, np.uint8)  # black frame: stretch capped, stays dark
    lv = Levels(smoothing=1.0)
    out = lv.apply(flat, histogram(flat))
    assert out.max() == 0


def test_temporal_denoise():
    from a16_ir_bridge.image import TemporalDenoise

    rng = np.random.default_rng(2)
    d = TemporalDenoise(0.4)
    outs = [d.apply(np.clip(100 + rng.normal(0, 20, (36, 56)), 0, 255).astype(np.uint8)) for _ in range(20)]
    assert outs[0].dtype == np.uint8
    assert outs[-1].std() < 12  # ~20 * sqrt(0.4 / 1.6) = 10
    assert abs(outs[-1].mean() - 100) < 2
