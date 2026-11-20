"""Collectors register themselves with @collector on import; topics run in
import order (usb first). The redflags topic runs with every topic."""

from . import usb  # noqa: F401, I001
from . import other  # noqa: F401
from . import redflags  # noqa: F401
