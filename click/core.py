"""Contexts, commands, groups and parameters."""
import enum
import errno
import inspect
import os
import sys
from contextlib import ExitStack, contextmanager
from functools import update_wrapper

from . import types
from .exceptions import (Abort, BadParameter, ClickException, Exit, MissingParameter,
                         NoArgsIsHelpError, UsageError)
from .formatting import HelpFormatter, join_options
from .globals import pop_context, push_context
from .parser import OptionParser, _flag_needs_value, split_opt
from .utils import echo, make_default_short_help, make_str, style

_MISSING = object()


class ParameterSource(enum.Enum):
    COMMANDLINE = enum.auto()
    ENVIRONMENT = enum.auto()
    DEFAULT = enum.auto()
    DEFAULT_MAP = enum.auto()
    PROMPT = enum.auto()


@contextmanager
def augment_usage_errors(ctx, param=None):
    """Attach context/param to usage errors raised inside the block."""
    try:
        yield
    except BadParameter as e:
        if e.ctx is None:
            e.ctx = ctx
        if param is not None and e.param is None:
            e.param = param
        raise
    except UsageError as e:
        if e.ctx is None:
            e.ctx = ctx
        raise


def iter_params_for_processing(invocation_order, declaration_order):
    """Eager params first, then in the order they appeared on the command line."""
    def key(item):
        try:
            idx = invocation_order.index(item)
        except ValueError:
            idx = float("inf")
        return (not item.is_eager, idx)

    return sorted(declaration_order, key=key)


class Context:
    formatter_class = HelpFormatter

    def __init__(self, command, parent=None, info_name=None, obj=None,
                 auto_envvar_prefix=None, default_map=None, terminal_width=None,
                 max_content_width=None, resilient_parsing=False, allow_extra_args=None,
                 allow_interspersed_args=None, ignore_unknown_options=None,
                 help_option_names=None, token_normalize_func=None, color=None,
                 show_default=None):
        self.parent = parent
        self.command = command
        self.info_name = info_name
        self.params = {}
        self.args = []
        self.protected_args = []
        self._opt_prefixes = set(parent._opt_prefixes) if parent else set()

        if obj is None and parent is not None:
            obj = parent.obj
        self.obj = obj
        self._meta = parent.meta if parent is not None else {}

        if default_map is None and info_name is not None and parent is not None \
                and parent.default_map is not None:
            default_map = parent.default_map.get(info_name)
        self.default_map = default_map
        self.invoked_subcommand = None

        if terminal_width is None and parent is not None:
            terminal_width = parent.terminal_width
        self.terminal_width = terminal_width
        if max_content_width is None and parent is not None:
            max_content_width = parent.max_content_width
        self.max_content_width = max_content_width

        if allow_extra_args is None:
            allow_extra_args = command.allow_extra_args
        self.allow_extra_args = allow_extra_args
        if allow_interspersed_args is None:
            allow_interspersed_args = command.allow_interspersed_args
        self.allow_interspersed_args = allow_interspersed_args
        if ignore_unknown_options is None:
            ignore_unknown_options = command.ignore_unknown_options
        self.ignore_unknown_options = ignore_unknown_options

        if help_option_names is None:
            help_option_names = parent.help_option_names if parent is not None else ["--help"]
        self.help_option_names = help_option_names

        if token_normalize_func is None and parent is not None:
            token_normalize_func = parent.token_normalize_func
        self.token_normalize_func = token_normalize_func
        self.resilient_parsing = resilient_parsing

        if auto_envvar_prefix is None:
            if parent is not None and parent.auto_envvar_prefix is not None \
                    and self.info_name is not None:
                auto_envvar_prefix = f"{parent.auto_envvar_prefix}_{self.info_name.upper()}"
        else:
            auto_envvar_prefix = auto_envvar_prefix.upper()
        if auto_envvar_prefix is not None:
            auto_envvar_prefix = auto_envvar_prefix.replace("-", "_")
        self.auto_envvar_prefix = auto_envvar_prefix

        if color is None and parent is not None:
            color = parent.color
        self.color = color
        if show_default is None and parent is not None:
            show_default = parent.show_default
        self.show_default = show_default

        self._close_callbacks = []
        self._depth = 0
        self._parameter_source = {}
        self._exit_stack = ExitStack()

    def to_info_dict(self):
        return {
            "command": self.command.to_info_dict(self),
            "info_name": self.info_name,
            "allow_extra_args": self.allow_extra_args,
            "allow_interspersed_args": self.allow_interspersed_args,
            "ignore_unknown_options": self.ignore_unknown_options,
            "auto_envvar_prefix": self.auto_envvar_prefix,
        }

    def __enter__(self):
        self._depth += 1
        push_context(self)
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._depth -= 1
        if self._depth == 0:
            self.close()
        pop_context()

    @contextmanager
    def scope(self, cleanup=True):
        if not cleanup:
            self._depth += 1
        try:
            with self as rv:
                yield rv
        finally:
            if not cleanup:
                self._depth -= 1

    @property
    def meta(self):
        return self._meta

    def make_formatter(self):
        return self.formatter_class(width=self.terminal_width, max_width=self.max_content_width)

    def with_resource(self, context_manager):
        return self._exit_stack.enter_context(context_manager)

    def call_on_close(self, f):
        return self._exit_stack.callback(f)

    def close(self):
        self._exit_stack.close()
        self._exit_stack = ExitStack()

    @property
    def command_path(self):
        rv = self.info_name or ""
        if self.parent is not None:
            pieces = [self.parent.command_path]
            if isinstance(self.parent.command, Command):
                for param in self.parent.command.get_params(self):
                    pieces.extend(param.get_usage_pieces(self))
            rv = f"{' '.join(pieces)} {rv}"
        return rv.lstrip()

    def find_root(self):
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    def find_object(self, object_type):
        node = self
        while node is not None:
            if isinstance(node.obj, object_type):
                return node.obj
            node = node.parent
        return None

    def ensure_object(self, object_type):
        rv = self.find_object(object_type)
        if rv is None:
            self.obj = rv = object_type()
        return rv

    def lookup_default(self, name, call=True):
        if self.default_map is not None:
            value = self.default_map.get(name)
            if call and callable(value):
                return value()
            return value
        return None

    def fail(self, message):
        raise UsageError(message, self)

    def abort(self):
        raise Abort()

    def exit(self, code=0):
        raise Exit(code)

    def get_usage(self):
        return self.command.get_usage(self)

    def get_help(self):
        return self.command.get_help(self)

    def _make_sub_context(self, command):
        return type(self)(command, info_name=command.name, parent=self)

    def invoke(self, callback, /, *args, **kwargs):
        if isinstance(callback, Command):
            other = callback
            if other.callback is None:
                raise TypeError("The given command does not have a callback that can be invoked.")
            callback = other.callback
            ctx = self._make_sub_context(other)
            for param in other.params:
                if param.name not in kwargs and param.expose_value:
                    kwargs[param.name] = param.type_cast_value(ctx, param.get_default(ctx))
            ctx.params.update(kwargs)
        else:
            ctx = self
        with augment_usage_errors(self):
            with ctx:
                return callback(*args, **kwargs)

    def forward(self, cmd, /, *args, **kwargs):
        if not isinstance(cmd, Command):
            raise TypeError("Callback is not a command.")
        for key, value in self.params.items():
            kwargs.setdefault(key, value)
        return self.invoke(cmd, *args, **kwargs)

    def set_parameter_source(self, name, source):
        self._parameter_source[name] = source

    def get_parameter_source(self, name):
        return self._parameter_source.get(name)


