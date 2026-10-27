"""Decorators that turn plain functions into commands, groups and parameters."""
import inspect
import sys
from functools import update_wrapper

from .core import Argument, Command, Group, Option
from .globals import get_current_context
from .utils import echo


def pass_context(f):
    def wrapper(*args, **kwargs):
        return f(get_current_context(), *args, **kwargs)
    return update_wrapper(wrapper, f)


def pass_obj(f):
    def wrapper(*args, **kwargs):
        return f(get_current_context().obj, *args, **kwargs)
    return update_wrapper(wrapper, f)


def make_pass_decorator(object_type, ensure=False):
    def decorator(f):
        def wrapper(*args, **kwargs):
            ctx = get_current_context()
            obj = ctx.ensure_object(object_type) if ensure else ctx.find_object(object_type)
            if obj is None:
                raise RuntimeError(
                    "Managed to invoke callback without a context object of type "
                    f"{object_type.__name__!r} existing."
                )
            return f(obj, *args, **kwargs)
        return update_wrapper(wrapper, f)
    return decorator


def pass_meta_key(key, *, doc_description=None):
    def decorator(f):
        def wrapper(*args, **kwargs):
            return f(get_current_context().meta[key], *args, **kwargs)
        return update_wrapper(wrapper, f)

    if doc_description is None:
        doc_description = f"the {key!r} key from :attr:`click.Context.meta`"
    decorator.__doc__ = (f"Decorator that passes {doc_description} as the first argument "
                         "to the decorated function.")
    return decorator



def _remember_param(f, param):
    if isinstance(f, Command):
        f.params.append(param)
    else:
        if not hasattr(f, "__click_params__"):
            f.__click_params__ = []
        f.__click_params__.append(param)


def _default_name(func_name):
    return func_name.lower().replace("_", "-")


def command(name=None, cls=None, **attrs):
    if callable(name) and not isinstance(name, str):
        func, name = name, None
        return command(name, cls, **attrs)(func)
    if cls is None:
        cls = Command

    def decorator(f):
        if isinstance(f, Command):
            raise TypeError("Attempted to convert a callback into a command twice.")
        opts = dict(attrs)
        params = list(opts.pop("params", None) or [])
        collected = getattr(f, "__click_params__", None)
        if collected is not None:
            del f.__click_params__
            params.extend(reversed(collected))
        if opts.get("help") is None:
            opts["help"] = f.__doc__

        cmd = cls(name=name or _default_name(f.__name__), callback=f, params=params, **opts)
        cmd.__doc__ = f.__doc__
        return cmd

    return decorator


def group(name=None, cls=None, **attrs):
    if cls is None:
        cls = Group
    if callable(name) and not isinstance(name, str):
        return command(None, cls, **attrs)(name)
    return command(name, cls, **attrs)


def argument(*param_decls, cls=None, **attrs):
    if cls is None:
        cls = Argument

    def decorator(f):
        _remember_param(f, cls(param_decls, **attrs))
        return f
    return decorator


def option(*param_decls, cls=None, **attrs):
    if cls is None:
        cls = Option

    def decorator(f):
        _remember_param(f, cls(param_decls, **attrs))
        return f
    return decorator


def confirmation_option(*param_decls, **kwargs):
    def callback(ctx, param, value):
        if not value:
            ctx.abort()

    if not param_decls:
        param_decls = ("--yes",)
    kwargs.setdefault("is_flag", True)
    kwargs.setdefault("callback", callback)
    kwargs.setdefault("expose_value", False)
    kwargs.setdefault("prompt", "Do you want to continue?")
    kwargs.setdefault("help", "Confirm the action without prompting.")
    return option(*param_decls, **kwargs)


def password_option(*param_decls, **kwargs):
    if not param_decls:
        param_decls = ("--password",)
    kwargs.setdefault("prompt", True)
    kwargs.setdefault("confirmation_prompt", True)
    kwargs.setdefault("hide_input", True)
    return option(*param_decls, **kwargs)


def version_option(version=None, *param_decls, package_name=None, prog_name=None,
                   message=None, **kwargs):
    if message is None:
        message = "%(prog)s, version %(version)s"

    if version is None and package_name is None:
        frame = inspect.currentframe()
        caller = frame.f_back if frame is not None else None
        mod = caller.f_globals.get("__name__") if caller is not None else None
        if mod == "__main__":
            mod = caller.f_globals.get("__package__") or None
        if mod:
            package_name = mod.partition(".")[0]

    def callback(ctx, param, value):
        if not value or ctx.resilient_parsing:
            return
        shown = version
        name = prog_name
        if name is None:
            name = ctx.find_root().info_name
        if shown is None:
            try:
                from importlib import metadata
            except ImportError:  # pragma: no cover
                import importlib_metadata as metadata
            try:
                shown = metadata.version(package_name)
            except metadata.PackageNotFoundError:
                raise RuntimeError(
                    f"{package_name!r} is not installed. Try passing 'package_name' instead."
                ) from None
            if shown is None:
                raise RuntimeError(
                    f"Could not determine the version for {package_name!r} automatically.")
        echo(message % {"prog": name, "package": package_name, "version": shown}, color=ctx.color)
        ctx.exit()

    if not param_decls:
        param_decls = ("--version",)
    kwargs.setdefault("is_flag", True)
    kwargs.setdefault("expose_value", False)
    kwargs.setdefault("is_eager", True)
    kwargs.setdefault("help", "Show the version and exit.")
    kwargs["callback"] = callback
    return option(*param_decls, **kwargs)


def help_option(*param_decls, **kwargs):
    def callback(ctx, param, value):
        if not value or ctx.resilient_parsing:
            return
        echo(ctx.get_help(), color=ctx.color)
        ctx.exit()

    if not param_decls:
        param_decls = ("--help",)
    kwargs.setdefault("is_flag", True)
    kwargs.setdefault("expose_value", False)
    kwargs.setdefault("is_eager", True)
    kwargs.setdefault("help", "Show this message and exit.")
    kwargs.setdefault("callback", callback)
    return option(*param_decls, **kwargs)
