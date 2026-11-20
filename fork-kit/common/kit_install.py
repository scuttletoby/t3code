"""The installer every kit shares (fork-kit/common/README.md, "The installer").

A kit's sbin/<name>-install is a small file: one subclass of Installer that names the kit's paths, files and
self-test, and adds what only that kit does (an option, a unit file, a check). Everything else is here, once:

- copy one release's files into <libexec>/versions/<sha>/ with MANIFEST.sha256, root-owned; a copy that is whole
  and unchanged is used as it is, any other is made again beside it, self-tested, and swapped in whole;
- switch the link <libexec>/current to it in one step, then the command links;
- keep a plain old command found where a link goes (a hand copy from before the kit) in <libexec>/replaced/, and
  put it back on uninstall;
- write <etc>/installed.tsv, which `self-check` reads to report drift;
- one installer (or uninstall) at a time; safe to run again and to stop at any step. Old version folders stay:
  a timer or a detached run may name one.

Plain Python 3 standard library. Paths are read under PREFIX and owners compared with ROOT_UID (tests only).
"""
import argparse, contextlib, ctypes, errno, fcntl, hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile

SHA_RE = re.compile(r'^[0-9a-f]{40}\Z')
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
AT_FDCWD, RENAME_EXCHANGE = -100, 2


class Refused(Exception):
    pass


def _renameat2():
    """libc's renameat2, or None where there is none."""
    try:
        fn = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return None
    fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    fn.restype = ctypes.c_int
    return fn


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 16), b''):
            h.update(block)
    return h.hexdigest()


def read_manifest(folder):
    out = {}
    with open(os.path.join(folder, 'MANIFEST.sha256'), encoding='utf-8') as f:
        for line in f:
            digest, _, rel = line.rstrip('\n').partition('  ')
            out[rel] = digest
    return out


