from a16_ir_bridge.emitter import Emitter


def setup(tmp_path, top=255):
    led = tmp_path / "ir:flash"
    led.mkdir()
    (led / "max_brightness").write_text(f"{top}\n")
    (led / "brightness").write_text("0\n")
    return led


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_on_tick_off(tmp_path):
    led = setup(tmp_path)
    clock = Clock()
    logs = []
    e = Emitter("ir:flash", 255, 15, logs.append, root=str(tmp_path), clock=clock)
    e.on()
    assert (led / "brightness").read_text() == "255\n"
    clock.t += 14.9
    e.tick()
    assert (led / "brightness").read_text() == "255\n"
    clock.t += 0.2
    e.tick()
    assert (led / "brightness").read_text() == "0\n"
    assert e.on_since is None
    e.off()  # idempotent
    assert (led / "brightness").read_text() == "0\n"


def test_brightness_clamped_to_max(tmp_path):
    led = setup(tmp_path, top=255)
    e = Emitter("ir:flash", 999, 15, lambda m: None, root=str(tmp_path))
    e.on()
    assert (led / "brightness").read_text() == "255\n"


def test_off_without_led_does_not_raise(tmp_path):
    logs = []
    e = Emitter("missing", 255, 15, logs.append, root=str(tmp_path))
    e.off()
    assert logs and "failed" in logs[0]


def test_set_dims_within_limits_and_only_while_lit(tmp_path):
    led = setup(tmp_path)
    e = Emitter("ir:flash", 200, 15, lambda m: None, root=str(tmp_path))
    e.set(100)  # not lit yet: ignored
    assert (led / "brightness").read_text() == "0\n"
    e.on()
    assert (led / "brightness").read_text() == "200\n"
    e.set(60)
    assert (led / "brightness").read_text() == "60\n"
    e.set(255)  # never above the configured brightness
    assert (led / "brightness").read_text() == "200\n"
    e.set(0)  # dimming never turns it off
    assert (led / "brightness").read_text() == "1\n"
    e.off()
    e.set(50)
    assert (led / "brightness").read_text() == "0\n"
