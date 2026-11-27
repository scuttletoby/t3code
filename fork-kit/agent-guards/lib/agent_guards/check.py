"""agent_guards.check: prove the wired hooks work. Each hook's command, as a settings file has it, is run with one
event it must refuse (block) and one it must let through, the way Claude Code runs it (`/bin/sh -c COMMAND`, the
event on stdin), and the entry's matcher must take the case's tool (Bash, Monitor, Write). Nothing of the machine is
changed: the memory index and the session ids are made up in a temporary folder, the hooks run with HOME there (so
the "~/" state folders, the refusal log and the cache are written there and removed with it; agent-slots and
facts-hook keep the real HOME: they write nothing, and their "~/" paths must mean what the wanted answer read), and
the state files a hook writes in a configured absolute folder for those made-up sessions are removed afterwards.

A hook that is not wired is reported as MISSING (a problem); agent-slots with no sessionsFile only gets its pass
case (it does nothing for any session then); facts-hook wants context when the machine has a state table with a row
or a record repo for the check's folder, else nothing.
"""
import json, os, shutil, signal, subprocess, tempfile

from . import config, facts, wiring

SESSION = 'agent-guards-check-'
READ_ONLY = ('agent-slots', 'facts-hook')   # hooks that write nothing: run with the caller's HOME
# (poll-loop-guard, read-size-guard and batch-nudge write nothing either; they run with HOME in the temporary folder)


