"""The old build's runtime, kept by the kit outside the T3 home, so that the way back W1 still works after an update.

W1 runs `<home>/runtime/versions/<old>/t3 update <old> --allow-downgrade --yes`. But every upstream `t3 update`
ends by pruning each whole runtime in runtime/versions that nothing names any more (apps/server/src/cli/update.ts
`prunePinnedRuntimes({keep: [currentVersion, targetVersion]})`, apps/server/src/cloud/pinnedRuntime.ts
`selectUnusedRuntimes`: not the service state, a running program, a launcher or the unit, and older than 1 h). So
the switch's own update removes the old build's runtime, and W2 and W3 remove it too.

So the switch keeps a copy first: keep() (inside one of its journalled steps, before its update; plan_keep() is the
read-only check before its first step) copies <home>/runtime/versions/<old>/ to <stateDir>/kept-runtimes/<old>/
runtime/ with a record of every entry (kept.json: kind, size, mode, sha256 of each file, the target of each link),
written last and renamed into place whole, so a folder without kept.json is an unfinished keep and never used.
check() reads a kept runtime back against its record; W1 uses it only when it matches, and put_back() copies it
into runtime/versions/<old>/ (a fresh .install-complete, so upstream's 1 h grace covers it) before the downgrade.
Upstream never looks in stateDir, so no update removes a kept runtime, and the kit never deletes one: a damaged
one is set aside (<version>.bad-<time>) when keep() runs again.
"""
import datetime, hashlib, json, os, shutil, stat

from golive.config import EXACT_VERSION

KEPT_DIR, RECORD, RECORD_VERSION, SENTINEL = 'kept-runtimes', 'kept.json', 1, '.install-complete'
_PUT_BACK = '.golive-put-back-'  # not a version name: upstream's install and prune pass it by


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def runtime_dir(home, version):
    return os.path.join(home, 'runtime', 'versions', version)


def home_t3(home, version):
    return os.path.join(runtime_dir(home, version), 't3')


def kept_dir(cfg, version):
    return os.path.join(cfg.state_dir, KEPT_DIR, version)


def installed_t3(home, version):
    """The home's own t3 of `version` when upstream installed it whole (runtime/versions/<v>/.install-complete
    names the version; the folder is not a link, which upstream's prune passes by too), else None."""
    if not (isinstance(version, str) and EXACT_VERSION.match(version)):
        return None
    folder = runtime_dir(home, version)
    if os.path.islink(folder):
        return None
    try:
        with open(os.path.join(folder, SENTINEL), encoding='utf-8') as f:
            whole = f.read().strip() == version
    except OSError:
        return None
    t3 = os.path.join(folder, 't3')
    return t3 if whole and os.path.isfile(t3) and os.access(t3, os.X_OK) else None


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _entries(top):
    """Every entry under top, sorted, links recorded and never followed."""
    out = []
    for d, dirs, names in os.walk(top):
        dirs.sort()
        for n in sorted(dirs + names):
            p = os.path.join(d, n)
            rel, st = os.path.relpath(p, top), os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out.append({'path': rel, 'kind': 'link', 'target': os.readlink(p)})
            elif stat.S_ISDIR(st.st_mode):
                out.append({'path': rel, 'kind': 'dir', 'mode': stat.S_IMODE(st.st_mode)})
            elif stat.S_ISREG(st.st_mode):
                out.append({'path': rel, 'kind': 'file', 'mode': stat.S_IMODE(st.st_mode), 'size': st.st_size,
                            'sha256': _sha(p)})
            else:
                raise ValueError(f'{p} is not a file, folder or link')
    return out


def _copy_file(src, dst, mode):
    """Copies src to dst; returns (size, sha256) of the bytes it wrote."""
    h, size = hashlib.sha256(), 0
    with open(src, 'rb') as a, open(dst, 'wb') as b:
        for chunk in iter(lambda: a.read(1024 * 1024), b''):
            h.update(chunk)
            size += len(chunk)
            b.write(chunk)
        b.flush()
        os.fsync(b.fileno())
    os.chmod(dst, mode)
    return size, h.hexdigest()


