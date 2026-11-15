"""Fork only: the wire format of the desk link (.github/fork-fleet.md, "The desk link"), the one module both of its
sides load: `fork-fleet-desk.py` on the always-on host (`serve`, `launch`) and the desk agent on the desktop.

The link: the desktop dials out to the always-on host over ssh with a key that may only run
`fork-fleet-desk.py serve POOL`. Through that session the always-on host's `launch POOL VERB ARGS...` (a pool's
`launch` command for `fork-fleet.py dispatch`) has the desktop run `fork-fleet-launch.sh VERB ARGS...`.

Frames: one JSON object per line (ASCII, "\n"-terminated, at most MAX_FRAME bytes), each with "v": VERSION.
  agent -> serve, first     {"v":1, "hello": POOL}                  the pool the agent serves (must be serve's)
  serve -> agent, answer    {"v":1, "hello": POOL}
  serve -> agent            {"v":1, "id": N, "verb": V, "args": [...], "stdin": S}   one launcher call ("stdin": start only)
  agent -> serve            {"v":1, "id": N, "exit": E, "stdout": O, "stderr": R}   its result; any order, once per id
  serve -> agent            {"v":1, "ping": N}       the agent answers {"v":1, "pong": N} at once, also while verbs run
  launch -> serve (socket)  a request without "id"; serve answers with an answer without "id".
Anything else ends the session (or drops that one socket client). Standard library only.
"""
import json, re

VERSION = 1
MAX_FRAME = 256 * 1024
MAX_TEXT = 64 * 1024          # stdout or stderr of one answer
POOL = re.compile(r'[a-z0-9][a-z0-9_-]{0,39}')                       # fork-fleet.py's POOL
NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,62}-[0-9]{1,20}')             # fork-fleet-launch.sh's NAME
RUNNER_VERSION = re.compile(r'[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}')    # fork-fleet-launch.sh's VERSION
MB = re.compile(r'[0-9]{1,7}')
URL = re.compile(r'https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?')
HOST_PORT = re.compile(r'[0-9]{1,3}(\.[0-9]{1,3}){3}:[0-9]{1,5}')
START_LINE = re.compile(r'(ACTIONS_RUNNER_INPUT_JITCONFIG=[A-Za-z0-9+/=]{1,65536}|FORK_FLEET_(RUN|ATTEMPT)=[1-9][0-9]{0,19})')
# The launcher's verbs: (argument patterns, then a pattern every further argument matches or None, seconds the desktop
# may take). fork-fleet.py's Host waits 120 s for its launch command: every verb it calls answers before that.
VERBS = {
    'start': ((NAME,), None, 100),
    'ready': ((RUNNER_VERSION,), None, 30),
    'active': ((), None, 30),
    'fits': ((MB,), None, 30),
    'stop': ((NAME,), None, 100),
    'prune': ((), None, 100),
    'check-net': ((URL, HOST_PORT), HOST_PORT, 300),
}
MARGIN = 10                   # seconds `launch` waits beyond a verb's own, for the link itself
UNKNOWN = 75                  # launch's exit when the verb may or may not have run (EX_TEMPFAIL)
NO_SESSION = 3                # launch's exit when no desk session is live: nothing was sent
CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')


class WireError(ValueError):
    pass


def encode(frame):
    data = json.dumps(frame, separators=(',', ':'), ensure_ascii=True).encode() + b'\n'
    if len(data) > MAX_FRAME:
        raise WireError('frame too long')
    return data


class Reader:
    """Bytes in, frames (dicts) out. WireError on a line that is too long or not a JSON object of this VERSION."""

    def __init__(self):
        self.buf = b''

    def feed(self, data):
        self.buf += data
        frames = []
        while b'\n' in self.buf:
            line, self.buf = self.buf.split(b'\n', 1)
            frames.append(decode(line))
        if len(self.buf) > MAX_FRAME:
            raise WireError('frame too long')
        return frames


