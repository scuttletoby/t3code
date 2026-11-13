#!/usr/bin/env python3
"""A Gradle build's Maven local repository from a reviewed lock: the same bytes on every runner.

  gradle_cache.py stage --lock FILE --out DIR       every file the lock lists, sha256-checked, in a new DIR; then
                                                     `./gradlew -Dmaven.repo.local=DIR`. Nothing else is in DIR, so a
                                                     jar the lock does not list fails in Gradle; a listed one that is
                                                     missing or has other bytes fails here, by name.
  gradle_cache.py check --lock FILE                 every asset the lock needs is listed, finished and has the locked
                                                     digest (no download): run it on a schedule to see a deleted asset
                                                     before a build needs it.
  gradle_cache.py publish --lock FILE G:A:V ...     by hand, after the version is in ~/.m2: uploads its jar, pom,
                                                     module and SNAPSHOT metadata to the lock's release and adds their
                                                     rows. A path the lock has with other bytes is refused (a version
                                                     is never reused); an asset of a locked row that is gone or
                                                     unfinished is uploaded again; a second run changes nothing.
  gradle_cache.py prune-lock --lock FILE --keep G:A:V ...   drops the rows of every other version. Never deletes assets:
                                                     old commits still stage.

The lock: {"release": "<tag>", "files": [{"path": "<group/artifact/version/file>", "sha256": "<hex>", "release": "<tag>"}]}.
A row's "release" (default: the lock's) holds its asset `<sha256>-<file name>`; publish writes to the lock's "release",
so a full release is left by naming a new one there. Assets are read with the job's own token (contents: read).
stage keeps a cache by sha256 and still asks GitHub for the asset list on every run, so a runner with a warm cache and a
fresh runner give the same result. Troubleshooting: README.md next to this file.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request

API = os.environ.get('GITHUB_API_URL') or 'https://api.github.com'
CACHE = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), 'fork-ci-gradle-cache')
M2 = os.path.expanduser('~/.m2/repository')
MAX_ASSETS = 1000   # GitHub's limit per release (docs: "About releases")
PART_AGE = 3600     # a .part- file is written and renamed in one go, so one an hour old is a killed run's
PATH_OK = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)+$')
SHA_OK = re.compile(r'^[0-9a-f]{64}$')
TAG_OK = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9._-]*$')
# What a build reads from a version folder. Sources and javadoc jars are for IDEs; the folder-level metadata is for ranges.
WANTED = re.compile(r'\.(jar|pom|module)$|^maven-metadata-local\.xml$')
NOT_WANTED = re.compile(r'-(sources|javadoc)\.jar$')


class Refused(Exception):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def asset_name(row):
    return f"{row['sha256']}-{os.path.basename(row['path'])}"


def release_of(lock, row):
    return row.get('release') or lock['release']


def row_problems(row):
    """What read_lock refuses in one row. publish checks its new rows with the same rule before the first upload, so it
    can never write a row that turns the whole lock (and every stage) red."""
    path, digest, problems = row.get('path', ''), row.get('sha256', ''), []
    if not PATH_OK.match(path) or '..' in path.split('/'):
        problems.append(f'bad path {path!r}')
    if not SHA_OK.match(digest):
        problems.append(f'{path}: bad sha256 {digest!r}')
    if 'release' in row and not TAG_OK.match(str(row['release'])):
        problems.append(f'{path}: bad release {row["release"]!r}')
    return problems


def read_lock(lock_file):
    try:
        with open(lock_file, encoding='utf-8') as f:
            lock = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f'{lock_file} cannot be read: {e}')
    if not isinstance(lock, dict) or not TAG_OK.match(str(lock.get('release', ''))):
        raise Refused(f'{lock_file}: no "release" (the release tag that holds the assets)')
    problems, seen = [], set()
    for row in lock.setdefault('files', []):
        path = row.get('path', '')
        problems += row_problems(row)
        if path in seen:
            problems.append(f'{path} is listed twice')
        seen.add(path)
    if problems:
        raise Refused(f'{lock_file}: ' + '; '.join(problems))
    return lock


def write_lock(lock, lock_file):
    lock['files'].sort(key=lambda r: r['path'])
    with open(lock_file + '.tmp', 'w', encoding='utf-8') as f:
        f.write(json.dumps(lock, indent=1) + '\n')
    os.replace(lock_file + '.tmp', lock_file)


class GitHub:
    """The two calls stage and check make, with the job's token. Authorization is never sent on to the redirect."""
    def __init__(self, token, repo):
        if not token:
            raise Refused('no GH_TOKEN or GITHUB_TOKEN: stage asks GitHub for the asset list on every run')
        self.token, self.repo = token, repo

    def _get(self, url, accept):
        req = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': 'fork-ci-gradle-cache'})
        req.add_unredirected_header('Authorization', f'Bearer {self.token}')
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def assets(self, tag):
        rel = json.loads(self._get(f'{API}/repos/{self.repo}/releases/tags/{tag}', 'application/vnd.github+json'))
        out, page = {}, 1
        while True:
            got = json.loads(self._get(f'{API}/repos/{self.repo}/releases/{rel["id"]}/assets?per_page=100&page={page}',
                                       'application/vnd.github+json'))
            out.update({a['name']: a for a in got})
            if len(got) < 100:
                return out
            page += 1

    def download(self, asset):
        return self._get(asset['url'], 'application/octet-stream')