class Command:
    context_class = Context
    allow_extra_args = False
    allow_interspersed_args = True
    ignore_unknown_options = False

    def __init__(self, name, context_settings=None, callback=None, params=None, help=None,
                 epilog=None, short_help=None, options_metavar="[OPTIONS]",
                 add_help_option=True, no_args_is_help=False, hidden=False, deprecated=False):
        self.name = name
        self.context_settings = dict(context_settings) if context_settings else {}
        self.callback = callback
        self.params = list(params or [])
        self.help = help
        self.epilog = epilog
        self.options_metavar = options_metavar
        self.short_help = short_help
        self.add_help_option = add_help_option
        self.no_args_is_help = no_args_is_help
        self.hidden = hidden
        self.deprecated = deprecated

    def to_info_dict(self, ctx):
        return {
            "name": self.name,
            "params": [p.to_info_dict() for p in self.get_params(ctx)],
            "help": self.help,
            "epilog": self.epilog,
            "short_help": self.short_help,
            "hidden": self.hidden,
            "deprecated": self.deprecated,
        }


    def __repr__(self):
        return f"<{type(self).__name__} {self.name}>"

    # -- help ---------------------------------------------------------
    def get_usage(self, ctx):
        f = ctx.make_formatter()
        self.format_usage(ctx, f)
        return f.getvalue().rstrip("\n")

    def get_params(self, ctx):
        rv = list(self.params)
        help_opt = self.get_help_option(ctx)
        if help_opt is not None:
            rv.append(help_opt)
        return rv

    def format_usage(self, ctx, formatter):
        pieces = self.collect_usage_pieces(ctx)
        formatter.write_usage(ctx.command_path, " ".join(pieces))

    def collect_usage_pieces(self, ctx):
        rv = [self.options_metavar] if self.options_metavar else []
        for param in self.get_params(ctx):
            rv.extend(param.get_usage_pieces(ctx))
        return rv

    def get_help_option_names(self, ctx):
        taken = {n for p in self.params for n in p.opts + p.secondary_opts}
        return [n for n in ctx.help_option_names if n not in taken]

    def get_help_option(self, ctx):
        names = self.get_help_option_names(ctx)
        if not names or not self.add_help_option:
            return None
        if getattr(self, "_help_option", None) is not None:
            return self._help_option

        def show_help(ctx, param, value):
            if value and not ctx.resilient_parsing:
                echo(ctx.get_help(), color=ctx.color)
                ctx.exit()

        self._help_option = Option(names, is_flag=True, is_eager=True, expose_value=False,
                                   callback=show_help, help="Show this message and exit.")
        return self._help_option


    def make_parser(self, ctx):
        parser = OptionParser(ctx)
        for param in self.get_params(ctx):
            param.add_to_parser(parser, ctx)
        return parser

    def get_help(self, ctx):
        f = ctx.make_formatter()
        self.format_help(ctx, f)
        return f.getvalue().rstrip("\n")

    def get_short_help_str(self, limit=45):
        if self.short_help:
            text = inspect.cleandoc(self.short_help)
        elif self.help:
            text = make_default_short_help(self.help, limit)
        else:
            text = ""
        if self.deprecated:
            text = f"(Deprecated) {text}"
        return text.strip()

    def format_help(self, ctx, formatter):
        self.format_usage(ctx, formatter)
        self.format_help_text(ctx, formatter)
        self.format_options(ctx, formatter)
        self.format_epilog(ctx, formatter)

    def format_help_text(self, ctx, formatter):
        text = inspect.cleandoc(self.help).partition("\f")[0] if self.help else ""
        if self.deprecated:
            text = f"(Deprecated) {text}"
        if text:
            formatter.write_paragraph()
            with formatter.indentation():
                formatter.write_text(text)

    def format_options(self, ctx, formatter):
        records = []
        for param in self.get_params(ctx):
            rec = param.get_help_record(ctx)
            if rec is not None:
                records.append(rec)
        if records:
            with formatter.section("Options"):
                formatter.write_dl(records)

    def format_epilog(self, ctx, formatter):
        if self.epilog:
            formatter.write_paragraph()
            with formatter.indentation():
                formatter.write_text(inspect.cleandoc(self.epilog))

    # -- parsing ------------------------------------------------------
    def make_context(self, info_name, args, parent=None, **extra):
        for key, value in self.context_settings.items():
            extra.setdefault(key, value)
        ctx = self.context_class(self, info_name=info_name, parent=parent, **extra)
        with ctx.scope(cleanup=False):
            self.parse_args(ctx, args)
        return ctx

    def parse_args(self, ctx, args):
        if not args and self.no_args_is_help and not ctx.resilient_parsing:
            echo(ctx.get_help(), color=ctx.color)
            ctx.exit()

        parser = self.make_parser(ctx)
        opts, args, order = parser.parse_args(args=list(args))

        for param in iter_params_for_processing(order, self.get_params(ctx)):
            value, args = param.handle_parse_result(ctx, opts, args)

        if args and not ctx.allow_extra_args and not ctx.resilient_parsing:
            noun = "argument" if len(args) == 1 else "arguments"
            ctx.fail(f"Got unexpected extra {noun} ({' '.join(map(make_str, args))})")

        ctx.args = args
        ctx._opt_prefixes.update(parser._opt_prefixes)
        return args

    # -- running ------------------------------------------------------
    def invoke(self, ctx):
        if self.deprecated:
            echo(style(f"DeprecationWarning: The command {self.name!r} is deprecated.", fg="red"),
                 err=True)
        if self.callback is not None:
            return ctx.invoke(self.callback, **ctx.params)

    def shell_complete(self, ctx, incomplete):
        from .shell_completion import CompletionItem

        results = []
        if incomplete and not incomplete[0].isalnum():
            for param in self.get_params(ctx):
                if not isinstance(param, Option) or param.hidden:
                    continue
                if (not param.multiple
                        and ctx.get_parameter_source(param.name) is ParameterSource.COMMANDLINE):
                    continue
                results.extend(
                    CompletionItem(name, help=param.help)
                    for name in [*param.opts, *param.secondary_opts]
                    if name.startswith(incomplete)
                )
        return results

    def main(self, args=None, prog_name=None, complete_var=None, standalone_mode=True,
             windows_expand_args=True, **extra):
        args = sys.argv[1:] if args is None else list(args)
        if prog_name is None:
            from .utils import _detect_program_name
            prog_name = _detect_program_name()


        self._main_shell_completion(extra, prog_name, complete_var)

        try:
            try:
                with self.make_context(prog_name, args, **extra) as ctx:
                    rv = self.invoke(ctx)
                    if not standalone_mode:
                        return rv
                    ctx.exit()
            except (EOFError, KeyboardInterrupt) as e:
                echo(file=sys.stderr)
                raise Abort() from e
            except ClickException as e:
                if not standalone_mode:
                    raise
                e.show()
                sys.exit(e.exit_code)
            except OSError as e:
                if e.errno == errno.EPIPE:
                    sys.stdout = _QuietFlush(sys.stdout)
                    sys.stderr = _QuietFlush(sys.stderr)
                    sys.exit(1)
                raise
        except Exit as e:
            if standalone_mode:
                sys.exit(e.exit_code)
            return e.exit_code
        except Abort:
            if not standalone_mode:
                raise
            echo("Aborted!", file=sys.stderr)
            sys.exit(1)

    def _main_shell_completion(self, ctx_args, prog_name, complete_var=None):
        if complete_var is None:
            complete_var = f"_{prog_name.replace('-', '_').replace('.', '_')}_COMPLETE".upper()
        instruction = os.environ.get(complete_var)
        if not instruction:
            return
        from .shell_completion import shell_complete

        sys.exit(shell_complete(self, ctx_args, prog_name, complete_var, instruction))

    def __call__(self, *args, **kwargs):
        return self.main(*args, **kwargs)


