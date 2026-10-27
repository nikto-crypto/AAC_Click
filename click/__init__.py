"""A composable command line interface toolkit."""
from .core import (Argument, BaseCommand, Command, CommandCollection, Context, Group,
                   MultiCommand, Option, Parameter, ParameterSource)
from .decorators import (argument, command, confirmation_option, group, help_option,
                         make_pass_decorator, option, pass_context, pass_meta_key, pass_obj,
                         password_option, version_option)
from .exceptions import (Abort, BadArgumentUsage, BadOptionUsage, BadParameter,
                         ClickException, FileError, MissingParameter, NoSuchOption, UsageError)
from .formatting import HelpFormatter, wrap_text
from .globals import get_current_context
from .termui import (clear, confirm, echo_via_pager, edit, getchar, launch, pause, progressbar,
                     prompt, secho, style, unstyle)
from .types import (BOOL, FLOAT, INT, STRING, UNPROCESSED, UUID, Choice, DateTime, File,
                    FloatRange, IntRange, ParamType, Path, Tuple)
from .utils import (echo, format_filename, get_app_dir, get_binary_stream, get_text_stream,
                    open_file)

__version__ = "0.1.0"
