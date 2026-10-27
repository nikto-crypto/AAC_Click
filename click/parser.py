"""Command line tokenizer: turns an argv list into option values and positionals."""
from collections import deque

from .exceptions import BadArgumentUsage, BadOptionUsage, NoSuchOption, UsageError


# Sentinel for options that were given without their optional value.
_flag_needs_value = object()


def split_opt(opt):
    first = opt[:1]
    if first.isalnum():
        return "", opt
    if opt[1:2] == first:
        return opt[:2], opt[2:]
    return first, opt[1:]


def normalize_opt(opt, ctx):
    if ctx is None or ctx.token_normalize_func is None:
        return opt
    prefix, name = split_opt(opt)
    return prefix + ctx.token_normalize_func(name)


def distribute_args(args, specs):
    """Hand out ``args`` to positionals described by ``specs`` (nargs values).

    A negative spec is greedy but leaves enough for fixed-size specs after it.
    Returns (values, leftovers).
    """
    pool = list(args)
    values = []
    for i, n in enumerate(specs):
        if n < 0:
            reserved = sum(s for s in specs[i + 1:] if s > 0)
            take = max(len(pool) - reserved, 0)
            values.append(tuple(pool[:take]))
            del pool[:take]
        elif n == 1:
            values.append(pool.pop(0) if pool else None)
        else:
            chunk = pool[:n]
            del pool[:n]
            chunk.extend([None] * (n - len(chunk)))
            values.append(tuple(chunk))
    return tuple(values), pool


class _Option:
    def __init__(self, obj, opts, dest, action="store", nargs=1, const=None, ctx=None):
        self.obj = obj
        self.dest = dest
        self.action = action
        self.nargs = nargs
        self.const = const
        self.short = []
        self.long = []
        self.prefixes = set()
        for raw in opts:
            prefix, name = split_opt(raw)
            if not prefix:
                raise ValueError(f"Invalid start character for option ({raw})")
            self.prefixes.add(prefix[0])
            norm = normalize_opt(raw, ctx)
            if len(prefix) == 1 and len(name) == 1:
                self.short.append(norm)
            else:
                self.long.append(norm)

    @property
    def takes_value(self):
        return self.action in ("store", "append")

    def apply(self, value, state):
        if self.action == "store":
            state.opts[self.dest] = value
        elif self.action == "store_const":
            state.opts[self.dest] = self.const
        elif self.action == "append":
            state.opts.setdefault(self.dest, []).append(value)
        elif self.action == "append_const":
            state.opts.setdefault(self.dest, []).append(self.const)
        elif self.action == "count":
            state.opts[self.dest] = state.opts.get(self.dest, 0) + 1
        else:
            raise ValueError(f"unknown action {self.action!r}")
        state.order.append(self.obj)


class _Positional:
    def __init__(self, obj, dest, nargs=1):
        self.obj = obj
        self.dest = dest
        self.nargs = nargs

    def apply(self, value, state):
        if self.nargs > 1:
            holes = sum(1 for v in value if v is None)
            if holes == len(value):
                value = None
            elif holes:
                raise BadArgumentUsage(f"Argument {self.dest!r} takes {self.nargs} values.")
        if self.nargs == -1 and getattr(self.obj, "envvar", None) is not None and value == ():
            value = None
        state.opts[self.dest] = value

        state.order.append(self.obj)


class _State:
    def __init__(self, args):
        self.rargs = deque(args)
        self.largs = []
        self.opts = {}
        self.order = []


