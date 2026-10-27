"""Output helpers, ANSI styling and file utilities."""
import os
import re
import sys

from .globals import resolve_color_default

from ._compat import _ansi_re as _ANSI_RE  # noqa: E402
from ._compat import isatty as _isatty  # noqa: E402
from ._compat import should_strip_ansi  # noqa: E402

_COLORS = {
    "black": 30, "red": 31, "green": 32, "yellow": 33, "blue": 34,
    "magenta": 35, "cyan": 36, "white": 37,
    "bright_black": 90, "bright_red": 91, "bright_green": 92,
    "bright_yellow": 93, "bright_blue": 94, "bright_magenta": 95,
    "bright_cyan": 96, "bright_white": 97,
    "reset": 39,
}


def unstyle(text):
    return _ANSI_RE.sub("", text)


def _color_code(value, background):
    offset = 10 if background else 0
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{38 + offset};5;{value}"
    if isinstance(value, (tuple, list)) and len(value) == 3:
        r, g, b = value
        return f"{38 + offset};2;{r};{g};{b}"
    try:
        return str(_COLORS[value] + offset)
    except (KeyError, TypeError):
        raise TypeError(f"Unknown color {value!r}") from None


def style(text, fg=None, bg=None, bold=None, dim=None, underline=None,
          overline=None, italic=None, blink=None, reverse=None,
          strikethrough=None, reset=True):
    if not isinstance(text, str):
        text = str(text)
    codes = []
    if fg is not None:
        codes.append(_color_code(fg, False))
    if bg is not None:
        codes.append(_color_code(bg, True))
    toggles = (
        (bold, 1, 22), (dim, 2, 22), (underline, 4, 24), (overline, 53, 55),
        (italic, 3, 23), (blink, 5, 25), (reverse, 7, 27), (strikethrough, 9, 29),
    )
    for flag, on, off in toggles:
        if flag is not None:
            codes.append(str(on if flag else off))
    prefix = "".join(f"\033[{c}m" for c in codes)
    return prefix + text + ("\033[0m" if reset else "")


def secho(message=None, file=None, nl=True, err=False, color=None, **styles):
    if message is not None and not isinstance(message, (bytes, bytearray)):
        message = style(message, **styles)
    return echo(message, file=file, nl=nl, err=err, color=color)



def _binary_target(file):
    """Return an underlying byte stream for ``file`` or None."""
    buf = getattr(file, "buffer", None)
    if buf is not None:
        return buf
    mode = getattr(file, "mode", "")
    if "b" in str(mode):
        return file
    return None


def echo(message=None, file=None, nl=True, err=False, color=None):
    if file is None:
        file = sys.stderr if err else sys.stdout
    if file is None:
        return

    if message is not None and not isinstance(message, (str, bytes, bytearray)):
        message = str(message)

    if nl:
        message = message or ""
        message += b"\n" if isinstance(message, (bytes, bytearray)) else "\n"

    if not message:
        try:
            file.flush()
        except Exception:
            pass
        return

    if isinstance(message, (bytes, bytearray)):
        target = _binary_target(file)
        if target is None:
            file.write(bytes(message).decode(getattr(file, "encoding", None) or "utf-8", "replace"))
            file.flush()
            return
        try:
            file.flush()
        except Exception:
            pass
        target.write(message)
        target.flush()
        return

    if should_strip_ansi(file, resolve_color_default(color)):
        message = unstyle(message)
    file.write(message)
    file.flush()


def get_binary_stream(name):
    stream = getattr(sys, name, None)
    if stream is None:
        raise RuntimeError(f"The standard stream {name!r} is not available.")
    return getattr(stream, "buffer", stream)


def get_text_stream(name, encoding=None, errors="strict"):
    if name not in ("stdin", "stdout", "stderr"):
        raise ValueError(f"Unknown standard stream {name!r}")
    stream = getattr(sys, name, None)
    if stream is None:
        raise RuntimeError(f"The standard stream {name!r} is not available.")
    if (encoding is not None or errors != "strict") and hasattr(stream, "buffer"):
        return _NonClosingText(stream.buffer, encoding or getattr(stream, "encoding", None) or "utf-8",
                               errors)
    return stream


def _NonClosingText(buffer, encoding, errors):
    """Text view over a shared byte stream that leaves the stream open when dropped."""
    import io

    class Wrapper(io.TextIOWrapper):
        def __del__(self):
            try:
                self.detach()
            except Exception:
                pass

    return Wrapper(buffer, encoding=encoding, errors=errors)


def format_filename(filename, shorten=False):
    if shorten:
        filename = os.path.basename(filename)
    else:
        filename = os.fspath(filename)
    if isinstance(filename, bytes):
        return filename.decode(sys.getfilesystemencoding(), "replace")
    return filename.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def make_str(value):
    if isinstance(value, bytes):
        try:
            return value.decode(sys.getfilesystemencoding())
        except UnicodeError:
            return value.decode("utf-8", "replace")
    return str(value)


def make_default_short_help(help, max_length=45):
    """First sentence of the first paragraph, trimmed to ``max_length``."""
    paragraph = help.split("\f", 1)[0].strip().split("\n\n", 1)[0]
    words = paragraph.split()
    if not words:
        return ""
    if words[0] == "\b":
        words = words[1:]
    out = []
    length = 0
    truncated = False
    for word in words:
        extra = len(word) + (1 if out else 0)
        if length + extra > max_length - 3 and length + extra > max_length:
            truncated = True
            break
        out.append(word)
        length += extra
        if word.endswith("."):
            break
    text = " ".join(out)
    if truncated:
        # leave room for the ellipsis
        while out and len(" ".join(out)) + 3 > max_length:
            out.pop()
        text = " ".join(out) + "..."
    return text


