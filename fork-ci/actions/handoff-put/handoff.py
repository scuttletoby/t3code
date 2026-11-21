#!/usr/bin/env python3
"""The hand-off pair's code (handoff-put and handoff-get, README.md next to this file): a file handed from one job to a
later job of the same run, keyed on the commit both jobs checked out.

  python3 -I handoff.py put --name N --path P --stage-dir D [--retention-days R]
      Wraps the file P as {format, name, sha, payload} in a new folder under D (written to a temporary name, then
      renamed). Outputs staged (true only once the file is complete), file, artifact (<name>-<sha>), sha, retention,
      overwrite (true only in a re-run attempt, $GITHUB_RUN_ATTEMPT > 1), reason.
  python3 -I handoff.py uploaded --staged S --outcome O --artifact A [--why W]
      Outputs ok (true only when staged and the upload succeeded) and reason.
  python3 -I handoff.py where --name N --path P --download-dir D [--refused]
      Removes P (so after the action, P exists only when ok is true), makes a new empty folder under D for the
      download. Outputs ready, artifact, sha, dir, reason. --refused (git-clean refused the checkout): only removes P.
  python3 -I handoff.py get --name N --path P --ready R --outcome O --dir D [--why W]
      Reads D/handoff.json and writes its payload to P (temporary name, then renamed) only when the download succeeded
      and the file is a hand-off of this name whose sha is this checkout's HEAD. Outputs ok, sha, reason.

Every command writes its outputs to $GITHUB_OUTPUT and exits 0: a missing or refused hand-off is ok=false, never a
failed step (an error nothing below expects too: main() writes the command's not-ok outputs and one plain line). The project checks the payload itself (its own keys and shape); this checks only the format, the name and
the commit.
"""
import argparse, json, os, re, stat, subprocess, sys, tempfile

FORMAT = 'fork-ci-handoff/1'
FILE = 'handoff.json'   # the one file inside the artifact
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,59}')
SHA = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
MAX_PAYLOAD = 16 * 1024 * 1024    # bytes of the handed file
MAX_FILE = 8 * MAX_PAYLOAD        # bytes of handoff.json read back (JSON escapes grow a payload)

# Every git call: no global or system config or attributes (on a self-hosted runner an earlier job may have written
# ~/.gitconfig), and no hooks, fsmonitor command, commit-graph or replace refs from the repository. The same as
# ../git-clean/git_clean.py's (git_clean.test.py pins them); the action's first step refused any other .git config.
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_ATTR_NOSYSTEM': '1'}
GIT_SAFE = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'core.commitGraph=false',
            '-c', 'core.attributesFile=/dev/null', '--no-replace-objects')


class No(Exception):
    """The hand-off is not there or not usable: ok=false with this reason."""


def head():
    """This checkout's HEAD commit (the working directory's repository)."""
    r = subprocess.run(['git', *GIT_SAFE, 'rev-parse', '--verify', '--quiet', 'HEAD^{commit}'], capture_output=True,
                       env={**os.environ, **GIT_ENV})
    sha = r.stdout.decode('ascii', 'replace').strip()
    if r.returncode != 0 or not SHA.fullmatch(sha):
        raise No('no HEAD commit in the checkout (run it after actions/checkout)')
    return sha


def checked_name(name):
    if not NAME.fullmatch(name or ''):
        raise No(f'name {name!r} is not 1-60 of A-Z a-z 0-9 . _ - (starting with a letter or digit)')
    return name


def checked_path(path):
    """A path relative to the workspace (the working directory): not absolute, no '..', not empty, and its folder,
    symlinks followed, inside the workspace (a symlinked folder must not lead the read, the removal or the write
    elsewhere). The last part itself is never followed: it is opened O_NOFOLLOW, removed or renamed over."""
    parts = (path or '').split('/')
    if not path or path.startswith('/') or '\0' in path or '..' in parts or all(p in ('', '.') for p in parts):
        raise No(f'path {path!r} is not a file path relative to the workspace')
    root = os.path.realpath('.')
    folder = os.path.realpath(os.path.dirname(path) or '.')
    if folder != root and not folder.startswith(root.rstrip(os.sep) + os.sep):
        raise No(f'path {path!r} leads outside the workspace through a symlinked folder: refused')
    return path


def file_mode():
    """The mode a normally written file gets: 0666 less the umask (mkstemp's is 0600)."""
    umask = os.umask(0o022)
    os.umask(umask)
    return 0o666 & ~umask


