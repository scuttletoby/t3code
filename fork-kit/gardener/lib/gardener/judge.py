"""gardener.judge: the optional judgment step: a model drafts a short opinion on the findings.

When the project's config has "model" {endpoint, model}, the findings as the issue shows them (report.
public_findings: nothing from outside the repository) and a few of the files they name (at most maxFiles, each cut
to maxFileBytes, only files git tracks in the project root: never an ignored or untracked file such as a local
.env) go to that endpoint's OpenAI-style /chat/completions in one request. The answer is added to the report under
"Judgment (model draft)": nobody checked it, and it is masked like the rest of the issue (report.public_text) and
left out when the issue shows counts only. An endpoint that is not reachable, is slower than timeoutSeconds in
all (connecting and reading the whole answer), or answers with an error, nonsense or not HTTP at all, is a note
("model unreachable") and the run goes on. The note never names the endpoint. It never sees the whole repository.

There is no fallback to a paid model, on purpose: the projects this was made for allow only their chosen (large)
paid models for agent work, and a cheap paid model would be the wrong trade; a local model costs nothing. A
project that wants a paid model can point `endpoint` at any OpenAI-compatible gateway itself.
"""
import json, os, subprocess, time, urllib.request

SYSTEM = ('You review a daily maintenance report of a software project. For each finding, say in one short line '
          'whether it looks real and what to do first. Use only the findings and files given. Plain English.')
MAX_ANSWER = 8000
MAX_RESPONSE = 1 << 20                         # bytes of the HTTP answer read at most


def cut_read_at(resp, left):
    """Let the next read of resp wait at most left seconds: the socket timeout set when the call started would let
    one stalled read run on that long again after the deadline."""
    sock = getattr(getattr(getattr(resp, 'fp', None), 'raw', None), '_sock', None)
    if sock is not None:
        try:
            sock.settimeout(max(0.05, left))
        except OSError:
            pass


def read_all(resp, deadline):
    """The response body (at most MAX_RESPONSE bytes), or TimeoutError once the monotonic deadline passes: the
    socket timeout alone bounds each read, not a server that trickles bytes."""
    body = b''
    while len(body) < MAX_RESPONSE:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('the answer took too long')
        cut_read_at(resp, left)
        chunk = resp.read1(min(65536, MAX_RESPONSE - len(body)))
        if not chunk:
            break
        body += chunk
    if time.monotonic() > deadline:
        raise TimeoutError('the answer took too long')
    return body


def tracked(root, rel):
    """True when git tracks the file rel in root."""
    try:
        p = subprocess.run(['git', '-C', root, 'ls-files', '--error-unmatch', '--', rel], capture_output=True,
                           timeout=30, stdin=subprocess.DEVNULL, env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def files_for(findings, root, max_files, max_bytes):
    """[(relative path, text)] of the files the (public) findings name: regular files inside root that git
    tracks."""
    if not root or max_files <= 0:
        return []
    real_root = os.path.realpath(root)
    out, seen = [], set()
    for f in findings:
        where = (f.get('where') or '').strip()
        path = where.rsplit(':', 1)[0] if where.rsplit(':', 1)[-1].isdigit() else where
        if not path or ': ' in path or os.path.isabs(path):
            continue
        full = os.path.realpath(os.path.join(real_root, path))
        if full in seen or not full.startswith(real_root + os.sep) or not os.path.isfile(full):
            continue
        seen.add(full)
        rel = os.path.relpath(full, real_root)
        if not tracked(real_root, rel):
            continue
        try:
            with open(full, 'rb') as fh:
                text = fh.read(max_bytes).decode('utf-8', 'replace')
        except OSError:
            continue
        out.append((rel, text))
        if len(out) >= max_files:
            break
    return out


def judge(model, findings, root):
    """(answer text or None, note or None). findings: report.public_findings. Never raises: the step is
    optional."""
    if not model:
        return None, None
    if not findings:
        return None, 'judgment: nothing to judge (no findings)'
    deadline = time.monotonic() + model['timeoutSeconds']
    try:
        files = files_for(findings, root, model['maxFiles'], model['maxFileBytes'])
        listed = [{k: f[k] for k in ('check', 'id', 'severity', 'where', 'text')} for f in findings]
        user = 'Findings (JSON):\n' + json.dumps(listed, indent=1)
        for path, text in files:
            user += f'\n\nFile {path}:\n```\n{text}\n```'
        req = urllib.request.Request(
            model['endpoint'].rstrip('/') + '/chat/completions',
            data=json.dumps({'model': model['model'], 'temperature': 0,
                             'messages': [{'role': 'system', 'content': SYSTEM},
                                          {'role': 'user', 'content': user}]}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('no time left')
        with urllib.request.urlopen(req, timeout=left) as resp:
            data = json.loads(read_all(resp, deadline))
        answer = data['choices'][0]['message']['content']
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('an empty answer')
    except Exception as e:                     # anything at all: an optional step never stops the run
        return None, f'judgment: model unreachable or no usable answer ({type(e).__name__}); no judgment today'
    return answer.strip()[:MAX_ANSWER], f'judgment: model reachable; it read {len(findings)} finding(s) and ' \
                                        f'{len(files)} file(s)'