class _QuietFlush:
    """Stdout wrapper that swallows errors on flush (broken pipes)."""

    def __init__(self, wrapped):
        self.wrapped = wrapped

    def flush(self):
        try:
            self.wrapped.flush()
        except OSError as e:
            if e.errno != errno.EPIPE:
                raise

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


class Group(Command):
    allow_extra_args = True
    allow_interspersed_args = False
    command_class = None
    group_class = None

    def __init__(self, name=None, commands=None, invoke_without_command=False,
                 no_args_is_help=None, subcommand_metavar=None, chain=False,
                 result_callback=None, **attrs):
        super().__init__(name, **attrs)
        if commands is None:
            commands = {}
        elif isinstance(commands, (list, tuple)):
            commands = {c.name: c for c in commands}
        self.commands = commands
        if no_args_is_help is None:
            no_args_is_help = not invoke_without_command
        self.no_args_is_help = no_args_is_help
        self.invoke_without_command = invoke_without_command
        if subcommand_metavar is None:
            subcommand_metavar = ("COMMAND1 [ARGS]... [COMMAND2 [ARGS]...]..." if chain
                                  else "COMMAND [ARGS]...")
        self.subcommand_metavar = subcommand_metavar
        self.chain = chain
        self._result_callback = result_callback
        if self.chain:
            for param in self.params:
                if isinstance(param, Argument) and not param.required:
                    raise RuntimeError("Multi commands in chain mode cannot have optional arguments.")


    def to_info_dict(self, ctx):
        info = super().to_info_dict(ctx)
        commands = {}
        for name in self.list_commands(ctx):
            sub = self.get_command(ctx, name)
            if sub is not None:
                commands[name] = sub.to_info_dict(ctx)
        info.update(commands=commands, chain=self.chain)
        return info

    def add_command(self, cmd, name=None):

        name = name or cmd.name
        if name is None:
            raise TypeError("Command has no name.")
        self.commands[name] = cmd

    def command(self, *args, **kwargs):
        from .decorators import command as make_command
        if args and callable(args[0]):
            if len(args) != 1 or kwargs:
                raise TypeError("Use 'command(**kwargs)(callable)' to provide arguments.")
            (func,) = args
            args = ()
        else:
            func = None
        if self.command_class is not None and "cls" not in kwargs:
            kwargs["cls"] = self.command_class

        def decorator(f):
            cmd = make_command(*args, **kwargs)(f)
            self.add_command(cmd)
            return cmd

        return decorator(func) if func is not None else decorator

    def group(self, *args, **kwargs):
        from .decorators import group as make_group
        if args and callable(args[0]):
            if len(args) != 1 or kwargs:
                raise TypeError("Use 'group(**kwargs)(callable)' to provide arguments.")
            (func,) = args
            args = ()
        else:
            func = None
        if self.group_class is not None and "cls" not in kwargs:
            kwargs["cls"] = type(self) if self.group_class is type else self.group_class

        def decorator(f):
            cmd = make_group(*args, **kwargs)(f)
            self.add_command(cmd)
            return cmd

        return decorator(func) if func is not None else decorator

    def result_callback(self, replace=False):
        def decorator(f):
            previous = self._result_callback
            if previous is None or replace:
                self._result_callback = f
                return f

            def chained(value, **kw):
                return f(previous(value, **kw), **kw)

            self._result_callback = update_wrapper(chained, f)
            return chained

        return decorator

    def get_command(self, ctx, cmd_name):
        return self.commands.get(cmd_name)

    def list_commands(self, ctx):
        return sorted(self.commands)

    def collect_usage_pieces(self, ctx):
        rv = super().collect_usage_pieces(ctx)
        rv.append(self.subcommand_metavar)
        return rv

    def format_options(self, ctx, formatter):
        super().format_options(ctx, formatter)
        self.format_commands(ctx, formatter)

    def format_commands(self, ctx, formatter):
        entries = []
        for sub in self.list_commands(ctx):
            cmd = self.get_command(ctx, sub)
            if cmd is None or cmd.hidden:
                continue
            entries.append((sub, cmd))
        if not entries:
            return
        limit = formatter.width - 6 - max(len(n) for n, _ in entries)
        rows = [(n, c.get_short_help_str(limit)) for n, c in entries]
        with formatter.section("Commands"):
            formatter.write_dl(rows)

    def parse_args(self, ctx, args):
        rest = super().parse_args(ctx, args)
        if self.chain:
            ctx.protected_args = rest
            ctx.args = []
        elif rest:
            ctx.protected_args, ctx.args = rest[:1], rest[1:]
        return ctx.args

    def _finish(self, ctx, value):
        if self._result_callback is not None:
            return ctx.invoke(self._result_callback, value, **ctx.params)
        return value

    def invoke(self, ctx):
        if not ctx.protected_args:
            if self.invoke_without_command:
                with ctx:
                    rv = super().invoke(ctx)
                    return self._finish(ctx, [] if self.chain else rv)
            ctx.fail("Missing command.")

        args = [*ctx.protected_args, *ctx.args]
        ctx.args = []
        ctx.protected_args = []

        if not self.chain:
            with ctx:
                name, cmd, args = self.resolve_command(ctx, args)
                ctx.invoked_subcommand = name
                super().invoke(ctx)
                sub = cmd.make_context(name, args, parent=ctx)
                with sub:
                    return self._finish(ctx, sub.command.invoke(sub))

        with ctx:
            ctx.invoked_subcommand = "*" if args else None
            super().invoke(ctx)
            subs = []
            while args:
                name, cmd, args = self.resolve_command(ctx, args)
                sub = cmd.make_context(name, args, parent=ctx, allow_extra_args=True,
                                       allow_interspersed_args=False)
                subs.append(sub)
                args, sub.args = sub.args, []
            results = []
            for sub in subs:
                with sub:
                    results.append(sub.command.invoke(sub))
            return self._finish(ctx, results)

    def resolve_command(self, ctx, args):
        name = make_str(args[0])
        original = name
        cmd = self.get_command(ctx, name)
        if cmd is None and ctx.token_normalize_func is not None:
            name = ctx.token_normalize_func(name)
            cmd = self.get_command(ctx, name)
        if cmd is None and not ctx.resilient_parsing:
            if split_opt(name)[0]:
                self.parse_args(ctx, ctx.args)
            message = f"No such command {original!r}."
            from difflib import get_close_matches

            close = get_close_matches(original, self.list_commands(ctx))

            if close:
                if len(close) == 1:
                    message += f"\nDid you mean {close[0]!r}?"
                else:
                    message += "\nDid you mean one of:\n    " + "\n    ".join(close)
            ctx.fail(message)
        return name, cmd, args[1:]

    def shell_complete(self, ctx, incomplete):
        from .shell_completion import CompletionItem

        results = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden or not name.startswith(incomplete):
                continue
            results.append(CompletionItem(name, help=cmd.get_short_help_str()))
        results.extend(super().shell_complete(ctx, incomplete))
        return results



