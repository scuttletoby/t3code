"""homelab.cli: the `homelab` command (fork-kit/homelab/README.md).

  homelab check [--no-reach] [--timeout SECONDS]
  homelab lease take MACHINE --holder HOLDER [--minutes N] [--note TEXT]
  homelab lease release MACHINE --holder HOLDER
  homelab lease list [--json]

Exit codes: 0 done; 1 a check failed, or the machine file or a lease cannot be used; 2 a usage error; 3 another
holder holds the lease (take and release), so a script can wait and try again.
"""
import argparse, json, socket, sys, time

from homelab import check, config, lease

HELD = 3


def _parser():
    p = argparse.ArgumentParser(prog='homelab', description='The machines of this home lab, and test-machine leases.')
    sub = p.add_subparsers(dest='command', required=True)
    c = sub.add_parser('check', help='is the machine file usable here, and does every machine answer?')
    c.add_argument('--no-reach', action='store_true', help='do not try to reach the machines')
    c.add_argument('--timeout', type=float, default=3.0, help='seconds to wait for each machine (default 3)')
    le = sub.add_parser('lease', help='who may use a shared test machine now')
    ls = le.add_subparsers(dest='action', required=True)
    t = ls.add_parser('take', help='take the lease, or renew yours')
    t.add_argument('machine')
    t.add_argument('--holder', required=True, help='who takes it, for example project/agent-name')
    t.add_argument('--minutes', type=int, help="how long (the machine's defaultMinutes when left out)")
    t.add_argument('--note', default='', help='what it is for, shown to others')
    r = ls.add_parser('release', help='give your lease back')
    r.add_argument('machine')
    r.add_argument('--holder', required=True)
    li = ls.add_parser('list', help='every machine that can be leased, and who holds it')
    li.add_argument('--json', action='store_true')
    return p


def _left(lease_, now):
    return f'{max(0, round((lease_["until"] - now) / 60))} min left'


def main(argv, now=None, connect=socket.create_connection):
    """`now` (seconds since 1970) and `connect` are for tests."""
    args = _parser().parse_args(argv)
    now = time.time() if now is None else now
    if args.command == 'check':
        rows = check.run(now, reach=not args.no_reach, timeout=args.timeout, connect=connect)
        for ok, text in rows:
            print(f'{"ok  " if ok else "FAIL"} {text}')
        return 0 if all(ok for ok, _ in rows) else 1
    try:
        cfg = config.load()
        if args.action == 'take':
            got, renewed, expired = lease.take(cfg, args.machine, args.holder, now, args.minutes, args.note)
            if expired:
                print(f'{args.machine}: the lease of {expired["holder"]} ended at {lease.stamp(expired["until"])}')
            print(f'{args.machine}: {"renewed" if renewed else "leased"} to {got["holder"]} until '
                  f'{lease.stamp(got["until"])} ({_left(got, now)})')
        elif args.action == 'release':
            back = lease.release(cfg, args.machine, args.holder, now)
            print(f'{args.machine}: released' if back else f'{args.machine}: was not leased')
        else:
            rows = lease.status(cfg, now)
            if args.json:
                print(json.dumps([{'machine': m['name'], 'lease': cur} for m, cur in rows], sort_keys=True))
            for m, cur in ([] if args.json else rows):
                print(f'{m["name"]}: leased by {cur["holder"]} until {lease.stamp(cur["until"])} ({_left(cur, now)})'
                      f'{" - " + cur["note"] if cur["note"] else ""}' if cur else f'{m["name"]}: free')
        return 0
    except lease.Held as e:
        print(f'homelab: {e}', file=sys.stderr)
        return HELD
    except (config.MachineFileError, lease.LeaseError) as e:
        print(f'homelab: {e}', file=sys.stderr)
        return 1
