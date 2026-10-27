"""Heavier terminal machinery behind ``termui``: progress bars, pager, editor."""
import math
import os
import subprocess
import sys
import tempfile
import time
from operator import length_hint

from ._compat import WIN, isatty, term_len
from .exceptions import ClickException
from .utils import echo

msvcrt = None  # only meaningful on Windows

BEFORE_BAR = "\r\033[?25l"
AFTER_BAR = "\033[?25h\n"


class ProgressBar:
    def __init__(self, iterable, length=None, fill_char="#", empty_char=" ",
                 bar_template="%(bar)s", info_sep="  ", show_eta=True, show_percent=None,
                 show_pos=False, item_show_func=None, label=None, file=None, color=None,
                 update_min_steps=1, width=30):
        self.fill_char = fill_char
        self.empty_char = empty_char
        self.bar_template = bar_template
        self.info_sep = info_sep
        self.show_eta = show_eta
        self.show_percent = show_percent
        self.show_pos = show_pos
        self.item_show_func = item_show_func
        self.label = label or ""
        if file is None:
            file = sys.stdout
        self.file = file
        self.color = color
        self.update_min_steps = update_min_steps
        self._completed_intervals = 0
        self.width = width
        self.autowidth = width == 0

        if length is None:
            length = length_hint(iterable, -1)
            if length == -1:
                length = None
        if iterable is None:
            if length is None:
                raise TypeError("iterable or length is required")
            iterable = range(length)
        self.iter = iter(iterable)
        self.length = length
        self.pos = 0
        self.avg = []
        self.last_eta = self.start = time.time()
        self.eta_known = False
        self.finished = False
        self.max_width = None
        self.entered = False
        self.current_item = None
        self.is_hidden = not isatty(self.file)
        self._last_line = None

    # -- context manager / iteration ---------------------------------
    def __enter__(self):
        self.entered = True
        self.render_progress()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.render_finish()

    def __iter__(self):
        if not self.entered:
            raise RuntimeError("You need to use progress bars in a with block.")
        self.render_progress()
        return self.generator()

    def __next__(self):
        return next(iter(self))

    def generator(self):
        if not self.entered:
            raise RuntimeError("You need to use progress bars in a with block.")
        if self.is_hidden:
            yield from self.iter
            return
        for item in self.iter:
            self.current_item = item
            if self._completed_intervals == 0:
                self.render_progress()
            yield item
            self.update(1)
        self.finish()
        self.render_progress()

    # -- progress arithmetic -----------------------------------------
    @property
    def pct(self):
        if self.finished:
            return 1.0
        return min(self.pos / (float(self.length or 1) or 1), 1.0)

    @property
    def time_per_iteration(self):
        if not self.avg:
            return 0.0
        return sum(self.avg) / float(len(self.avg))

    @property
    def eta(self):
        if self.length is not None and not self.finished:
            return self.time_per_iteration * (self.length - self.pos)
        return 0.0

    def make_step(self, n_steps):
        self.pos += n_steps
        if self.length is not None and self.pos >= self.length:
            self.finished = True
        if (time.time() - self.last_eta) < 1.0:
            return
        self.last_eta = time.time()
        elapsed = time.time() - self.start
        step = elapsed / self.pos if self.pos else elapsed
        self.avg = self.avg[-6:] + [step]
        self.eta_known = self.length is not None

    def update(self, n_steps, current_item=None):
        self._completed_intervals += n_steps
        if self._completed_intervals >= self.update_min_steps:
            self.make_step(self._completed_intervals)
            if current_item is not None:
                self.current_item = current_item
            self.render_progress()
            self._completed_intervals = 0

    def finish(self):
        self.eta_known = False
        self.current_item = None
        self.finished = True

    # -- formatting ---------------------------------------------------
    def format_eta(self):
        if not self.eta_known:
            return ""
        t = int(self.eta)
        seconds = t % 60
        t //= 60
        minutes = t % 60
        t //= 60
        hours = t % 24
        days = t // 24
        if days > 0:
            return f"{days}d {hours:02}:{minutes:02}:{seconds:02}"
        return f"{hours:02}:{minutes:02}:{seconds:02}"

    def format_pos(self):
        pos = str(self.pos)
        if self.length is not None:
            pos += f"/{self.length}"
        return pos

    def format_pct(self):
        return f"{int(self.pct * 100): 4}%"[1:]

    def format_bar(self):
        if self.length is not None:
            filled = min(int(self.pct * self.width), self.width)
            return self.fill_char * filled + self.empty_char * (self.width - filled)
        if self.finished:
            return self.fill_char * self.width
        cells = list(self.empty_char * (self.width or 1))
        if self.time_per_iteration != 0:
            idx = int((math.cos(self.pos * self.time_per_iteration) / 2.0 + 0.5) * self.width)
            cells[min(idx, len(cells) - 1)] = self.fill_char
        return "".join(cells)

    def format_progress_line(self):
        show_percent = self.show_percent
        bits = []
        if self.length is not None and show_percent is None:
            show_percent = not self.show_pos
        if self.show_pos:
            bits.append(self.format_pos())
        if show_percent:
            bits.append(self.format_pct())
        if self.show_eta and self.eta_known and not self.finished:
            bits.append(self.format_eta())
        if self.item_show_func is not None:
            extra = self.item_show_func(self.current_item)
            if extra is not None:
                bits.append(extra)
        return (self.bar_template % {
            "label": self.label,
            "bar": self.format_bar(),
            "info": self.info_sep.join(bits),
        }).rstrip()

    # -- output -------------------------------------------------------
    def render_progress(self):
        if self.is_hidden:
            if self._last_line != self.label:
                self._last_line = self.label
                echo(self.label, file=self.file, color=self.color)
            return

        if self.autowidth:
            import shutil
            old = self.width
            self.width = 0
            clear_width = shutil.get_terminal_size().columns
            reserved = term_len(self.format_progress_line())
            self.width = max(clear_width - reserved, 0)
            if old and self.width == 0:
                self.width = old
        clear_width = self.width if self.max_width is None else self.max_width

        line = self.format_progress_line()
        length = term_len(line)
        if self.max_width is None or self.max_width < length:
            self.max_width = length
        out = BEFORE_BAR + line + " " * max(clear_width - length, 0)
        if out != self._last_line:
            self._last_line = out
            echo(out, file=self.file, color=self.color, nl=False)
            self.file.flush()

    def render_finish(self):
        if self.is_hidden:
            return
        self.file.write(AFTER_BAR)
        self.file.flush()