def listed_assets(lock, rows, gh):
    """{release: {asset name: asset}} for every release a row names, and what is wrong with the rows' assets."""
    assets = {tag: gh.assets(tag) for tag in sorted({release_of(lock, r) for r in rows})}
    problems = []
    for row in rows:
        tag = release_of(lock, row)
        a = assets[tag].get(asset_name(row))
        if a is None:
            problems.append(f'{row["path"]}: the release {tag} has no asset {asset_name(row)} '
                            '(put it back: gradle_cache.py publish; README.md "Troubleshooting")')
        elif a.get('state', 'uploaded') != 'uploaded':
            problems.append(f'{row["path"]}: the upload of {asset_name(row)} to {tag} never finished (state '
                            f'{a["state"]}; gradle_cache.py publish uploads it again)')
        elif a.get('digest') and a['digest'] != 'sha256:' + row['sha256']:
            problems.append(f'{row["path"]}: the asset has digest {a["digest"]}, the lock says sha256:{row["sha256"]}')
    return assets, problems


def prune_cache(cache, keep, max_age_days, say, now):
    """#1330 and #1324: remove a killed run's .part- files and entries no lock row here used for max_age_days. An entry
    another runner removes while this run reads it is a cache miss there, so removing is never unsafe."""
    removed, unknown = 0, []
    for name in sorted(os.listdir(cache)):
        path = os.path.join(cache, name)
        try:
            age = now - os.lstat(path).st_mtime
        except FileNotFoundError:
            continue
        if name.startswith('.part-'):
            stale = age > PART_AGE
        elif SHA_OK.match(name):
            stale = name not in keep and age > max_age_days * 86400
        else:
            unknown.append(name)
            continue
        if stale:
            try:
                os.remove(path)
                removed += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                say(f'WARN: cannot remove {path}: {e}')
    if removed:
        say(f'cache {cache}: removed {removed} files (killed downloads, or unused for {max_age_days} days)')
    if unknown:
        say(f'cache {cache}: left {len(unknown)} files this tool did not write: {", ".join(unknown[:5])}')


def cached(cache, row, say):
    """The verified bytes of a cache entry, or None (a miss). Read once: the bytes staged are the bytes checked."""
    path = os.path.join(cache, row['sha256'])
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except FileNotFoundError:
        return None
    if sha256(data) != row['sha256']:
        say(f'WARN: {path} does not match its sha256; downloading it again')
        return None
    try:
        os.utime(path)   # its age restarts: prune_cache keeps what any lock still uses
    except OSError:
        pass
    return data


