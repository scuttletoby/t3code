"""capacity_rule: the capacity rule of dynamic slots, the kit's evaluator (packages/shared/src/capacity/README.md).

The server's TypeScript evaluator (packages/shared/src/capacityRule.ts, capacityPressure.ts, capacityFormat.ts,
capacityVectors.ts) and this one read the same capacity-rule.json and must give the same answer to every case of
capacity-vectors.json and capacity-format.json (tests/capacity_rule.test.py runs them). Each function here mirrors
the TypeScript function of the same name; change both together. Pure: no I/O but load(), no clock. Agreement rules
(capacity-format.json): a bool is never a number; NaN, Infinity and 1e400 are refused; JSON numbers are doubles.
Plain Python 3 standard library.
"""
import json, math, os

KIT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
MAX_SAFE = 2 ** 53 - 1


def folder():
    """The capacity files: <kit>/capacity once installed (heavy-install copies them), else the repository's."""
    for d in (os.path.join(KIT, 'capacity'),
              os.path.join(os.path.dirname(os.path.dirname(KIT)), 'packages', 'shared', 'src', 'capacity')):
        if os.path.isfile(os.path.join(d, 'capacity-rule.json')):
            return d
    raise FileNotFoundError('capacity-rule.json is neither in the kit nor in packages/shared/src/capacity')


def _no_constant(name):
    raise ValueError(f'{name} is not JSON')


def loads(text):
    return json.loads(text, parse_constant=_no_constant)


def load(name, where=None):
    with open(os.path.join(where or folder(), name), encoding='utf-8') as f:
        return loads(f.read())


