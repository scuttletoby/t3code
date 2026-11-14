"""The privileged gh-as: runs one gh command with an App token. Runs only as the run user (ghrun), started by the
gh-as wrapper through sudo. The agent's one sudoers rule allows exactly this program.

  gh-as --app NAME --repo OWNER/NAME -- gh <args>

It checks the app and repository against the root-owned config, reads the arguments with the same table as the
wrapper (here every file path is refused: files arrive on stdin), applies the guard, asks gh-mint (as the keys
user) for a token, and runs the configured gh in an empty folder with a clean environment. gh's output is passed
through with the token (and anything that looks like a token or key) replaced by [redacted]. Each run has its own
folder under /var/lib/gh-apps/run and holds a lock on it; the lock is handed to gh, so a run whose gh-as was killed
keeps its folder until gh ends, and the next run removes only folders whose lock is free.
"""
import contextlib, fcntl, os, re, shutil, signal, stat, subprocess, sys, tempfile, threading

from . import common as c, policy
from .common import Refused

STDIN_LIMIT = 25 * 1024 * 1024
HOLD_BACK = 512          # bytes kept back between output chunks, longer than any token, so none is split
SECRET_B = re.compile(c.SECRET_RE.pattern.encode())


def note(msg):
    print(f'gh-as: {c.redact(msg)}', file=sys.stderr)


def mint_command(app_name):
    """How the token is asked for (tests replace this function)."""
    return [c.SUDO, '-n', '-u', c.KEYS_USER, '--', c.p(c.MINT), '--app', app_name]