MultiCommand = Group
BaseCommand = Command


class CommandCollection(Group):
    def __init__(self, name=None, sources=None, **attrs):
        super().__init__(name, **attrs)
        self.sources = list(sources or [])

    def add_source(self, group):
        self.sources.append(group)

    def get_command(self, ctx, cmd_name):
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        for source in self.sources:
            cmd = source.get_command(ctx, cmd_name)
            if cmd is not None:
                return cmd
        return None

    def list_commands(self, ctx):
        names = set(super().list_commands(ctx))
        for source in self.sources:
            names.update(source.list_commands(ctx))
        return sorted(names)


def _check_iter(value):
    if isinstance(value, str):
        raise TypeError("Value must be an iterable, not a string.")
    return iter(value)


class Parameter:
    param_type_name = "parameter"

    def __init__(self, param_decls=None, type=None, required=False, default=_MISSING,
                 callback=None, nargs=None, multiple=False, metavar=None, expose_value=True,
                 is_eager=False, envvar=None, shell_complete=None):
        self.name, self.opts, self.secondary_opts = self._parse_decls(param_decls or (), expose_value)
        self.type = types.convert_type(type, None if default is _MISSING else default)
        if nargs is None:
            nargs = self.type.arity if self.type.is_composite else 1
        self.required = required
        self.callback = callback
        self.nargs = nargs
        self.multiple = multiple
        self.expose_value = expose_value
        self.default = None if default is _MISSING else default
        self._default_given = default is not _MISSING
        self.is_eager = is_eager
        self.metavar = metavar
        self.envvar = envvar
        self._custom_shell_complete = shell_complete

        if self.type.is_composite and nargs != self.type.arity:
            raise ValueError(
                f"'nargs' must be {self.type.arity} (or None) for type {self.type!r}, but it was {nargs}.")
        self._check_default(default, nargs, multiple)

    @staticmethod
    def _check_default(default, nargs, multiple):
        if default is _MISSING or default is None or callable(default):
            return
        probe = default
        if multiple:
            try:
                probe = next(_check_iter(probe), None)
            except TypeError:
                raise ValueError("'default' must be a list when 'multiple' is true.") from None
        if nargs != 1 and probe is not None:
            try:
                _check_iter(probe)
            except TypeError:
                raise ValueError(
                    "'default' must be a list when 'multiple' is true or 'nargs' != 1.") from None
            if nargs > 1 and len(probe) != nargs:
                subject = "item length" if multiple else "length"
                raise ValueError(f"'default' {subject} must match nargs={nargs}.")

    @property
    def human_readable_name(self):
        return self.name

    def to_info_dict(self):
        return {
            "name": self.name,
            "param_type_name": self.param_type_name,
            "opts": self.opts,
            "secondary_opts": self.secondary_opts,
            "type": self.type.to_info_dict(),
            "required": self.required,
            "nargs": self.nargs,
            "multiple": self.multiple,
            "default": self.default,
            "envvar": self.envvar,
        }


    def __repr__(self):
        return f"<{type(self).__name__} {self.name}>"

    def _parse_decls(self, decls, expose_value):
        raise NotImplementedError()

    def make_metavar(self):
        if self.metavar is not None:
            return self.metavar
        mv = self.type.get_metavar(self)
        if mv is None:
            mv = self.type.name.upper()
        if self.nargs != 1:
            mv += "..."
        return mv

    def get_default(self, ctx, call=True):
        value = ctx.lookup_default(self.name, call=False)
        if value is None:
            value = self.default
        if call and callable(value):
            value = value()
        return value

    def add_to_parser(self, parser, ctx):
        raise NotImplementedError()

    def consume_value(self, ctx, opts):
        value = opts.get(self.name)
        source = ParameterSource.COMMANDLINE
        if value is None:
            value = self.value_from_envvar(ctx)
            source = ParameterSource.ENVIRONMENT
        if value is None:
            value = ctx.lookup_default(self.name)
            source = ParameterSource.DEFAULT_MAP
        if value is None:
            value = self.get_default(ctx)
            source = ParameterSource.DEFAULT
        if value is None:
            source = None
        return value, source

    def type_cast_value(self, ctx, value):
        if value is None:
            return () if (self.multiple or self.nargs == -1) else None

        def convert_one(v):
            return self.type(v, param=self, ctx=ctx)

        def convert_nargs(v):
            if self.nargs == 1 or self.type.is_composite:
                return convert_one(v)
            v = tuple(_check_iter(v))
            if self.nargs > 1 and len(v) != self.nargs:
                were = "was" if len(v) == 1 else "were"
                raise BadParameter(
                    f"Takes {self.nargs} values but {len(v)} {were} given.", ctx=ctx, param=self)
            return tuple(convert_one(x) for x in v)

        if self.multiple:
            return tuple(convert_nargs(x) for x in _check_iter(value))
        if self.nargs == -1:
            return tuple(convert_one(x) for x in _check_iter(value))
        return convert_nargs(value)

    def value_is_missing(self, value):
        if value is None:
            return True
        if (self.nargs != 1 or self.multiple) and value == ():
            return True
        return False

    def process_value(self, ctx, value):
        value = self.type_cast_value(ctx, value)
        if self.required and self.value_is_missing(value):
            raise MissingParameter(ctx=ctx, param=self)
        if self.callback is not None:
            value = self.callback(ctx, self, value)
        return value

    def resolve_envvar_value(self, ctx):
        if not self.envvar:
            return None
        if isinstance(self.envvar, str):
            rv = os.environ.get(self.envvar)
            return rv or None
        for name in self.envvar:
            rv = os.environ.get(name)
            if rv:
                return rv
        return None

    def value_from_envvar(self, ctx):
        rv = self.resolve_envvar_value(ctx)
        if rv is None:
            return None
        depth = (self.nargs != 1) + bool(self.multiple)
        if depth > 0:
            rv = self.type.split_envvar_value(rv)
            if self.multiple and self.nargs != 1:
                rv = [tuple(rv[i:i + self.nargs]) for i in range(0, len(rv), self.nargs)]
        return rv

    def handle_parse_result(self, ctx, opts, args):
        with augment_usage_errors(ctx, param=self):
            value, source = self.consume_value(ctx, opts)
            ctx.set_parameter_source(self.name, source)
            try:
                value = self.process_value(ctx, value)
            except Exception:
                if not ctx.resilient_parsing:
                    raise
                value = None
        if self.expose_value:
            weak = source in (ParameterSource.DEFAULT, ParameterSource.DEFAULT_MAP, None)
            if self.name not in ctx.params or not weak or ctx.params[self.name] is None:
                ctx.params[self.name] = value
        return value, args

    def get_help_record(self, ctx):
        return None

    def get_usage_pieces(self, ctx):
        return []

    def get_error_hint(self, ctx):
        names = self.opts or [self.human_readable_name]
        return " / ".join(f"'{n}'" for n in names)

    def shell_complete(self, ctx, incomplete):
        if self._custom_shell_complete is not None:
            from .shell_completion import CompletionItem

            out = self._custom_shell_complete(ctx, self, incomplete)
            if out and isinstance(out[0], str):
                out = [CompletionItem(c) for c in out]
            return out
        return self.type.shell_complete(ctx, self, incomplete)



