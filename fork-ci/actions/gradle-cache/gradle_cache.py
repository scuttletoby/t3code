#!/usr/bin/env python3
"""A Gradle build's Maven local repository from a reviewed lock: the same bytes on every runner.

  gradle_cache.py stage --lock FILE --out DIR       every file the lock lists, sha256-checked, in a new DIR; then
                                                     `./gradlew -Dmaven.repo.local=DIR`. Nothing else is in DIR, so a
                                                     jar the lock does not list fails in Gradle; a listed one that is
                                                     missing or has other bytes fails here, by name.
  gradle_cache.py publish --lock FILE G:A:V ...     by hand, after the version is in ~/.m2: uploads its jar, pom,
                                                     module and SNAPSHOT metadata to the lock's release and adds their
                                                     rows to the lock. A path the lock has with other bytes is refused
                                                     (a version is never reused); a second run uploads and changes
                                                     nothing.

The lock: {"release": "<tag>", "files": [{"path": "<group/artifact/version/file>", "sha256": "<hex>"}]}. The bytes are
the release's assets named `<sha256>-<file name>`, readable with the job's own token (contents: read), so a runner needs
no other secret. An asset with other bytes cannot pass the sha256 check. stage keeps a cache by sha256 and still asks
GitHub for the asset list on every run, so a runner with a warm cache and a fresh runner give the same result.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, urllib.error, urllib.request

API = os.environ.get('GITHUB_API_URL') or 'https://api.github.com'
CACHE = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), 'fork-ci-gradle-cache')
M2 = os.path.expanduser('~/.m2/repository')
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


def row_problems(row):
    """What read_lock refuses in one row. publish checks its new rows with the same rule before the first upload, so it
    can never write a row that turns the whole lock (and every stage) red."""
    path, digest, problems = row.get('path', ''), row.get('sha256', ''), []
    if not PATH_OK.match(path) or '..' in path.split('/'):
        problems.append(f'bad path {path!r}')
    if not SHA_OK.match(digest):
        problems.append(f'{path}: bad sha256 {digest!r}')
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
    """The two calls stage makes, with the job's token. Authorization is never sent on to the redirect (the storage URL)."""
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


def stage(lock_file, out, gh, cache=CACHE, say=print):
    lock = read_lock(lock_file)
    rows = lock['files']
    if not rows:
        raise Refused(f'{lock_file} lists no files')
    if os.path.exists(out) and os.listdir(out):
        raise Refused(f'{out} is not empty: stage writes only into an empty folder, so nothing old can stand in')
    assets = gh.assets(lock['release'])
    problems = []
    for row in rows:
        a = assets.get(asset_name(row))
        if a is None:
            problems.append(f'{row["path"]}: the release {lock["release"]} has no asset {asset_name(row)} (publish it: gradle_cache.py publish)')
        elif a.get('digest') and a['digest'] != 'sha256:' + row['sha256']:
            problems.append(f'{row["path"]}: the asset has digest {a["digest"]}, the lock says sha256:{row["sha256"]}')
    if problems:
        raise Refused('\n'.join(problems))
    os.makedirs(cache, exist_ok=True)
    downloaded = 0
    for row in rows:   # everything is in the cache and checked before OUT gets its first file
        cached = os.path.join(cache, row['sha256'])
        if os.path.isfile(cached):
            with open(cached, 'rb') as f:
                if sha256(f.read()) == row['sha256']:
                    continue
            say(f'WARN: {cached} does not match its sha256; downloading it again')
            os.remove(cached)
        data = gh.download(assets[asset_name(row)])
        if sha256(data) != row['sha256']:
            raise Refused(f'{row["path"]}: the download has sha256 {sha256(data)}, the lock says {row["sha256"]}')
        fd, tmp = tempfile.mkstemp(dir=cache, prefix='.part-')
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, cached)
        downloaded += 1
    for row in rows:
        dest = os.path.join(out, *row['path'].split('/'))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(cache, row['sha256']), dest)
    say(f'staged {len(rows)} files from {lock_file} in {out} ({downloaded} downloaded, {len(rows) - downloaded} from the cache)')


