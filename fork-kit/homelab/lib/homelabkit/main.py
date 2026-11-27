"""homelab: the machines of a home lab, from the private machine file (fork-kit/homelab/README.md).

  homelab check [--json]          the machine file's homelab section, the lease folder, and whether each machine
                                  answers on its port; exit 1 on a problem, 2 when the machine file is refused
  homelab lease take MACHINE --holder WHO [--minutes N] [--purpose TEXT]
                                  lease a test machine to WHO (the project and agent) for N minutes (default: the
                                  machine file's homelab.leases.defaultMinutes); exit 1 while it is leased, also
                                  to WHO (renew extends a lease)
  homelab lease renew MACHINE --holder WHO [--minutes N]
                                  the holder's live lease now ends N minutes from now; exit 1 when it is not WHO's
                                  or has ended
  homelab lease release MACHINE --holder WHO
                                  end WHO's lease; a machine that is not leased is fine (exit 0); exit 1 when it is
                                  someone else's
  homelab lease list [--json]     each machine and its live lease

Exit 2: a bad request, or a machine file or lease folder that cannot be used (the message names it and why).
"""
import argparse, json, socket, sys

from . import config, leases

MACHINE_PATH = None         # the fixed path of the machine file (fork-kit/common/README.md); tests set their own
OWNER_UID = 0               # who must own it


def _settings():
    return config.load(MACHINE_PATH, OWNER_UID)


def _store(s):
    lc = s['leases']
    return leases.Store(lc['dir'], [m['name'] for m in s['machines']], lc['defaultMinutes'], lc['maxMinutes'])


def _reach(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as e:
        return str(e.strerror or e) if not isinstance(e, TimeoutError) else f'no answer in {timeout} s'


def check(as_json):
    s = _settings()
    store = _store(s)
    folder_problem = store.check_folder()
    rows, problems = [], 0 if folder_problem is None else 1
    for m in s['machines']:
        unreachable = _reach(m['host'], m['port'], s['checkTimeoutSeconds'])
        lease = lease_problem = None
        if folder_problem is None:
            try:
                lease = store.lease_of(m['name'])
            except leases.LeaseError as e:
                lease_problem = str(e)
        problems += (unreachable is not None) + (lease_problem is not None)
        rows.append({'name': m['name'], 'host': m['host'], 'port': m['port'], 'note': m['note'],
                     'reachable': unreachable is None, 'problem': unreachable, 'lease': lease,
                     'leaseProblem': lease_problem})
    if as_json:
        print(json.dumps({'machineFile': s['path'], 'ok': problems == 0,
                          'leaseDir': {'path': store.folder, 'problem': folder_problem}, 'machines': rows},
                         indent=2, sort_keys=True))
    else:
        print(f'machine file: {s["path"]}')
        print(f'lease folder: {store.folder}: {folder_problem or "ok"}')
        width = max(len(r['name']) for r in rows)
        addr = max(len(f'{r["host"]}:{r["port"]}') for r in rows)
        for r in rows:
            state = 'reachable' if r['reachable'] else f'not reachable ({r["problem"]})'
            held = (f'lease: {r["leaseProblem"]}' if r['leaseProblem'] else
                    f'leased to {leases.describe(r["lease"])}' if r['lease'] else 'free')
            print(f'{r["name"]:<{width}}  {r["host"] + ":" + str(r["port"]):<{addr}}  {state}  {held}')
        print('homelab check: ok' if problems == 0 else
              f'homelab check: {problems} problem{"s" if problems > 1 else ""}')
    return 0 if problems == 0 else 1


def lease_list(as_json):
    s = _settings()
    store = _store(s)
    rows = [{'machine': m['name'], 'lease': store.lease_of(m['name'])} for m in s['machines']]
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        width = max(len(r['machine']) for r in rows)
        for r in rows:
            held = r['lease']
            print(f'{r["machine"]:<{width}}  ' + (f'leased to {leases.describe(held)}' +
                                                   (f': {held["purpose"]}' if held['purpose'] else '')
                                                   if held else 'free'))
    return 0


def parser():
    p = argparse.ArgumentParser(prog='homelab', description=__doc__.split('\n', 1)[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    c = sub.add_parser('check')
    c.add_argument('--json', action='store_true')
    lease = sub.add_parser('lease').add_subparsers(dest='verb', required=True)
    for verb in ('take', 'renew', 'release'):
        v = lease.add_parser(verb)
        v.add_argument('machine')
        v.add_argument('--holder', required=True)
        if verb != 'release':
            v.add_argument('--minutes', type=int)
        if verb == 'take':
            v.add_argument('--purpose', default='')
    lease.add_parser('list').add_argument('--json', action='store_true')
    return p


def main(argv):
    a = parser().parse_args(argv)
    try:
        if a.command == 'check':
            return check(a.json)
        if a.verb == 'list':
            return lease_list(a.json)
        store = _store(_settings())
        if a.verb == 'take':
            lease, old = store.take(a.machine, a.holder, a.minutes, a.purpose)
            replaced = f' (replaces the expired lease of {old["holder"]})' if old else ''
            print(f'{a.machine} leased to {a.holder} until {leases.when(lease["until"])}{replaced}')
        elif a.verb == 'renew':
            lease = store.renew(a.machine, a.holder, a.minutes)
            print(f'{a.machine} leased to {a.holder} until {leases.when(lease["until"])}')
        else:
            print(f'{a.machine}: lease of {a.holder} ended' if store.release(a.machine, a.holder)
                  else f'{a.machine}: not leased')
        return 0
    except leases.Refused as e:
        print(f'homelab: {e}', file=sys.stderr)
        return 1
    except (config.MachineFileError, leases.LeaseError) as e:
        print(f'homelab: {e}', file=sys.stderr)
        return 2
