#!/usr/bin/env python3
"""Tests of action.yml: its step run as a runner runs it (bash -e, no token, an empty GH_CONFIG_DIR), with the
README's neutral config. The router itself: .github/scripts/fork-fleet.test.py. Run: python3 <this>."""
import json, os, re, subprocess, tempfile, unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ACTION = os.path.join(HERE, 'action.yml')
README = os.path.join(HERE, 'README.md')
TOKENS = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')


def step():
    with open(ACTION) as f:
        steps = yaml.safe_load(f)['runs']['steps']
    assert len(steps) == 1, steps
    return steps[0]['env'], steps[0]['run']


def example():
    """The config in the README's "Config" section."""
    with open(README) as f:
        text = f.read()
    return json.loads(re.search(r'## Config\n.*?```json\n(.*?)```', text, re.S)[1])


def usage_runs_on():
    """The routed job's runs-on expression in the README's "Usage" section, without ${{ }}."""
    with open(README) as f:
        text = f.read()
    workflow = yaml.safe_load(re.search(r'## Usage\n.*?```yaml\n(.*?)```', text, re.S)[1])
    return re.fullmatch(r'\$\{\{(.*)\}\}', workflow['jobs']['plugins']['runs-on'].strip(), re.S)[1]


def evaluate(expression, context):
    """A GitHub Actions expression, as the runner evaluates the operators the README uses: literals, context paths,
    property access, fromJSON, ==, !=, !, && and || (which return an operand and short-circuit), and parentheses.
    Strings compare case-insensitively; a missing property is null."""
    tokens = re.findall(r"\s*('(?:[^']|'')*'|&&|\|\||==|!=|!|\(|\)|\.|[A-Za-z_][\w-]*|\S)", expression)
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else None

    def take(want=None):
        got = peek()
        if want is not None and got != want:
            raise SyntaxError(f'{want!r} expected, got {got!r}')
        pos[0] += 1
        return got

    def truthy(v):
        return v not in (None, False, 0, '') and v == v

    def equal(a, b):
        if isinstance(a, str) and isinstance(b, str):
            return a.lower() == b.lower()
        return a == b

    def binary(op, lower):
        def parse(run):
            left = lower(run)
            while peek() == op:
                take()
                if op == '&&':
                    right_run = run and truthy(left)
                    right = lower(right_run)
                    left = right if right_run else left
                elif op == '||':
                    right_run = run and not truthy(left)
                    right = lower(right_run)
                    left = right if right_run else left
            return left
        return parse

    def equality(run):
        left = unary(run)
        while peek() in ('==', '!='):
            op = take()
            right = unary(run)
            left = equal(left, right) if op == '==' else not equal(left, right)
        return left

    def unary(run):
        if peek() == '!':
            take()
            return not truthy(unary(run))
        return postfix(run)

    def postfix(run):
        value = primary(run)
        while peek() == '.':
            take()
            name = take()
            value = value.get(name) if isinstance(value, dict) else None
        return value

    def primary(run):
        got = take()
        if got == '(':
            value = expr(run)
            take(')')
            return value
        if got.startswith("'"):
            return got[1:-1].replace("''", "'")
        if got in ('true', 'false', 'null'):
            return {'true': True, 'false': False, 'null': None}[got]
        if peek() == '(':
            take('(')
            arg = expr(run)
            take(')')
            if got != 'fromJSON':
                raise SyntaxError(got)
            return json.loads(arg) if run else None
        return context.get(got)

    expr = binary('||', binary('&&', equality))
    value = expr(True)
    if peek() is not None:
        raise SyntaxError(f'left over: {tokens[pos[0]:]}')
    return value


