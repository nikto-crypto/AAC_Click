"""Utilities for driving commands from tests."""
import contextlib
import io
import os
import shutil
import sys
import tempfile

from . import formatting, termui, utils


class _Stream(io.TextIOWrapper):
    """Text stream over a byte buffer with a stable ``name`` and ``mode``."""

    def __init__(self, buffer, charset, name, mode, errors="strict"):
        super().__init__(buffer, encoding=charset, errors=errors, write_through=True)
        self._label = name
        self._mode = mode

    @property
    def name(self):
        return self._label

    @property
    def mode(self):
        return self._mode


class EchoingStdin:
    """Byte reader that copies whatever is consumed into an output buffer."""

    def __init__(self, source, sink):
        self._source = source
        self._sink = sink

    def __getattr__(self, name):
        # Hide read1 so wrapping text streams go through the echoing read().
        if name == "read1":
            raise AttributeError(name)
        return getattr(self._source, name)

    def _copy(self, data):
        self._sink.write(data)
        return data

    def read(self, n=-1):
        return self._copy(self._source.read(n))

    def readline(self, n=-1):
        return self._copy(self._source.readline(n))

    def readlines(self):
        return [self._copy(line) for line in self._source.readlines()]

    def __iter__(self):
        return iter(self._copy(line) for line in self._source)


class Result:
    def __init__(self, runner, stdout_bytes, stderr_bytes, return_value, exit_code,
                 exception, exc_info=None):
        self.runner = runner
        self.stdout_bytes = stdout_bytes
        self.stderr_bytes = stderr_bytes
        self.return_value = return_value
        self.exit_code = exit_code
        self.exception = exception
        self.exc_info = exc_info

    @property
    def output(self):
        return self.stdout

    @property
    def stdout(self):
        return self.stdout_bytes.decode(self.runner.charset, "replace").replace("\r\n", "\n")

    @property
    def stderr(self):
        if self.stderr_bytes is None:
            raise ValueError("stderr not separately captured")
        return self.stderr_bytes.decode(self.runner.charset, "replace").replace("\r\n", "\n")

    def __repr__(self):
        name = type(self.exception).__name__ if self.exception else "okay"
        return f"<{type(self).__name__} {name}>"


class CliRunner:
    def __init__(self, charset="utf-8", env=None, echo_stdin=False, mix_stderr=True):
        self.charset = charset
        self.env = dict(env or {})
        self.echo_stdin = echo_stdin
        self.mix_stderr = mix_stderr

    def get_default_prog_name(self, cli):
        return cli.name or "root"

    def make_env(self, overrides=None):
        rv = dict(self.env)
        if overrides:
            rv.update(overrides)
        return rv

    @contextlib.contextmanager
    def isolation(self, input=None, env=None, color=False):
        if input is None:
            data = b""
        elif isinstance(input, str):
            data = input.encode(self.charset)
        elif isinstance(input, bytes):
            data = input
        else:
            data = input.read()
            if isinstance(data, str):
                data = data.encode(self.charset)

        saved = (sys.stdin, sys.stdout, sys.stderr, termui.visible_prompt_func,
                 termui.hidden_prompt_func, termui._getchar, utils.should_strip_ansi,
                 formatting.FORCED_WIDTH)

        out_buf = io.BytesIO()
        err_buf = out_buf if self.mix_stderr else io.BytesIO()
        raw_in = io.BytesIO(data)
        in_buf = EchoingStdin(raw_in, out_buf) if self.echo_stdin else raw_in

        stdin = _Stream(in_buf, self.charset, "<stdin>", "r")
        stdout = _Stream(out_buf, self.charset, "<stdout>", "w", errors="backslashreplace")
        stderr = stdout if self.mix_stderr else _Stream(
            err_buf, self.charset, "<stderr>", "w", errors="backslashreplace")

        eof_hits = [0]

        def read_line():
            line = raw_in.readline()
            if not line:
                # a first empty read lets prompts fall back to defaults; repeating
                # means nothing will ever arrive, so stop instead of looping
                eof_hits[0] += 1
                if eof_hits[0] > 2:
                    raise EOFError()
            return line

        def visible_input(prompt=None):
            sys.stdout.write(prompt or "")
            line = read_line()
            if self.echo_stdin:
                out_buf.write(line)
            val = line.decode(self.charset, "replace").rstrip("\r\n")
            if not self.echo_stdin:
                sys.stdout.write(f"{val}\n")
            sys.stdout.flush()
            return val

        def hidden_input(prompt=None):
            sys.stdout.write(f"{prompt or ''}\n")
            sys.stdout.flush()
            line = read_line()
            return line.decode(self.charset, "replace").rstrip("\r\n")


        def getchar(echo):
            ch = raw_in.read(1).decode(self.charset, "replace")
            if echo:

                sys.stdout.write(ch)
                sys.stdout.flush()
            return ch

        def should_strip(stream=None, color_arg=None):
            if color_arg is None:
                return not color
            return not color_arg

        sys.stdin, sys.stdout, sys.stderr = stdin, stdout, stderr
        termui.visible_prompt_func = visible_input
        termui.hidden_prompt_func = hidden_input
        termui._getchar = getchar
        utils.should_strip_ansi = should_strip
        formatting.FORCED_WIDTH = 80

        previous_env = {}
        for key, value in self.make_env(env).items():
            previous_env[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            yield (out_buf, None if self.mix_stderr else err_buf)
        finally:
            for stream in {id(s): s for s in (stdin, stdout, stderr)}.values():
                try:
                    stream.flush()
                except ValueError:
                    pass
                stream.detach()
            for key, value in previous_env.items():

                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            (sys.stdin, sys.stdout, sys.stderr, termui.visible_prompt_func,
             termui.hidden_prompt_func, termui._getchar, utils.should_strip_ansi,
             formatting.FORCED_WIDTH) = saved

    def invoke(self, cli, args=None, input=None, env=None, catch_exceptions=True,
               color=False, **extra):
        exc_info = None
        return_value = None
        exit_code = 0
        exception = None
        with self.isolation(input=input, env=env, color=color) as (out_buf, err_buf):
            try:
                if isinstance(args, str):
                    import shlex
                    args = shlex.split(args)
                prog = extra.pop("prog_name", None) or self.get_default_prog_name(cli)
                return_value = cli.main(args=args or (), prog_name=prog, **extra)
            except SystemExit as e:
                exc_info = sys.exc_info()
                code = e.code
                if code is None:
                    code = 0
                if code != 0:
                    exception = e
                if not isinstance(code, int):
                    sys.stdout.write(str(code))
                    sys.stdout.write("\n")
                    code = 1
                exit_code = code
            except Exception as e:
                if not catch_exceptions:
                    raise
                exception = e
                exit_code = 1
                exc_info = sys.exc_info()
            finally:
                sys.stdout.flush()
                stdout = out_buf.getvalue()
                stderr = None
                if err_buf is not None:
                    sys.stderr.flush()
                    stderr = err_buf.getvalue()
        return Result(self, stdout, stderr, return_value, exit_code, exception, exc_info)

    @contextlib.contextmanager
    def isolated_filesystem(self, temp_dir=None):
        cwd = os.getcwd()
        dt = tempfile.mkdtemp(dir=temp_dir)
        os.chdir(dt)
        try:
            yield dt
        finally:
            os.chdir(cwd)
            if temp_dir is None:
                try:
                    shutil.rmtree(dt)
                except OSError:
                    pass
