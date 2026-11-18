"""More of the fake machine (golive_fixture.py) for `back` and `status`: a stub t3 that acts as the fork build (its
report), the old build or an upstream nightly (`update <version> --allow-downgrade --yes`), an adapter on which that
update starts the service again, and an HTTP listener of the test's own. Nothing here touches a real T3, its port or
~/.t3: every server-runtime record the tests write names the test's own port.
"""
import http.server, json, os, shutil, socket, subprocess, sys, threading

import golive_fixture as fx

BASE = '0.1.10-nightly.20261114.4682'  # fx.TARGET without .fork.1
STABLE = '0.1.10'

# Acts as any t3 the kit runs (back.T3_STUB). `update`: installs the version in the home if it is not there (an
# upstream nightly), names it in the service state, asks the service to start (the adapter below starts it on its
# next look) and prunes the other runtimes as upstream does. Exit code from update.code; a file kill-after-update
# kills the kit run that called it.
STUB_T3 = '''#!{python} -I
import json, os, shutil, signal, sys
root = {root!r}
with open(os.path.join(root, 'back-t3-calls.jsonl'), 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] == '__fork-report':
    with open(os.path.join(root, 'stub-report.txt')) as f:
        sys.stdout.write(f.read())
    sys.exit(0)
assert sys.argv[1] == 'update', sys.argv
version, home = sys.argv[2], sys.argv[sys.argv.index('--base-dir') + 1]
try:
    with open(os.path.join(root, 'update.code')) as f:
        code = int(f.read())
except FileNotFoundError:
    code = 0
if code:
    print('update failed', file=sys.stderr)
    sys.exit(code)
d = os.path.join(home, 'runtime', 'versions', version)
if not os.path.exists(os.path.join(d, '.install-complete')):
    os.makedirs(d, exist_ok=True)
    shutil.copy({sleep!r}, os.path.join(d, 't3'))
    with open(os.path.join(d, '.install-complete'), 'w') as f:
        f.write(version)
with open(os.path.join(home, 'runtime', 'service-state.json'), 'w') as f:
    json.dump({{'protocol': 3, 'activeVersion': version}}, f)
open(os.path.join(root, 'start-requested'), 'w').close()
# Like upstream's prunePinnedRuntimes(keep: [current, target]) at the end of every update: every whole runtime that
# nothing names any more goes (the kit always runs the target's own t3, so current is the target; the service now
# runs the target). Upstream spares installs younger than 1 h; every runtime in a test is fresh, and on go-live day
# the old build's runtime is far older, so the stub has no grace.
versions = os.path.join(home, 'runtime', 'versions')
for v in sorted(os.listdir(versions)):
    d = os.path.join(versions, v)
    if v.startswith('.') or v == version or os.path.islink(d):
        continue
    try:
        with open(os.path.join(d, '.install-complete')) as f:
            whole = f.read().strip() == v
    except OSError:
        continue
    if whole:
        shutil.rmtree(d)
        with open(os.path.join(root, 'pruned.jsonl'), 'a') as f:
            f.write(json.dumps(v) + '\\n')
if os.path.exists(os.path.join(root, 'kill-after-update')):
    os.kill(os.getppid(), signal.SIGKILL)
'''


# A pre or post hook: appends its name to hooks.log, exits with the number in <name>.code (0 when missing).
HOOK = '''#!{python} -I
import os, sys
with open({log!r}, 'a') as f:
    f.write({name!r} + '\\n')
try:
    with open({log!r} + '.' + {name!r} + '.code') as f:
        sys.exit(int(f.read()))
except FileNotFoundError:
    pass
'''


class Adapter(fx.FakeAdapter):
    """FakeAdapter plus disable(); a start the stub update asked for happens on the next look at the service (as
    upstream's install enables and starts the unit), unless the test says that build never starts."""

    def service(self):
        asked = os.path.join(self.root, 'start-requested')
        if os.path.exists(asked):
            os.unlink(asked)
            if not os.path.exists(os.path.join(self.root, 'update-leaves-disabled')):
                self._svc(UnitFileState='enabled')
            self.start()
        return super().service()

    def start(self):
        """A build named in the test's `never-starts` file does not come up (a crash loop): the unit stays inactive."""
        try:
            with open(os.path.join(self.root, 'never-starts')) as f:
                broken = f.read().split()
        except FileNotFoundError:
            broken = []
        with open(os.path.join(self.root, 'home/runtime/service-state.json')) as f:
            if json.load(f)['activeVersion'] in broken:
                return self._record('start-failed')
        super().start()

    def disable(self):
        self._svc(UnitFileState='disabled')
        self._record('disable')


class Machine(fx.Machine):
    """A fake machine whose server-runtime records name the test's own HTTP port, never T3's default one."""

    def __init__(self):
        super().__init__()
        self.http = http.server.HTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.port = self.http.server_address[1]
        self.write('bin/t3-stub', STUB_T3.format(python=sys.executable, root=self.root,
                                                 sleep=shutil.which('sleep', path='/usr/bin:/bin')), mode=0o755)

    def runtime_file(self, **fields):
        port = fields.pop('port', getattr(self, 'port', None) or closed_port())
        fields.setdefault('origin', f'http://127.0.0.1:{port}')
        super().runtime_file(port=port, **fields)

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        super().close()

    def hooks(self, *names):
        """Pre and post hooks in the machine file (pre-*, post-* by name); each logs its name to hooks.log."""
        paths = {n: self.write(f'bin/{n}', HOOK.format(python=sys.executable, log=self.path('hooks.log'), name=n),
                               mode=0o755) for n in names}
        self.config(preHooks=[p for n, p in paths.items() if n.startswith('pre')],
                    postHooks=[p for n, p in paths.items() if n.startswith('post')])

    def hook_log(self):
        try:
            with open(self.path('hooks.log')) as f:
                return f.read().split()
        except FileNotFoundError:
            return []

    def upstream_update(self, version):
        """What `<t3 version> update <version>` does to the home (the stub above): names it, prunes the rest."""
        subprocess.run([self.path('bin/t3-stub'), 'update', version, '--allow-downgrade', '--yes', '--base-dir',
                        self.home], check=True, timeout=60)
        os.unlink(self.path('start-requested'))  # the caller runs the server of `version` itself

    def install(self, version):
        """The home's own runtime of `version`, as upstream's update installs it (a copy of sleep: /proc shows it)."""
        with open(shutil.which('sleep', path='/usr/bin:/bin'), 'rb') as f:
            self.write(f'home/runtime/versions/{version}/t3', f.read(), mode=0o755)
        self.write(f'home/runtime/versions/{version}/.install-complete', version)

    def switched(self, version=fx.TARGET):
        """What a switch leaves: the fork build installed and running, the database migrated, settings changed."""
        self.install(version)
        self.write('home/runtime/service-state.json', json.dumps({'protocol': 3, 'activeVersion': version}))
        self.write('home/userdata/statev2.sqlite', b'SQLite format 3\0' + b'migrated' * 700)
        self.write('home/userdata/statev2.sqlite-wal', b'migrated wal')
        self.write('home/userdata/settings.json', '{"agentSlots": true}')
        self.spawn_server(version)


class Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)  # any HTTP answer counts: T3 asks for a login first
        self.end_headers()

    def log_message(self, *args):
        pass


def closed_port():
    """A port nothing listens on (bound, then closed)."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