def split_arg_string(string):
    """Split a command line string into words, tolerating unbalanced quotes."""
    import shlex

    lex = shlex.shlex(string, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    words = []
    try:
        for word in lex:
            words.append(word)
    except ValueError:
        # unterminated quote or trailing escape: keep what the lexer has so far
        remainder = lex.token
        if remainder:
            words.append(remainder)
    return words


class OptionParser:
    def __init__(self, ctx=None):
        self.ctx = ctx
        self.allow_interspersed_args = True
        self.ignore_unknown_options = False
        if ctx is not None:
            self.allow_interspersed_args = ctx.allow_interspersed_args
            self.ignore_unknown_options = ctx.ignore_unknown_options
        self._short = {}
        self._long = {}
        self._opt_prefixes = {"-", "--"}

        self._positionals = []

    def add_option(self, obj, opts, dest, action="store", nargs=1, const=None):
        opts = [normalize_opt(o, self.ctx) for o in opts]
        opt = _Option(obj, opts, dest, action, nargs, const, self.ctx)
        self._opt_prefixes.update(opt.prefixes)
        for s in opt.short:
            self._short[s] = opt
        for l in opt.long:
            self._long[l] = opt

    def add_argument(self, obj, dest, nargs=1):
        self._positionals.append(_Positional(obj, dest, nargs))

    def parse_args(self, args):
        state = _State(args)
        try:
            self._scan(state)
            self._assign_positionals(state)
        except UsageError as e:
            if self.ctx is not None and self.ctx.resilient_parsing:
                return state.opts, state.largs, state.order
            if e.ctx is None:
                e.ctx = self.ctx
            raise

        return state.opts, state.largs, state.order

    def _scan(self, state):
        while state.rargs:
            arg = state.rargs.popleft()
            if arg == "--":
                return
            if arg[:1] in self._opt_prefixes and len(arg) > 1:
                self._handle_dashed(arg, state)
            elif self.allow_interspersed_args:
                state.largs.append(arg)
            else:
                state.rargs.appendleft(arg)
                return

    def _assign_positionals(self, state):
        specs = [p.nargs for p in self._positionals]
        values, leftover = distribute_args(list(state.largs) + list(state.rargs), specs)
        for pos, val in zip(self._positionals, values):
            pos.apply(val, state)
        state.largs = leftover
        state.rargs.clear()

    def _handle_dashed(self, arg, state):
        if "=" in arg:
            name, explicit = arg.split("=", 1)
        else:
            name, explicit = arg, None
        name = normalize_opt(name, self.ctx)
        try:
            self._long_option(name, explicit, state)
        except NoSuchOption:
            if arg[:2] not in self._opt_prefixes:
                self._short_cluster(arg, state)
            elif not self.ignore_unknown_options:
                raise
            else:
                state.largs.append(arg)

    def _long_option(self, name, explicit, state):
        opt = self._long.get(name)
        if opt is None:
            from difflib import get_close_matches

            raise NoSuchOption(name, possibilities=get_close_matches(name, self._long), ctx=self.ctx)

        if opt.takes_value:
            if explicit is not None:
                state.rargs.appendleft(explicit)
            value = self._take_value(name, opt, state)
        elif explicit is not None:
            raise BadOptionUsage(name, f"Option {name!r} does not take a value.", ctx=self.ctx)
        else:
            value = None
        opt.apply(value, state)

    def _short_cluster(self, arg, state):
        prefix = arg[0]
        unknown = []
        for i, ch in enumerate(arg[1:], start=2):
            name = normalize_opt(prefix + ch, self.ctx)
            opt = self._short.get(name)
            if opt is None:
                if self.ignore_unknown_options:
                    unknown.append(ch)
                    continue
                raise NoSuchOption(name, ctx=self.ctx)
            stop = False
            if opt.takes_value:
                if i < len(arg):
                    state.rargs.appendleft(arg[i:])
                    stop = True
                value = self._take_value(name, opt, state)
            else:
                value = None
            opt.apply(value, state)
            if stop:
                break
        if self.ignore_unknown_options and unknown:
            state.largs.append(f"{prefix}{''.join(unknown)}")

    def _take_value(self, name, opt, state):
        n = opt.nargs
        optional = getattr(opt.obj, "_flag_needs_value", False)
        if len(state.rargs) < n:
            if optional:
                return _flag_needs_value
            raise BadOptionUsage(
                name,
                f"Option {name!r} requires an argument." if n == 1
                else f"Option {name!r} requires {n} arguments.",
                ctx=self.ctx,
            )
        if n == 1:
            nxt = state.rargs[0]
            if optional and isinstance(nxt, str) and nxt[:1] in self._opt_prefixes and len(nxt) > 1:
                return _flag_needs_value
            return state.rargs.popleft()
        return tuple(state.rargs.popleft() for _ in range(n))