def safecall(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            pass
    return wrapper


def _detect_program_name(path=None, _main=None):
    """Work out how the program was started, for use in usage messages."""
    if _main is None:
        _main = sys.modules["__main__"]
    if not path:
        path = sys.argv[0]

    package = getattr(_main, "__package__", None)
    if package in (None, "") or (
        os.name == "nt" and package == "" and not os.path.exists(path)
        and os.path.exists(f"{path}.exe")
    ):
        # a plain script or console entry point
        return os.path.basename(path)

    module = package
    name = os.path.splitext(os.path.basename(path))[0]
    if name != "__main__":
        module = f"{module}.{name}"
    return f"python -m {module.lstrip('.')}"


def _expand_args(args, *, user=True, env=True, glob_recursive=True):
    """Apply ~, $VAR and glob expansion to each argument (shells do this on POSIX)."""
    from glob import glob

    out = []
    for arg in args:
        if user:
            arg = os.path.expanduser(arg)
        if env:
            arg = os.path.expandvars(arg)
        try:
            matches = glob(arg, recursive=glob_recursive)
        except re.error:
            matches = []
        out.extend(matches if matches else [arg])
    return out


def get_app_dir(
app_name, roaming=True, force_posix=False):
    if sys.platform.startswith("win"):
        key = "APPDATA" if roaming else "LOCALAPPDATA"
        folder = os.environ.get(key) or os.path.expanduser("~")
        return os.path.join(folder, app_name)
    if force_posix:
        return os.path.join(os.path.expanduser(f"~/.{app_name.lower().replace(' ', '-')}"))
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"), app_name)
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, app_name.lower().replace(" ", "-"))


class _AtomicFile:
    """Writes to a temp file and renames it into place on a clean close."""

    def __init__(self, stream, tmp_path, real_path):
        self._stream = stream
        self._tmp = tmp_path
        self._real = real_path

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def close(self, delete=False):
        if self._stream.closed:
            return
        self._stream.close()
        if delete:
            os.unlink(self._tmp)
        else:
            os.replace(self._tmp, self._real)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close(delete=exc_type is not None)

    def __iter__(self):
        return iter(self._stream)

    @property
    def name(self):
        return self._real


def open_stream(filename, mode="r", encoding=None, errors="strict", atomic=False):
    """Open ``filename``; returns ``(stream, should_close)``. ``-`` is stdio."""
    if filename == "-":
        binary = "b" in mode
        if "w" in mode or "a" in mode or "x" in mode:
            stream = get_binary_stream("stdout") if binary else get_text_stream("stdout", encoding, errors)
        else:
            stream = get_binary_stream("stdin") if binary else get_text_stream("stdin", encoding, errors)
        return stream, False

    if not atomic or "r" in mode or "a" in mode:
        return open(filename, mode, encoding=None if "b" in mode else encoding,
                    errors=None if "b" in mode else errors), True

    import tempfile
    directory, base = os.path.split(os.path.abspath(os.fspath(filename)))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".__atomic-{base}.")
    os.close(fd)
    os.unlink(tmp)
    try:
        perms = os.stat(filename).st_mode & 0o7777
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        perms = 0o666 & ~umask
    stream = open(tmp, mode, encoding=None if "b" in mode else encoding,
                  errors=None if "b" in mode else errors)
    try:
        os.chmod(tmp, perms)
    except OSError:
        pass
    return _AtomicFile(stream, tmp, os.path.abspath(os.fspath(filename))), True


class KeepOpenFile:
    """Proxy that ignores close requests (used for stdio)."""

    def __init__(self, file):
        self._file = file

    def __getattr__(self, name):
        return getattr(self._file, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def close(self):
        pass

    def __iter__(self):
        return iter(self._file)

    def __repr__(self):
        return repr(self._file)


class LazyFile:
    """File proxy that postpones opening until first use."""

    def __init__(self, filename, mode="r", encoding=None, errors="strict", atomic=False):
        self.name = os.fspath(filename)
        self.mode = mode
        self.encoding = encoding
        self.errors = errors
        self.atomic = atomic
        self._should_close = False
        if self.name == "-":
            self._f, self._should_close = open_stream(filename, mode, encoding, errors)
        elif "r" in mode:
            # fail early on unreadable files
            open(filename, mode).close()
            self._f = None
        else:
            self._f = None

    def __getattr__(self, name):
        return getattr(self.open(), name)

    def __repr__(self):
        if self._f is not None:
            return repr(self._f)
        return f"<unopened file '{format_filename(self.name)}' {self.mode}>"

    def open(self):
        if self._f is not None:
            return self._f
        from .exceptions import FileError
        try:
            self._f, self._should_close = open_stream(
                self.name, self.mode, self.encoding, self.errors, atomic=self.atomic)
        except OSError as e:
            raise FileError(self.name, hint=e.strerror) from e
        return self._f

    def close(self):
        if self._f is not None:
            self._f.close()

    def close_intelligently(self):
        if self._should_close:
            self.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close_intelligently()

    def __iter__(self):
        self.open()
        return iter(self._f)


def open_file(filename, mode="r", encoding=None, errors="strict", lazy=False, atomic=False):
    if lazy:
        return LazyFile(filename, mode, encoding, errors, atomic=atomic)
    stream, should_close = open_stream(filename, mode, encoding, errors, atomic=atomic)
    if not should_close:
        return KeepOpenFile(stream)
    return stream
