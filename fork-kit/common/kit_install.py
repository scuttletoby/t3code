"""The installer every kit shares (fork-kit/common/README.md, "The installer").

A kit's sbin/<name>-install is a small file: one subclass of Installer that names the kit's paths, files and
self-test, and adds what only that kit does (an option, a unit file, a check). Everything else is here, once:

- copy one release's files into <libexec>/versions/<sha>/ with MANIFEST.sha256, root-owned; a copy that is whole
  and unchanged is used as it is, any other is made again beside it, self-tested, and swapped in whole;
- switch the link <libexec>/current to it in one step, then the command links;
- keep a plain old command found where a link goes (a hand copy from before the kit) in <libexec>/replaced/, and
  put the last one kept back on uninstall, with its mode and owner, over the link in one step (replaced/order lists
  them in the order they were kept);
- write <etc>/installed.tsv, which `self-check` reads to report drift;
- one installer (or uninstall) at a time; safe to run again and to stop at any step. Old version folders stay:
  a timer or a detached run may name one. uninstall refuses while a process or a transient systemd unit (a timer
  still pending) runs code from <libexec>;
- install only the release whose installer runs: an installed installer given a newer release refuses (its file
  list is not the release's), and the bootstrap runs the release's own.

Plain Python 3 standard library. Paths are read under PREFIX and owners compared with ROOT_UID; PROC and RUN name
the process list and the runtime folder (tests only, all four).
"""
import argparse, contextlib, ctypes, errno, fcntl, hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile

SHA_RE = re.compile(r'^[0-9a-f]{40}\Z')
ENGINE = os.path.abspath(__file__)      # this file: install compares it with the release's common/kit_install.py
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
AT_FDCWD, RENAME_EXCHANGE = -100, 2


def _no_constant(name):
    """json: NaN and Infinity are no JSON (json.dumps would write them back as such)."""
    raise ValueError(f'{name} is not a JSON number')