def cache_dir(path):
    """The action passes cache-dir as written, so `~` is expanded here. Any other relative path is refused: it would be a
    folder in the caller's checkout, which the checkout step wipes, so the cache would never be warm."""
    full = os.path.expanduser(path)
    if not os.path.isabs(full):
        raise Refused(f'cache dir {path!r} must be an absolute path or start with ~/ (a relative one lands in the checkout)')
    return full


def write_atomic(dest_dir, name, data):
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix='.part-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, os.path.join(dest_dir, name))
    except BaseException:
        os.unlink(tmp)
        raise


def stage(lock_file, out, gh, cache=CACHE, max_age_days=30, say=print, now=None):
    """OUT appears whole or not at all: the files go into a sibling folder that is renamed to OUT at the end."""
    lock = read_lock(lock_file)
    rows = lock['files']
    if not rows:
        raise Refused(f'{lock_file} lists no files')
    if os.path.exists(out) and os.listdir(out):
        raise Refused(f'{out} is not empty: stage writes only into an empty folder, so nothing old can stand in')
    if max_age_days < 1:
        raise Refused(f'cache max age {max_age_days} days: at least 1')
    assets, problems = listed_assets(lock, rows, gh)
    if problems:
        raise Refused('\n'.join(problems))
    try:   # the cache saves downloads only: one that cannot be made, read or written is a warning, never a red stage
        os.makedirs(cache, exist_ok=True)
        prune_cache(cache, {r['sha256'] for r in rows}, max_age_days, say, time.time() if now is None else now)
    except OSError as e:
        say(f'WARN: cache {cache} cannot be used ({e}); every file is downloaded')
        cache = None
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    building = tempfile.mkdtemp(dir=os.path.dirname(out), prefix=f'.{os.path.basename(out)}.part-')
    downloaded = 0
    try:
        for row in rows:
            data = None
            if cache:
                try:
                    data = cached(cache, row, say)
                except OSError as e:
                    say(f'WARN: cache {cache} cannot be read ({e}); the rest is downloaded and not cached')
                    cache = None
            if data is None:
                data = gh.download(assets[release_of(lock, row)][asset_name(row)])
                if sha256(data) != row['sha256']:
                    raise Refused(f'{row["path"]}: the download has sha256 {sha256(data)}, the lock says {row["sha256"]}')
                downloaded += 1
                if cache:
                    try:
                        write_atomic(cache, row['sha256'], data)
                    except OSError as e:
                        say(f'WARN: cache {cache} cannot be written ({e}); the rest is downloaded and not cached')
                        cache = None
            dest = os.path.join(building, *row['path'].split('/'))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(data)
        if os.path.isdir(out):
            os.rmdir(out)   # empty, checked above
        os.rename(building, out)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    say(f'staged {len(rows)} files from {lock_file} in {out} ({downloaded} downloaded, {len(rows) - downloaded} from the cache)')


def check(lock_file, gh, say=print):
    lock = read_lock(lock_file)
    assets, problems = listed_assets(lock, lock['files'], gh)
    if problems:
        raise Refused('\n'.join(problems))
    for tag, listed in assets.items():
        say(f'release {tag}: {len(listed)} of {MAX_ASSETS} assets')
    say(f'{lock_file}: all {len(lock["files"])} assets are listed, finished and match')


def version_dir(coord):
    parts = coord.split(':')
    if len(parts) != 3 or not all(parts):
        raise Refused(f'{coord!r} is not group:artifact:version')
    group, artifact, version = parts
    return '/'.join([*group.split('.'), artifact, version])


def run_gh(cmd):
    if os.environ.get('FORK_CI_TEST_MODE'):
        raise Refused('a test reached the real gh runner')
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def release_assets(run, repo, tag):
    """{asset name: {name, state, size, digest}} of the release, every page; None when it does not exist."""
    try:
        rel = json.loads(run(['gh', 'api', f'repos/{repo}/releases/tags/{tag}']))
    except subprocess.CalledProcessError as e:
        if 'HTTP 404' in (e.stderr or ''):
            return None
        raise Refused(f'gh api releases/tags/{tag} failed: {(e.stderr or "").strip()[:300]}')
    lines = run(['gh', 'api', '--paginate', f'repos/{repo}/releases/{rel["id"]}/assets?per_page=100',
                 '--jq', '.[] | {name, state, size, digest}'])
    return {a['name']: a for a in map(json.loads, filter(None, lines.splitlines()))}


