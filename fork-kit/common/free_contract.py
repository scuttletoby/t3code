"""free/v1, "is this machine free?" (FREE.md beside this file): runs a free script and judges its answer.

One parser for every Python consumer (the desk launcher's `fits`, the desk kit's check); free-fixtures.json beside
it holds the cases every parser, in any language, must agree on.

  python3 -I free_contract.py ask [--need-mb MB] /absolute/script
      one line on stdout (free / busy: REASON / not available: REASON);
      exit 0 free, 75 busy, 1 not available, 2 a wrong call
"""
import argparse, datetime, json, os, select, signal, stat, subprocess, sys, time

VERSION, LIMIT_SECONDS, MAX_BYTES, MAX_AGE_SECONDS = 1, 3, 4096, 60
FREE, BUSY = 0, 75
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}


def number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def parse_at(at):
    if not isinstance(at, str):
        return None
    try:
        when = datetime.datetime.fromisoformat(at[:-1] + '+00:00' if at.endswith('Z') else at)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def judge(code, out, now, need_mb=None):
    """(verdict, reason, facts) for one answer: verdict 'free', 'busy' or 'unavailable' (only 'free' is free).

    code: the exit code, None when the script gave no answer in time. out: its stdout (bytes). now: an aware
    datetime. need_mb: when given, a `memory.freeMb` below it makes a free answer busy."""
    def no(why):
        return 'unavailable', why, None
    if len(out) > MAX_BYTES:
        return no(f'more than {MAX_BYTES} bytes on stdout')
    if code is None:
        return no(f'no answer within {LIMIT_SECONDS} seconds')
    if code not in (FREE, BUSY):
        return no(f'exit {code}')
    try:
        facts = json.loads(out.decode('utf-8'))
    except ValueError:   # UnicodeDecodeError is one
        return no('stdout is not JSON')
    if not isinstance(facts, dict):
        return no('stdout is not a JSON object')
    v, free, reason = facts.get('v'), facts.get('free'), facts.get('reason')
    if type(v) is not int or v < 1:
        return no('no version "v"')
    if v > VERSION:
        return no(f'version {v}, this reader knows {VERSION}')
    if type(free) is not bool or free != (code == FREE):
        return no(f'"free" is {json.dumps(free)} but the exit code is {code}')
    if not free and not (isinstance(reason, str) and reason.strip()):
        return no('busy without a "reason"')
    when = parse_at(facts.get('at'))
    if when is None:
        return no('no time "at" (ISO 8601 with a zone)')
    if abs((now - when).total_seconds()) > MAX_AGE_SECONDS:
        return no(f'"at" is {facts["at"]}, more than {MAX_AGE_SECONDS} seconds from now')
    if not free:
        return 'busy', ' '.join(reason.split())[:200], facts
    memory = facts.get('memory')
    free_mb = memory.get('freeMb') if isinstance(memory, dict) else None
    if need_mb is not None and number(free_mb) and free_mb < need_mb:
        return 'busy', f'{free_mb:g} MB free, {need_mb} MB needed', facts
    return 'free', '', facts


def unsafe(command, uid=None):
    """Why COMMAND must not run, or None: an absolute plain path to an executable file that it and every folder
    above it belong to root or to this user and that no one else may change (a sticky folder like /tmp is fine)."""
    uid = os.geteuid() if uid is None else uid
    if not os.path.isabs(command) or os.path.normpath(command) != command:
        return f'{command!r} is not an absolute plain path'
    path = os.path.realpath(command)
    try:
        if not stat.S_ISREG(os.stat(path).st_mode) or not os.access(path, os.X_OK):
            return f'{path} is not an executable file'
        while True:
            st = os.stat(path)
            if st.st_uid not in (0, uid):
                return f'{path} belongs to uid {st.st_uid}'
            if st.st_mode & 0o022 and not (stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX):
                return f'{path} may be changed by others'
            if path == '/':
                return None
            path = os.path.dirname(path)
    except OSError as e:
        return f'{path}: {e.strerror}'


def ask(command, need_mb=None, timeout=LIMIT_SECONDS):
    """Runs the free script (no arguments, stdin empty, a plain environment, its whole process group ended after it)
    and judges its answer: (verdict, reason, facts)."""
    why = unsafe(command)
    if why:
        return 'unavailable', why, None
    try:
        p = subprocess.Popen([command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=ENV, cwd='/', start_new_session=True)
    except OSError as e:
        return 'unavailable', f'cannot run {command}: {e.strerror}', None
    out, code, deadline = b'', None, time.monotonic() + timeout
    with p:
        try:
            while len(out) <= MAX_BYTES:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([p.stdout], [], [], left)[0]:
                    break
                chunk = os.read(p.stdout.fileno(), 65536)
                if not chunk:
                    try:
                        code = p.wait(max(0.0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        pass
                    break
                out += chunk
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return judge(code, out, datetime.datetime.now(datetime.timezone.utc), need_mb)


def main(argv):
    parser = argparse.ArgumentParser(prog='free_contract.py')
    sub = parser.add_subparsers(dest='verb', required=True)
    a = sub.add_parser('ask', help='run a free script and say what it answered')
    a.add_argument('--need-mb', type=int)
    a.add_argument('command')
    args = parser.parse_args(argv)
    verdict, reason, _ = ask(args.command, args.need_mb)
    print({'free': 'free', 'busy': f'busy: {reason}'}.get(verdict, f'not available: {reason}'))
    return {'free': 0, 'busy': BUSY}.get(verdict, 1)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
