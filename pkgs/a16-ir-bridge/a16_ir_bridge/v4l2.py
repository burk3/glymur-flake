"""The few V4L2 ioctls the bridge needs (64-bit Linux layouts)."""

import fcntl
import struct


def fourcc(s):
    return s[0] | (s[1] << 8) | (s[2] << 16) | (s[3] << 24)


V4L2_PIX_FMT_GREY = fourcc(b"GREY")
V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_FIELD_NONE = 1
V4L2_COLORSPACE_RAW = 11

V4L2_CID_EXPOSURE = 0x00980911
V4L2_CID_ANALOGUE_GAIN = 0x009E0903

# _IOWR('V', 27/28, struct v4l2_control) (8 bytes)
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
# _IOWR('V', 5, struct v4l2_format) (208 bytes on 64-bit: the union is
# 8-aligned because struct v4l2_window holds pointers)
VIDIOC_S_FMT = 0xC0D05605

_FMT = "I4x12I152x"
assert struct.calcsize(_FMT) == 208


def pack_pix_format(buf_type, width, height, pixelformat, bytesperline, sizeimage, colorspace=V4L2_COLORSPACE_RAW):
    return struct.pack(
        _FMT,
        buf_type,
        width,
        height,
        pixelformat,
        V4L2_FIELD_NONE,
        bytesperline,
        sizeimage,
        colorspace,
        0,  # priv
        0,  # flags
        0,  # ycbcr_enc
        0,  # quantization
        0,  # xfer_func
    )


def unpack_pix_format(data):
    v = struct.unpack(_FMT, data)
    return {
        "type": v[0],
        "width": v[1],
        "height": v[2],
        "pixelformat": v[3],
        "bytesperline": v[5],
        "sizeimage": v[6],
    }


def set_output_format(fd, width, height, pixelformat=V4L2_PIX_FMT_GREY):
    bpl = width
    req = bytearray(pack_pix_format(V4L2_BUF_TYPE_VIDEO_OUTPUT, width, height, pixelformat, bpl, bpl * height))
    fcntl.ioctl(fd, VIDIOC_S_FMT, req, True)
    return unpack_pix_format(bytes(req))


def set_ctrl(fd, cid, value):
    fcntl.ioctl(fd, VIDIOC_S_CTRL, bytearray(struct.pack("Ii", cid, int(value))), True)


def get_ctrl(fd, cid):
    req = bytearray(struct.pack("Ii", cid, 0))
    fcntl.ioctl(fd, VIDIOC_G_CTRL, req, True)
    return struct.unpack("Ii", req)[1]