def publish(lock_file, coords, repo, m2=M2, run=None, say=print, max_assets=MAX_ASSETS):
    """Upload first, then the lock: a crash in between leaves assets nobody pins, and the next run only writes the rows.
    Every row is written with its release, so a later change of the lock's "release" moves only new uploads."""
    run = run or run_gh
    lock = read_lock(lock_file)
    tag = lock['release']
    have = {r['path']: r for r in lock['files']}
    want, seen = [], set()   # (row, source file, size): every build file of the versions, locked or new, once
    for coord in coords:
        rel = version_dir(coord)
        folder = os.path.join(m2, *rel.split('/'))
        names = sorted(n for n in (os.listdir(folder) if os.path.isdir(folder) else [])
                       if WANTED.search(n) and not NOT_WANTED.search(n))
        if not any(n.endswith('.jar') for n in names) or not any(n.endswith('.pom') for n in names):
            raise Refused(f'{folder} has no jar and pom: publish {coord} to ~/.m2 first')
        for n in names:
            with open(os.path.join(folder, n), 'rb') as f:
                data = f.read()
            row = {'path': f'{rel}/{n}', 'sha256': sha256(data), 'release': tag}
            if row['path'] in seen:   # a version named twice (or as g.a and g/a): one row, never two that break the lock
                continue
            seen.add(row['path'])
            old = have.get(row['path'])
            if old and old['sha256'] != row['sha256']:
                raise Refused(f'{row["path"]} is in the lock with other bytes ({old["sha256"][:16]}, ~/.m2 has '
                              f'{row["sha256"][:16]}): a version is never reused; publish a new version')
            want.append((dict(row, release=release_of(lock, old)) if old else row, os.path.join(folder, n), len(data)))
    problems = [p for row, _, _ in want for p in row_problems(row)]
    if problems:
        raise Refused('the lock could not read these rows, so nothing is uploaded: ' + '; '.join(problems)
                      + ' (a version takes only [A-Za-z0-9._-])')
    listed = {}   # the lock's release too: after it moved to a new tag, a repair of an older row still runs
    for t in sorted({row['release'] for row, _, _ in want} | {tag}):
        listed[t] = release_assets(run, repo, t)
        if listed[t] is None and t != tag:
            raise Refused(f'locked rows name the release {t}, which is gone: restore it by hand (README.md)')
    # A row without "release" is read from the lock's, and publish writes that tag into it. That is only true when its
    # asset is in the lock's release now: after the lock moved to a new tag the asset is in an older one, and which one
    # cannot be known here, so nothing is guessed, uploaded or written (lens S2 round 2).
    unnamed = [r['path'] for r in lock['files'] if 'release' not in r and asset_name(r) not in (listed[tag] or {})]
    if unnamed:
        raise Refused(f'{len(unnamed)} locked rows have no "release" of their own and the release {tag} '
                      f'{"has no asset for them" if listed[tag] is not None else "does not exist yet"}: '
                      f'{", ".join(unnamed[:10])}{" ..." if len(unnamed) > 10 else ""}. Write into each row the tag '
                      'that holds its asset ("release": "<tag>"), or set the lock\'s "release" back and publish a '
                      'locked version first (README.md "A full release"). Nothing was uploaded and the lock is unchanged')
    uploads = []
    for row, src, size in want:
        a = (listed[row['release']] or {}).get(asset_name(row))
        good = a and a.get('state', 'uploaded') == 'uploaded' and a.get('size', size) == size and \
            a.get('digest') in (None, 'sha256:' + row['sha256'])
        if not good:
            uploads.append((row, src, a is not None))
    for t in sorted({row['release'] for row, _, _ in uploads}):
        count = len(listed[t] or {})
        adds = sum(1 for row, _, clobber in uploads if row['release'] == t and not clobber)
        if count + adds > max_assets:
            raise Refused(f'the release {t} has {count} assets and this publish adds {adds}: GitHub allows {max_assets} '
                          f'per release (asset limit). Set "release" in {lock_file} to a new tag; rows keep theirs')
    if listed[tag] is None and any(row['release'] == tag for row, _, _ in uploads):
        run(['gh', 'release', 'create', tag, '-R', repo, '--prerelease', '--latest=false', '--title', 'CI: Maven local jars',
             '--notes', 'The jars a Gradle build takes from a Maven local repository, named <sha256>-<file>. '
             'Pinned by a lock file; written only by fork-ci/actions/gradle-cache/gradle_cache.py publish.'])
    with tempfile.TemporaryDirectory() as d:
        for row, src, clobber in uploads:
            named = os.path.join(d, asset_name(row))
            shutil.copyfile(src, named)
            run(['gh', 'release', 'upload', row['release'], '-R', repo, *(['--clobber'] if clobber else []), named])
            say(f'uploaded {asset_name(row)} to {row["release"]}' + (' (replaced an unfinished or other one)' if clobber else ''))
    new = [row for row, _, _ in want if row['path'] not in have]
    for row in lock['files']:
        row.setdefault('release', tag)
    lock['files'].extend(new)
    write_lock(lock, lock_file)
    say(f'{lock_file}: {len(new)} new rows, {len(uploads)} uploads' + ('' if new or uploads else ' (already complete)'))