def is_num(v):
    """A finite JSON number (a double): never a bool; an int too large for a double is infinity, as in JavaScript."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(float(v))
    except OverflowError:
        return False


def _dumps(v):
    return json.dumps(v, separators=(',', ':'))


# ---------- capacityFormat.ts ----------

def _alternative(value, alt, types, path, errors):
    if alt.startswith('='):
        return isinstance(value, str) and value == alt[1:]
    if alt.startswith('@'):
        if alt[1:] not in types:
            errors.append(f'{path}: the format names an unknown type {alt}')
            return False
        before = len(errors)
        _check(value, types[alt[1:]], types, path, errors)
        return len(errors) == before
    if alt == 'any':
        return True
    if alt == 'null':
        return value is None
    if alt == 'bool':
        return isinstance(value, bool)
    if alt == 'string':
        return isinstance(value, str)
    if alt == 'number':
        return is_num(value)
    if alt == 'number>=0':
        return is_num(value) and value >= 0
    if alt == 'uint':
        return is_num(value) and float(value).is_integer() and 0 <= value <= MAX_SAFE
    errors.append(f'{path}: the format has an unknown type "{alt}"')
    return False


def _check(value, spec, types, path, errors):
    if isinstance(spec, str):
        tries = []
        for alt in spec.split('|'):
            own = []
            if _alternative(value, alt, types, path, own) and not own:
                return
            tries.append(own)
        detail = next((own for own in tries if own), None)
        errors.extend(detail or [f'{path}: expected {spec}, got {_dumps(value)}'])
        return
    if 'array' in spec:
        if not isinstance(value, list):
            errors.append(f'{path}: expected a list')
            return
        if 'minItems' in spec and len(value) < spec['minItems']:
            errors.append(f'{path}: needs at least {spec["minItems"]} entries, has {len(value)}')
        for i, item in enumerate(value):
            _check(item, spec['array'], types, f'{path}[{i}]', errors)
        return
    if not isinstance(value, dict):
        errors.append(f'{path}: expected an object')
        return
    if 'map' in spec:
        for key, item in value.items():
            if not key.startswith('//'):
                _check(item, spec['map'], types, f'{path}.{key}', errors)
        return
    known = set()
    for raw, field in spec['object'].items():
        key = raw[:-1] if raw.endswith('?') else raw
        known.add(key)
        if key not in value:
            if not raw.endswith('?'):
                errors.append(f'{path}.{key}: missing')
            continue
        _check(value[key], field, types, f'{path}.{key}', errors)
    errors.extend(f'{path}.{key}: unknown key' for key in value if key not in known and not key.startswith('//'))


def spec_errors(value, spec, path='value', fmt=None):
    fmt = load('capacity-format.json') if fmt is None else fmt
    if not isinstance(fmt, dict) or fmt.get('format') != 'capacity-format/1' or not isinstance(fmt.get('types'), dict):
        return ['format file: not capacity-format/1']
    errors = []
    _check(value, spec, fmt['types'], path, errors)
    return errors


def parse_rule(value, fmt=None):
    """(rule, []) or (None, the reasons it cannot be used): shape first, then cross-references."""
    errors = spec_errors(value, '@rule', 'rule', fmt)
    if errors:
        return None, errors
    rule = dict(value, kinds={k: v for k, v in value['kinds'].items() if not k.startswith('//')})

    def need(name, group, where):
        if rule['kinds'].get(name, {}).get('group') != group:
            errors.append(f'rule.{where}: "{name}" is not a {group} kind')
    need(rule['unknownKinds']['job'], 'job', 'unknownKinds.job')
    need(rule['unknownKinds']['other'], 'agent', 'unknownKinds.other')
    need(rule['writers']['kind'], 'agent', 'writers.kind')
    need(rule['writers']['buildKind'], 'job', 'writers.buildKind')
    errors.extend(f'rule.kinds.{n}.startMb: must be at least 1' for n, s in rule['kinds'].items() if s['startMb'] < 1)
    if rule['learnedBounds']['minPercent'] > rule['learnedBounds']['maxPercent']:
        errors.append('rule.learnedBounds: minPercent is above maxPercent')
    if rule['readingBounds']['minTotalMb'] > rule['readingBounds']['maxTotalMb']:
        errors.append('rule.readingBounds: minTotalMb is above maxTotalMb')
    return (None, errors) if errors else (rule, [])


def shipped_rule(where=None):
    fmt = load('capacity-format.json', where)
    rule, errors = parse_rule(load('capacity-rule.json', where), fmt)
    if errors:
        raise ValueError('capacity-rule.json: ' + '; '.join(errors))
    return rule


# ---------- capacityPressure.ts ----------

def read_psi(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        return 'unreadable'
    some, full = value.get('someAvg10Pct'), value.get('fullAvg10Pct')
    ok = all(is_num(p) and 0 <= p <= 100 for p in (some, full))
    return {'someAvg10Pct': some, 'fullAvg10Pct': full} if ok else 'unreadable'


def raw_level(prule, r, margin):
    el, sev, calm = prule['elevated'], prule['severe'], prule['calm']
    reads = [p for p in (r['psi'], r['boxPsi']) if isinstance(p, dict)]
    avail100 = r['availableMb'] * 100
    if (any(p['fullAvg10Pct'] >= sev['psiFullAvg10Pct'] for p in reads)
            or avail100 < margin * sev['availableBelowMarginPercent'] or r['osLevel'] == 'severe'):
        return 'severe', False
    swap = r['swap'] if isinstance(r['swap'], dict) else None
    if ('unreadable' in (r['psi'], r['boxPsi'], r['swap'], r['osLevel']) or r['osLevel'] == 'elevated'
            or any(p['someAvg10Pct'] >= el['psiSomeAvg10Pct'] or p['fullAvg10Pct'] >= el['psiFullAvg10Pct']
                   for p in reads)
            or avail100 < margin * el['availableBelowMarginPercent']
            or (swap is not None and swap['freeMb'] * 100 < swap['totalMb'] * el['swapFreeBelowPercent'])):
        return 'elevated', False
    quiet = (all(p['someAvg10Pct'] < calm['psiSomeAvg10Pct'] and p['fullAvg10Pct'] < calm['psiFullAvg10Pct']
                 for p in reads)
             and avail100 >= margin * calm['availableAtLeastMarginPercent']
             and (swap is None or swap['freeMb'] * 100 >= swap['totalMb'] * calm['swapFreeAtLeastPercent']))
    return 'ok', quiet


def next_pressure(prule, raw, previous, now_ms):
    level, calm = raw
    if level == 'severe':
        return {'level': 'severe', 'calmSinceMs': None}
    if previous is None or previous.get('level') == 'ok':
        return {'level': level, 'calmSinceMs': None}

    def step_down(to, seconds):
        if not is_num(now_ms):
            return {'level': previous['level'], 'calmSinceMs': previous.get('calmSinceMs')}
        since = previous.get('calmSinceMs')
        since = since if is_num(since) and since <= now_ms else now_ms
        if now_ms - since >= seconds * 1000:
            return {'level': to, 'calmSinceMs': None}
        return {'level': previous['level'], 'calmSinceMs': since}
    if previous.get('level') == 'severe':
        return step_down('elevated', prule['severeClearSeconds'])
    if level == 'elevated' or not calm:
        return {'level': 'elevated', 'calmSinceMs': None}
    return step_down('ok', prule['elevatedClearSeconds'])


# ---------- capacityRule.ts ----------

def _ceil_div(n, d):
    return math.ceil(n / d)


def _read_facts(rule, facts):
    b = rule['readingBounds']
    total_raw, avail, cores = facts.get('totalMb'), facts.get('availableMb'), facts.get('cpuCores')
    if not (is_num(total_raw) and is_num(avail) and is_num(cores)):
        return None
    total = math.floor(total_raw)
    if total < b['minTotalMb'] or total > b['maxTotalMb'] or avail < 0 or avail > total_raw:
        return None
    if cores <= 0 or cores > b['maxCpuCores'] or not isinstance(facts.get('pieces'), list):
        return None

    def reading(v):          # an impossible reading (negative, larger than the machine) is not trusted: 0
        return math.floor(v) if is_num(v) and 0 <= v <= total_raw else 0
    pieces = []
    for piece in facts['pieces']:
        rec = piece if isinstance(piece, dict) else {}
        pieces.append({'kind': rec['kind'] if isinstance(rec.get('kind'), str) else '',
                       'demandMb': reading(rec.get('demandMb')), 'declaredMb': rec.get('declaredMb')})
    reservations = facts.get('reservations')
    reservations = [] if reservations is None else reservations
    if not isinstance(reservations, list):
        return None
    reserved = 0
    for res in reservations:
        rec = res if isinstance(res, dict) else {}
        mb = min(math.ceil(rec['mb']), b['maxTotalMb']) if is_num(rec.get('mb')) and rec['mb'] > 0 else 0
        reserved += max(0, mb - reading(rec.get('useMb')))
    swap = facts.get('swap')
    if swap is not None:
        st, sf = (swap.get('totalMb'), swap.get('freeMb')) if isinstance(swap, dict) else (None, None)
        if not (is_num(st) and is_num(sf)) or st < 0 or sf < 0 or sf > st or st > b['maxTotalMb']:
            swap = 'unreadable'
        else:
            swap = None if st == 0 else {'totalMb': math.floor(st), 'freeMb': math.floor(sf)}
    os_level = facts.get('osLevel')
    if os_level is not None and not (isinstance(os_level, str) and os_level in ('ok', 'elevated', 'severe')):
        os_level = 'unreadable'
    return {'totalMb': total, 'availableMb': math.floor(avail), 'centiCores': math.floor(cores * 100),
            'pieces': pieces, 'demandSumMb': sum(p['demandMb'] for p in pieces), 'reservedMb': reserved,
            'swap': swap, 'psi': read_psi(facts.get('psi')), 'boxPsi': read_psi(facts.get('boxPsi')),
            'osLevel': os_level}


def capacity_kind(rule, kind):
    if kind in rule['kinds'] and not kind.startswith('//'):
        return kind
    u = rule['unknownKinds']
    return u['job'] if kind.startswith(u['jobPrefix']) else u['other']


def cost_of(rule, kind, learned, declared):
    spec = rule['kinds'][kind]
    if spec.get('plusDeclared') is True:
        ok = is_num(declared) and 0 < declared <= rule['readingBounds']['maxTotalMb']
        mb = spec['startMb'] + (math.ceil(declared) if ok else spec.get('declaredFallbackMb', 0))
        return {'mb': mb, 'centiCores': spec['startCentiCores'], 'unlearnedMb': mb}
    lo, hi = rule['learnedBounds']['minPercent'], rule['learnedBounds']['maxPercent']

    def clamp(v, start):
        if not (is_num(v) and v > 0):
            return start
        return min(max(math.ceil(v), _ceil_div(start * lo, 100)), math.floor(start * hi / 100))
    own = learned.get(kind) if isinstance(learned, dict) else None
    own = own if isinstance(own, dict) else {}
    return {'mb': clamp(own.get('mb'), spec['startMb']), 'centiCores': clamp(own.get('centiCores'),
            spec['startCentiCores']), 'unlearnedMb': spec['startMb']}


def _margin(rule, r, piece_caps):
    m = rule['margin']
    base100 = max(m['minMb'] * 100, r['totalMb'] * m['totalPercent'])
    wide = not piece_caps or r['swap'] is None or r['swap'] == 'unreadable'
    return _ceil_div(base100 * m['noSwapOrNoCapsPercent'], 10000) if wide else _ceil_div(base100, 100)


def _unreadable_level(previous):
    level = 'severe' if previous is not None and previous.get('level') == 'severe' else 'elevated'
    return level, {'level': level, 'calmSinceMs': None}


def level(rule, facts, previous=None, now_ms=None):
    """capacityLevel: (level, pressure state to keep, margin or None)."""
    r = _read_facts(rule, facts)
    if r is None:
        return (*_unreadable_level(previous), None)
    margin = _margin(rule, r, facts.get('pieceCaps') is True)
    p = next_pressure(rule['pressure'], raw_level(rule['pressure'], r, margin), previous, now_ms)
    return p['level'], p, margin


def decision(rule, facts, ask, learned=None, previous=None, now_ms=None):
    """capacityDecision: admit one piece of ask['kind'] or not, and why (the same keys as the TypeScript)."""
    kind = capacity_kind(rule, ask['kind'])
    spec = rule['kinds'][kind]
    cost = cost_of(rule, kind, learned, ask.get('declaredMb'))
    r = _read_facts(rule, facts)
    if r is None:
        lvl, pressure = _unreadable_level(previous)
        return {'admit': False, 'reason': 'unreadable', 'kind': kind, 'level': lvl, 'pressure': pressure,
                'freeMb': None, 'marginMb': None, 'owedMb': None, 'reservedMb': None, 'costMb': cost['mb']}
    margin = _margin(rule, r, facts.get('pieceCaps') is True)
    live = []
    for p in r['pieces']:
        pk = capacity_kind(rule, p['kind'])
        live.append({'kind': pk, 'group': rule['kinds'][pk]['group'],
                     'cost': cost_of(rule, pk, learned, p['declaredMb']), 'demandMb': p['demandMb']})
    owed = sum(max(0, p['cost']['mb'] - p['demandMb']) for p in live)
    free = r['availableMb'] - margin - owed - r['reservedMb']
    pressure = next_pressure(rule['pressure'], raw_level(rule['pressure'], r, margin), previous, now_ms)

    def result(admit, reason):
        return {'admit': admit, 'reason': reason, 'kind': kind, 'level': pressure['level'], 'pressure': pressure,
                'freeMb': free, 'marginMb': margin, 'owedMb': owed, 'reservedMb': r['reservedMb'],
                'costMb': cost['mb']}
    group_live = sum(p['group'] == spec['group'] for p in live)
    if group_live >= rule['ceilings'][spec['group']]:
        return result(False, 'ceiling')
    if pressure['level'] == 'severe':
        return result(False, 'severe')
    if 'maxAtOnce' in spec and sum(p['kind'] == kind for p in live) >= spec['maxAtOnce']:
        return result(False, 'kind-limit')
    core_limit = math.floor(r['centiCores'] * rule['jobCoresPercent'] / 100)

    def fail():
        if pressure['level'] != 'ok':
            return 'elevated'
        if kind == rule['writers']['kind']:
            build = cost_of(rule, rule['writers']['buildKind'], learned, None)
            idle_free = r['availableMb'] + r['demandSumMb'] - margin - r['reservedMb']
            by_memory = math.floor(max(0, idle_free) / build['mb'])
            by_cores = math.floor(core_limit / build['centiCores']) if build['centiCores'] > 0 else by_memory
            cap = max(rule['writers']['min'], rule['writers']['perBuildThatFits'] * min(by_memory, by_cores))
            if sum(p['kind'] == kind for p in live) >= cap:
                return 'writers'
        if spec['group'] == 'job':
            job_cores = sum(p['cost']['centiCores'] for p in live if p['group'] == 'job')
            if job_cores + cost['centiCores'] > core_limit:
                return 'cores'
        return None if cost['mb'] <= free else 'budget'
    why = fail()
    if why is None:
        return result(True, 'fits')
    # The floor never takes the machine below the severe line (capacityRule.ts).
    after = r['availableMb'] - owed - min(cost['mb'], cost['unlearnedMb'])
    if (group_live < rule['floors'][spec['group']]
            and after * 100 >= margin * rule['pressure']['severe']['availableBelowMarginPercent']):
        return result(True, 'floor')
    return result(False, why)


def fill(rule, facts, steps, learned=None, previous=None, now_ms=None):
    """capacityFill: per step, how many were admitted one by one and the first refusal's reason (None: all were)."""
    readable = isinstance(facts.get('pieces'), list)
    pieces = list(facts['pieces']) if readable else []
    admitted, stopped = [], []
    for step in steps:
        count, stop = 0, None
        for _ in range(int(step['count'])):     # 2.0 is 2 (a JSON parser may give a float)
            ask = {'kind': step['kind']}
            if 'declaredMb' in step:
                ask['declaredMb'] = step['declaredMb']
            d = decision(rule, dict(facts, pieces=pieces) if readable else facts, ask, learned, previous, now_ms)
            if not d['admit']:
                stop = d['reason']
                break
            count += 1
            pieces.append(dict(ask, demandMb=None))
        admitted.append(count)
        stopped.append(stop)
    return {'admitted': admitted, 'stoppedBy': stopped}


# ---------- capacityVectors.ts ----------

def run_vector(rule, vectors, vector):
    facts = vector['facts']
    if isinstance(facts, str):
        if facts.startswith('//') or facts not in vectors['factSets']:
            raise ValueError(f'{vector["name"]}: unknown fact set {facts}')
        facts = vectors['factSets'][facts]
    if ('ask' in vector) == ('fill' in vector):
        raise ValueError(f'{vector["name"]}: needs exactly one of ask and fill')
    rule = dict(rule, floors=vector.get('floors', rule['floors']), ceilings=vector.get('ceilings', rule['ceilings']))
    facts = dict(facts, **vector.get('patch', {}))
    args = (vector.get('learned'), vector.get('previous'), vector.get('nowMs'))
    if 'fill' in vector:
        return fill(rule, facts, vector['fill'], *args)
    d = decision(rule, facts, vector['ask'], *args)
    return {'admit': d['admit'], 'reason': d['reason'], 'level': d['level'],
            'calmSinceMs': d['pressure']['calmSinceMs'], 'freeMb': d['freeMb'], 'marginMb': d['marginMb'],
            'owedMb': d['owedMb'], 'reservedMb': d['reservedMb'], 'costMb': d['costMb']}