def decode(line):
    if len(line) + 1 > MAX_FRAME:
        raise WireError('frame too long')
    try:
        frame = json.loads(line.decode('ascii'))
    except (UnicodeDecodeError, ValueError):
        raise WireError('not a JSON line') from None
    if not isinstance(frame, dict) or frame.get('v') != VERSION or type(frame['v']) is not int:
        raise WireError(f'not a version {VERSION} frame')
    return frame


def _exact(frame, keys):
    if set(frame) != {'v', *keys}:
        raise WireError(f'a frame with keys {sorted(frame)}')


def _count(value):
    if type(value) is not int or not 0 <= value < 2 ** 53:
        raise WireError('not a frame number')
    return value


def check_hello(frame, pool):
    _exact(frame, ('hello',))
    if frame['hello'] != pool:
        raise WireError(f'hello for pool {str(frame["hello"])[:40]!r}, not {pool}')
    return frame


def check_ping(frame, key):
    _exact(frame, (key,))
    return _count(frame[key])


def check_start_input(text):
    """The launcher's `start` input: its three lines, each once, nothing else."""
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    keys = [line.split('=', 1)[0] for line in lines]
    if sorted(keys) != ['ACTIONS_RUNNER_INPUT_JITCONFIG', 'FORK_FLEET_ATTEMPT', 'FORK_FLEET_RUN'] or \
            not all(START_LINE.fullmatch(line) for line in lines):
        raise WireError('start: the input is not the three launcher lines')


def request(verb, args, stdin=None, ident=None):
    """A checked request frame (`ident` None: the socket's form, without "id")."""
    frame = {'v': VERSION, 'verb': verb, 'args': list(args)}
    if stdin is not None:
        frame['stdin'] = stdin
    if ident is not None:
        frame['id'] = ident
    return check_request(frame, with_id=ident is not None)


def check_request(frame, with_id):
    keys = {'verb', 'args'} | ({'id'} if with_id else set()) | ({'stdin'} if frame.get('verb') == 'start' else set())
    _exact(frame, keys)
    if with_id:
        _count(frame['id'])
    verb, args = frame['verb'], frame['args']
    if verb not in VERBS or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise WireError(f'not a launcher verb: {str(verb)[:40]!r}')
    fixed, rest, _ = VERBS[verb]
    if len(args) < len(fixed) or (rest is None and len(args) > len(fixed)):
        raise WireError(f'{verb}: {len(args)} arguments')
    for i, arg in enumerate(args):
        if not (fixed[i] if i < len(fixed) else rest).fullmatch(arg):
            raise WireError(f'{verb}: argument {i + 1} is not well formed')
    if verb == 'start':
        if not isinstance(frame['stdin'], str):
            raise WireError('start: no input')
        check_start_input(frame['stdin'])
    return frame


def seconds(verb):
    """How long `launch` waits for an answer to `verb`."""
    return VERBS[verb][2] + MARGIN


def clean(text):
    """Text shown to a person or a log: no control characters but newline and tab."""
    return CONTROL.sub('?', text)


def answer(exit_code, stdout='', stderr='', ident=None):
    frame = {'v': VERSION, 'exit': exit_code, 'stdout': stdout, 'stderr': stderr}
    if ident is not None:
        frame['id'] = ident
    return check_answer(frame, with_id=ident is not None)


def check_answer(frame, with_id):
    _exact(frame, ('exit', 'stdout', 'stderr') + (('id',) if with_id else ()))
    if with_id:
        _count(frame['id'])
    if type(frame['exit']) is not int or not 0 <= frame['exit'] <= 255:
        raise WireError('answer: exit is not 0 to 255')
    for key in ('stdout', 'stderr'):
        if not isinstance(frame[key], str) or len(frame[key]) > MAX_TEXT:
            raise WireError(f'answer: {key} is not text of at most {MAX_TEXT} characters')
        frame[key] = clean(frame[key])
    return frame
