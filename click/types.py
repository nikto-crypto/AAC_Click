"""Parameter types: conversion and validation of command line values."""
import datetime
import os
import stat

from .exceptions import BadParameter
from .utils import format_filename, open_file


class ParamType:
    """Base class. Subclasses implement ``convert``."""

    is_composite = False
    arity = 1
    name = ""
    envvar_list_splitter = None

    def to_info_dict(self):
        kind = type(self).__name__.partition("ParamType")[0].partition("ParameterType")[0]
        return {"param_type": kind, "name": self.name or type(self).__name__}

    def __call__(self, value, param=None, ctx=None):
        if value is not None:
            return self.convert(value, param, ctx)

    def get_metavar(self, param, ctx=None):
        return None

    def get_missing_message(self, param, ctx=None):
        return None

    def convert(self, value, param, ctx):
        return value

    def split_envvar_value(self, rv):
        return (rv or "").split(self.envvar_list_splitter)

    def fail(self, message, param=None, ctx=None):
        raise BadParameter(message, ctx=ctx, param=param)

    def shell_complete(self, ctx, param, incomplete):
        return []


class CompositeParamType(ParamType):
    is_composite = True

    @property
    def arity(self):
        raise NotImplementedError()


class FuncParamType(ParamType):
    def __init__(self, func):
        self.name = getattr(func, "__name__", repr(func))
        self.func = func

    def to_info_dict(self):
        d = super().to_info_dict()
        d["func"] = self.func
        return d


    def convert(self, value, param, ctx):
        try:
            return self.func(value)
        except ValueError:
            try:
                value = str(value)
            except UnicodeError:
                value = value.decode("utf-8", "replace")
            self.fail(value, param, ctx)


class UnprocessedParamType(ParamType):
    name = "text"

    def convert(self, value, param, ctx):
        return value

    def __repr__(self):
        return "UNPROCESSED"


class StringParamType(ParamType):
    name = "text"

    def convert(self, value, param, ctx):
        if isinstance(value, bytes):
            import sys
            for enc in (getattr(sys.stdin, "encoding", None), sys.getfilesystemencoding()):
                if not enc:
                    continue
                try:
                    return value.decode(enc)
                except UnicodeError:
                    pass
            return value.decode("utf-8", "replace")
        return str(value)

    def __repr__(self):
        return "STRING"


class Choice(ParamType):
    name = "choice"

    def __init__(self, choices, case_sensitive=True):
        self.choices = tuple(choices)
        self.case_sensitive = case_sensitive

    def to_info_dict(self):
        d = super().to_info_dict()
        d["choices"] = list(self.choices)
        d["case_sensitive"] = self.case_sensitive
        return d

    def _norm(self, text):
        return text if self.case_sensitive else text.casefold()

    def get_metavar(self, param, ctx=None):
        body = "|".join(str(c) for c in self.choices)
        if param.required and param.param_type_name == "argument":
            return "{" + body + "}"
        return "[" + body + "]"

    def get_missing_message(self, param, ctx=None):
        return "Choose from:\n\t" + ",\n\t".join(str(c) for c in self.choices)

    def convert(self, value, param, ctx):
        normalize = ctx.token_normalize_func if ctx is not None else None
        text = str(value)
        names = {str(c): c for c in self.choices}
        if normalize is not None:
            text = normalize(text)
            names = {normalize(k): v for k, v in names.items()}
        wanted = self._norm(text)
        lookup = {self._norm(k): v for k, v in names.items()}
        if wanted in lookup:
            return lookup[wanted]

        shown = ", ".join(repr(str(c)) for c in self.choices)
        self.fail(f"{value!r} is not one of {shown}.", param, ctx)

    def __repr__(self):
        return f"Choice({list(self.choices)})"

    def shell_complete(self, ctx, param, incomplete):
        from .shell_completion import CompletionItem
        prefix = self._norm(incomplete)
        return [CompletionItem(str(c)) for c in self.choices if self._norm(str(c)).startswith(prefix)]


class DateTime(ParamType):
    name = "datetime"

    def __init__(self, formats=None):
        self.formats = list(formats) if formats else ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]

    def to_info_dict(self):
        d = super().to_info_dict()
        d["formats"] = self.formats
        return d

    def get_metavar(self, param, ctx=None):
        return "[" + "|".join(self.formats) + "]"

    def convert(self, value, param, ctx):
        if isinstance(value, datetime.datetime):
            return value
        for fmt in self.formats:
            try:
                return datetime.datetime.strptime(value, fmt)
            except ValueError:
                continue
        if len(self.formats) == 1:
            msg = f"{value!r} does not match the format {self.formats[0]!r}."
        else:
            msg = f"{value!r} does not match the formats " + ", ".join(repr(f) for f in self.formats) + "."
        self.fail(msg, param, ctx)

    def __repr__(self):
        return "DateTime"


