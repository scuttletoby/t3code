"""Fake systemctl and docker for tests/sweep.test.py: they answer from a JSON state file and record each call in it.

  python3 fake_tools.py STATE_FILE systemctl|docker ARGS...

State: {"proc": fake /proc folder, "units": {name: {MainPID, ControlPID, Description, ActiveState}},
"systemdDown": bool, "stuckUnits": [names], "containers": [docker inspect objects], "dockerDown": bool,
"vanishOnRm": [ids], "calls": [[tool, args...]]}. Stopping a unit removes the fake processes in it.
"""
import fnmatch, json, os, shutil, sys


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
        for c in containers:
            if c.get('hideFromPs'):
                continue
            if label is not None and label not in (c['Config'].get('Labels') or {}):
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
        cid = args[-1]
        match = [c for c in containers if c['Id'].startswith(cid)]
        if not match or cid in state.get('vanishOnRm', []):
            state['containers'] = [c for c in containers if not c['Id'].startswith(cid)]
            return 1, '', f'Error response from daemon: No such container: {cid}'
        state['containers'] = [c for c in containers if not c['Id'].startswith(cid)]
        return 0, cid + '\n', ''
    return 1, '', f'fake docker: unknown verb {verb}'


def main():
    path, tool, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    state = load(path)
    state.setdefault('calls', []).append([tool, *args])
    code, out, err = (systemctl if tool == 'systemctl' else docker)(state, args)
    save(path, state)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == '__main__':
    sys.exit(main())
