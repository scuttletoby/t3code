"""gardener.judge: the optional judgment step (phase 1: the interface only).

When the project's config has "model" {endpoint, model}, the findings and a few of the files they name (at most
maxFiles, each cut to maxFileBytes, only regular files inside the project root) go to that endpoint's
OpenAI-style /chat/completions in one request. The answer is added to the report under "Judgment". An endpoint
that is not reachable, or answers with an error or nonsense, is skipped with a note; there is no fallback to a paid
model. It never sees the whole repository.
"""
import json, os, urllib.error, urllib.request

SYSTEM = ('You review a daily maintenance report of a software project. For each finding, say in one short line '
          'whether it looks real and what to do first. Use only the findings and files given. Plain English.')
MAX_ANSWER = 8000


def files_for(findings, root, max_files, max_bytes):
    """[(relative path, text)] of the files the findings name, inside root."""
    if not root or max_files <= 0:
        return []
    real_root = os.path.realpath(root)
    out, seen = [], set()
    for f in findings:
        where = f.get('where') or ''
        path = where.split(':', 1)[0].strip()
        if not path:
            continue
        full = os.path.realpath(os.path.join(real_root, path))
        if full in seen or not full.startswith(real_root + os.sep) or not os.path.isfile(full):
            continue
        seen.add(full)
        try:
            with open(full, 'rb') as fh:
                text = fh.read(max_bytes).decode('utf-8', 'replace')
        except OSError:
            continue
        out.append((os.path.relpath(full, real_root), text))
        if len(out) >= max_files:
            break
    return out


def judge(model, findings, root):
    """(answer text or None, note or None)."""
    if not model:
        return None, None
    if not findings:
        return None, 'judgment: nothing to judge (no findings)'
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
    try:
        with urllib.request.urlopen(req, timeout=model['timeoutSeconds']) as resp:
            data = json.loads(resp.read(1 << 20))
        answer = data['choices'][0]['message']['content']
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('an empty answer')
    except (OSError, urllib.error.URLError, ValueError, KeyError, IndexError, TypeError) as e:
        return None, f'judgment skipped: the model at {model["endpoint"]} gave no answer ({type(e).__name__})'
    return answer.strip()[:MAX_ANSWER], f'judgment: {model["model"]} read {len(findings)} finding(s) and ' \
                                        f'{len(files)} file(s)'
