"""homelab.check: `homelab check`, is the machine file's homelab section usable here? (README, homelab check).

Reads the section (every rule of machine_file and config), checks the lease folder and every lease in it, and
asks each machine's port for an answer (a TCP connect, nothing sent). Each finding is one line, `ok` or `FAIL`;
the exit code is 1 when any line fails. It changes nothing.
"""
import os, socket, time

from homelab import config, lease


def _reach(m, timeout, connect):
    where = f'{m["host"]}:{m["port"]}'
    start = time.monotonic()
    try:
        connect((m['host'], m['port']), timeout).close()
    except OSError as e:
        why = e.strerror or str(e) or type(e).__name__
        return False, f'{m["name"]}: {where} does not answer ({why})'
    return True, f'{m["name"]}: {where} answers ({(time.monotonic() - start) * 1000:.0f} ms)'


def run(now, reach=True, timeout=3.0, connect=socket.create_connection):
    """[(ok, text)], one per finding."""
    try:
        cfg = config.load()
    except config.MachineFileError as e:
        return [(False, f'machine file: {e}')]
    n = len(cfg['machines'])
    leasable = [m for m in cfg['machines'] if m['lease']]
    out = [(True, f'machine file: {n} machine{"s" * (n != 1)}, {len(leasable)} can be leased')]
    if leasable:
        try:
            d = lease.lease_dir(cfg)
        except lease.LeaseError as e:
            out.append((False, str(e)))
        else:
            if os.access(d, os.W_OK | os.X_OK):
                out.append((True, f'lease folder {d}: you can take leases'))
            else:
                out.append((False, f'lease folder {d}: you cannot write it; the owner adds you to its group'))
            for m in leasable:
                try:
                    held = lease.status({'leaseDir': d, 'machines': [m]}, now)[0][1]
                except lease.LeaseError as e:
                    out.append((False, str(e)))
                    continue
                out.append((True, f'{m["name"]}: leased by {held["holder"]} until {lease.stamp(held["until"])}'
                            if held else f'{m["name"]}: free'))
    if reach:
        out += [_reach(m, timeout, connect) for m in cfg['machines']]
    return out