def version_dir(coord):
    parts = coord.split(':')
    if len(parts) != 3 or not all(parts):
        raise Refused(f'{coord!r} is not group:artifact:version')
    group, artifact, version = parts
    return '/'.join([*group.split('.'), artifact, version])


def publish(lock_file, coords, repo, m2=M2, run=None, say=print):
    """Upload first, then the lock: a crash in between leaves assets nobody pins, and the next run only writes the rows."""
    run = run or (lambda cmd: subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
    lock = read_lock(lock_file)
    have = {r['path']: r for r in lock['files']}
    new = []
    for coord in coords:
        rel = version_dir(coord)
        folder = os.path.join(m2, *rel.split('/'))
        names = sorted(n for n in (os.listdir(folder) if os.path.isdir(folder) else [])
                       if WANTED.search(n) and not NOT_WANTED.search(n))
        if not any(n.endswith('.jar') for n in names) or not any(n.endswith('.pom') for n in names):
            raise Refused(f'{folder} has no jar and pom: publish {coord} to ~/.m2 first')
        for n in names:
            with open(os.path.join(folder, n), 'rb') as f:
                row = {'path': f'{rel}/{n}', 'sha256': sha256(f.read())}
            old = have.get(row['path'])
            if old and old['sha256'] != row['sha256']:
                raise Refused(f'{row["path"]} is in the lock with other bytes ({old["sha256"][:16]}, ~/.m2 has '
                              f'{row["sha256"][:16]}): a version is never reused; publish a new version')
            if not old:
                new.append((row, os.path.join(folder, n)))
    problems = [p for row, _ in new for p in row_problems(row)]
    if problems:
        raise Refused('the lock could not read these rows, so nothing is uploaded: ' + '; '.join(problems)
                      + ' (a version takes only [A-Za-z0-9._-])')
    tag = lock['release']
    try:
        listed = json.loads(run(['gh', 'release', 'view', tag, '-R', repo, '--json', 'assets']))['assets']
    except subprocess.CalledProcessError as e:
        if 'release not found' not in (e.stderr or '').lower():
            raise Refused(f'gh release view {tag} failed: {(e.stderr or "").strip()[:300]}')
        run(['gh', 'release', 'create', tag, '-R', repo, '--prerelease', '--latest=false', '--title', 'CI: Maven local jars',
             '--notes', 'The jars a Gradle build takes from a Maven local repository, named <sha256>-<file>. '
             'Pinned by a lock file; written only by fork-ci/actions/gradle-cache/gradle_cache.py publish.'])
        listed = []
    names = {a['name'] for a in listed}
    with tempfile.TemporaryDirectory() as d:
        for row, src in new:
            if asset_name(row) in names:
                continue
            named = os.path.join(d, asset_name(row))
            shutil.copyfile(src, named)
            run(['gh', 'release', 'upload', tag, '-R', repo, named])
            say(f'uploaded {asset_name(row)}')
    lock['files'].extend(row for row, _ in new)
    write_lock(lock, lock_file)
    say(f'{lock_file}: {len(new)} new rows' + ('' if new else ' (already complete)'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('stage')
    s.add_argument('--lock', required=True)
    s.add_argument('--out', required=True)
    s.add_argument('--cache', default=CACHE)
    s.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY'))
    p = sub.add_parser('publish')
    p.add_argument('--lock', required=True)
    p.add_argument('--repo', required=True)
    p.add_argument('--m2', default=M2, help='the Maven local repository to take the files from (default ~/.m2/repository)')
    p.add_argument('coords', nargs='+')
    args = ap.parse_args(argv)
    try:
        if not args.repo:
            raise Refused('no --repo and no GITHUB_REPOSITORY')
        if args.cmd == 'stage':
            stage(args.lock, args.out, GitHub(os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'), args.repo),
                  cache=args.cache)
        else:
            publish(args.lock, args.coords, args.repo, m2=args.m2)
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