def _pager_command():
    return os.environ.get("PAGER", "").strip() or None


def pager(text, color=None):
    """Show ``text`` through the user's pager (or print it when not interactive)."""
    stdout = sys.stdout
    if not isatty(sys.stdin) or not isatty(stdout):
        echo(text, color=color, nl=False)
        return
    cmd = _pager_command() or "less"
    env = dict(os.environ)
    if os.path.basename(cmd.split()[0]) == "less":
        env.setdefault("LESS", "-R")
        color = True if color is None else color
    if not color:
        from ._compat import strip_ansi
        text = strip_ansi(text)
    try:
        proc = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, env=env)
    except OSError:
        echo(text, color=color, nl=False)
        return
    try:
        proc.stdin.write(text.encode(getattr(stdout, "encoding", None) or "utf-8", "replace"))
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    while True:
        try:
            proc.wait()
            break
        except KeyboardInterrupt:
            pass


class Editor:
    def __init__(self, editor=None, env=None, require_save=True, extension=".txt"):
        self.editor = editor
        self.env = env
        self.require_save = require_save
        self.extension = extension

    def get_editor(self):
        if self.editor:
            return self.editor
        for key in ("VISUAL", "EDITOR"):
            val = os.environ.get(key)
            if val:
                return val
        if WIN:
            return "notepad"
        for name in ("sensible-editor", "vim", "nano"):
            if any(os.access(os.path.join(p, name), os.X_OK)
                   for p in os.environ.get("PATH", "").split(os.pathsep)):
                return name
        return "vi"

    def edit_file(self, filename):
        editor = self.get_editor()
        env = None
        if self.env:
            env = os.environ.copy()
            env.update(self.env)
        try:
            rc = subprocess.Popen(f'{editor} "{filename}"', env=env, shell=True).wait()
        except OSError as e:
            raise ClickException(f"{editor}: Editing failed: {e}") from e
        if rc != 0:
            raise ClickException(f"{editor}: Editing failed")

    def edit(self, text):
        binary = isinstance(text, (bytes, bytearray))
        if text is None:
            text = ""
        if not binary:
            text = text.replace("\r\n", "\n")
            if text and not text.endswith("\n"):
                text += "\n"
            payload = text.encode("utf-8")

            if WIN:
                payload = text.replace("\n", "\r\n").encode("utf-8-sig")
        else:
            payload = bytes(text)

        fd, name = tempfile.mkstemp(prefix="editor-", suffix=self.extension)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            before = os.path.getmtime(name)
            self.edit_file(name)
            if self.require_save and os.path.getmtime(name) == before:
                return None
            with open(name, "rb") as f:
                data = f.read()
            if binary:
                return data
            return data.decode("utf-8-sig").replace("\r\n", "\n")
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass


def open_url(url, wait=False, locate=False):
    if sys.platform == "darwin":
        args = ["open"] + (["-R"] if locate else []) + (["-W"] if wait else []) + [url]
    elif WIN:
        args = ["explorer", f"/select,{url}"] if locate else ["cmd", "/c", "start", "", url]
    else:
        target = (os.path.dirname(url) or ".") if locate else url
        args = ["xdg-open", target]
    try:
        if wait:
            return subprocess.Popen(args).wait()
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 0
    except OSError:
        return 127


def getchar(echo_input):
    """Read one keypress from the controlling terminal without waiting for Enter."""
    if not isatty(sys.stdin):
        return sys.stdin.read(1)
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 32).decode(getattr(sys.stdin, "encoding", None) or "utf-8", "replace")
        if echo_input and isatty(sys.stdout):
            sys.stdout.write(ch)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    if ch == "\x03":
        raise KeyboardInterrupt()
    if ch == "\x04":
        raise EOFError()
    return ch