def get_token(app):
    try:
        proc = subprocess.run(mint_command(app['name']), stdin=subprocess.DEVNULL, capture_output=True, cwd='/',
                              env={'PATH': c.SAFE_PATH, 'LANG': 'C.UTF-8'}, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused(f'app {app["name"]}: cannot ask gh-mint for a token ({type(e).__name__})') from None
    token = proc.stdout.decode('ascii', 'replace').strip()
    err = c.redact(proc.stderr.decode('utf-8', 'replace'), token).strip()
    if proc.returncode != 0 or not c.TOKEN_RE.match(token):
        raise Refused(err or f'app {app["name"]}: gh-mint gave no token')
    return token


def gh_binary(cfg):
    path = c.p(cfg['gh_path'])
    real = c.trusted_chain(path, 'gh')
    st = os.stat(real)
    if os.path.basename(real) != 'gh' or not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o111:
        raise Refused(f'gh_path {cfg["gh_path"]}: must be an executable file named gh')
    return real


# ---------- run folders

def run_root():
    root = c.p(c.RUN)
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise Refused(f'{c.RUN} must be a folder of {c.RUN_USER} with mode 700')
    return root


@contextlib.contextmanager
def sweep_lock(root):
    fd = os.open(os.path.join(root, '.sweep.lock'), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def sweep(root):
    """Remove run folders whose lock no process holds (gh-as and its gh both ended). Call under sweep_lock."""
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not name.startswith('gh-as-') or os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            fd = os.open(os.path.join(path, '.lock'), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            fd = None   # made and locked under the sweep lock, so a folder without a lock is a broken one
        except OSError:
            continue
        try:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue   # alive
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        finally:
            if fd is not None:
                os.close(fd)
    return removed


@contextlib.contextmanager
def run_folder():
    root = run_root()
    with sweep_lock(root):
        sweep(root)
        folder = tempfile.mkdtemp(prefix='gh-as-', dir=root)
        lock = os.open(os.path.join(folder, '.lock'), os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        for sub in ('config', 'cache', 'data', 'state', 'tmp'):
            os.mkdir(os.path.join(folder, sub), 0o700)
        yield folder, lock
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        os.close(lock)


def clean_env(folder, token, repo):
    return {
        'PATH': c.SAFE_PATH, 'HOME': folder, 'TMPDIR': folder + '/tmp', 'LANG': 'C.UTF-8', 'TERM': 'dumb',
        'GH_CONFIG_DIR': folder + '/config', 'XDG_CONFIG_HOME': folder + '/config', 'XDG_CACHE_HOME': folder + '/cache',
        'XDG_DATA_HOME': folder + '/data', 'XDG_STATE_HOME': folder + '/state',
        'GH_TOKEN': token, 'GH_HOST': c.HOST, 'GH_REPO': repo, 'GH_PROMPT_DISABLED': '1',
        'GH_NO_UPDATE_NOTIFIER': '1', 'GH_NO_EXTENSION_UPDATE_NOTIFIER': '1', 'GH_SPINNER_DISABLED': '1',
        'GH_PAGER': 'cat', 'PAGER': 'cat', 'GH_BROWSER': '/bin/false', 'BROWSER': '/bin/false',
        'GH_EDITOR': '/bin/false', 'EDITOR': '/bin/false', 'VISUAL': '/bin/false', 'GIT_TERMINAL_PROMPT': '0',
    }


# ---------- output scrubbing

class Scrubber:
    def __init__(self, out, token):
        self.out, self.token, self.buf = out, token.encode(), b''

    def _clean(self, data):
        return SECRET_B.sub(b'[redacted]', data.replace(self.token, b'[redacted]'))

    def feed(self, chunk):
        self.buf += chunk
        cut = len(self.buf) - HOLD_BACK
        if cut <= 0:
            return
        # never cut through the token or a token-like match; one that starts before the cut is whole in the buffer,
        # because HOLD_BACK is longer than any token
        spans = [(m.start(), m.end()) for m in SECRET_B.finditer(self.buf)]
        i = self.buf.find(self.token)
        while i >= 0:
            spans.append((i, i + len(self.token)))
            i = self.buf.find(self.token, i + 1)
        for s, e in sorted(spans):
            if s < cut < e:
                cut = e
        self._write(self._clean(self.buf[:cut]))
        self.buf = self.buf[cut:]

    def close(self):
        self._write(self._clean(self.buf))
        self.buf = b''

    def _write(self, data):
        if data:
            with contextlib.suppress(BrokenPipeError):
                self.out.write(data)
                self.out.flush()


def pump(src, scrubber):
    try:
        while True:
            chunk = os.read(src.fileno(), 65536)
            if not chunk:
                break
            scrubber.feed(chunk)
    finally:
        scrubber.close()


def read_stdin():
    data = sys.stdin.buffer.read(STDIN_LIMIT + 1)
    if len(data) > STDIN_LIMIT:
        raise Refused(f'stdin is larger than {STDIN_LIMIT // (1024 * 1024)} MB')
    return data


def stdin_file(data):
    if data is None:
        return None
    fd = os.memfd_create('gh-as-stdin', 0) if hasattr(os, 'memfd_create') else None
    f = os.fdopen(fd, 'w+b') if fd is not None else tempfile.TemporaryFile()
    f.write(data)
    f.flush()
    f.seek(0)
    return f


def execute(gh, args, env, data, lock):
    src = stdin_file(data)
    try:
        proc = subprocess.Popen([gh, *args], cwd=c.p(c.EMPTY), env=env, close_fds=True, pass_fds=(lock,),
                                stdin=src if src is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finally:
        if src is not None:
            src.close()
    token = env['GH_TOKEN']

    def forward(sig, _frame):
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)
    old = {s: signal.signal(s, forward) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        threads = [threading.Thread(target=pump, args=(proc.stdout, Scrubber(sys.stdout.buffer, token))),
                   threading.Thread(target=pump, args=(proc.stderr, Scrubber(sys.stderr.buffer, token)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        code = proc.wait()
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    return 128 - code if code < 0 else code


def main(argv):
    app_name = repo = None
    args = list(argv)
    try:
        while args and args[0] in ('--app', '--repo') and len(args) > 1:
            if args[0] == '--app':
                app_name = args[1]
            else:
                repo = args[1]
            args = args[2:]
        if len(args) < 2 or args[0] != '--' or args[1] != 'gh' or not app_name or not repo:
            print('usage: gh-as --app NAME --repo OWNER/NAME -- gh <args> (started by the gh-as wrapper)',
                  file=sys.stderr)
            return 2
        args = args[2:]
        c.require_user(c.RUN_USER)
        cfg = c.load_config()
        app = c.load_app(app_name)
        if (c.norm_repo(repo) or '').lower() != app['repo'].lower():
            raise Refused(f'app {app_name} is for {app["repo"]}, not {repo}')
        c.require_only_app(app)
        parsed = policy.parse(args, app['repo'], 'run')
        data = read_stdin() if parsed.stdin_uses else None
        policy.guard(parsed, data)
        gh = gh_binary(cfg)
        os.chdir('/')
        token = get_token(app)
        with run_folder() as (folder, lock):
            return execute(gh, args, clean_env(folder, token, app['repo']), data, lock)
    except Refused as e:
        note(e)
        return 1
    except OSError as e:
        note(f'{e.strerror or type(e).__name__}: {c.redact(getattr(e, "filename", "") or "")}')
        return 1