class Action(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.config = os.path.join(self.d, 'fleet.json')
        cfg = example()
        with open(self.config, 'w') as f:
            json.dump(cfg, f)
        self.cfg = cfg
        self.event = os.path.join(self.d, 'event.json')

    def tearDown(self):
        subprocess.run(['rm', '-rf', '--', self.d], check=True)

    def run_step(self, event, event_name='push', attempt='1', **inputs):
        """(exit code, {output: value}, log) of the action's step."""
        with open(self.event, 'w') as f:
            json.dump(event, f)
        out = os.path.join(self.d, 'out')
        open(out, 'w').close()
        env_map, run = step()
        given = {'config': self.config, 'vars': '{}', 'mode': '', 'gates': '', **inputs}
        values = {k: given[re.fullmatch(r'\$\{\{ inputs\.([\w-]+) \}\}', v)[1]] for k, v in env_map.items()}
        repo = self.cfg['repository']
        env = {k: v for k, v in os.environ.items() if k not in TOKENS and not k.startswith(('GITHUB_', 'RUNNER_'))}
        gh_config = os.path.join(self.d, 'gh-config')
        os.makedirs(gh_config, exist_ok=True)
        env.update(values, GH_CONFIG_DIR=gh_config, GITHUB_OUTPUT=out, GITHUB_EVENT_PATH=self.event,
                   GITHUB_ACTION_PATH=HERE, GITHUB_REPOSITORY=repo, GITHUB_EVENT_NAME=event_name,
                   GITHUB_WORKFLOW_REF=f'{repo}/{self.cfg["trusted"]["workflows"][0]}@refs/heads/main',
                   GITHUB_ACTOR=self.cfg['trusted']['actors'][0], GITHUB_TRIGGERING_ACTOR=self.cfg['trusted']['actors'][0],
                   GITHUB_REF='refs/heads/main' if event_name == 'push' else 'refs/pull/7/merge',
                   GITHUB_SHA='0' * 40, GITHUB_RUN_ID='5', GITHUB_RUN_ATTEMPT=attempt)
        r = subprocess.run(['bash', '--noprofile', '--norc', '-e', '-c', run], cwd=self.d, text=True,
                           capture_output=True, env=env, timeout=60)
        with open(out) as f:
            outputs = dict(line.split('=', 1) for line in f.read().splitlines() if '=' in line)
        return r.returncode, outputs, r.stdout + r.stderr

    def own(self, private=True):
        """A push event's payload: this repository, private unless told otherwise."""
        return {'repository': {'full_name': self.cfg['repository'], 'private': private}}

    def pull_request(self, head=None, is_draft=False):
        repo = self.cfg['repository']
        return {**self.own(),
                'pull_request': {'head': {'repo': {'full_name': head or repo}}, 'base': {'repo': {'full_name': repo}},
                                 'user': {'login': self.cfg['trusted']['actors'][0]}, 'draft': is_draft}}

    def with_config(self, **over):
        """Use the README's config with these keys changed."""
        with open(self.config, 'w') as f:
            json.dump({**self.cfg, **over}, f)

    def test_the_readme_example_is_a_valid_config(self):
        code, outputs, log = self.run_step(self.own())
        self.assertEqual(code, 0, log)
        self.assertEqual((outputs['mode'], outputs['attempt'], outputs['watch']), ('fleet', '1', 'true'))

    def test_a_fresh_heartbeat_gives_one_job_per_run_its_run_label_with_the_attempt(self):
        beat = json.dumps({'at': int(__import__('time').time()), 'free': 2})
        code, outputs, log = self.run_step(self.own(), attempt='2',
                                           vars=json.dumps({'FLEET_BEACON_DESKTOP': beat, 'CI_RUNNERS': 'auto'}))
        self.assertEqual(code, 0, log)
        runners = json.loads(outputs['runners'])
        self.assertEqual(runners['plugins'][0], 'example-desktop')
        self.assertRegex(runners['plugins'][1], r'^example-desktop-plugins-[0-9a-f]{16}-a2$')
        self.assertEqual(runners['engine'], ['self-hosted', 'example-build'])
        self.assertEqual(outputs['attempt'], '2')

    def test_a_stale_heartbeat_is_red_and_the_jobs_go_to_the_always_on_pool(self):
        beat = json.dumps({'at': 1, 'free': 2})
        code, outputs, log = self.run_step(self.own(),
                                           vars=json.dumps({'FLEET_BEACON_DESKTOP': beat}))
        self.assertEqual(code, 0, log)
        self.assertIn('::error::fork-fleet: pool desktop', log)
        self.assertEqual(set(map(tuple, json.loads(outputs['runners']).values())), {('self-hosted', 'example-build')})

    def test_unreadable_vars_are_red_not_silent(self):
        code, outputs, log = self.run_step(self.own(), vars='{')
        self.assertEqual(code, 0, log)
        self.assertIn('::error::fork-fleet: the vars input', log)

    def test_draft_and_ready_pull_requests(self):
        beat = json.dumps({'at': int(__import__('time').time()), 'free': 2})
        given = {'vars': json.dumps({'FLEET_BEACON_DESKTOP': beat})}
        _, outputs, _ = self.run_step(self.pull_request(is_draft=True), 'pull_request', **given)
        self.assertEqual(json.loads(outputs['runners'])['plugins'][0], 'example-desktop')
        _, outputs, _ = self.run_step(self.pull_request(), 'pull_request', **given)
        self.assertEqual(set(map(tuple, json.loads(outputs['runners']).values())), {('self-hosted', 'example-ready')})

    def test_a_pull_request_from_another_repository_is_red_without_hosted_runners_else_github_hosted(self):
        """With "hosted": false there is nowhere safe for it: red with a clear error, never skipped, never self-hosted
        (lens #53 correctness, major 1). With a hosted label: every job there."""
        beat = json.dumps({'at': int(__import__('time').time()), 'free': 2})
        for hosted in (False, 'ubuntu-24.04'):
            self.with_config(hosted=hosted)
            for mode in ('', 'fleet', 'github'):
                with self.subTest(hosted=hosted, mode=mode):
                    code, outputs, log = self.run_step(self.pull_request(head='someone/example-app'), 'pull_request',
                                                       mode=mode, vars=json.dumps({'FLEET_BEACON_DESKTOP': beat}))
                    if hosted:
                        self.assertEqual(code, 0, log)
                        self.assertEqual(set(json.loads(outputs['runners']).values()), {hosted})
                        self.assertEqual(json.loads(outputs['fallback']), hosted)
                    else:
                        self.assertEqual(code, 1, log)
                        self.assertRegex(log, r'::error::fork-fleet: .*this run may hold code from another repository')
                        self.assertEqual(outputs, {})
                    self.assertNotIn('ubuntu-latest', log)

    def test_a_public_repository_never_uses_a_static_pool(self):
        """A static pool has no job-started gate: the event must say the repository is private (lens #53 security,
        major 2). The README's config has no hosted label, so nothing can take the run: red."""
        for event in (self.own(private=False), {'repository': {'full_name': self.cfg['repository']}}):
            with self.subTest(event=event):
                code, outputs, log = self.run_step(event)
                self.assertEqual(code, 1, log)
                self.assertIn('static pools serve private repositories only', log)
                self.assertEqual(outputs, {})
        self.with_config(hosted='ubuntu-24.04')
        code, outputs, log = self.run_step(self.own(private=False))
        self.assertEqual(code, 0, log)
        self.assertEqual(set(json.loads(outputs['runners']).values()), {'ubuntu-24.04'})

    def test_the_readme_runs_on_gives_a_self_hosted_runner_only_to_a_run_of_this_repository(self):
        """The README's runs-on expression, evaluated with the action's real outputs (lens #55 correctness, major 1;
        lens #53 correctness, major 2): a route that passed for this attempt gives its labels; one that passed for an
        earlier attempt (a re-run of failed jobs) gives `fallback`, never a dispatcher label (whose runner is made for
        one attempt); a failed route gives the always-on pool only to a push or a pull_request whose head is this
        repository, and every other event goes GitHub-hosted."""
        expression = usage_runs_on()
        repo = self.cfg['repository']
        fork = 'someone/example-app'
        dispatcher = {p['label'] for p in self.cfg['pools'] if not p.get('static')}
        beat = json.dumps({'at': int(__import__('time').time()), 'free': 2})
        events = {
            'push': ('push', self.own()),
            'draft pull request': ('pull_request', self.pull_request(is_draft=True)),
            'ready pull request': ('pull_request', self.pull_request()),
            'pull request from a fork': ('pull_request', self.pull_request(head=fork)),
            'pull_request_target from a fork': ('pull_request_target', self.pull_request(head=fork)),
            'pull_request_target of this repository': ('pull_request_target', self.pull_request()),
            'workflow_run': ('workflow_run', self.own()),
            'schedule': ('schedule', self.own()),
        }
        for hosted in (False, 'ubuntu-24.04'):
            self.with_config(hosted=hosted)
            for name, (event_name, event) in events.items():
                code, outputs, log = self.run_step(event, event_name,
                                                   vars=json.dumps({'FLEET_BEACON_DESKTOP': beat}))
                ours = event_name == 'push' or event_name == 'pull_request' and \
                    event['pull_request']['head']['repo']['full_name'] == repo
                cases = {'route failed': ('failure', {})}
                if code == 0:
                    cases['route passed'] = ('success', outputs)
                for case, (result, got) in cases.items():
                    for attempt in ('1', '2'):
                        with self.subTest(hosted=hosted, event=name, route=case, attempt=attempt):
                            labels = evaluate(expression, {
                                'needs': {'route': {'result': result, 'outputs': got}},
                                'github': {'event_name': event_name, 'run_attempt': attempt, 'repository': repo,
                                           'event': event}})
                            flat = labels if isinstance(labels, list) else [labels]
                            if result == 'failure':
                                self.assertEqual(labels, ['self-hosted', 'example-build'] if ours else 'ubuntu-latest')
                            elif attempt == '1':
                                self.assertEqual(labels, json.loads(got['runners'])['plugins'])
                            else:
                                self.assertEqual(labels, json.loads(got['fallback']))
                                self.assertFalse(dispatcher & set(flat), labels)
                            if name in ('pull request from a fork', 'pull_request_target from a fork',
                                        'workflow_run'):
                                self.assertTrue(labels in ('ubuntu-latest', hosted), labels)
                            elif result == 'failure' and not ours:
                                self.assertEqual(labels, 'ubuntu-latest')

    def test_a_missing_or_broken_config_or_setting_fails_and_puts_out_nothing(self):
        broken = os.path.join(self.d, 'broken.json')
        with open(broken, 'w') as f:
            json.dump({**self.cfg, 'hosted': True}, f)
        cases = {'missing': {'config': os.path.join(self.d, 'nope.json')}, 'broken': {'config': broken},
                 'a typo in the setting': {'mode': 'fleeet'}}
        for name, given in cases.items():
            with self.subTest(name):
                code, outputs, log = self.run_step(self.own(), **given)
                self.assertEqual(code, 1, log)
                self.assertIn('::error::fork-fleet:', log)
                self.assertEqual(outputs, {})

    def test_inputs_reach_the_step_by_env_only(self):   # never ${{ }} inside run: (script injection)
        env_map, run = step()
        self.assertNotIn('${{', run)
        self.assertEqual(set(env_map), {'CONFIG', 'FLEET_VARS', 'FLEET_MODE', 'GATES'})
        self.assertIn('--config="$CONFIG"', run)   # a value starting with - is never an option
        self.assertTrue(run.startswith('exec python3 -I "$GITHUB_ACTION_PATH/../../../.github/scripts/fork-fleet.py" '))

    def test_outputs_come_from_the_step(self):
        with open(ACTION) as f:
            outputs = yaml.safe_load(f)['outputs']
        self.assertEqual({k: v['value'] for k, v in outputs.items()},
                         {k: f'${{{{ steps.route.outputs.{k} }}}}'
                          for k in ('runners', 'mode', 'attempt', 'fallback', 'watch')})

    def test_the_router_ships_in_every_release(self):
        """A release commit holds only the configured paths: the router must be one of them."""
        with open(os.path.join(HERE, '..', '..', '..', '.github', 'fork-ci-release.json')) as f:
            paths = json.load(f)['paths']
        self.assertIn('.github/scripts/fork-fleet.py', paths)


if __name__ == '__main__':
    unittest.main()
