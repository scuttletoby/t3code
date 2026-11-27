"""homelab.check: `homelab check`, is the machine file's homelab section usable here? (README, homelab check).

Reads the section (every rule of machine_file and config), checks the lease folder and every lease in it, and
asks each machine's port for an answer (a TCP connect, nothing sent). Each finding is one line, `ok` or `FAIL`;
the exit code is 1 when any line fails. It changes nothing. `--json` gives the same findings for scripts.
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
        return _row(False, 'reach', f'{m["name"]}: {where} does not answer ({why})', m)
    return _row(True, 'reach', f'{m["name"]}: {where} answers ({(time.monotonic() - start) * 1000:.0f} ms)', m)


def _row(ok, what, text, m=None):
    """One finding. what: machineFile, leaseDir, lease or reach; machine: its name, or None."""
    return {'ok': ok, 'what': what, 'machine': m and m['name'], 'text': text}


def run(now, reach=True, timeout=3.0, connect=socket.create_connection):
    """[finding], one _row per finding, in the order they are printed."""
    try:
        cfg = config.load()
    except config.MachineFileError as e:
        return [_row(False, 'machineFile', f'machine file: {e}')]
    n = len(cfg['machines'])
    leasable = [m for m in cfg['machines'] if m['lease']]
    out = [_row(True, 'machineFile', f'machine file: {n} machine{"s" * (n != 1)}, {len(leasable)} can be leased')]
    if leasable:
        d = cfg['leaseDir']
        try:
            dir_fd = lease.open_dir(cfg)
        except lease.LeaseError as e:
            out.append(_row(False, 'leaseDir', str(e)))
        else:
            try:
                if lease.writable(d, dir_fd):
                    out.append(_row(True, 'leaseDir', f'lease folder {d}: you can take leases'))
                else:
                    out.append(_row(False, 'leaseDir',
                                    f'lease folder {d}: you cannot write it; the owner adds you to its group'))
            finally:
                os.close(dir_fd)
            for m in leasable:
                try:
                    held = lease.status({'leaseDir': d, 'machines': [m]}, now)[0][1]
                except lease.LeaseError as e:
                    out.append(_row(False, 'lease', str(e), m))
                    continue
                out.append(_row(True, 'lease', f'{m["name"]}: leased by {held["holder"]} until '
                                f'{lease.stamp(held["until"])}' if held else f'{m["name"]}: free', m))
    if reach:
        out += [_reach(m, timeout, connect) for m in cfg['machines']]
    return out
