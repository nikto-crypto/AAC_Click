"""Interactive terminal features: prompts, progress bars, pagers, editors."""
import sys

from . import types
from .exceptions import Abort, UsageError
from .globals import resolve_color_default
from ._compat import isatty as isatty
from .utils import echo, secho, style, unstyle  # noqa: F401  (re-exported)

# Replaceable so tests can feed canned input.
visible_prompt_func = input


def hidden_prompt_func(prompt=""):
    import getpass

    return getpass.getpass(prompt)


_TRUE_ANSWERS = ("y", "yes")
_FALSE_ANSWERS = ("n", "no")


def _read(hidden, text):
    func = hidden_prompt_func if hidden else visible_prompt_func
    try:
        echo(text.rstrip(" "), nl=False, err=_read.err)
        return func(" ")
    except (KeyboardInterrupt, EOFError):
        if hidden:
            echo(None, err=_read.err)
        raise Abort() from None


_read.err = False


def _show_default(default):
    name = getattr(default, "name", None)
    if name is not None and hasattr(default, "read"):
        return name
    return default


def _build_prompt(text, suffix, show_default=False, default=None, show_choices=True, type=None):
    prompt = text
    if type is not None and show_choices and isinstance(type, types.Choice):
        prompt += " (" + ", ".join(map(str, type.choices)) + ")"
    if default is not None and show_default:
        prompt = f"{prompt} [{_show_default(default)}]"
    return prompt + suffix


def prompt(text, default=None, hide_input=False, confirmation_prompt=False, type=None,
           value_proc=None, prompt_suffix=": ", show_default=True, err=False, show_choices=True):
    if value_proc is None:
        value_proc = types.convert_type(type, default)
    _read.err = err
    shown = "(hidden)" if (hide_input and default is not None) else default
    full = _build_prompt(text, prompt_suffix, show_default, shown, show_choices, type)

    if confirmation_prompt is True:
        confirmation_prompt = "Repeat for confirmation"
    while True:
        while True:
            value = _read(hide_input, full)
            if value:
                break
            if default is not None:
                value = default
                break
        try:
            result = value_proc(value)
        except UsageError as e:
            if hide_input:
                echo("Error: The value you entered was invalid.", err=err)
            else:
                echo(f"Error: {e.message}", err=err)
            continue
        if not confirmation_prompt:
            return result
        while True:
            again = _read(hide_input, confirmation_prompt + prompt_suffix)
            if again or (not value and not again):
                break

        if value == again:
            return result
        echo("Error: The two entered values do not match.", err=err)


def confirm(text, default=False, abort=False, prompt_suffix=": ", show_default=True, err=False):
    hint = "y/n"
    if show_default:
        if default is True:
            hint = "Y/n"
        elif default is False:
            hint = "y/N"
    full = f"{text} [{hint}]{prompt_suffix}" if show_default else f"{text}{prompt_suffix}"
    _read.err = err
    while True:
        answer = _read(False, full).strip().lower()
        if answer in _TRUE_ANSWERS:
            result = True
        elif answer in _FALSE_ANSWERS:
            result = False
        elif default is not None and answer == "":
            result = default
        else:
            echo("Error: invalid input", err=err)
            continue
        break
    if abort and not result:
        raise Abort()
    return result


def get_terminal_size():
    import shutil

    return shutil.get_terminal_size()


def clear():
    if not isatty(sys.stdout):
        return
    echo("\033[2J\033[1;1H", nl=False)


_getchar = None


def getchar(echo=False):
    if _getchar is not None:
        return _getchar(echo)
    from ._termui_impl import getchar as impl

    return impl(echo)


def pause(info=None, err=False):
    if not isatty(sys.stdin) or not isatty(sys.stdout):
        return
    if info is None:
        info = "Press any key to continue..."
    try:
        if info:
            echo(info, nl=False, err=err)
        try:
            getchar()
        except (KeyboardInterrupt, EOFError):
            pass
    finally:
        if info:
            echo(err=err)


def edit(text=None, editor=None, env=None, require_save=True, extension=".txt", filename=None):
    from ._termui_impl import Editor

    ed = Editor(editor=editor, env=env, require_save=require_save, extension=extension)
    if filename is None:
        return ed.edit(text)
    ed.edit_file(filename)


def launch(url, wait=False, locate=False):
    from ._termui_impl import open_url

    return open_url(url, wait=wait, locate=locate)


def echo_via_pager(text_or_generator, color=None):
    if callable(text_or_generator):
        text_or_generator = text_or_generator()
    if isinstance(text_or_generator, str):
        chunks = [text_or_generator]
    else:
        chunks = (c if isinstance(c, str) else str(c) for c in text_or_generator)
    text = "".join(chunks)
    from ._termui_impl import pager

    pager(text if text.endswith("\n") else text + "\n", resolve_color_default(color))


def progressbar(iterable=None, length=None, label=None, show_eta=True, show_percent=None,
                show_pos=False, item_show_func=None, fill_char="#", empty_char="-",
                bar_template="%(label)s  [%(bar)s]  %(info)s", info_sep="  ", width=36,
                file=None, color=None, update_min_steps=1):
    from ._termui_impl import ProgressBar

    color = resolve_color_default(color)
    return ProgressBar(iterable, length=length, fill_char=fill_char, empty_char=empty_char,
                       bar_template=bar_template, info_sep=info_sep, show_eta=show_eta,
                       show_percent=show_percent, show_pos=show_pos,
                       item_show_func=item_show_func, label=label, file=file, color=color,
                       update_min_steps=update_min_steps, width=width)
