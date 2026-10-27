"""Exceptions raised while parsing and running commands."""
import os

from .utils import echo, format_filename, get_text_stream


def _join_hints(hint):
    if isinstance(hint, (list, tuple)):
        return " / ".join(repr(h) for h in hint)
    return hint


class ClickException(Exception):
    """Base for errors that are reported to the user as ``Error: ...``."""

    exit_code = 1

    def __init__(self, message):
        super().__init__(message)
        self.message = message

    def format_message(self):
        return self.message

    def __str__(self):
        return self.message

    def show(self, file=None):
        if file is None:
            file = get_text_stream("stderr")
        echo(f"Error: {self.format_message()}", file=file)


class UsageError(ClickException):
    exit_code = 2

    def __init__(self, message, ctx=None):
        super().__init__(message)
        self.ctx = ctx
        self.cmd = ctx.command if ctx is not None else None

    def show(self, file=None):
        if file is None:
            file = get_text_stream("stderr")
        color = None
        hint = ""
        ctx = self.ctx
        if ctx is not None:
            if ctx.command.get_help_option(ctx) is not None:
                hint = f"Try '{ctx.command_path} {ctx.help_option_names[0]}' for help.\n"
            color = ctx.color
            echo(f"{ctx.get_usage()}\n{hint}", file=file, color=color)
        echo(f"Error: {self.format_message()}", file=file, color=color)


class BadParameter(UsageError):
    def __init__(self, message, ctx=None, param=None, param_hint=None):
        super().__init__(message, ctx)
        self.param = param
        self.param_hint = param_hint

    def format_message(self):
        if self.param_hint is not None:
            hint = self.param_hint
        elif self.param is not None:
            hint = self.param.get_error_hint(self.ctx)
        else:
            return f"Invalid value: {self.message}"
        return f"Invalid value for {_join_hints(hint)}: {self.message}"


class MissingParameter(BadParameter):
    def __init__(self, message=None, ctx=None, param=None, param_hint=None, param_type=None):
        super().__init__(message or "", ctx, param, param_hint)
        self.param_type = param_type

    def format_message(self):
        if self.param_hint is not None:
            hint = self.param_hint
        elif self.param is not None:
            hint = self.param.get_error_hint(self.ctx)
        else:
            hint = None
        hint = _join_hints(hint)
        hint = f" {hint}" if hint else ""

        kind = self.param_type
        if kind is None and self.param is not None:
            kind = self.param.param_type_name

        msg = self.message
        if self.param is not None:
            extra = self.param.type.get_missing_message(self.param)
            if extra:
                msg = f"{msg}. {extra}" if msg else extra
        msg = f" {msg}" if msg else ""
        return f"Missing {kind}{hint}.{msg}"

    def __str__(self):
        if not self.message:
            name = self.param.name if self.param is not None else None
            return f"Missing parameter: {name}"
        return self.message



class NoSuchOption(UsageError):
    def __init__(self, option_name, message=None, possibilities=None, ctx=None):
        if message is None:
            message = f"No such option: {option_name}"
        super().__init__(message, ctx)
        self.option_name = option_name
        self.possibilities = possibilities

    def format_message(self):
        if not self.possibilities:
            return self.message
        names = sorted(self.possibilities)
        if len(names) == 1:
            return f"{self.message} Did you mean {names[0]}?"
        return f"{self.message} (Possible options: {', '.join(names)})"


class BadOptionUsage(UsageError):
    def __init__(self, option_name, message, ctx=None):
        super().__init__(message, ctx)
        self.option_name = option_name


class BadArgumentUsage(UsageError):
    pass


class NoArgsIsHelpError(UsageError):
    def __init__(self, ctx):
        self.ctx = ctx
        super().__init__(ctx.get_help(), ctx=ctx)

    def show(self, file=None):
        echo(self.format_message(), file=file, err=True, color=self.ctx.color)


class FileError(ClickException):
    def __init__(self, filename, hint=None):
        if hint is None:
            hint = "unknown error"
        super().__init__(hint)
        self.ui_filename = format_filename(filename)
        self.filename = filename

    def format_message(self):
        return f"Could not open file {self.ui_filename!r}: {self.message}"


class Abort(RuntimeError):
    """Raised to abort execution; prints ``Aborted!`` and exits 1."""


class Exit(RuntimeError):
    """Raised to leave the program with a specific exit code."""

    __slots__ = ("exit_code",)

    def __init__(self, code=0):
        self.exit_code = code
