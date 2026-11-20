import os
import socket
import struct
import threading

from a16_ir_bridge import trigger, v4l2


def test_format_layout():
    data = v4l2.pack_pix_format(2, 560, 360, v4l2.V4L2_PIX_FMT_GREY, 560, 201600)
    assert len(data) == 208
    assert struct.unpack_from("I", data, 0)[0] == 2
    assert struct.unpack_from("III", data, 8) == (560, 360, v4l2.V4L2_PIX_FMT_GREY)
    f = v4l2.unpack_pix_format(data)
    assert (f["bytesperline"], f["sizeimage"]) == (560, 201600)
    assert v4l2.V4L2_PIX_FMT_GREY == 0x59455247


def serve_once(path, reply):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(1)

    def run():
        c, _ = s.accept()
        if reply is not None:
            c.sendall(reply)
            c.close()
        else:
            threading.Event().wait(2)  # never answers
            c.close()
        s.close()

    threading.Thread(target=run, daemon=True).start()


def test_trigger_ready(tmp_path):
    (tmp_path / "alice.dat").write_text("[]")
    sock = str(tmp_path / "s")
    serve_once(sock, b"ready\n")
    assert trigger.main(["--user", "alice", "--models-dir", str(tmp_path), "--socket", sock]) == 0


def test_trigger_error_and_timeout(tmp_path):
    sock = str(tmp_path / "s")
    serve_once(sock, b"error: camera in use\n")
    assert trigger.main(["--any-user", "--socket", sock]) == 1
    sock2 = str(tmp_path / "s2")
    serve_once(sock2, None)
    assert trigger.main(["--any-user", "--socket", sock2, "--timeout", "0.5"]) == 1


def test_trigger_no_socket_or_model(tmp_path):
    assert trigger.main(["--any-user", "--socket", str(tmp_path / "missing")]) == 1
    # no model: never touches the socket
    assert trigger.main(["--user", "bob", "--models-dir", str(tmp_path), "--socket", "/nonexistent"]) == 1
    assert trigger.main(["--user", "../etc", "--models-dir", str(tmp_path)]) == 1
