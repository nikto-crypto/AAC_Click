"""Small platform and terminal helpers shared across modules."""
import re
import sys

WIN = sys.platform.startswith("win")
_ansi_re = re.compile(r"\033\[[;?0-9]*[a-zA-Z]")


def strip_ansi(value):
    return _ansi_re.sub("", value)


def term_len(value):
    return len(strip_ansi(value))


def isatty(stream):
    try:
        return stream.isatty()
    except Exception:
        return False


def _is_jupyter_kernel_output(stream):
    while hasattr(stream, "_stream"):
        stream = stream._stream
    return stream.__class__.__module__.startswith("ipykernel.")


def should_strip_ansi(stream=None, color=None):
    if color is None:
        if stream is None:
            stream = sys.stdin
        return not isatty(stream) and not _is_jupyter_kernel_output(stream)
    return not color