def _run(cmd, event, cwd, env=None, argv=None, timeout=30):
    """Run one case in a process group of its own, ended whole afterwards: a hook that hangs, or a background
    command of a case, leaves nothing running."""
    p = subprocess.Popen(argv or ['/bin/sh', '-c', cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, cwd=cwd, env=env, start_new_session=True)
    try:
        out, err = p.communicate(json.dumps(event), timeout=timeout)
        return subprocess.CompletedProcess(p.args, p.returncode, out, err)
    except subprocess.TimeoutExpired:
        _end(p)
        p.communicate()
        return subprocess.CompletedProcess(p.args, 124, '', f'timed out after {timeout} s')
    finally:
        _end(p)


def _end(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def _answer(r):
    """What a hook said: 'refused' (exit 2, a deny or a block), 'context', or 'passed' (exit 0, nothing); any other
    exit is 'failed (exit N)'."""
    try:
        out = json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        out = {}
    out = out if isinstance(out, dict) else {}
    spec = out.get('hookSpecificOutput') if isinstance(out.get('hookSpecificOutput'), dict) else {}
    if r.returncode == 2 or spec.get('permissionDecision') == 'deny' or out.get('decision') == 'block':
        return 'refused'
    if r.returncode != 0:
        return f'failed (exit {r.returncode})'
    ctx = spec.get('additionalContext')
    # a hook that only says its machine file section is broken (config.notice) passed: the self-check names that
    return 'context' if ctx and ctx != out.get('systemMessage') else 'passed'


def _remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def cases(tmp, sid):
    """[(hook, event name, label, event, want, how)]; how: None (plain sh), or for bg-guard 'claude' (under a fake
    claude parent with a background Bash tool command) or 'idle' (the same parent with none)."""
    cap = config.load('capacityPipe')[0]['commands'][0]
    mem = config.load('memoryIndex')[0]
    def project(name, index_text):
        proj = os.path.join(tmp, 'projects', name)
        os.makedirs(os.path.join(proj, 'memory'), exist_ok=True)
        transcript = os.path.join(proj, 'session.jsonl')
        with open(transcript, 'w') as f:
            f.write('{"timestamp":"2000-01-01T00:00:00.000Z"}\n')
        index = os.path.join(proj, 'memory', 'MEMORY.md')
        with open(index, 'w') as f:
            f.write(index_text)
        return transcript, index

    transcript, index = project('pre', '- [a](a.md) - ok\n')                    # within budget
    post_transcript, _ = project('post', '- ' + 'w' * (mem['maxChars'] + 10) + '\n')   # over, changed since start

    def bash(command):
        return {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': command},
                'session_id': sid, 'cwd': tmp}

    def write(content):
        return {'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'session_id': sid,
                'transcript_path': transcript, 'tool_input': {'file_path': index, 'content': content}}

    post = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'tool_input': {'command': 'true'},
            'session_id': sid, 'transcript_path': post_transcript}
    out = [
        ('actions-disable-guard', 'PreToolUse', 'gh workflow disable', bash('gh workflow disable 1 -R example/example'),
         'refused', None),
        ('actions-disable-guard', 'PreToolUse', 'the same in a Monitor',
         dict(bash('gh workflow disable 1 -R example/example'), tool_name='Monitor'), 'refused', None),
        ('actions-disable-guard', 'PreToolUse', 'echo that only mentions it',
         bash("echo 'gh workflow disable 1 is refused'"), 'passed', None),
        ('capacity-pipe-guard', 'PreToolUse', f'{cap} piped', bash(f'{cap} 2048 | tail -1 && true'), 'refused', None),
        ('capacity-pipe-guard', 'PreToolUse', f'{cap} gating', bash(f'{cap} 2048 && true'), 'passed', None),
        ('memory-index-guard', 'PreToolUse', 'Write of an index over budget',
         write('x' * (mem['maxChars'] + 1)), 'refused', None),
        ('memory-index-guard', 'PreToolUse', 'Write of a small index', write('- [a](a.md) - ok\n'), 'passed', None),
        ('memory-index-guard', 'PostToolUse', 'index over budget, changed', post, 'refused', None),
        ('memory-index-guard', 'PostToolUse', 'same index again (seen)', post, 'passed', None),
        ('memory-index-guard', 'PostToolUse', 'same index after a Monitor (seen)', dict(post, tool_name='Monitor'),
         'passed', None),
        ('bg-guard', 'Stop', 'stop with a background command running', {'hook_event_name': 'Stop', 'session_id': sid},
         'refused', 'claude'),
        ('bg-guard', 'Stop', 'stop with none', {'hook_event_name': 'Stop', 'session_id': sid}, 'passed', 'idle'),
        ('agent-slots', 'Stop', 'a session it does not watch', {'hook_event_name': 'Stop', 'session_id': sid},
         'passed', None),
    ]
    out += speed_cases(tmp, sid, bash)
    fc = config.load('facts')[0]       # read only: the machine's own tables and record repos, if any; the hook
    projects = facts.projects_for(fc, tmp)   # runs with this process's HOME (READ_ONLY), so "~/" means the same
    text = facts.session_text(facts.places(fc, tmp, projects=projects), projects, fc['indexLines'],
                              fc['indexChars'], facts.records(fc, projects))
    out.append(('facts-hook', 'SessionStart', 'a new session',
                {'hook_event_name': 'SessionStart', 'source': 'startup', 'session_id': sid, 'cwd': tmp},
                'context' if text else 'passed', None))
    slots = config.load('agentSlots')[0]
    watched = []
    try:
        with open(config.expand(slots['sessionsFile'])) as f:
            watched = [ln.strip() for ln in f if ln.strip() and not ln.startswith('#')]
    except (OSError, ValueError):
        pass
    if slots['sessionsFile'] and watched:   # read only: it adds a context line, it writes nothing
        out.append(('agent-slots', 'UserPromptSubmit', 'a prompt of a watched session',
                    {'hook_event_name': 'UserPromptSubmit', 'session_id': watched[0], 'prompt': 'check'}, 'context',
                    None))
    return out


def speed_cases(tmp, sid, bash):
    """The cases of the three speed hooks. A hook turned off in the machine file ("enabled": false), or whose part
    cannot be used (it then lets every call through), only gets its pass case."""
    out = []
    poll, problem = config.load('pollLoop')
    loop = f'while ! gh pr checks 1; do sleep {poll["maxSleepSeconds"] + 1}; done'
    if poll['enabled'] and not problem:
        out.append(('poll-loop-guard', 'PreToolUse', 'a sleep loop on CI', bash(loop), 'refused', None))
    out.append(('poll-loop-guard', 'PreToolUse', 'the same in the background',
                dict(bash(loop), tool_input={'command': loop, 'run_in_background': True}), 'passed', None))
    size, problem = config.load('readSize')
    big = os.path.join(tmp, 'big.txt')
    with open(big, 'w') as f:
        f.write(('x' * 99 + '\n') * (size['maxChars'] // 100 + 2))
    read = {'hook_event_name': 'PreToolUse', 'tool_name': 'Read', 'session_id': sid, 'cwd': tmp,
            'tool_input': {'file_path': big}}
    if size['enabled'] and not problem:
        out.append(('read-size-guard', 'PreToolUse', 'a whole-file Read of a big file', read, 'refused', None))
    out.append(('read-size-guard', 'PreToolUse', 'the same with a limit',
                dict(read, tool_input={'file_path': big, 'limit': 50}), 'passed', None))
    nudge, problem = config.load('batchNudge')
    turns = max(1, nudge['afterTurns'])
    transcript = os.path.join(tmp, 'batch.jsonl')
    with open(transcript, 'w') as f:
        for k in range(turns):
            f.write(json.dumps({'type': 'assistant', 'message': {'id': f'm{k}', 'content': [
                {'type': 'tool_use', 'id': f't{k}', 'name': 'Read', 'input': {'file_path': big, 'limit': 5}}]}}) + '\n')
    post = {'hook_event_name': 'PostToolUse', 'tool_name': 'Read', 'session_id': sid, 'cwd': tmp,
            'transcript_path': transcript, 'tool_input': {'file_path': big, 'limit': 5}}
    if nudge['enabled'] and not problem and 'Read' in nudge['readOnlyTools']:
        out.append(('batch-nudge', 'PostToolUse', f'{turns} one-read turns in a row', post, 'context', None))
    out.append(('batch-nudge', 'PostToolUse', 'no transcript', dict(post, transcript_path=os.path.join(tmp, 'none')),
                'passed', None))
    return out


def _fake_claude(tmp):
    """A copy of sh named claude: the parent process bg-guard looks for. It starts a command that looks like a
    Bash tool command in the background, runs the hook, and ends the background command."""
    fake = os.path.join(tmp, 'claude')
    shutil.copy('/bin/sh', fake)
    os.chmod(fake, 0o700)
    hook = 'printf "%s" "$AG_EVENT" | /bin/sh -c "$AG_HOOK"; r=$?; '
    busy = ("sh -c 'sleep 30; true' shell-snapshots/snapshot-agent-guards-check >/dev/null 2>&1 & B=$!; "
            + hook + 'kill $B 2>/dev/null; exit $r')
    return fake, {'claude': busy, 'idle': hook + 'exit $r'}


def run(settings, source, say=print):
    """Run every case against the hooks in `settings` (a settings object read from `source`). 0: all as wanted."""
    tmp = tempfile.mkdtemp(prefix='agent-guards-check-')
    sid = SESSION + str(os.getpid())
    bad = 0
    try:
        fake, scripts = _fake_claude(tmp)
        home = os.path.join(tmp, 'home')
        os.makedirs(home)
        missing = set()
        for hook, event, label, ev, want, how in cases(tmp, sid):
            tool = ev.get('tool_name') if event.endswith('ToolUse') else None
            cmd = wiring.find(settings, hook, event, tool)
            if cmd is None:
                if (hook, event, tool) not in missing:
                    missing.add((hook, event, tool))
                    bad += 1
                    say(f'MISSING {hook} ({event}{", " + tool if tool else ""}): not wired in {source}')
                continue
            env = dict(os.environ, HOME=home) if hook not in READ_ONLY else dict(os.environ)
            if how:
                env.update(AG_EVENT=json.dumps(ev), AG_HOOK=cmd)
                r = _run(cmd, ev, tmp, env=env, argv=[fake, '-c', scripts[how]])
            else:
                r = _run(cmd, ev, tmp, env=env)
            got = _answer(r)
            ok = got == want
            bad += not ok
            note = '' if ok else f' (wanted {want}; stderr: {r.stderr.strip()[-200:]!r})'
            say(f'{"PASS" if ok else "FAIL"} {hook} ({event}) {label}: {got}{note}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            _remove(os.path.join(config.expand(config.load('memoryIndex')[0]['stateDir']), sid))
            _remove(os.path.join(config.expand(config.load('bgGuard')[0]['stateDir']), sid + '.count'))
        except Exception:  # noqa: BLE001 - leftovers of a made-up session are harmless
            pass
    say(f'check: {source}: ' + ('OK' if not bad else f'{bad} problem(s)'))
    return 0 if not bad else 1