def _copy_tree(src, dst, entries, verify=False):
    """verify=True (the put-back): each file is hashed as it is copied and must match its entry, so a kept file
    changed after check() is never put back (RuntimeError)."""
    os.makedirs(dst, mode=0o700)
    for e in entries:
        target = os.path.join(dst, e['path'])
        if e['kind'] == 'dir':
            os.mkdir(target, 0o700)
        elif e['kind'] == 'link':
            os.symlink(e['target'], target)
        else:
            got = _copy_file(os.path.join(src, e['path']), target, e['mode'])
            if verify and got != (e.get('size'), e.get('sha256')):
                raise RuntimeError(f'{os.path.join(src, e["path"])} changed since it was checked against {RECORD}: '
                                   'not put back')
    for e in entries:  # folders last: a folder without write bit would stop the copy into it
        if e['kind'] == 'dir':
            os.chmod(os.path.join(dst, e['path']), e['mode'])


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe(rel):
    return isinstance(rel, str) and rel and not os.path.isabs(rel) and '..' not in rel.split(os.sep)


def check(cfg, version):
    """Read only: (folder, None) when a kept runtime of `version` matches its record entry by entry (the same
    entries, sizes, sha256, links; .install-complete names the version; t3 executable), else (None, why)."""
    folder, _, why = _check(cfg, version)
    return folder, why


def _check(cfg, version):
    """check(), plus the record's entries it checked: (folder, entries, None) or (None, None, why)."""
    folder = kept_dir(cfg, version)
    tree = os.path.join(folder, 'runtime')
    for path in (folder, tree):
        if os.path.islink(path):
            return None, None, f'{path} is a link: a kept runtime is never followed through a link'
    try:
        with open(os.path.join(folder, RECORD), encoding='utf-8') as f:
            record = json.load(f)
    except FileNotFoundError:
        if os.path.lexists(folder):
            return None, None, f'{folder} has no {RECORD}: an unfinished or damaged kept runtime, never used'
        return None, None, f'no kept runtime of t3 {version} in {os.path.dirname(folder)}'
    except (OSError, ValueError) as e:
        return None, None, f'{os.path.join(folder, RECORD)} cannot be read: {e}'
    entries = record.get('entries') if isinstance(record, dict) else None
    if not (isinstance(record, dict) and record.get('recordVersion') == RECORD_VERSION
            and record.get('version') == version and isinstance(entries, list)
            and all(isinstance(e, dict) and _safe(e.get('path')) for e in entries)):
        return None, None, f'{os.path.join(folder, RECORD)} is not a record of t3 {version} this kit reads'
    try:
        found = {e['path']: e for e in _entries(tree)}
    except (OSError, ValueError) as e:
        return None, None, f'{tree} cannot be read: {e}'
    for e in entries:
        got = found.pop(e['path'], None)
        same = got is not None and all(got.get(k) == e.get(k) for k in ('kind', 'size', 'sha256', 'target'))
        if not same:
            return None, None, f'{os.path.join(tree, e["path"])} does not match {RECORD}: the kept runtime is damaged'
    if found:
        return None, None, f'{tree} has entries {RECORD} does not name ({", ".join(sorted(found)[:3])})'
    try:
        with open(os.path.join(tree, SENTINEL), encoding='utf-8') as f:
            whole = f.read().strip() == version
    except OSError:
        whole = False
    t3 = os.path.join(tree, 't3')
    if not (whole and os.path.isfile(t3) and os.access(t3, os.X_OK)):
        return None, None, f'{tree} is not a whole runtime of t3 {version} ({SENTINEL}, an executable t3)'
    return folder, entries, None


def size(cfg, version):
    """Read only: the bytes a keep of the home's runtime of `version` needs (0 when a good one is kept)."""
    if check(cfg, version)[0]:
        return 0
    return sum(e.get('size', 0) for e in _entries(runtime_dir(cfg.home, version)))


def plan_keep(cfg, version, floor_bytes=0):
    """Read only, for the switch before its first step: the bytes keep() will write; ValueError (say it as a
    refusal) when there is no whole runtime of `version` in the home to keep, or too little free disk."""
    if check(cfg, version)[0]:
        return 0
    if installed_t3(cfg.home, version) is None:
        raise ValueError(f'the runtime of t3 {version} is not installed whole in {runtime_dir(cfg.home, version)}: '
                         'there is nothing to keep for the way back W1')
    need = size(cfg, version)
    parent = cfg.state_dir if os.path.isdir(cfg.state_dir) else os.path.dirname(cfg.state_dir)
    try:
        free = shutil.disk_usage(parent).free
    except OSError as e:
        raise ValueError(f'the free disk for {cfg.state_dir} cannot be measured: {e}') from None
    if free - need < floor_bytes:
        raise ValueError(f'keeping the runtime of t3 {version} needs {need} bytes in {cfg.state_dir}; {free} are '
                         f'free and {floor_bytes} must stay free')
    return need


