"""Tab completion for bash, zsh and fish."""
import os
import re

from .core import Argument, Command, Group, Option, ParameterSource
from .parser import split_arg_string
from .utils import echo


def shell_complete(cli, ctx_args, prog_name, complete_var, instruction):
    """Handle a completion request described by ``instruction`` (e.g. ``bash_source``).

    Returns the process exit status.
    """
    shell, _, action = instruction.partition("_")
    comp_cls = get_completion_class(shell)
    if comp_cls is None:
        return 1
    comp = comp_cls(cli, ctx_args, prog_name, complete_var)
    if action == "source":
        echo(comp.source())
        return 0
    if action == "complete":
        echo(comp.complete())
        return 0
    return 1


class CompletionItem:
    """One candidate: its text, how the shell should treat it, and optional help."""

    __slots__ = ("value", "type", "help", "_info")

    def __init__(self, value, type="plain", help=None, **kwargs):
        self.value = value
        self.type = type
        self.help = help
        self._info = kwargs

    def __getattr__(self, name):
        return self._info.get(name)


_BASH_TEMPLATE = """\
%(complete_func)s() {
    local IFS=$'\\n'
    local response
    response=$(env COMP_WORDS="${COMP_WORDS[*]}" COMP_CWORD=$COMP_CWORD %(complete_var)s=bash_complete $1)
    for completion in $response; do
        IFS=',' read type value <<< "$completion"
        if [[ $type == 'dir' ]]; then
            COMPREPLY=()
            compopt -o dirnames
        elif [[ $type == 'file' ]]; then
            COMPREPLY=()
            compopt -o default
        elif [[ $type == 'plain' ]]; then
            COMPREPLY+=($value)
        fi
    done
    return 0
}

complete -o nosort -F %(complete_func)s %(prog_name)s
"""

_ZSH_TEMPLATE = """\
#compdef %(prog_name)s

%(complete_func)s() {
    local -a response
    (( ! $+commands[%(prog_name)s] )) && return 1
    response=("${(@f)$(env COMP_WORDS="${words[*]}" COMP_CWORD=$((CURRENT-1)) %(complete_var)s=zsh_complete %(prog_name)s)}")
    local -a plain described
    local type key descr
    for type key descr in ${response}; do
        if [[ "$type" == "plain" ]]; then
            if [[ "$descr" == "_" ]]; then
                plain+=("$key")
            else
                described+=("${key//:/\\\\:}":"$descr")
            fi
        elif [[ "$type" == "dir" ]]; then
            _path_files -/
        elif [[ "$type" == "file" ]]; then
            _path_files -f
        fi
    done
    [[ -n "$described" ]] && _describe -V unsorted described -U
    [[ -n "$plain" ]] && compadd -U -V unsorted -a plain
}

if [[ $zsh_eval_context[-1] == loadautofunc ]]; then
    %(complete_func)s "$@"
else
    compdef %(complete_func)s %(prog_name)s
fi
"""

_FISH_TEMPLATE = """\
function %(complete_func)s
    set -l response (env %(complete_var)s=fish_complete COMP_WORDS=(commandline -cp) COMP_CWORD=(commandline -t) %(prog_name)s)
    for completion in $response
        set -l metadata (string split "," -- $completion)
        if test $metadata[1] = "dir"
            __fish_complete_directories $metadata[2]
        else if test $metadata[1] = "file"
            __fish_complete_path $metadata[2]
        else if test $metadata[1] = "plain"
            echo $metadata[2]
        end
    end
end

complete --no-files --command %(prog_name)s --arguments "(%(complete_func)s)"
"""


class ShellComplete:
    """Base class for a shell integration; subclasses set ``name`` and ``source_template``."""

    name = None
    source_template = None

    def __init__(self, cli, ctx_args, prog_name, complete_var):
        self.cli = cli
        self.ctx_args = ctx_args
        self.prog_name = prog_name
        self.complete_var = complete_var

    @property
    def func_name(self):
        safe = re.sub(r"\W*", "", self.prog_name.replace("-", "_"), flags=re.ASCII)
        return f"_{safe}_completion"

    def source_vars(self):
        return {
            "complete_func": self.func_name,
            "complete_var": self.complete_var,
            "prog_name": self.prog_name,
        }

    def source(self):
        return self.source_template % self.source_vars()

    def get_completion_args(self):
        raise NotImplementedError

    def get_completions(self, args, incomplete):
        ctx = _resolve_context(self.cli, self.ctx_args, self.prog_name, args)
        obj, incomplete = _resolve_incomplete(ctx, args, incomplete)
        items = obj.shell_complete(ctx, incomplete)
        node = ctx
        while node is not None and not hasattr(node, "_chain_used"):
            node = node.parent
        if node is not None and obj is ctx.command and not _start_of_option(ctx, incomplete):
            # inside a chain, the siblings that haven't run yet are valid next words
            used = node._chain_used
            parent = node.parent

            for name in parent.command.list_commands(parent):
                sibling = parent.command.get_command(parent, name)
                if (name.startswith(incomplete) and name not in used and sibling is not None
                        and not sibling.hidden):
                    items.append(CompletionItem(name, help=sibling.get_short_help_str()))
        return items

    def format_completion(self, item):
        raise NotImplementedError

    def complete(self):
        args, incomplete = self.get_completion_args()
        return "\n".join(self.format_completion(i) for i in self.get_completions(args, incomplete))