class _NumberParamType(ParamType):
    _py = int
    _label = "integer"

    def convert(self, value, param, ctx):
        try:
            return self._py(value)
        except ValueError:
            self.fail(f"{value!r} is not a valid {self._label}.", param, ctx)


class IntParamType(_NumberParamType):
    name = "integer"

    def __repr__(self):
        return "INT"


class FloatParamType(_NumberParamType):
    name = "float"
    _py = float
    _label = "float"

    def __repr__(self):
        return "FLOAT"


class _NumberRange(ParamType):
    """Mixin logic shared by IntRange and FloatRange."""

    def __init__(self, min=None, max=None, min_open=False, max_open=False, clamp=False):
        self.min = min
        self.max = max
        self.min_open = min_open
        self.max_open = max_open
        self.clamp = clamp

    def to_info_dict(self):
        d = super().to_info_dict()
        d.update(min=self.min, max=self.max, min_open=self.min_open,
                 max_open=self.max_open, clamp=self.clamp)
        return d

    def _out_of_range(self, rv):
        if self.min is not None and (rv < self.min or (self.min_open and rv == self.min)):
            return -1
        if self.max is not None and (rv > self.max or (self.max_open and rv == self.max)):
            return 1
        return 0

    def _clamped(self, rv, side):
        bound, is_open = (self.min, self.min_open) if side < 0 else (self.max, self.max_open)
        if not is_open:
            return bound
        return self._step(bound, -side)

    def _step(self, bound, direction):
        raise NotImplementedError

    def convert(self, value, param, ctx):
        rv = super().convert(value, param, ctx)
        side = self._out_of_range(rv)
        if side:
            if self.clamp:
                return self._clamped(rv, side)
            self.fail(f"{rv} is not in the range {self._describe_range()}.", param, ctx)
        return rv

    def _describe_range(self):
        if self.min is None:
            op = "<" if self.max_open else "<="
            return f"x{op}{self.max}"
        if self.max is None:
            op = ">" if self.min_open else ">="
            return f"x{op}{self.min}"
        lo = "<" if self.min_open else "<="
        hi = "<" if self.max_open else "<="
        return f"{self.min}{lo}x{hi}{self.max}"

    def __repr__(self):
        return f"{type(self).__name__.replace('Range', 'Range')}({self.min!r}, {self.max!r})"


class IntRange(_NumberRange, IntParamType):
    name = "integer range"

    def _step(self, bound, direction):
        return bound + direction


class FloatRange(_NumberRange, FloatParamType):
    name = "float range"

    def __init__(self, min=None, max=None, min_open=False, max_open=False, clamp=False):
        super().__init__(min, max, min_open, max_open, clamp)
        if clamp and (min_open or max_open):
            raise TypeError("Clamping is not supported for open bounds.")

    def _step(self, bound, direction):
        raise RuntimeError("Clamping is not supported for open bounds.")


class BoolParamType(ParamType):
    name = "boolean"
    _true = {"1", "true", "t", "yes", "y", "on"}
    _false = {"0", "false", "f", "no", "n", "off"}

    def convert(self, value, param, ctx):
        if value in {False, True}:
            return bool(value)
        text = str(value).strip().lower()
        if text in self._true:
            return True
        if text in self._false:
            return False
        self.fail(f"{value!r} is not a valid boolean.", param, ctx)

    def __repr__(self):
        return "BOOL"


class UUIDParameterType(ParamType):
    name = "uuid"

    def convert(self, value, param, ctx):
        import uuid

        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value).strip())

        except ValueError:
            self.fail(f"{value!r} is not a valid UUID.", param, ctx)

    def __repr__(self):
        return "UUID"


class File(ParamType):
    name = "filename"
    envvar_list_splitter = os.path.pathsep

    def __init__(self, mode="r", encoding=None, errors="strict", lazy=None, atomic=False):
        self.mode = mode
        self.encoding = encoding
        self.errors = errors
        self.lazy = lazy
        self.atomic = atomic

    def to_info_dict(self):
        d = super().to_info_dict()
        d.update(mode=self.mode, encoding=self.encoding)
        return d

    def resolve_lazy_flag(self, value):
        if self.lazy is not None:
            return self.lazy
        if os.fspath(value) == "-":
            return False
        if "w" in self.mode:
            return True
        return False

    def convert(self, value, param, ctx):
        if hasattr(value, "read") or hasattr(value, "write"):
            return value
        try:
            lazy = self.resolve_lazy_flag(value)
            f = open_file(value, self.mode, self.encoding, self.errors, lazy=lazy, atomic=self.atomic)
            if ctx is not None:
                if lazy:
                    ctx.call_on_close(f.close_intelligently)
                else:
                    ctx.call_on_close(getattr(f, "close", lambda: None))
            return f
        except OSError as e:
            self.fail(f"'{format_filename(value)}': {e.strerror}", param, ctx)

    def shell_complete(self, ctx, param, incomplete):
        from .shell_completion import CompletionItem
        return [CompletionItem(incomplete, type="file")]


