r"""Fork only: the wire format of the desk link (.github/fork-fleet.md, "The desk link"), the one module both of its
sides load: `fork-fleet-desk.py` on the always-on host (`serve`, `launch`) and the desk agent on the desktop.

The link: the desktop dials out to the always-on host over ssh with a key that may only run
`fork-fleet-desk.py serve POOL`. Through that session the always-on host's `launch POOL VERB ARGS...` (a pool's
`launch` command for `fork-fleet.py dispatch`) has the desktop run `fork-fleet-launch.sh VERB ARGS...`.

Frames: one JSON object per line, each with "v": VERSION. Sizes are in BYTES: a frame is ASCII (JSON's \uXXXX
escapes for anything else) and at most MAX_FRAME bytes with its "\n"; the stdout and stderr of an answer are each at
most MAX_TEXT bytes as written in the frame (text_size; `fit` cuts a text to that, `answer` uses it). Too long is a
WireError, never a cut on the receiving side.
  agent -> serve, first     {"v":1, "hello": POOL}                  the pool the agent serves (must be serve's)
  serve -> agent, answer    {"v":1, "hello": POOL}
  serve -> agent            {"v":1, "id": N, "verb": V, "args": [...], "stdin": S}   one launcher call ("stdin": start only)
  agent -> serve            {"v":1, "id": N, "exit": E, "stdout": O, "stderr": R}   its result; any order, once per id
  serve -> agent            {"v":1, "ping": N}       the agent answers {"v":1, "pong": N} at once, also while verbs run.
                            serve sends one whenever it has sent nothing for PING (15) s, however much the agent
                            sends: the agent ends the session after 45 s with no frame from serve.
  launch -> serve (socket)  a request without "id"; serve answers with an answer without "id".
Anything else ends the session (or drops that one socket client). A hello of another VERSION ends it with serve's
exit WRONG_VERSION (ssh passes it to the agent) and a line on ssh's stderr that says which side to upgrade.

Time (the dispatcher, fork-fleet.py Host, gives a launch command 120 s):
  limit(verb)      VERBS' seconds. The agent MUST kill a verb still running at its limit and answer it UNKNOWN, and
                   MUST kill every verb still running when the session ends: a `start` left running after its 75
                   could come up after the next pass deleted its registration.
  serve_seconds    limit + SERVE_MARGIN: serve answers UNKNOWN itself when the agent has not answered by then.
  seconds(verb)    limit + MARGIN: `launch`'s whole time, connect and send included; it then exits UNKNOWN.
                   The longest verb the dispatcher calls (start, stop, prune) ends by 110 s, under its 120 s.
Exit codes of `launch`: the launcher's own (0-255), except NO_SESSION (3: nothing was sent; a launcher's own 3 is
turned into 1), UNKNOWN (75: the verb may or may not have run; the agent answers 75 only for a verb it killed, and
answers a launcher's own 75 as 1), 2 (refused before sending) and 1 (any other failure). Standard library only.
"""
import json, re