class Option(Parameter):
    param_type_name = "option"

    def __init__(self, param_decls=None, show_default=None, prompt=False,
                 confirmation_prompt=False, prompt_required=True, hide_input=False,
                 is_flag=None, flag_value=None, multiple=False, count=False,
                 allow_from_autoenv=True, type=None, help=None, hidden=False,
                 show_choices=True, show_envvar=False, **attrs):
        if help:
            help = inspect.cleandoc(help)
        default_given = "default" in attrs
        super().__init__(param_decls, type=type, multiple=multiple, **attrs)

        if prompt is True:
            if self.name is None:
                raise TypeError("'name' is required with 'prompt=True'.")
            prompt_text = self.name.replace("_", " ").capitalize()
        elif prompt is False:
            prompt_text = None
        else:
            prompt_text = prompt
        self.prompt = prompt_text
        self.confirmation_prompt = confirmation_prompt
        self.prompt_required = prompt_required
        self.hide_input = hide_input
        self.hidden = hidden

        if is_flag is None:
            is_flag = flag_value is not None or bool(self.secondary_opts)
        self.is_flag = is_flag
        self._flag_needs_value = (
            (self.prompt is not None and not prompt_required)
            or (is_flag is False and flag_value is not None)
        )
        self.is_bool_flag = False

        if self.nargs == -1:
            raise TypeError("nargs=-1 is not supported for options.")
        if is_flag and not default_given and not multiple and not self.required:
            self.default = False if (flag_value is None or isinstance(flag_value, bool)) else None
        if flag_value is None:
            flag_value = not self.default
        self.flag_value = flag_value
        if is_flag and type is None:
            self.type = types.convert_type(None, flag_value)

        self.count = count
        if count:
            if type is None:
                self.type = types.IntRange(min=0)
            if not default_given:
                self.default = 0

        self.allow_from_autoenv = allow_from_autoenv
        self.help = help
        self.show_default = show_default
        self.show_choices = show_choices
        self.show_envvar = show_envvar

        self.is_bool_flag = bool(is_flag and isinstance(self.type, types.BoolParamType))
        if self.is_bool_flag and set(self.opts) & set(self.secondary_opts):
            raise ValueError("cannot use the same flag for true/false")

        if self.prompt and self.is_flag and not self.is_bool_flag:
            raise TypeError("'prompt' is not valid for non-boolean flag.")
        if not self.is_bool_flag and self.secondary_opts:
            raise TypeError("Secondary flag is not valid for non-boolean flag.")
        if self.is_bool_flag and self.hide_input and self.prompt is not None:
            raise TypeError("'prompt' with 'hide_input' is not valid for boolean flag.")
        if self.count:
            if self.multiple:
                raise TypeError("'count' is not valid with 'multiple'.")
            if self.is_flag:
                raise TypeError("'count' is not valid with 'is_flag'.")


    def _parse_decls(self, decls, expose_value):
        opts, secondary = [], []
        name = None
        possible = []
        for decl in decls:
            if decl.isidentifier():
                if name is not None:
                    raise TypeError(f"Name '{name}' defined twice")
                name = decl
                continue
            split = ";" if decl[:1] == "/" else "/"
            if split in decl:
                first, second = decl.split(split, 1)
                first = first.rstrip()
                if first:
                    possible.append(split_opt(first))
                    opts.append(first)
                second = second.lstrip()
                if second:
                    secondary.append(second)
            else:
                possible.append(split_opt(decl))
                opts.append(decl)
        if name is None and possible:
            possible.sort(key=lambda x: -len(x[0]))
            name = possible[0][1].replace("-", "_").lower()
            if not name.isidentifier():
                name = None
        if name is None:
            if not expose_value:
                return None, opts, secondary
            raise TypeError("Could not determine name for option")
        if not opts and not secondary:
            raise TypeError(
                f"No options defined but a name was passed ({name}). Did you mean to declare an "
                f"argument instead? Did you mean to pass '--{name}'?")
        return name, opts, secondary

    def add_to_parser(self, parser, ctx):
        if self.multiple:
            action = "append"
        elif self.count:
            action = "count"
        else:
            action = "store"
        if self.is_flag:
            action = "append_const" if self.multiple else "store_const"
            if self.is_bool_flag and self.secondary_opts:
                parser.add_option(self, self.opts, self.name, action, const=True)
                parser.add_option(self, self.secondary_opts, self.name, action, const=False)
                return
            parser.add_option(self, self.opts, self.name, action, const=self.flag_value)
            return

        parser.add_option(self, self.opts, self.name, action, nargs=self.nargs)

    def get_help_record(self, ctx):
        if self.hidden:
            return None

        slash = False

        def render(names):
            nonlocal slash
            text, has_slash = join_options(names)
            slash = slash or has_slash
            if not self.is_flag and not self.count:
                text += f" {self.make_metavar()}"
            return text

        parts = [render(self.opts)]
        if self.secondary_opts:
            parts.append(render(self.secondary_opts))
        names = ("; " if slash else " / ").join(parts)

        extra = []
        if self.show_envvar:
            env = self.envvar or self._auto_envvar(ctx)
            if env is not None:
                env = ", ".join(env) if isinstance(env, (list, tuple)) else env
                extra.append(f"env var: {env}")

        show = self.show_default if self.show_default is not None else ctx.show_default
        default = self.get_default(ctx, call=False)
        shown_str = isinstance(self.show_default, str)
        if shown_str or (default is not None and show):
            if shown_str:
                text = f"({self.show_default})"
            elif isinstance(default, (list, tuple)):
                text = ", ".join(str(d) for d in default)
            elif inspect.isfunction(default):
                text = "(dynamic)"
            elif default == "":
                text = '""'
            elif self.is_bool_flag and self.secondary_opts:
                text = split_opt((self.opts if default else self.secondary_opts)[0])[1]
            elif self.is_bool_flag and not default:
                text = ""
            else:
                text = str(default)
            if text:
                extra.append(f"default: {text}")

        if hasattr(self.type, "_describe_range") and not self.count:
            rng = self.type._describe_range()
            if rng:
                extra.append(rng)
        if self.required:
            extra.append("required")

        help_text = self.help or ""
        if extra:
            tail = "; ".join(extra)
            help_text = f"{help_text}  [{tail}]" if help_text else f"[{tail}]"
        return names, help_text

    def to_info_dict(self):
        info = super().to_info_dict()
        info.update(help=self.help, prompt=self.prompt, is_flag=self.is_flag,
                    flag_value=self.flag_value, count=self.count, hidden=self.hidden)
        return info

    def _auto_envvar(self, ctx):
        if self.allow_from_autoenv and ctx.auto_envvar_prefix is not None and self.name is not None:
            return f"{ctx.auto_envvar_prefix}_{self.name.upper()}"
        return None

    def get_default(self, ctx, call=True):
        if self.is_flag and not self.is_bool_flag:
            for p in ctx.command.params:
                if p.name == self.name and getattr(p, "default", None) is True:
                    return p.flag_value
        return super().get_default(ctx, call=call)

    def prompt_for_value(self, ctx):
        default = self.get_default(ctx)
        if self.is_bool_flag:
            from .termui import confirm
            return confirm(self.prompt, default)
        from .termui import prompt
        return prompt(self.prompt, default=default, type=self.type, hide_input=self.hide_input,
                      show_choices=self.show_choices, confirmation_prompt=self.confirmation_prompt,
                      value_proc=lambda x: self.process_value(ctx, x))

    def resolve_envvar_value(self, ctx):
        rv = super().resolve_envvar_value(ctx)
        if rv is None:
            auto = self._auto_envvar(ctx)
            if auto is not None:
                rv = os.environ.get(auto) or None
        if rv is not None and self.is_flag and not self.is_bool_flag:
            truth = types.BOOL(rv, param=self, ctx=ctx)
            return str(self.flag_value) if truth else None
        return rv

    def consume_value(self, ctx, opts):
        value, source = super().consume_value(ctx, opts)
        if value is _flag_needs_value:
            if self.prompt is not None and not ctx.resilient_parsing:
                value = self.prompt_for_value(ctx)
                source = ParameterSource.PROMPT
            else:
                value = self.flag_value
                source = ParameterSource.COMMANDLINE
        elif self.multiple and value is not None and any(v is _flag_needs_value for v in value):
            value = [self.flag_value if v is _flag_needs_value else v for v in value]
            source = ParameterSource.COMMANDLINE
        elif (source in (None, ParameterSource.DEFAULT) and self.prompt is not None
              and (self.required or self.prompt_required) and not ctx.resilient_parsing):
            value = self.prompt_for_value(ctx)
            source = ParameterSource.PROMPT
        return value, source