def _no_twice(pairs):
    """json: a key given twice is refused, not read as its last value (which one was meant is unknown)."""
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f'key {k!r} is given twice')
        out[k] = v
    return out


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
    OPTIONAL_LINKS = ()     # link paths with a common name: when something else is there it is left alone (a warning)
    SOURCE_FILES = ()       # the files of a release, relative to the kit folder (common/... from fork-kit/common)
    EXECUTABLE = frozenset()
    PACKAGE = None          # 'cleaner': lib/<PACKAGE>/<module>.py for each of MODULES; each is imported by the self-test
    MODULES = ()
    HELP = None             # ('bin/fork-cleaner', 'fork-cleaner daily'): its --help must print that text
    HAS_CONFIG = False      # write <etc>/config.json from config()
    KIT_STEPS = ()          # the kit's own steps, after `links`
    PROGRAM = None          # the kit's installer file (its __file__): install refuses a release whose one differs

    PREFIX = ''             # tests only: every fixed path is read under this folder
    ROOT_UID = 0            # tests only: who must run the installer and own the installed files
    PROC = '/proc'          # tests only: the process list uninstall reads (not under PREFIX)
    RUN = '/run'            # tests only: where systemd keeps transient units (systemd/transient, user/<uid>/...)

    Refused = Refused

    def __init__(self):
        self.CONFIG = self.ETC + '/config.json'
        self.INSTALLED = self.ETC + '/installed.tsv'
        self.LOCK = self.ETC + '/install.lock'
        self.VERSIONS = self.LIBEXEC + '/versions'
        self.CURRENT = self.LIBEXEC + '/current'
        self.REPLACED = self.LIBEXEC + '/replaced'
        self.ORDER = self.REPLACED + '/order'
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

    CONFIG_KEYS = None   # the config.json keys config() owns; others (added by hand) are kept. None: written whole

    def config_to_write(self, a, plan):
        """config() plus, when CONFIG_KEYS names the kit's own keys, every other key the old config.json had."""
        new = self.config(a, plan)
        if self.CONFIG_KEYS is None:
            return new
        return dict({k: v for k, v in self.read_config().items() if k not in self.CONFIG_KEYS}, **new)

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
        rewritten. Temporary files a killed earlier run left for path are removed first (drop_temp_leftovers)."""
        real = self.p(path)
        self.drop_temp_leftovers(path)
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

    def drop_temp_leftovers(self, path):
        """Remove the temporary files write_file left beside path when a run was killed between writing one and
        renaming it (`.NAME.XXXXXXXX.tmp`; the unlink in write_file never ran). Every write_file runs under the
        install lock, so none of them is a write in progress. Returns the names removed."""
        folder, name = os.path.split(self.p(path))
        mine = re.compile(r'\.' + re.escape(name) + r'\.[a-z0-9_]{8}\.tmp\Z')
        try:
            names = os.listdir(folder)
        except OSError:
            return []
        gone = []
        for n in sorted(names):
            full = os.path.join(folder, n)
            with contextlib.suppress(FileNotFoundError):
                if mine.match(n) and stat.S_ISREG(os.lstat(full).st_mode):
                    os.unlink(full)
                    gone.append(n)
        return gone

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
        """The copy matches its manifest, every module of the kit imports from it alone, and the --help of its
        command and of its own installer work (an installer that cannot load from the copy could not update,
        check or uninstall it later)."""
        problems = self.tree_problems(dest)
        run = dict(capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, cwd=dest)
        if self.PACKAGE:
            code = ('import sys; sys.path[:0] = [sys.argv[1] + "/lib", sys.argv[1] + "/common"]\n'
                    f'for m in sys.argv[2:]: __import__("{self.PACKAGE}." + m)')
            r = subprocess.run([sys.executable, '-I', '-B', '-c', code, dest, *self.MODULES], **run)
            if r.returncode != 0:
                problems.append(f'lib/{self.PACKAGE} does not import: {(r.stderr or r.stdout).strip()[-300:]}')
        for rel, text in (self.HELP, (f'sbin/{self.PROG}', 'self-check')):
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
                data = json.load(f, parse_constant=_no_constant, object_pairs_hook=_no_twice)
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
            if not os.path.lexists(real) or self.ours(path, target) or path in self.OPTIONAL_LINKS:
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
        self.check_same_installer(os.path.realpath(a.source))
        self.check_kit(a)

    def check_same_installer(self, source):
        """The release's installer must be the one that runs: its file list (SOURCE_FILES, MODULES), self-test and
        steps are the ones this run uses. An installed installer given a newer release would copy its own list, not
        the release's, and could leave out a file the newer release needs and still report OK. The bootstrap runs
        the release's own installer, so it always passes here."""
        for rel, running in ((f'sbin/{self.PROG}', self.PROGRAM), ('common/kit_install.py', ENGINE)):
            try:
                with open(running, 'rb') as f:
                    same = f.read() == self.read_source(source, rel)
            except (OSError, TypeError) as e:
                raise Refused(f'cannot read the installer that runs ({running}: {e}): install with the bootstrap, as '
                              f'root (fork-kit/{self.KIT}/README.md, Install)') from None
            if not same:
                raise Refused(f'--source {source}: its {rel} differs from the installer that runs ({running}), so this '
                              'installer would copy its own file list, not the release\'s. Install that release with '
                              f'the bootstrap, as root (fork-kit/{self.KIT}/README.md, Install): it runs the '
                              'release\'s own installer. Nothing was changed')

    def keep_old(self, path):
        """A plain old file at a link path goes to REPLACED (named by its hash, so a second run copies nothing;
        root-owned, 0755 or 0644, whatever it was) and its name, mode and owner to the end of REPLACED/order (the
        last one kept is the one uninstall puts back, with that mode and owner); the link then replaces it in one
        step, so the path is never missing."""
        real = self.p(path)
        if not self.plain_file(real):
            return
        self.ensure_dir(self.REPLACED)
        folder = self.p(self.REPLACED)
        for name in os.listdir(folder):     # a copy a stopped run left half made
            if name.endswith('.tmp'):
                os.unlink(os.path.join(folder, name))
        digest = sha256_file(real)
        st = os.lstat(real)
        name = f'{os.path.basename(path)}.{digest[:16]}'
        dest = os.path.join(folder, name)
        if not os.path.exists(dest):
            tmp = dest + '.tmp'
            shutil.copyfile(real, tmp)
            os.chmod(tmp, 0o755 if st.st_mode & 0o111 else 0o644)
            os.replace(tmp, dest)
        meta = (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)
        rows = [r for r in self.order_rows() if r[0] != name] + [(name, meta)]
        self.write_file(self.ORDER, ''.join(n + (f'\t{m[0]:o}\t{m[1]}\t{m[2]}' if m else '') + '\n'
                                            for n, m in rows))
        self.say(f'the old {path} (sha256 {digest[:16]}) is kept as {self.REPLACED}/{name}; '
                 'uninstall puts it back')

    def order_rows(self):
        """REPLACED/order, first kept first: (name, (mode, uid, gid) of the old file, or None in an order from
        before the mode and owner were kept: it comes back as the kept copy is, root-owned). None when there is no
        such file."""
        try:
            with open(self.p(self.ORDER), encoding='utf-8') as f:
                lines = [line.strip().split('\t') for line in f if line.strip()]
        except FileNotFoundError:
            return []
        rows = []
        for f in lines:
            meta = None
            if len(f) == 4 and re.fullmatch(r'[0-7]{1,4}', f[1]) and f[2].isdigit() and f[3].isdigit():
                meta = (int(f[1], 8), int(f[2]), int(f[3]))
            rows.append((f[0], meta))
        return rows

    def kept_order(self):
        """The names in REPLACED/order, first kept first (none when there is no such file)."""
        return [name for name, _ in self.order_rows()]

    def replaced(self):
        """The kept old files: [(link path, kept path)], in the order they were kept (REPLACED/order), the last
        kept last. One not in the order yet (a run stopped between keeping it and writing the order) was the last
        kept, so it goes last too."""
        folder = self.p(self.REPLACED)
        if not os.path.isdir(folder) or os.path.islink(folder):
            return []
        found = {}
        for name in os.listdir(folder):
            base, _, digest = name.rpartition('.')
            for path in self.KEEP_OLD:
                if base == os.path.basename(path) and re.fullmatch(r'[0-9a-f]{16}', digest):
                    found[name] = (path, os.path.join(folder, name))
        order = [n for n in self.kept_order() if n in found]
        return [found[n] for n in order] + [found[n] for n in sorted(set(found) - set(order))]

    def foreign(self, path, target):
        """An OPTIONAL_LINKS path where something that is not the kit's link is: left alone."""
        return path in self.OPTIONAL_LINKS and os.path.lexists(self.p(path)) and not self.ours(path, target)

    def record_rows(self, sha, plan):
        rows = [(path, 'link', target) for path, target in sorted(self.links().items())
                if not self.foreign(path, target)]
        rows.append((self.CURRENT, 'link', f'{self.VERSIONS}/{sha}'))
        for rel, digest in sorted(read_manifest(self.p(f'{self.VERSIONS}/{sha}')).items()):
            rows.append((f'{self.VERSIONS}/{sha}/{rel}', 'file', digest))
        if self.HAS_CONFIG:
            rows.append((self.CONFIG, 'file', sha256_file(self.p(self.CONFIG))))
        for _, kept in self.replaced():
            rows.append((kept[len(self.PREFIX):], 'file', sha256_file(kept)))
        if os.path.isfile(self.p(self.ORDER)):
            rows.append((self.ORDER, 'file', sha256_file(self.p(self.ORDER))))
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
            text = json.dumps(self.config_to_write(a, plan), indent=2, sort_keys=True) + '\n'
            self.write_file(self.CONFIG, text)
        done('config')
        self.switch_link(self.CURRENT, f'{self.VERSIONS}/{a.sha}')
        done('switch')
        for path, target in self.links().items():
            if self.foreign(path, target):
                self.say(f'warning: {path} is there and is not this kit\'s link: left alone (run '
                         f'{target} by its full path, or remove {path} and install again)')
                continue
            if path in self.KEEP_OLD:
                self.drop_restore_leftover(path)
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
            in_use = self.in_use()
            if in_use:
                raise Refused('uninstall refused: code of this kit is in use, and removing it would leave that '
                              'with nothing to run:\n  ' + '\n  '.join(in_use) + '\nLet it end (or stop it) and '
                              'run uninstall again')
            return self._remove()

    def code_paths(self):
        """The paths a command line or a unit names when it runs this kit's code: <libexec>/ (and its real path, where
        /usr/local or a folder in it is a link: the kit's runs name the real path of their version), and the kit's
        links."""
        roots = {self.LIBEXEC}
        real = os.path.realpath(self.p(self.LIBEXEC))
        if real.startswith(self.PREFIX + '/'):
            roots.add(real[len(self.PREFIX):])
        return tuple(os.fsencode(r + '/') for r in sorted(roots)), {os.fsencode(p) for p in self.LINKS}

    @staticmethod
    def _norm(arg, cwd):
        """A command-line word as the absolute path it names: `/usr/local/bin//t3-golive`, `./t3-golive` and
        `../bin/t3-golive` (against the process's folder) all name the link. A bare word (`python3 t3-golive`, run
        from /usr/local/bin) is not taken as a path, so such a run is not seen; that is harmless today, as every
        golive run that changes anything holds the kit's lock, which uninstall takes first."""
        if not arg.startswith(b'/'):
            if b'/' not in arg or cwd is None:
                return arg
            arg = cwd + b'/' + arg
        return b'/' + os.path.normpath(arg).lstrip(b'/')

    def _names_code(self, args, roots, links, cwd=None):
        return any(arg.startswith(roots) or arg in links for a in args if a for arg in (a, self._norm(a, cwd)))

    def _open_folder(self, parent, name, owners, dev, shown):
        """The folder `name` under the open folder `parent`, opened with no link followed and never waited on; None
        when there is none. Refused when it is anything else, owned by someone not in owners, or (dev) on another
        filesystem than that: uninstall cannot see what runs from there."""
        why = 'uninstall cannot see whether a transient unit runs code of this kit; look at it and run again'
        try:
            fd = os.open(name, DIR_FLAGS | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise Refused(f'{shown} cannot be opened as a folder without following a link ({e.strerror}): {why}') \
                from None
        st = os.fstat(fd)
        if st.st_uid not in owners or (dev is not None and st.st_dev != dev):
            os.close(fd)
            raise Refused(f'{shown} belongs to uid {st.st_uid} or is a mount of its own: {why}')
        return fd

    def _open_chain(self, top, parts, owners, shown):
        """parts below the open folder top, each by _open_folder, all on the first one's filesystem."""
        fd, dev, here = None, None, shown
        try:
            for part in parts:
                here += '/' + part
                new = self._open_folder(top if fd is None else fd, part, owners, dev, here)
                if fd is not None:
                    os.close(fd)
                fd = new
                if fd is None:
                    return None
                dev = os.fstat(fd).st_dev if dev is None else dev
            return fd
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise

    def unit_folders(self):
        """The folders systemd keeps transient units in, open: [(shown path, fd)]. <run>/systemd/transient is
        root's. <run>/user/<uid>/systemd/transient is that user's, who could put a link (to /proc/self, say), a FIFO,
        another folder or a mount there: each step is opened with no link followed and never waited on, must be
        owned by root or that user, and must be on the filesystem of <run>/user/<uid>. Refused when one cannot be
        opened like that, so root's uninstall never reads where a user points it. Known limit: <run>/user/<uid>
        itself is that user's, so where /etc/fuse.conf has user_allow_other they can mount a FUSE filesystem of
        their own over it (allow_other), and root's open there then waits on their program, maybe forever, with
        both locks held (as a read of /proc/<pid>/cmdline of a process with a FUSE-backed command line can). That
        fails safe: nothing is removed yet, and Ctrl-C ends the wait and frees both locks."""
        try:
            run = os.open(self.RUN, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError:
            return []
        except OSError as e:
            raise Refused(f'{self.RUN} cannot be read ({e.strerror}): uninstall cannot see whether a transient unit '
                          'runs code of this kit') from None
        out, users = [], None
        try:
            chains = [(run, ('systemd', 'transient'), {self.ROOT_UID})]
            users = self._open_chain(run, ('user',), {self.ROOT_UID}, self.RUN)
            if users is not None:
                try:
                    names = sorted(os.listdir(users))
                except OSError as e:
                    raise Refused(f'{self.RUN}/user cannot be read ({e.strerror}): uninstall cannot see whether a '
                                  'transient unit runs code of this kit') from None
                chains += [(users, (n, 'systemd', 'transient'), {self.ROOT_UID, int(n)}) for n in names if n.isdigit()]
            for top, parts, owners in chains:
                shown = self.RUN + ('/user' if top == users else '')
                fd = self._open_chain(top, parts, owners, shown)
                if fd is not None:
                    out.append((shown + '/' + '/'.join(parts), fd))
        except BaseException:
            for _, fd in out:
                os.close(fd)
            raise
        finally:
            if users is not None:
                os.close(users)
            os.close(run)
        return out

    @staticmethod
    def _read_unit(folder, fd, name, limit=1 << 20):
        """A transient unit file's first bytes, opened in its folder with no link followed and never waited on; None
        for anything but a regular file (a FIFO, a link: systemd writes neither) or one that is gone. Refused when
        it cannot be read."""
        try:
            ufd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ELOOP):
                return None                 # gone (the unit ended), or a link
            raise Refused(f'{folder}/{name} cannot be read ({e.strerror}): uninstall cannot see whether it runs code '
                          'of this kit; look at it and run again') from None
        try:
            if not stat.S_ISREG(os.fstat(ufd).st_mode):
                return None
            return os.read(ufd, limit)
        except OSError as e:
            raise Refused(f'{folder}/{name} cannot be read ({e.strerror}): uninstall cannot see whether it runs code '
                          'of this kit; look at it and run again') from None
        finally:
            os.close(ufd)

    def in_use(self):
        """What runs this kit's code now, one line each: a process whose command line names it (but this one and
        the ones that started it), and a transient systemd unit (systemd-run's: a detached run, or the service of a
        timer still pending) whose unit file names it. Refused when the process list or a unit folder cannot be
        read."""
        roots, links = self.code_paths()
        found, mine, pid = [], set(), os.getpid()
        while pid > 1 and pid not in mine:  # this uninstall and what started it (sudo, a shell) name the links
            mine.add(pid)
            try:
                with open(f'{self.PROC}/{pid}/stat', 'rb') as f:
                    pid = int(f.read().rpartition(b')')[2].split()[1])
            except (OSError, ValueError, IndexError):
                break
        try:
            pids = [n for n in os.listdir(self.PROC) if n.isdigit()]
        except OSError as e:
            raise Refused(f'{self.PROC} cannot be read ({e.strerror}): uninstall cannot see whether code of this '
                          'kit is running') from None
        for name in sorted(pids, key=int):
            if int(name) in mine:
                continue
            try:
                with open(f'{self.PROC}/{name}/cmdline', 'rb') as f:
                    args = f.read().split(b'\0')
            except OSError:
                continue                    # it ended
            try:
                cwd = os.readlink(os.fsencode(f'{self.PROC}/{name}/cwd'))
            except OSError:
                cwd = None
            if self._names_code(args, roots, links, cwd):
                shown = b' '.join(a for a in args if a).decode('utf-8', 'replace')
                found.append(f'process {name}: {shown[:200]}')
        folders = self.unit_folders()
        try:
            for folder, fd in folders:
                for name in sorted(os.listdir(fd)):
                    text = self._read_unit(folder, fd, name)
                    if text is None:
                        continue
                    if any(r in text for r in roots) or any(re.search(rb'(?:^|[\s="\'@!+:-])' + re.escape(link)
                                                                        + rb'(?:[\s"\']|$)', text) for link in links):
                        found.append(f'transient unit {name} ({folder}): a detached run, or a timer still pending')
        finally:
            for _, fd in folders:
                os.close(fd)
        return found

    def drop_restore_leftover(self, path):
        """<path>.restore, the folder of root's an uninstall that stopped while putting an old file back left (with
        or without the copy in it). Only root can make it there: path's folder is root's."""
        tmp = self.p(path) + '.restore'
        if os.path.isdir(tmp) and not os.path.islink(tmp):
            shutil.rmtree(tmp)
        elif os.path.lexists(tmp):
            os.unlink(tmp)

    def put_back(self, path, kept, meta):
        """The kept old file at path, with its mode and owner (meta), in one step: the link (or nothing) there is
        replaced by the whole file, so the command is never missing. The copy is made in <path>.restore, a folder
        only root can enter (0700), and only renamed out of it once it has its mode: between chown and chmod the
        old owner owns the copy, and could write their own bytes into it before chmod adds a set-gid bit of a
        group they are not in, were it where they could open it. (chmod cannot go first: chown clears set-id
        bits; and an owner can always open their own file for writing, whatever its mode.) The folder is on
        path's filesystem, so the rename is one step."""
        real, stage = self.p(path), self.p(path) + '.restore'
        self.drop_restore_leftover(path)
        os.mkdir(stage, 0o700)
        tmp = os.path.join(stage, os.path.basename(real))
        shutil.copyfile(kept, tmp)
        if meta is None:                    # an order from before the mode and owner were kept
            os.chmod(tmp, stat.S_IMODE(os.stat(kept).st_mode))
        else:
            mode, uid, gid = meta
            os.chown(tmp, uid, gid)
            os.chmod(tmp, mode)             # after chown, which clears a set-id bit
        os.replace(tmp, real)
        os.rmdir(stage)
        self.say(f'put the old {path} back')

    def _remove(self):
        """In an order that a stop at any point, then a run again, finishes: each kept old file over its link in one
        step, the other command links, the kit's own files, <libexec>, then the installer's own link, then <etc>. A
        stop before <libexec> goes is run again with the installer's link; one while it goes leaves the link to a
        half-removed version: install again with the bootstrap, then uninstall (the README says so)."""
        kept, meta, last = self.replaced(), dict(self.order_rows()), {}
        for path, src in reversed(kept):    # the newest kept copy of each path
            last.setdefault(path, src)
        own = {path for path, rel in self.LINKS.items() if rel == f'sbin/{self.PROG}'}
        for path, target in self.links().items():
            if path in own:
                continue
            if path in last and (self.ours(path, target) or not os.path.lexists(self.p(path))):
                self.put_back(path, last[path], meta.get(os.path.basename(last[path])))
            elif self.ours(path, target):
                os.unlink(self.p(path))
            if path in self.KEEP_OLD:
                self.drop_restore_leftover(path)
        self.kit_uninstall()
        if os.path.isdir(self.p(self.LIBEXEC)) and not os.path.islink(self.p(self.LIBEXEC)):
            shutil.rmtree(self.p(self.LIBEXEC))
        for path, target in self.links().items():
            if path in own and self.ours(path, target):
                os.unlink(self.p(path))
        if os.path.isdir(self.p(self.ETC)) and not os.path.islink(self.p(self.ETC)):
            shutil.rmtree(self.p(self.ETC))
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