VERSION = 1
MAX_FRAME = 256 * 1024
MAX_TEXT = 64 * 1024          # bytes of the stdout or the stderr of one answer, as written in the frame
POOL = re.compile(r'[a-z0-9][a-z0-9_-]{0,39}')                       # fork-fleet.py's POOL
NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,62}-[0-9]{1,20}')             # fork-fleet-launch.sh's NAME
RUNNER_VERSION = re.compile(r'[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}')    # fork-fleet-launch.sh's VERSION
MB = re.compile(r'[0-9]{1,7}')
START_LINE = re.compile(r'(ACTIONS_RUNNER_INPUT_JITCONFIG=[A-Za-z0-9+/=]{1,65536}|FORK_FLEET_(RUN|ATTEMPT)=[1-9][0-9]{0,19})')
# The launcher's verbs: (argument patterns, then a pattern every further argument matches or None, the verb's limit
# in seconds: the agent kills it then). fork-fleet.py's Host waits 120 s for its launch command: see "Time" above.
VERBS = {
    'start': ((NAME,), None, 100),
    'ready': ((RUNNER_VERSION,), None, 30),
    'active': ((), None, 30),
    'fits': ((MB,), None, 30),
    'stop': ((NAME,), None, 100),
    'prune': ((), None, 100),
}
# The launcher's verbs that only its own machine runs (a root timer or a person at the machine), never the link:
# `serve` and the agent refuse them like an unknown verb, and the contract test counts them as the launcher's.
# check-net connects, as root and from the machine's own network, to each HOST:PORT it is given and says which one it
# cannot reach: through the link that would let the always-on host map the desktop's home network.
LOCAL_ONLY = frozenset({'refresh', 'check-net'})
MARGIN = 10                   # seconds `launch` waits beyond a verb's limit, all told
SERVE_MARGIN = 5              # seconds `serve` waits beyond it before it answers UNKNOWN itself
UNKNOWN = 75                  # launch's exit when the verb may or may not have run (EX_TEMPFAIL)
NO_SESSION = 3                # launch's exit when no desk session is live: nothing was sent
WRONG_VERSION = 76            # serve's exit on a frame of another VERSION (EX_PROTOCOL): one side needs an upgrade
CONTROL = re.compile('[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]')   # also lone surrogates: they cannot be printed


class WireError(ValueError):
    pass


class VersionError(WireError):
    """A frame of another VERSION: `got` is its "v"."""

    def __init__(self, got):
        side = 'the desktop agent' if got < VERSION else "this host's fork-fleet files"
        super().__init__(f'a frame of wire version {got}, this side speaks {VERSION}: upgrade {side}')
        self.got = got


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
    except RecursionError:
        raise WireError('not a JSON line: nested too deep') from None
    if not isinstance(frame, dict) or type(frame.get('v')) is not int:
        raise WireError(f'not a version {VERSION} frame')
    if frame['v'] != VERSION:
        raise VersionError(frame['v'])
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
    """The launcher's `start` input: its three lines, each once and ending in "\n" (the launcher's `read` drops an
    unended last line), nothing else."""
    if not text.endswith('\n'):
        raise WireError('start: the input does not end in a newline')
    lines = text.split('\n')[:-1]
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
    if not isinstance(verb, str) or verb not in VERBS or not isinstance(args, list) or \
            not all(isinstance(a, str) for a in args):   # str first: a list or an object is no key, and no crash
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


def limit(verb):
    """When the agent kills `verb` (still running) and answers UNKNOWN."""
    return VERBS[verb][2]


def serve_seconds(verb):
    """How long `serve` waits for the agent's answer to `verb` before it answers UNKNOWN itself."""
    return VERBS[verb][2] + SERVE_MARGIN


def seconds(verb):
    """How long `launch` takes for `verb` at most, all told (connect, send, the answer)."""
    return VERBS[verb][2] + MARGIN


def clean(text):
    """Text shown to a person or a log: no control characters but newline and tab, no lone surrogates."""
    return CONTROL.sub('?', text)


def text_size(text):
    """Bytes `text` takes in a frame."""
    return len(json.dumps(text, ensure_ascii=True)) - 2


CUT = '\n[cut to fit the desk link]\n'


def fit(text, most=MAX_TEXT):
    """`text`, or its longest start that fits in `most` bytes of a frame, then CUT."""
    if text_size(text) <= most:
        return text
    lo, hi = 0, len(text)            # text[:lo] + CUT fits, text[:hi] + CUT does not
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if text_size(text[:mid] + CUT) <= most else (lo, mid)
    return text[:lo] + CUT


def answer(exit_code, stdout='', stderr='', ident=None):
    """A checked answer frame; stdout and stderr are cut to fit (`fit`)."""
    frame = {'v': VERSION, 'exit': exit_code, 'stdout': fit(stdout), 'stderr': fit(stderr)}
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
        if not isinstance(frame[key], str) or text_size(frame[key]) > MAX_TEXT:
            raise WireError(f'answer: {key} is not text of at most {MAX_TEXT} bytes')
        frame[key] = clean(frame[key])
    return frame