def _is_file_like(value):
    return hasattr(value, "read") or hasattr(value, "write")


class Path(ParamType):
    envvar_list_splitter = os.path.pathsep

    def __init__(self, exists=False, file_okay=True, dir_okay=True, writable=False,
                 readable=True, resolve_path=False, allow_dash=False, path_type=None,
                 executable=False):
        self.exists = exists
        self.file_okay = file_okay
        self.dir_okay = dir_okay
        self.readable = readable
        self.writable = writable
        self.executable = executable
        self.resolve_path = resolve_path
        self.allow_dash = allow_dash
        self.type = path_type
        if self.file_okay and not self.dir_okay:
            self.name = "file"
        elif self.dir_okay and not self.file_okay:
            self.name = "directory"
        else:
            self.name = "path"

    def to_info_dict(self):
        d = super().to_info_dict()
        d.update(exists=self.exists, file_okay=self.file_okay, dir_okay=self.dir_okay,
                 writable=self.writable, readable=self.readable, allow_dash=self.allow_dash)
        return d

    def coerce_path_result(self, rv):
        if self.type is not None and not isinstance(rv, self.type):
            if self.type is str:
                return os.fsdecode(rv)
            if self.type is bytes:
                return os.fsencode(rv)
            return self.type(rv)
        return rv

    def convert(self, value, param, ctx):
        rv = value
        is_dash = self.file_okay and self.allow_dash and rv in (b"-", "-")
        if not is_dash:
            if self.resolve_path:
                rv = os.path.realpath(rv)
            try:
                st = os.stat(rv)
            except OSError:
                if not self.exists:
                    return self.coerce_path_result(rv)
                self.fail(f"{self.name.title()} {format_filename(value)!r} does not exist.", param, ctx)
            if not self.file_okay and stat.S_ISREG(st.st_mode):
                self.fail(f"{self.name.title()} {format_filename(value)!r} is a file.", param, ctx)
            if not self.dir_okay and stat.S_ISDIR(st.st_mode):
                self.fail(f"{self.name.title()} {format_filename(value)!r} is a directory.", param, ctx)
            if self.readable and not os.access(rv, os.R_OK):
                self.fail(f"{self.name.title()} {format_filename(value)!r} is not readable.", param, ctx)
            if self.writable and not os.access(rv, os.W_OK):
                self.fail(f"{self.name.title()} {format_filename(value)!r} is not writable.", param, ctx)
            if self.executable and not os.access(value, os.X_OK):
                self.fail(f"{self.name.title()} {format_filename(value)!r} is not executable.", param, ctx)
        return self.coerce_path_result(rv)

    def shell_complete(self, ctx, param, incomplete):
        from .shell_completion import CompletionItem
        kind = "dir" if self.dir_okay and not self.file_okay else "file"
        return [CompletionItem(incomplete, type=kind)]


class Tuple(CompositeParamType):
    def __init__(self, types):
        self.types = [convert_type(t) for t in types]

    def to_info_dict(self):
        d = super().to_info_dict()
        d["types"] = [t.to_info_dict() for t in self.types]
        return d

    @property
    def name(self):
        return "<" + " ".join(t.name for t in self.types) + ">"

    @property
    def arity(self):
        return len(self.types)

    def convert(self, value, param, ctx):
        if len(value) != len(self.types):
            raise TypeError("It would appear that nargs is set to conflict with the composite type arity.")
        return tuple(t(v, param, ctx) for t, v in zip(self.types, value))


def convert_type(ty, default=None):
    """Turn a Python type, tuple of types, or ParamType into a ParamType."""
    guessed = False
    if ty is None and default is not None:
        if isinstance(default, (tuple, list)):
            if default:
                item = default[0]
                if isinstance(item, (tuple, list)):
                    ty = tuple(type(d) for d in item)
                else:
                    ty = type(item)
        else:
            ty = type(default)
        guessed = True

    if isinstance(ty, tuple):
        return Tuple(ty)
    if isinstance(ty, ParamType):
        return ty
    if ty is str or ty is None:
        return STRING
    if ty is int:
        return INT
    if ty is float:
        return FLOAT
    if ty is bool:
        return BOOL
    if guessed:
        return STRING
    return FuncParamType(ty)


UNPROCESSED = UnprocessedParamType()
STRING = StringParamType()
INT = IntParamType()
FLOAT = FloatParamType()
BOOL = BoolParamType()
UUID = UUIDParameterType()