class BashComplete(ShellComplete):
    name = "bash"
    source_template = _BASH_TEMPLATE

    @staticmethod
    def _check_version():
        import subprocess

        bash = _which("bash")
        if bash is None:
            echo("Couldn't detect Bash version, shell completion is not supported.", err=True)
            return
        out = subprocess.run([bash, "--norc", "-c", 'echo "${BASH_VERSION}"'],
                             stdout=subprocess.PIPE).stdout.decode()
        match = re.search(r"^(\d+)\.(\d+)\.\d+", out)
        if match is None:
            return
        major, minor = int(match.group(1)), int(match.group(2))
        if major < 4 or (major == 4 and minor < 4):
            echo("Shell completion is not supported for Bash versions older than 4.4.",
                 err=True)

    def source(self):
        self._check_version()
        return super().source()

    def get_completion_args(self):
        words = split_arg_string(os.environ["COMP_WORDS"])
        cword = int(os.environ["COMP_CWORD"])
        args = words[1:cword]
        try:
            incomplete = words[cword]
        except IndexError:
            incomplete = ""
        return args, incomplete

    def format_completion(self, item):
        return f"{item.type},{item.value}"


class ZshComplete(ShellComplete):
    name = "zsh"
    source_template = _ZSH_TEMPLATE

    get_completion_args = BashComplete.get_completion_args

    def format_completion(self, item):
        return f"{item.type}\n{item.value}\n{item.help if item.help else '_'}"


class FishComplete(ShellComplete):
    name = "fish"
    source_template = _FISH_TEMPLATE

    def get_completion_args(self):
        words = split_arg_string(os.environ["COMP_WORDS"])
        incomplete = os.environ["COMP_CWORD"]
        args = words[1:]
        # fish repeats the partial word in COMP_WORDS; it is not a finished argument
        if incomplete and args and args[-1] == incomplete:
            args.pop()
        return args, incomplete

    def format_completion(self, item):
        if item.help:
            return f"{item.type},{item.value}\t{item.help}"
        return f"{item.type},{item.value}"


_available_shells = {}


def add_completion_class(cls, name=None):
    if name is None:
        name = cls.name
    _available_shells[name] = cls
    return cls


def get_completion_class(shell):
    return _available_shells.get(shell)


for _cls in (BashComplete, ZshComplete, FishComplete):
    add_completion_class(_cls)


def _which(program):
    import shutil

    return shutil.which(program)


def _is_incomplete_argument(ctx, param):
    if not isinstance(param, Argument):
        return False
    value = ctx.params[param.name]
    return (
        param.nargs == -1
        or ctx.get_parameter_source(param.name) is not ParameterSource.COMMANDLINE
        or (param.nargs > 1 and isinstance(value, (tuple, list)) and len(value) < param.nargs)
    )


def _start_of_option(ctx, value):
    return bool(value) and value[0] in ctx._opt_prefixes


def _is_incomplete_option(ctx, args, param):
    """True if the trailing words are this option's name plus an unfinished value."""
    if not isinstance(param, Option) or param.is_flag or param.count:
        return False
    last = None
    for index, word in enumerate(reversed(args)):
        if index + 1 > param.nargs:
            break
        if _start_of_option(ctx, word):
            last = word
    return last is not None and last in param.opts


def _resolve_context(cli, ctx_args, prog_name, args):
    """Build the context chain for the words typed so far, tolerating bad input."""
    ctx_args["resilient_parsing"] = True
    ctx = cli.make_context(prog_name, list(args), **ctx_args)
    args = ctx.protected_args + ctx.args

    while args:
        command = ctx.command
        if not isinstance(command, Group):
            break
        if not command.chain:
            name, cmd, args = command.resolve_command(ctx, args)
            if cmd is None:
                return ctx
            ctx = cmd.make_context(name, args, parent=ctx, resilient_parsing=True)
            args = ctx.protected_args + ctx.args
        else:
            sub_ctx = ctx
            used = []
            while args:
                name, cmd, args = command.resolve_command(ctx, args)
                if cmd is None:
                    return ctx
                sub_ctx = cmd.make_context(name, args, parent=ctx, allow_extra_args=True,
                                           allow_interspersed_args=False, resilient_parsing=True)
                used.append(name)
                args = sub_ctx.args
            sub_ctx._chain_used = used
            ctx = sub_ctx

            args = [*sub_ctx.protected_args, *sub_ctx.args]
    return ctx


def _resolve_incomplete(ctx, args, incomplete):
    """Decide which object (command or parameter) should supply completions."""
    # shells disagree on whether "--opt=val" arrives joined or split
    if "=" in incomplete:
        name, _, incomplete = incomplete.partition("=")
        args.append(name)

    # after "--" everything is a value, even if it looks like an option
    if "--" not in args and _start_of_option(ctx, incomplete):
        return ctx.command, incomplete

    params = ctx.command.get_params(ctx)
    for param in params:
        if _is_incomplete_option(ctx, args, param):
            return param, incomplete
    for param in params:
        if _is_incomplete_argument(ctx, param):
            return param, incomplete
    return ctx.command, incomplete
