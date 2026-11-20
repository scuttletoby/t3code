"""Fake systemctl and docker for tests/sweep.test.py: they answer from a JSON state file and record each call in it.

  python3 fake_tools.py STATE_FILE systemctl|docker ARGS...

State: {"proc": fake /proc folder, "units": {name: {MainPID, ControlPID, Description, ActiveState}},
"systemdDown": bool, "stuckUnits": [names], "containers": [docker inspect objects], "dockerDown": bool,
"vanishOnRm": [ids], "calls": [[tool, args...]]}. Stopping a unit removes the fake processes in it.
docker also takes `run [-d] [--label K=V]... [--name N] IMAGE ...` (tests/heavy.test.py: a container a test starts),
and `ps --filter label=K` or `label=K=V` as Docker reads it (split at the first "=").
"""
import fcntl, fnmatch, json, os, secrets, shutil, sys, time


def load(path):
    with open(path) as f:
        return json.load(f)


def save(path, state):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, path)


def unit_of(cgroup):
    for part in reversed(cgroup.split('/')):
        if part.endswith(('.service', '.scope')):
            return part
    return ''


def systemctl(state, args):
    if args[:1] != ['--user']:
        return 1, '', 'fake systemctl: only --user'
    if state.get('systemdDown'):
        return 1, '', 'Failed to connect to bus: No medium found'
    args = [a for a in args[1:] if a != '--']
    verb = args[0]
    units = state.setdefault('units', {})
    if verb == 'show':
        names = [a for a in args[1:] if not a.startswith('-') and a != args[args.index('-p') + 1]]
        blocks = []
        for n in names:
            u = units.get(n, {})
            blocks.append('\n'.join([f'Id={n}', f'MainPID={u.get("MainPID", 0)}', f'ControlPID={u.get("ControlPID", 0)}',
                                     f'Description={u.get("Description", n)}',
                                     f'ActiveState={u.get("ActiveState", "inactive")}']))
        return 0, '\n\n'.join(blocks) + '\n', ''
    if verb == 'list-units':
        pattern = args[-1]
        lines = [f'{n} loaded {u.get("ActiveState", "active")} running {u.get("Description", n)}'
                 for n, u in sorted(units.items()) if fnmatch.fnmatchcase(n, pattern)]
        return 0, ''.join(line + '\n' for line in lines), ''
    if verb == 'stop':
        name = args[1]
        if name in state.get('stuckUnits', []):
            return 1, '', f'Job for {name} failed'
        proc = state['proc']
        for pid in os.listdir(proc):
            if pid.isdigit():
                try:
                    with open(os.path.join(proc, pid, 'cgroup')) as f:
                        cg = f.read().strip()[3:]
                except OSError:
                    continue
                if unit_of(cg) == name:
                    shutil.rmtree(os.path.join(proc, pid), ignore_errors=True)
        units.pop(name, None)
        return 0, '', ''
    if verb == 'reset-failed':
        return 0, '', ''
    return 1, '', f'fake systemctl: unknown verb {verb}'


def docker(state, args):
    if state.get('dockerDown'):
        return 1, '', ('Cannot connect to the Docker daemon at unix:///var/run/docker.sock. '
                       'Is the docker daemon running?')
    containers = state.setdefault('containers', [])
    verb = args[0]
    if verb == 'ps':
        out = []
        label = next((a.split('=', 1)[1] for a in args if a.startswith('label=')), None)
        key, has_value, value = (label or '').partition('=')
        for c in containers:
            if c.get('hideFromPs'):
                continue
            labels = c['Config'].get('Labels') or {}
            if label is not None and (key not in labels or (has_value and labels[key] != value)):
                continue
            if '-a' not in args and not c['State']['Running']:
                continue
            out.append(c['Id'])
        return 0, ''.join(i + '\n' for i in out), ''
    if verb == 'inspect':
        ids = [a for a in args[1:] if not a.startswith('--') and a != 'container']
        found = [c for c in containers if c['Id'] in ids and not c.get('goneBeforeInspect')]
        missing = [i for i in ids if i not in [c['Id'] for c in found]]
        err = ''.join(f'Error: No such object: {i}\n' for i in missing)
        return (1 if missing else 0), json.dumps([{k: v for k, v in c.items() if k[0].isupper()} for c in found]), err
    if verb == 'rm':
        refs = args[args.index('--') + 1:] if '--' in args else [a for a in args[1:] if not a.startswith('-')]
        out, err = '', ''
        for cid in refs:
            containers = state['containers']
            # as Docker resolves a reference: the full id, then an exact name, then an id prefix
            match = ([c for c in containers if c['Id'] == cid] or
                     [c for c in containers if c.get('Name', '').lstrip('/') == cid] or
                     [c for c in containers if c['Id'].startswith(cid)])[:1]
            state['containers'] = [c for c in containers if c not in match]
            if not match or match[0]['Id'] in state.get('vanishOnRm', []):
                err += f'Error response from daemon: No such container: {cid}\n'
            else:
                out += cid + '\n'
        return (1 if err else 0), out, err
    if verb == 'run':
        labels, name, rest = {}, '', list(args[1:])
        while rest and rest[0].startswith('-'):
            a = rest.pop(0)
            if a in ('--label', '-l') and rest:
                k, _, v = rest.pop(0).partition('=')
                labels[k] = v
            elif a == '--name' and rest:
                name = rest.pop(0)
        if not rest:
            return 125, '', 'fake docker: run needs an image'
        if name and any(c.get('Name', '').lstrip('/') == name for c in containers):     # as Docker: a 409
            return 125, '', (f'docker: Error response from daemon: Conflict. The container name "/{name}" is '
                             f'already in use.')
        cid = secrets.token_hex(32)
        created = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()) + '.000000000Z'
        containers.append({'Id': cid, 'Name': '/' + (name or cid[:12]), 'Created': created,
                           'State': {'Running': True}, 'Config': {'Labels': labels, 'Image': rest[0]}})
        return 0, cid + '\n', ''
    return 1, '', f'fake docker: unknown verb {verb}'


def main():
    path, tool, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    lock = open(path + '.lock', 'a')        # one call at a time: a test, heavy and heavy's guard may call at once
    fcntl.flock(lock, fcntl.LOCK_EX)
    state = load(path)
    state.setdefault('calls', []).append([tool, *args])
    code, out, err = (systemctl if tool == 'systemctl' else docker)(state, args)
    save(path, state)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == '__main__':
    sys.exit(main())
