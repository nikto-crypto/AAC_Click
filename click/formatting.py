"""Layout of help pages."""
import shutil
from contextlib import contextmanager

from .utils import _ANSI_RE

FORCED_WIDTH = None


def term_len(text):
    return len(_ANSI_RE.sub("", text))


def measure_table(rows):
    widths = {}
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths.get(i, 0), term_len(cell))
    return tuple(w for _, w in sorted(widths.items()))


def iter_rows(rows, col_count):
    for row in rows:
        yield tuple(row) + ("",) * (col_count - len(row))


def _split_paragraphs(text):
    """Yield (indent, raw, text) for each blank-line separated block."""
    blocks = []
    lines = []
    indent = None

    def flush():
        if not lines:
            return
        if lines[0].strip() == "\b":
            blocks.append((indent or 0, True, "\n".join(lines[1:])))
        else:
            blocks.append((indent or 0, False, " ".join(ln.strip() for ln in lines)))

        del lines[:]

    for line in text.splitlines():
        if not line:
            flush()
            indent = None
            continue
        if indent is None:
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            line = stripped
        lines.append(line)
    flush()
    return blocks


def wrap_text(text, width=78, initial_indent="", subsequent_indent="", preserve_paragraphs=False):
    import textwrap

    def fill(
body, extra, first):
        w = textwrap.TextWrapper(
            width=width,
            initial_indent=(initial_indent if first else subsequent_indent) + extra,
            subsequent_indent=subsequent_indent + extra,
            replace_whitespace=False,
        )
        return w.fill(body)

    if not preserve_paragraphs:
        return fill(text, "", True)

    out = []
    for n, (indent, raw, body) in enumerate(_split_paragraphs(text)):
        extra = " " * indent
        if raw:
            prefix = (initial_indent if n == 0 else subsequent_indent) + extra
            out.append("\n".join(prefix + ln for ln in body.splitlines()))
        else:
            out.append(fill(body, extra, n == 0))
    return "\n\n".join(out)


class HelpFormatter:
    def __init__(self, indent_increment=2, width=None, max_width=None):
        self.indent_increment = indent_increment
        if max_width is None:
            max_width = 80
        if width is None:
            width = FORCED_WIDTH
            if width is None:
                width = max(min(shutil.get_terminal_size().columns, max_width) - 2, 50)
        self.width = width
        self.current_indent = 0
        self.buffer = []

    def write(self, text):
        self.buffer.append(text)

    def indent(self):
        self.current_indent += self.indent_increment

    def dedent(self):
        self.current_indent -= self.indent_increment

    def write_usage(self, prog, args="", prefix="Usage: "):
        lead = f"{prefix:>{self.current_indent}}{prog} "
        room = self.width - self.current_indent
        if room >= (term_len(lead) - self.current_indent) + 20:
            pad = " " * term_len(lead)
            self.write(wrap_text(args, room, initial_indent=lead, subsequent_indent=pad))
        else:
            self.write(lead)
            self.write("\n")
            pad = " " * (max(self.current_indent, term_len(prefix)) + 4)
            self.write(wrap_text(args, room, initial_indent=pad, subsequent_indent=pad))
        self.write("\n")

    def write_heading(self, heading):
        self.write(f"{'':>{self.current_indent}}{heading}:\n")

    def write_paragraph(self):
        if self.buffer:
            self.write("\n")

    def write_text(self, text):
        pad = " " * self.current_indent
        self.write(wrap_text(text, self.width, initial_indent=pad,
                             subsequent_indent=pad, preserve_paragraphs=True))
        self.write("\n")

    def write_dl(self, rows, col_max=30, col_spacing=2):
        rows = list(rows)
        widths = measure_table(rows)
        if len(widths) != 2:
            raise TypeError("Expected two columns for definition list")
        first_col = min(widths[0], col_max) + col_spacing
        for first, second in iter_rows(rows, 2):
            self.write(f"{'':>{self.current_indent}}{first}")
            if not second:
                self.write("\n")
                continue
            if term_len(first) <= first_col - col_spacing:
                self.write(" " * (first_col - term_len(first)))
            else:
                self.write("\n")
                self.write(" " * (first_col + self.current_indent))
            text_width = max(self.width - first_col - 2, 10)
            lines = wrap_text(second, text_width, preserve_paragraphs=True).splitlines()
            if lines:
                self.write(lines[0] + "\n")
                for line in lines[1:]:
                    self.write(f"{'':>{first_col + self.current_indent}}{line}\n")
            else:
                self.write("\n")

    @contextmanager
    def section(self, name):
        self.write_paragraph()
        self.write_heading(name)
        self.indent()
        try:
            yield
        finally:
            self.dedent()

    @contextmanager
    def indentation(self):
        self.indent()
        try:
            yield
        finally:
            self.dedent()

    def getvalue(self):
        return "".join(self.buffer)


def join_options(options):
    """Join option names for help; short prefixes first."""
    from .parser import split_opt
    ranked = []
    slash = False
    for opt in options:
        prefix = split_opt(opt)[0]
        if prefix == "/":
            slash = True
        ranked.append((len(prefix), opt))
    ranked.sort(key=lambda p: p[0])
    return ", ".join(o for _, o in ranked), slash