def prune_lock(lock_file, keep, say=print):
    lock = read_lock(lock_file)
    dirs = {version_dir(c): c for c in keep}
    present = {r['path'].rsplit('/', 1)[0] for r in lock['files']}
    missing = [c for d, c in dirs.items() if d not in present]
    if missing or not dirs:
        raise Refused(f'--keep {", ".join(missing) or "(nothing)"}: the lock has no rows for it, so nothing is dropped')
    dropped = sorted({r['path'].rsplit('/', 1)[0] for r in lock['files']} - set(dirs))
    lock['files'] = [r for r in lock['files'] if r['path'].rsplit('/', 1)[0] in dirs]
    write_lock(lock, lock_file)
    say(f'{lock_file}: dropped {len(dropped)} versions ({", ".join(dropped) or "none"}); their assets stay in the release')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('stage', 'check', 'publish', 'prune-lock'):
        p = sub.add_parser(name)
        p.add_argument('--lock', required=True)
        if name != 'prune-lock':
            p.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY'))
    sub.choices['stage'].add_argument('--out', required=True)
    sub.choices['stage'].add_argument('--cache', default=CACHE)
    sub.choices['stage'].add_argument('--cache-max-age-days', type=int, default=30)
    sub.choices['publish'].add_argument('--m2', default=M2, help='the Maven local repository (default ~/.m2/repository)')
    sub.choices['publish'].add_argument('coords', nargs='+')
    sub.choices['prune-lock'].add_argument('--keep', nargs='+', required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd != 'prune-lock' and not args.repo:
            raise Refused('no --repo and no GITHUB_REPOSITORY')
        token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if args.cmd == 'stage':
            stage(args.lock, args.out, GitHub(token, args.repo), cache=cache_dir(args.cache),
                  max_age_days=args.cache_max_age_days)
        elif args.cmd == 'check':
            check(args.lock, GitHub(token, args.repo))
        elif args.cmd == 'publish':
            publish(args.lock, args.coords, args.repo, m2=args.m2)
        else:
            prune_lock(args.lock, args.keep)
    except Refused as e:
        print(f'::error::gradle-cache {args.cmd}: {e}', file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:   # the release or an asset is gone, or the token cannot read it
        print(f'::error::gradle-cache {args.cmd}: GitHub answered {e.code} for {e.url}', file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, subprocess.CalledProcessError) as e:
        print(f'::error::gradle-cache {args.cmd}: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