class Argument(Parameter):
    param_type_name = "argument"

    def __init__(self, param_decls, required=None, **attrs):
        if required is None:
            if attrs.get("default") is not None:
                required = False
            else:
                required = attrs.get("nargs", 1) > 0
        if "multiple" in attrs:
            raise TypeError("__init__() got an unexpected keyword argument 'multiple'.")
        super().__init__(param_decls, required=required, **attrs)
        if self.default is not None and self.nargs < 0:
            raise TypeError("'default' is not supported for nargs=-1.")

    @property
    def human_readable_name(self):
        if self.metavar is not None:
            return self.metavar
        return self.name.upper()

    def make_metavar(self):
        if self.metavar is not None:
            return self.metavar
        var = self.type.get_metavar(self)
        if not var:
            var = self.name.upper()
        if not self.required:
            var = f"[{var}]"
        if self.nargs != 1:
            var += "..."
        return var

    def _parse_decls(self, decls, expose_value):
        if not decls:
            if not expose_value:
                return None, [], []
            raise TypeError("Could not determine name for argument")
        if len(decls) == 1:
            name = arg = decls[0]
            name = name.replace("-", "_").lower()
        else:
            raise TypeError(f"Arguments take exactly one parameter declaration, got {len(decls)}.")
        return name, [arg], []

    def get_usage_pieces(self, ctx):
        return [self.make_metavar()]

    def get_error_hint(self, ctx):
        return f"'{self.make_metavar()}'"

    def add_to_parser(self, parser, ctx):
        parser.add_argument(self, dest=self.name, nargs=self.nargs)