class Installer:
    """One kit's installer. A subclass sets the names below; every path is absolute on the host."""
    PROG = None             # 'fork-cleaner-install': the command, and the prefix of what it prints
    KIT = None              # 'cleaner': the kit's folder in fork-kit
    ETC = None              # '/etc/fork-cleaner': the install record, the lock and the kit's config
    LIBEXEC = None          # '/usr/local/libexec/fork-cleaner': versions/, current, replaced/
    LINKS = {}              # {'/usr/local/bin/fork-cleaner': 'bin/fork-cleaner'}: path -> file under current
    KEEP_OLD = ()           # link paths where a plain old file (a hand copy) is kept, not refused
    SOURCE_FILES = ()       # the files of a release, relative to the kit folder (common/... from fork-kit/common)
    EXECUTABLE = frozenset()
    PACKAGE = None          # 'cleaner': lib/<PACKAGE>/<module>.py for each of MODULES; each is imported by the self-test
    MODULES = ()
    HELP = None             # ('bin/fork-cleaner', 'fork-cleaner daily'): its --help must print that text
    HAS_CONFIG = False      # write <etc>/config.json from config()
    KIT_STEPS = ()          # the kit's own steps, after `links`

    PREFIX = ''             # tests only: every fixed path is read under this folder
    ROOT_UID = 0            # tests only: who must run the installer and own the installed files

    Refused = Refused

    def __init__(self):
        self.CONFIG = self.ETC + '/config.json'
        self.INSTALLED = self.ETC + '/installed.tsv'
        self.LOCK = self.ETC + '/install.lock'
        self.VERSIONS = self.LIBEXEC + '/versions'
        self.CURRENT = self.LIBEXEC + '/current'
        self.REPLACED = self.LIBEXEC + '/replaced'
        self.STEPS = ('check', 'folders', 'copy', 'self-test', 'config', 'switch', 'links') + tuple(self.KIT_STEPS) \
            + ('record',)
        self._renameat2 = _renameat2

    # -- what a kit adds (each one does nothing here) --------------------------------------------------------------

    def add_install_options(self, parser):
        pass

    def check_kit(self, a):
        """Before the lock, nothing written: the kit's own option checks."""

    def plan(self, a):
        """Under the lock, before anything is written: what this run will do (a dict passed to the hooks below);
        refuse here what must not be overwritten."""
        return {}

    def config(self, a, plan):
        """The kit's config.json (HAS_CONFIG)."""
        return {'version': 1}

    def kit_step(self, step, a, plan, dest):
        """One of KIT_STEPS, after the links: the release it writes for is the one in use."""

    def kit_rows(self, plan):
        """Install-record rows of what kit_step wrote: (path, 'file' or 'link', sha256 or target)."""
        return []

    def uninstall_guard(self):
        """Under the lock, held for the whole uninstall: refuse here when uninstalling now would break something."""
        return contextlib.nullcontext()

    def kit_uninstall(self):
        """Under the lock: remove what kit_step wrote (the folders go after this)."""

    def installed_summary(self, plan):
        return ''

    # -- shared ---------------------------------------------------------------------------------------------------

    def p(self, path):
        return self.PREFIX + path

    def say(self, msg):
        print(f'{self.PROG}: {msg}', flush=True)

    def links(self):
        return {path: f'{self.CURRENT}/{rel}' for path, rel in self.LINKS.items()}

    def source_files(self):
        mods = tuple(f'lib/{self.PACKAGE}/{name}.py' for name in self.MODULES) if self.PACKAGE else ()
        return tuple(self.SOURCE_FILES) + mods

    @staticmethod
    def source_base(source, rel):
        """The folder a kit file is read from: common/ is shared by the kits and sits next to them (fork-kit/common);
        a source folder that has its own common/ (a copy) is used as it is."""
        if rel.startswith('common/') and not os.path.lexists(os.path.join(source, rel)):
            return os.path.dirname(os.path.normpath(source))
        return source

    def read_source(self, source, rel):
        """The bytes of one kit file of the source. Below the source folder no link is followed: a link (to a file
        only root may read, say) is refused, never copied, and so is anything that is not a regular file."""
        base, parts = self.source_base(source, rel), rel.split('/')
        where = f'point it at the fork-kit/{self.KIT} folder'
        try:
            fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as e:
            raise Refused(f'--source {source}: {e.strerror} ({where})') from None
        try:
            for part in parts[:-1]:
                new = os.open(part, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = new
            ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                raise Refused(f'--source {source}: {rel}: a link is on its way or it is not a file (the installer '
                              'copies no links)') from None
            raise Refused(f'--source {source}: {rel}: {e.strerror} ({where})') from None
        finally:
            os.close(fd)
        try:
            if not stat.S_ISREG(os.fstat(ffd).st_mode):
                raise Refused(f'--source {source}: {rel} is not a regular file')
            with os.fdopen(ffd, 'rb', closefd=False) as f:
                return f.read()
        finally:
            os.close(ffd)

    def ensure_dir(self, path, mode=0o755):
        real = self.p(path)
        if os.path.lexists(real) and (os.path.islink(real) or not os.path.isdir(real)):
            raise Refused(f'{path} exists and is not a folder: remove it by hand and run again')
        os.makedirs(real, mode=0o755, exist_ok=True)
        os.chmod(real, mode)

    def write_file(self, path, text, mode=0o644):
        """Atomic: a temporary file in the same folder, then renamed over the old one. Unchanged text is not
        rewritten."""
        real = self.p(path)
        with contextlib.suppress(OSError):
            with open(real, encoding='utf-8') as f:
                if f.read() == text and stat.S_IMODE(os.lstat(real).st_mode) == mode and not os.path.islink(real):
                    return False
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix='.' + os.path.basename(real) + '.',
                                   suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, real)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return True

    def switch_link(self, path, target):
        """Point path at target in one step (a new link renamed over the old one, or over a plain old file)."""
        real = self.p(path)
        if os.path.islink(real) and os.readlink(real) == target:
            return
        os.makedirs(os.path.dirname(real), mode=0o755, exist_ok=True)
        tmp = f'{real}.new-link'
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        os.symlink(target, tmp)
        os.replace(tmp, real)

    def ours(self, path, target):
        real = self.p(path)
        return os.path.islink(real) and os.readlink(real) == target

    @staticmethod
    def plain_file(real):
        return os.path.isfile(real) and not os.path.islink(real)

    @contextlib.contextmanager
    def install_lock(self):
        """One installer at a time: a second one is refused while the first runs (it never waits)."""
        self.ensure_dir(self.ETC)
        fd = os.open(self.p(self.LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused(f'another {self.PROG} is running: wait for it to finish and run again') from None
            yield
        finally:
            os.close(fd)

    def tree_problems(self, folder):
        """What differs between a version folder and its manifest, or is not root-owned, or others may write."""
        try:
            want = read_manifest(folder)
        except OSError as e:
            return [f'{folder}: no readable MANIFEST.sha256 ({e.strerror})']
        problems, seen = [], set()
        for dirpath, dirnames, filenames in os.walk(folder):
            for name in dirnames + filenames + ([] if dirpath != folder else ['.']):
                path = os.path.normpath(os.path.join(dirpath, name))
                st = os.lstat(path)
                if st.st_uid != self.ROOT_UID or st.st_mode & 0o022 or stat.S_ISLNK(st.st_mode):
                    problems.append(f'{path}: not root-owned, writable by others, or a link')
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), folder)
                if rel == 'MANIFEST.sha256':
                    continue
                seen.add(rel)
                if rel not in want:
                    problems.append(f'{rel}: not in the manifest')
                elif sha256_file(os.path.join(dirpath, name)) != want[rel]:
                    problems.append(f'{rel}: changed since install')
        problems += [f'{rel}: missing' for rel in sorted(set(want) - seen)]
        return problems

    def exchange(self, new, dest):
        """Puts the folder `new` at `dest` and the old `dest` at `new`, in one step where the kernel and the
        filesystem can (renameat2 RENAME_EXCHANGE): `dest` is never missing. Elsewhere two renames, with a moment
        in which it is (a unit then skips its run: ConditionPathExists)."""
        fn = self._renameat2()
        if fn is not None:
            if fn(AT_FDCWD, os.fsencode(new), AT_FDCWD, os.fsencode(dest), RENAME_EXCHANGE) == 0:
                return
            err = ctypes.get_errno()
            if err not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
                raise OSError(err, os.strerror(err), dest)
        aside = new + '-old'
        os.rename(dest, aside)
        try:
            os.rename(new, dest)
        except BaseException:
            os.rename(aside, dest)          # the old copy back where it was
            raise
        os.rename(aside, new)

    def self_test(self, dest):
        """The copy matches its manifest, every module of the kit imports from it alone, and --help works."""
        problems = self.tree_problems(dest)
        run = dict(capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, cwd=dest)
        if self.PACKAGE:
            code = ('import sys; sys.path[:0] = [sys.argv[1] + "/lib", sys.argv[1] + "/common"]\n'
                    f'for m in sys.argv[2:]: __import__("{self.PACKAGE}." + m)')
            r = subprocess.run([sys.executable, '-I', '-B', '-c', code, dest, *self.MODULES], **run)
            if r.returncode != 0:
                problems.append(f'lib/{self.PACKAGE} does not import: {(r.stderr or r.stdout).strip()[-300:]}')
        rel, text = self.HELP
        r = subprocess.run([sys.executable, '-I', '-B', os.path.join(dest, rel), '--help'], **run)
        if r.returncode != 0 or text not in r.stdout:
            problems.append(f'{rel} --help failed: {(r.stderr or r.stdout).strip()[:200]}')
        return problems

    def copy_tool(self, source, sha):
        """versions/<sha>, checked: the copy there when it is whole and unchanged, else a new copy made beside it
        and self-tested first, and only then put in its place in one step. A copy that fails stops here, and the
        version in use is the one it was. Refused when a self-test fails."""
        versions = self.p(self.VERSIONS)
        dest = os.path.join(versions, sha)
        files = self.source_files()
        if os.path.lexists(dest) and os.path.isdir(dest) and not os.path.islink(dest) \
                and not self.tree_problems(dest) and set(read_manifest(dest)) == set(files):
            problems = self.self_test(dest)
            if problems:
                raise Refused('the copy failed its self-test (nothing was changed):\n  ' + '\n  '.join(problems))
            return dest
        for name in os.listdir(versions):   # a run that stopped while copying (or an old copy it swapped out)
            if name.startswith('.tmp-'):
                shutil.rmtree(os.path.join(versions, name), ignore_errors=True)
        tmp = tempfile.mkdtemp(prefix=f'.tmp-{sha[:12]}-', dir=versions)
        try:
            lines = []
            for rel in sorted(files):
                dst = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(dst), mode=0o755, exist_ok=True)
                data = self.read_source(source, rel)
                with open(dst, 'wb') as f:
                    f.write(data)
                os.chmod(dst, 0o755 if rel in self.EXECUTABLE else 0o644)
                lines.append(f'{hashlib.sha256(data).hexdigest()}  {rel}')
            with open(os.path.join(tmp, 'MANIFEST.sha256'), 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
            os.chmod(os.path.join(tmp, 'MANIFEST.sha256'), 0o644)
            for dirpath, dirnames, _ in os.walk(tmp):
                for name in dirnames:
                    os.chmod(os.path.join(dirpath, name), 0o755)
            os.chmod(tmp, 0o755)
            problems = self.self_test(tmp)
            if problems:
                raise Refused('the new copy failed its self-test (the version in use is unchanged):\n  '
                              + '\n  '.join(problems))
            if not os.path.lexists(dest):
                os.rename(tmp, dest)
                return dest
            self.say(f'versions/{sha} is incomplete or changed: made again')
            if os.path.islink(dest) or not os.path.isdir(dest):
                os.unlink(dest)
                os.rename(tmp, dest)
                return dest
            self.exchange(tmp, dest)        # the old copy is now at tmp, removed below
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return dest

    def read_config(self):
        try:
            with open(self.p(self.CONFIG), encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise Refused(f'{self.CONFIG} cannot be read ({e}): fix or remove it and run again')
        if not isinstance(data, dict):
            raise Refused(f'{self.CONFIG} is not a JSON object: fix or remove it and run again')
        return data

    def check_old_state(self):
        """Refuse, before writing anything, what the installer must not overwrite."""
        for path, target in self.links().items():
            real = self.p(path)
            if not os.path.lexists(real) or self.ours(path, target):
                continue
            if path in self.KEEP_OLD and self.plain_file(real):
                continue                    # a plain old copy: kept in REPLACED, put back by uninstall
            raise Refused(f'{path} exists and is not the kit\'s link (a link elsewhere, or a folder): remove it by '
                          'hand and run again')
        for path in (self.ETC, self.LIBEXEC, self.VERSIONS, self.REPLACED):
            real = self.p(path)
            if os.path.lexists(real) and (os.path.islink(real) or not os.path.isdir(real)):
                raise Refused(f'{path} exists and is not a folder: remove it by hand and run again')
        if os.path.lexists(self.p(self.CURRENT)) and not os.path.islink(self.p(self.CURRENT)):
            raise Refused(f'{self.CURRENT} exists and is not a link: remove it by hand and run again')

    def check_options(self, a):
        if not SHA_RE.match(a.sha or ''):
            raise Refused('--sha must be the full 40-character release commit')
        for rel in self.source_files():
            self.read_source(os.path.realpath(a.source or ''), rel)
        self.check_kit(a)

    def keep_old(self, path):
        """A plain old file at a link path goes to REPLACED (named by its hash, so a second run copies nothing);
        the link then replaces it in one step, so the path is never missing."""
        real = self.p(path)
        if not self.plain_file(real):
            return
        self.ensure_dir(self.REPLACED)
        digest = sha256_file(real)
        name = f'{os.path.basename(path)}.{digest[:16]}'
        dest = self.p(f'{self.REPLACED}/{name}')
        if not os.path.exists(dest):
            tmp = dest + '.tmp'
            shutil.copy2(real, tmp)
            os.chmod(tmp, 0o755 if os.stat(real).st_mode & 0o111 else 0o644)
            os.replace(tmp, dest)
        self.say(f'the old {path} (sha256 {digest[:16]}) is kept as {self.REPLACED}/{name}; '
                 'uninstall puts it back')

    def replaced(self):
        """The kept old files: [(link path, kept path)], newest last."""
        folder = self.p(self.REPLACED)
        if not os.path.isdir(folder) or os.path.islink(folder):
            return []
        out = []
        for name in os.listdir(folder):
            base, _, digest = name.rpartition('.')
            for path in self.KEEP_OLD:
                if base == os.path.basename(path) and re.fullmatch(r'[0-9a-f]{16}', digest):
                    out.append((path, os.path.join(folder, name)))
        return sorted(out, key=lambda pk: os.path.getmtime(pk[1]))

    def record_rows(self, sha, plan):
        rows = [(path, 'link', target) for path, target in sorted(self.links().items())]
        rows.append((self.CURRENT, 'link', f'{self.VERSIONS}/{sha}'))
        for rel, digest in sorted(read_manifest(self.p(f'{self.VERSIONS}/{sha}')).items()):
            rows.append((f'{self.VERSIONS}/{sha}/{rel}', 'file', digest))
        if self.HAS_CONFIG:
            rows.append((self.CONFIG, 'file', sha256_file(self.p(self.CONFIG))))
        for _, kept in self.replaced():
            rows.append((kept[len(self.PREFIX):], 'file', sha256_file(kept)))
        return rows + list(self.kit_rows(plan))

    def install(self, a, stop_after=None):
        def done(step):
            if step == stop_after:
                raise SystemExit(f'stopped after {step} (test)')

        if os.geteuid() != self.ROOT_UID:
            raise Refused('run this as root')
        self.check_options(a)
        self.check_old_state()
        with self.install_lock():
            return self._install(a, done)

    def _install(self, a, done):
        self.check_old_state()      # again, now that no other installer runs
        plan = self.plan(a)
        done('check')
        for path in (self.ETC, self.LIBEXEC, self.VERSIONS):
            self.ensure_dir(path)
        done('folders')
        dest = self.copy_tool(os.path.realpath(a.source), a.sha)    # self-tested before it is used
        done('copy')
        done('self-test')
        if self.HAS_CONFIG:
            self.write_file(self.CONFIG, json.dumps(self.config(a, plan), indent=2, sort_keys=True) + '\n')
        done('config')
        self.switch_link(self.CURRENT, f'{self.VERSIONS}/{a.sha}')
        done('switch')
        for path, target in self.links().items():
            if path in self.KEEP_OLD:
                self.keep_old(path)
            self.switch_link(path, target)
        done('links')
        for step in self.KIT_STEPS:
            self.kit_step(step, a, plan, dest)
            done(step)
        rows = self.record_rows(a.sha, plan)
        self.write_file(self.INSTALLED, f'# path\tkind\twant (written by {self.PROG}; read by its self-check)\n'
                        + ''.join('\t'.join(r) + '\n' for r in rows))
        done('record')
        self.say(f'installed release {a.sha}' + self.installed_summary(plan))
        for path, kind, want in rows:
            self.say(f'  {path}  {kind}  {want}')
        return 0

    def uninstall(self):
        if os.geteuid() != self.ROOT_UID:
            raise Refused('run this as root')
        with self.install_lock():           # never from under a running install
            return self._uninstall()

    def _uninstall(self):
        with self.uninstall_guard():
            return self._remove()

    def _remove(self):
        kept = self.replaced()
        for path, target in self.links().items():
            if self.ours(path, target):
                os.unlink(self.p(path))
        for path, src in reversed(kept):    # the newest kept copy of each path
            real = self.p(path)
            if not os.path.lexists(real):
                tmp = real + '.restore'
                shutil.copy2(src, tmp)
                os.replace(tmp, real)
                self.say(f'put the old {path} back')
        self.kit_uninstall()
        for path in (self.LIBEXEC, self.ETC):
            if os.path.isdir(self.p(path)) and not os.path.islink(self.p(path)):
                shutil.rmtree(self.p(path))
        self.say(f'uninstalled (the machine file and what the {self.KIT} kit wrote elsewhere stay)')
        return 0

    def self_check(self):
        problems = []
        try:
            with open(self.p(self.INSTALLED), encoding='utf-8') as f:
                rows = [line.rstrip('\n').split('\t') for line in f if line.strip() and not line.startswith('#')]
        except OSError as e:
            print(f'PROBLEM: cannot read {self.INSTALLED} ({e.strerror}): not installed?')
            return 1
        for row in rows:
            if len(row) != 3:
                problems.append(f'bad row {row!r}')
                continue
            path, kind, want = row
            try:
                ok = (os.readlink(self.p(path)) == want) if kind == 'link' else (sha256_file(self.p(path)) == want)
            except OSError:
                ok = False
            if not ok:
                problems.append(f'{path}: differs from what was installed ({kind})')
        if os.path.islink(self.p(self.CURRENT)):
            problems += self.tree_problems(self.p(os.readlink(self.p(self.CURRENT))))
        for prob in problems:
            print(f'PROBLEM: {prob}')
        print(f'self-check: {len(rows)} installed paths, ' + ('OK' if not problems else f'{len(problems)} problems'))
        return 0 if not problems else 1

    def main(self, argv, stop_after=None, doc=''):
        ap = argparse.ArgumentParser(prog=self.PROG, description=(doc or self.__doc__ or '').split('\n\n')[0])
        sub = ap.add_subparsers(dest='cmd', required=True)
        i = sub.add_parser('install')
        i.add_argument('--source', required=True)
        i.add_argument('--sha', required=True)
        self.add_install_options(i)
        sub.add_parser('uninstall')
        sub.add_parser('self-check')
        a = ap.parse_args(argv)
        os.umask(0o022)
        try:
            if a.cmd == 'install':
                return self.install(a, stop_after)
            if a.cmd == 'uninstall':
                return self.uninstall()
            return self.self_check()
        except Refused as e:
            print(f'{self.PROG}: {e}', file=sys.stderr)
            return 1

