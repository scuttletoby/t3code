"""gh-apps-admin: the owner's command for the Apps on this host. Runs as root, in a shell no agent sees.

  gh-apps-admin add-key APP_NAME --app-id N --repo OWNER/NAME [--installation-id N]    the key text on stdin
  gh-apps-admin remove-app APP_NAME
  gh-apps-admin list
  gh-apps-admin self-check

add-key stores the key as /etc/gh-apps/keys/APP_NAME.pem (root:ghkeys 640) and the app as
/etc/gh-apps/apps/APP_NAME.json (root, 644; agents read it to pick the app), finds the installation id if none is
given, drops the app's cached token and makes one test token. Run it again with a new key to rotate.
"""
import contextlib, json, os, re, sys

from . import common as c, install
from .common import Refused

KEY_LIMIT = 16 * 1024
PEM_RE = re.compile(r'^-----BEGIN (?:RSA )?PRIVATE KEY-----\n[A-Za-z0-9+/=\n]+-----END (?:RSA )?PRIVATE KEY-----\n?$')


def say(msg):
    print(f'gh-apps-admin: {c.redact(msg)}', file=sys.stderr)


def read_key():
    if sys.stdin.isatty():
        say('paste the key, then press Ctrl-D (or: gh-apps-admin add-key ... < key.pem)')
    data = sys.stdin.buffer.read(KEY_LIMIT + 1)
    if len(data) > KEY_LIMIT:
        raise Refused('the key is too long for an App private key')
    text = data.decode('ascii', 'replace').replace('\r\n', '\n').strip() + '\n'
    if not PEM_RE.match(text):
        raise Refused('stdin is not a PEM private key (the .pem file GitHub gives for an App)')
    return text


def mint_as_keys_user(ops, name, *flags):
    return ops.run_as(c.KEYS_USER, [c.p(c.MINT), '--app', name, *flags])


def drop_cached_token(name):
    with contextlib.suppress(FileNotFoundError):
        os.unlink(c.p(f'{c.CACHE}/{name}.token'))   # unlink never follows a link, so the folder owner cannot aim it


def add_key(ops, name, app_id, repo, installation_id=None, key_text=None):
    if not c.NAME_RE.match(name or ''):
        raise Refused(f'bad app name {name!r} (lower-case letters, digits, - and _)')
    if not (isinstance(app_id, int) and app_id > 0):
        raise Refused('--app-id must be the App\'s number')
    if not c.REPO_RE.match(repo or ''):
        raise Refused('--repo must be OWNER/NAME')
    if os.geteuid() != c.ROOT_UID:
        raise Refused('run this as root')
    if not os.path.isdir(c.p(c.KEYS)) or c.ids(c.KEYS_USER) is None:
        raise Refused('the gh-apps kit is not installed (run the installer first)')
    key_text = key_text if key_text is not None else read_key()
    others = [n for n in c.app_names() if n != name and c.load_app(n)['repo'].lower() == repo.lower()]
    if others:
        raise Refused(f'{repo} already has app {others[0]}: one app per repository (remove-app it first)')
    install.write_file(ops, f'{c.KEYS}/{name}.pem', key_text, 'root', c.KEYS_USER, 0o640)
    app = {'app_id': app_id, 'installation_id': installation_id, 'repo': repo}
    install.write_file(ops, f'{c.APPS}/{name}.json', json.dumps(app, indent=2) + '\n')
    drop_cached_token(name)
    if installation_id is None:
        code, out, err = mint_as_keys_user(ops, name, '--find-installation')
        if code != 0 or not out.strip().isdigit():
            raise Refused(f'app {name}: cannot find its installation: {err.strip()} (the key and config are '
                          'stored; fix it and run add-key again)')
        app['installation_id'] = int(out.strip())
        install.write_file(ops, f'{c.APPS}/{name}.json', json.dumps(app, indent=2) + '\n')
    code, _out, err = mint_as_keys_user(ops, name, '--check')
    if code != 0:
        raise Refused(f'app {name}: the test token failed: {err.strip()}')
    say(f'app {name} is set up for {repo} (installation {app["installation_id"]}); {err.strip()}')


def remove_app(name):
    if os.geteuid() != c.ROOT_UID:
        raise Refused('run this as root')
    c.load_app(name)
    for path in (f'{c.APPS}/{name}.json', f'{c.KEYS}/{name}.pem'):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(c.p(path))
    drop_cached_token(name)
    say(f'app {name} removed (also delete its key on GitHub if it is no longer used)')


def list_apps():
    names = c.app_names()
    if not names:
        print('no app set up (gh-apps-admin add-key)')
    for n in names:
        try:
            a = c.load_app(n)
            key = 'key ok' if os.path.isfile(c.p(f'{c.KEYS}/{n}.pem')) else 'NO KEY'
            print(f'{n}\t{a["repo"]}\tapp {a["app_id"]}\tinstallation {a["installation_id"]}\t{key}')
        except Refused as e:
            print(f'{n}\tbroken: {e}')
    return 0


def main(argv, ops=None):
    import argparse
    ap = argparse.ArgumentParser(prog='gh-apps-admin', allow_abbrev=False)
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('add-key', allow_abbrev=False)
    a.add_argument('name')
    a.add_argument('--app-id', type=int, required=True)
    a.add_argument('--repo', required=True)
    a.add_argument('--installation-id', type=int)
    r = sub.add_parser('remove-app')
    r.add_argument('name')
    sub.add_parser('list')
    sub.add_parser('self-check')
    args = ap.parse_args(argv)
    try:
        if args.cmd == 'add-key':
            add_key(ops or install.Ops(), args.name, args.app_id, args.repo, args.installation_id)
        elif args.cmd == 'remove-app':
            remove_app(args.name)
        elif args.cmd == 'list':
            return list_apps()
        else:
            return install.self_check_main()
    except Refused as e:
        say(f'refused: {e}')
        return 1
    except OSError as e:
        say(f'failed: {e.strerror or e}')
        return 1
    return 0