def keep(run, cfg, version):
    """Inside a journalled step (the switch's, before its update): keep the home's runtime of `version` in
    <stateDir>/kept-runtimes/<version>/. Safe to run again: a good kept runtime is left as it is; an unfinished or
    damaged one is set aside, never deleted. Returns facts for the journal."""
    run.require_step(f'keep the runtime of t3 {version}')
    folder = kept_dir(cfg, version)
    if check(cfg, version)[0]:
        return {'kept': folder, 'already': True}
    src = runtime_dir(cfg.home, version)
    if installed_t3(cfg.home, version) is None:
        raise RuntimeError(f'the runtime of t3 {version} is not installed whole in {src}: nothing to keep')
    parent = os.path.dirname(folder)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    facts = {'kept': folder, 'already': False}
    if os.path.lexists(folder):
        aside = f'{folder}.bad-{_stamp()}'
        os.rename(folder, aside if not os.path.lexists(aside) else f'{aside}-{os.getpid()}')
        facts['setAside'] = aside
    partial = os.path.join(parent, f'.partial-{version}')
    if os.path.lexists(partial):  # this kit's own unfinished copy from a keep that was stopped
        shutil.rmtree(partial)
    entries = _entries(src)
    _copy_tree(src, os.path.join(partial, 'runtime'), entries)
    record = {'recordVersion': RECORD_VERSION, 'version': version, 'keptAt': _stamp(), 'from': src,
              'bytes': sum(e.get('size', 0) for e in entries), 'entries': entries}
    with open(os.path.join(partial, RECORD), 'w', encoding='utf-8') as f:
        json.dump(record, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    _fsync_dir(partial)
    os.rename(partial, folder)
    _fsync_dir(parent)
    ok, why = check(cfg, version)
    if not ok:
        raise RuntimeError(f'the kept runtime does not read back: {why}')
    facts.update(files=sum(e['kind'] == 'file' for e in entries), bytes=record['bytes'])
    return facts


def put_back(run, cfg, version):
    """Inside a journalled step (W1, before the downgrade): when the home has no whole runtime of `version`, copy
    the kept one to runtime/versions/<version>/ (an incomplete folder there is set aside, never deleted). Safe to
    run again. Returns facts for the journal."""
    run.require_step(f'put the runtime of t3 {version} back')
    target = runtime_dir(cfg.home, version)
    if installed_t3(cfg.home, version):
        return {'runtime': target, 'putBack': False}
    folder, entries, why = _check(cfg, version)
    if folder is None:
        raise RuntimeError(why)
    versions = os.path.dirname(target)
    os.makedirs(versions, exist_ok=True)
    staging = os.path.join(versions, f'{_PUT_BACK}{version}')
    if os.path.lexists(staging):  # this kit's own unfinished copy from a put-back that was stopped
        shutil.rmtree(staging)
    _copy_tree(os.path.join(folder, 'runtime'), staging, entries, verify=True)  # the entries check() read
    os.chmod(staging, 0o755)
    facts = {'runtime': target, 'putBack': True, 'from': folder}
    if os.path.lexists(target):
        aside = os.path.join(versions, f'.golive-aside-{version}-{_stamp()}')
        os.rename(target, aside)
        facts['setAside'] = aside
    os.rename(staging, target)
    _fsync_dir(versions)
    if installed_t3(cfg.home, version) is None:
        raise RuntimeError(f'{target} is not a whole runtime after the put-back')
    return facts


def listing(cfg):
    """Read only, for status: [(version or folder name, folder, bytes, why or None)] of the kept runtimes."""
    parent = os.path.join(cfg.state_dir, KEPT_DIR)
    try:
        names = sorted(n for n in os.listdir(parent) if not n.startswith('.'))
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        folder = os.path.join(parent, n)
        if not EXACT_VERSION.match(n):
            out.append((n, folder, 0, 'set aside (damaged), never used'))
            continue
        ok, why = check(cfg, n)
        try:
            with open(os.path.join(folder, RECORD), encoding='utf-8') as f:
                size_ = int(json.load(f).get('bytes') or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            size_ = 0
        out.append((n, folder, size_, why))
    return out
