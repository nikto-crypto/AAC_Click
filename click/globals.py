"""Per-thread stack of active contexts."""
import threading

_state = threading.local()


def _stack():
    try:
        return _state.stack
    except AttributeError:
        _state.stack = []
        return _state.stack


def get_current_context(silent=False):
    """Return the innermost active Context (or None if ``silent``)."""
    stack = _stack()
    if stack:
        return stack[-1]
    if not silent:
        raise RuntimeError("There is no active click context.")
    return None


def push_context(ctx):
    _stack().append(ctx)


def pop_context():
    _stack().pop()


def resolve_color_default(color=None):
    """Explicit value wins; otherwise defer to the active context."""
    if color is not None:
        return color
    ctx = get_current_context(silent=True)
    return ctx.color if ctx is not None else None
