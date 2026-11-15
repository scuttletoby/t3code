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


def no_constant(name):
    raise ValueError(f'{name} is not JSON')


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
        facts = json.loads(out.decode('utf-8'), parse_constant=no_constant)
    except ValueError:   # UnicodeDecodeError is one, and so is NaN or Infinity (not JSON, and NaN < size is False)
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


def kept(path, st, uid):
    """Why the folder PATH (its stat ST) may not be trusted to hold what we run, or None."""
    if st.st_uid not in (0, uid):
        return f'{path} belongs to uid {st.st_uid}'
    if st.st_mode & 0o022 and not (stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX):
        return f'{path} may be changed by others'
    return None


def resolve(command, uid=None):
    """(the file COMMAND leads to, None), or (None, why not). Follows the path one part at a time, as the kernel
    does, so the folders of the path as written are judged, not only those of the file it leads to: every folder
    on the way belongs to root or to this user and no one else may change it (a sticky folder like /tmp is fine),
    and every link on the way belongs to root or to this user. The kernel's fs.protected_symlinks would refuse
    another user's link in a sticky folder; resolving here must not quietly follow one."""
    uid = os.geteuid() if uid is None else uid
    if not os.path.isabs(command) or os.path.normpath(command) != command:
        return None, f'{command!r} is not an absolute plain path'
    parts, path, links = command.split('/')[1:], '/', 0
    try:
        while parts:
            name = parts.pop(0)
            if name in ('', '.'):
                continue
            if name == '..':
                path = os.path.dirname(path)
                continue
            why = kept(path, os.stat(path), uid)
            if why:
                return None, why
            here = os.path.join(path, name)
            st = os.lstat(here)
            if not stat.S_ISLNK(st.st_mode):
                path = here
                continue
            if st.st_uid not in (0, uid):
                return None, f'{here} is a link of uid {st.st_uid}'
            links += 1
            if links > 40:
                return None, f'{command}: too many levels of links'
            target = os.readlink(here)
            parts = target.split('/') + parts
            if target.startswith('/'):
                path = '/'
    except OSError as e:
        return None, f'{os.path.join(path, name)}: {e.strerror}'
    return path, None


def unsafe(command, uid=None):
    """Why COMMAND must not run, or None: an absolute plain path that resolve() accepts, to an executable file
    that it and every folder above it belong to root or to this user and that no one else may change (a sticky
    folder like /tmp is fine). ask() resolves a link once and both judges and runs the file it leads to.
    Plain mode bits only: an ACL that lets others write is not seen."""
    uid = os.geteuid() if uid is None else uid
    path, why = resolve(command, uid)
    if why:
        return why
    try:
        if not stat.S_ISREG(os.stat(path).st_mode) or not os.access(path, os.X_OK):
            return f'{path} is not an executable file'
        while True:
            why = kept(path, os.stat(path), uid)
            if why or path == '/':
                return why
            path = os.path.dirname(path)
    except OSError as e:
        return f'{path}: {e.strerror}'


def holders(pipe):
    """The other processes that hold the pipe PIPE (a file object) open: a script's child that left its process
    group (setsid) and kept stdout. Only processes this user may look at are seen (all of them for root)."""
    name, found = f'pipe:[{os.fstat(pipe.fileno()).st_ino}]', []
    for pid in filter(str.isdigit, os.listdir('/proc')):
        if int(pid) == os.getpid():
            continue
        try:
            if any(os.readlink(f'/proc/{pid}/fd/{fd}') == name for fd in os.listdir(f'/proc/{pid}/fd')):
                found.append(int(pid))
        except OSError:   # gone, or not ours to look at
            pass
    return found


def ask(command, need_mb=None, timeout=LIMIT_SECONDS):
    """Runs the free script (no arguments, stdin empty, a plain environment) and judges its answer: (verdict,
    reason, facts). After it, its whole process group is ended, and so is any other process that still holds its
    stdout (a child that left the group with setsid). A child that leaves the group and closes stdout is not seen:
    FREE.md asks scripts to leave nothing running."""
    path, why = resolve(command)   # resolved once: a link repointed later changes nothing
    why = why or unsafe(path)
    if why:
        return 'unavailable', why, None
    try:
        p = subprocess.Popen([path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=ENV, cwd='/', start_new_session=True)
    except OSError as e:
        return 'unavailable', f'cannot run {path}: {e.strerror}', None
    out, code, closed, deadline = b'', None, False, time.monotonic() + timeout
    poll = select.poll()   # not select(): a long-lived consumer may hold descriptors above 1024
    poll.register(p.stdout, select.POLLIN)
    try:
        while len(out) <= MAX_BYTES:
            left = deadline - time.monotonic()
            if left <= 0 or not poll.poll(left * 1000):
                break
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                closed = True
                try:
                    code = p.wait(max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
                break
            out += chunk
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:   # a group we may not signal (a set-uid script): end at least the script
            try:
                p.kill()
            except OSError:
                pass
        if not closed:   # someone still holds stdout open
            for pid in holders(p.stdout):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        p.stdout.close()
        try:   # never a wait without a limit: a script we could not end is left to finish on its own
            p.wait(1)
        except subprocess.TimeoutExpired:
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