def write_atomic(path, data):
    """data at path, or nothing at path: written under a temporary name in the same folder, then renamed over it."""
    folder = os.path.dirname(path) or '.'
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.handoff-')
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), file_mode())   # a container step running as another user can read path
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_regular(path, limit, what):
    """The bytes of a regular file (never through a symlink), at most limit bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise No(f'no {what} at {path}')
    except OSError as e:
        raise No(f'{what} {path} cannot be read ({e.strerror or e}; a symlink is refused)')
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise No(f'{what} {path} is not a regular file')
        if st.st_size > limit:
            raise No(f'{what} {path} is {st.st_size} bytes, over {limit}')
        data = f.read(limit + 1)
    if len(data) > limit:
        raise No(f'{what} {path} is over {limit} bytes')
    return data


def envelope(name, sha, payload):
    return json.dumps({'format': FORMAT, 'name': name, 'sha': sha, 'payload': payload},
                      ensure_ascii=False).encode('utf-8') + b'\n'


WS = re.compile(r'[ \t\n\r]*')
SCALAR = json.JSONDecoder()   # strict: a raw control character in a string is an error


def flat_object(text):
    """The JSON object text holds, when each of its values is a string, number, true, false or null (a hand-off file
    is such an object). A bounded parse: never more than 5 keys, and a nested value is refused before anything parses
    it, so no file can recurse (a file nested 200000 deep is a plain refusal, not a RecursionError). A key twice, data
    after the object or anything JSON does not allow: ValueError (or No)."""
    i = WS.match(text).end()
    if text[i:i + 1] != '{':
        raise No('the hand-off file is not a JSON object: refused')
    doc, i = {}, WS.match(text, i + 1).end()
    if text[i:i + 1] == '}':
        i += 1
    else:
        while True:
            if len(doc) > 4:
                raise No(f'the hand-off file is not a {FORMAT} file (too many keys): refused')
            if text[i:i + 1] != '"':
                raise ValueError('a key must be a string')
            key, i = SCALAR.raw_decode(text, i)
            i = WS.match(text, i).end()
            if text[i:i + 1] != ':':
                raise ValueError('a key must be followed by a colon')
            i = WS.match(text, i + 1).end()
            if text[i:i + 1] in ('{', '['):
                raise No('the hand-off file holds a nested value: refused')
            if key in doc:
                raise No(f'the hand-off file has the key {shown(key)} twice: refused')
            doc[key], i = SCALAR.raw_decode(text, i)
            i = WS.match(text, i).end()
            after, i = text[i:i + 1], WS.match(text, i + 1).end()
            if after == '}':
                break
            if after != ',':
                raise ValueError('a value must be followed by a comma or the end of the object')
    if WS.match(text, i).end() != len(text):
        raise ValueError('data after the object')
    return doc


def unwrap(data, name, sha):
    """The payload of a hand-off file, if it is one of this name and commit; No otherwise."""
    try:
        doc = flat_object(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):   # JSONDecodeError, a number of over 4300 digits
        raise No('the hand-off file is not JSON (garbled or cut short): refused')
    if not isinstance(doc, dict) or set(doc) != {'format', 'name', 'sha', 'payload'} or doc['format'] != FORMAT:
        raise No(f'the hand-off file is not a {FORMAT} file: refused')
    if doc['name'] != name:
        raise No(f'the hand-off file is for {shown(doc["name"])}, not {name}: refused')
    if doc['sha'] != sha:
        raise No(f'the hand-off file is for commit {shown(doc["sha"])}, not this checkout HEAD {sha}: refused')
    if not isinstance(doc['payload'], str):
        raise No('the hand-off payload is not text: refused')
    try:
        return doc['payload'].encode('utf-8', 'strict')
    except UnicodeEncodeError:      # a lone surrogate (\ud800), which put never writes
        raise No('the hand-off payload is not valid text (a lone surrogate): refused')


def shown(value, limit=80):
    """One printable line: control characters and line separators escaped."""
    text = value if isinstance(value, str) else json.dumps(value)
    return ''.join(c if c.isprintable() else ascii(c)[1:-1] for c in text[:limit])


def output(**values):
    """Each value on one line (a newline in an input cannot add an output)."""
    lines = ''.join(f'{k}={shown(str(v), None)}\n' for k, v in values.items())
    target = os.environ.get('GITHUB_OUTPUT')
    if target:
        with open(target, 'a', encoding='utf-8') as f:
            f.write(lines)
    else:
        sys.stdout.write(lines)


def say(ok, text, warn=False):
    level = 'notice' if ok or not warn else 'warning'
    print(f'::{level}::handoff: {text}')


def put(a):
    out = {'staged': 'false', 'file': '', 'artifact': '', 'sha': '', 'retention': '', 'overwrite': 'false'}
    try:
        name, path = checked_name(a.name), checked_path(a.path)
        if not re.fullmatch(r'[1-9][0-9]?', a.retention_days or '') or int(a.retention_days) > 90:
            raise No(f'retention-days {a.retention_days!r} is not 1-90')
        sha = head()
        try:
            payload = read_regular(path, MAX_PAYLOAD, 'file').decode('utf-8')
        except UnicodeDecodeError:
            raise No(f'file {path} is not UTF-8 text')
        os.makedirs(a.stage_dir, exist_ok=True)
        staged = os.path.join(tempfile.mkdtemp(dir=a.stage_dir, prefix='handoff-put-'), FILE)
        write_atomic(staged, envelope(name, sha, payload))
        # A first attempt never overwrites: a second job (or matrix leg) handing off the same name in this run then
        # fails its upload and reports ok false, instead of silently replacing the first one's hand-off. A re-run
        # attempt must overwrite its own earlier attempt's artifact.
        rerun = re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ATTEMPT', '')) and \
            os.environ['GITHUB_RUN_ATTEMPT'] != '1'
        out.update(staged='true', file=staged, artifact=f'{name}-{sha}', sha=sha, retention=a.retention_days,
                   overwrite='true' if rerun else 'false', reason=f'staged {path} as {name}-{sha}')
        say(True, out['reason'])
    except (No, OSError) as e:
        out['reason'] = str(e)
        say(False, f'nothing handed off: {e}', warn=True)
    output(**out)


def uploaded(a):
    if a.staged != 'true':
        output(ok='false', reason=a.why or 'nothing was staged (the step before did not finish)')
    elif a.outcome != 'success':
        why = f'upload {a.outcome or "did not run"}'
        if a.overwrite != 'true':
            why += (f' (in a first attempt also what a second job or matrix leg of this run handing off {a.artifact} '
                    f'gets: one producer per name per run)')
        say(False, f'nothing handed off: {why}', warn=True)
        output(ok='false', reason=why)
    else:
        say(True, f'handed off {a.artifact}')
        output(ok='true', reason=f'uploaded {a.artifact}')


def where(a):
    out = {'ready': 'false', 'artifact': '', 'sha': '', 'dir': ''}
    try:
        name, path = checked_name(a.name), checked_path(a.path)
        if os.path.isdir(path) and not os.path.islink(path):
            raise No(f'path {path} is a folder')
        try:
            os.unlink(path)      # an older copy must not be read as this run's hand-off
        except FileNotFoundError:
            pass
        if a.refused:
            raise No("git-clean refused the checkout's git config (above)")
        sha = head()
        os.makedirs(a.download_dir, exist_ok=True)
        out.update(ready='true', artifact=f'{name}-{sha}', sha=sha,
                   dir=tempfile.mkdtemp(dir=a.download_dir, prefix='handoff-get-'), reason='')
    except No as e:
        out['reason'] = str(e)
        say(False, f'no hand-off: {e}', warn=True)
    except OSError as e:
        out['reason'] = f'{e.strerror or e}: {e.filename}'
        say(False, f'no hand-off: {out["reason"]}', warn=True)
    output(**out)


def get(a):
    out = {'ok': 'false', 'sha': ''}
    try:
        if a.ready != 'true':
            raise No(f'not looked for: {a.why or "the step before did not finish"}')
        name, path, sha = checked_name(a.name), checked_path(a.path), head()
        out['sha'] = sha
        if a.outcome != 'success':
            raise No(f'no hand-off {name}-{sha} in this run (download {a.outcome or "did not run"}): the consumer '
                     f'falls back')
        payload = unwrap(read_regular(os.path.join(a.dir, FILE), MAX_FILE, 'hand-off file'), name, sha)
        write_atomic(path, payload)
        out.update(ok='true', reason=f'{path} from {name}-{sha}')
        say(True, out['reason'])
    except (No, OSError) as e:
        out['reason'] = str(e)
        say(False, str(e), warn=not isinstance(e, No) or 'refused' in str(e))
    output(**out)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = p.add_subparsers(dest='command', required=True)
    c = sub.add_parser('put')
    c.add_argument('--name', required=True)
    c.add_argument('--path', required=True)
    c.add_argument('--stage-dir', required=True)
    c.add_argument('--retention-days', default='3')
    c = sub.add_parser('uploaded')
    c.add_argument('--staged', default='')
    c.add_argument('--outcome', default='')
    c.add_argument('--artifact', default='')
    c.add_argument('--overwrite', default='')
    c.add_argument('--why', default='', help="the stage's reason when nothing was staged")
    c = sub.add_parser('where')
    c.add_argument('--name', required=True)
    c.add_argument('--path', required=True)
    c.add_argument('--download-dir', required=True)
    c.add_argument('--refused', action='store_true', help='git-clean refused the checkout: clear path, no git')
    c = sub.add_parser('get')
    c.add_argument('--name', required=True)
    c.add_argument('--path', required=True)
    c.add_argument('--ready', default='')
    c.add_argument('--outcome', default='')
    c.add_argument('--dir', default='')
    c.add_argument('--why', default='', help="where's reason when it was not ready")
    a = p.parse_args(argv)
    try:
        {'put': put, 'uploaded': uploaded, 'where': where, 'get': get}[a.command](a)
    except Exception as e:      # nothing above expects it: still the not-ok outputs and one plain line, exit 0
        reason = f'the hand-off could not be {"read" if a.command in ("where", "get") else "made"} ' \
                 f'({type(e).__name__}): refused'
        say(False, reason, warn=True)
        output(**NOT_OK[a.command], reason=reason)


# The outputs that mean "no hand-off" for each command (main's last resort).
NOT_OK = {'put': {'staged': 'false'}, 'uploaded': {'ok': 'false'}, 'where': {'ready': 'false'},
          'get': {'ok': 'false', 'sha': ''}}


if __name__ == '__main__':
    main()
