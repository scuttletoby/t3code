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

    def pull_request(self, head=None, is_draft=False):
        repo = self.cfg['repository']
        return {'pull_request': {'head': {'repo': {'full_name': head or repo}}, 'base': {'repo': {'full_name': repo}},
                                 'user': {'login': self.cfg['trusted']['actors'][0]}, 'draft': is_draft}}

    def test_the_readme_example_is_a_valid_config(self):
        code, outputs, log = self.run_step({'repository': {'full_name': self.cfg['repository']}})
        self.assertEqual(code, 0, log)
        self.assertEqual((outputs['mode'], outputs['attempt'], outputs['watch']), ('fleet', '1', 'true'))

    def test_a_fresh_heartbeat_gives_one_job_per_run_its_run_label_with_the_attempt(self):
        beat = json.dumps({'at': int(__import__('time').time()), 'free': 2})
        code, outputs, log = self.run_step({'repository': {'full_name': self.cfg['repository']}}, attempt='2',
                                           vars=json.dumps({'FLEET_BEACON_DESKTOP': beat, 'CI_RUNNERS': 'auto'}))
        self.assertEqual(code, 0, log)
        runners = json.loads(outputs['runners'])
        self.assertEqual(runners['plugins'][0], 'example-desktop')
        self.assertRegex(runners['plugins'][1], r'^example-desktop-plugins-[0-9a-f]{16}-a2$')
        self.assertEqual(runners['engine'], ['self-hosted', 'example-build'])
        self.assertEqual(outputs['attempt'], '2')

    def test_a_stale_heartbeat_is_red_and_the_jobs_go_to_the_always_on_pool(self):
        beat = json.dumps({'at': 1, 'free': 2})
        code, outputs, log = self.run_step({'repository': {'full_name': self.cfg['repository']}},
                                           vars=json.dumps({'FLEET_BEACON_DESKTOP': beat}))
        self.assertEqual(code, 0, log)
        self.assertIn('::error::fork-fleet: pool desktop', log)
        self.assertEqual(set(map(tuple, json.loads(outputs['runners']).values())), {('self-hosted', 'example-build')})

    def test_unreadable_vars_are_red_not_silent(self):
        code, outputs, log = self.run_step({'repository': {'full_name': self.cfg['repository']}}, vars='{')
        self.assertEqual(code, 0, log)
        self.assertIn('::error::fork-fleet: the vars input', log)

    def test_draft_and_ready_pull_requests(self):
        beat = json.dumps({'at': int(__import__('time').time()), 'free': 2})
        given = {'vars': json.dumps({'FLEET_BEACON_DESKTOP': beat})}
        _, outputs, _ = self.run_step(self.pull_request(is_draft=True), 'pull_request', **given)
        self.assertEqual(json.loads(outputs['runners'])['plugins'][0], 'example-desktop')
        _, outputs, _ = self.run_step(self.pull_request(), 'pull_request', **given)
        self.assertEqual(set(map(tuple, json.loads(outputs['runners']).values())), {('self-hosted', 'example-ready')})

    def test_a_pull_request_from_another_repository_is_github_hosted(self):
        beat = json.dumps({'at': int(__import__('time').time()), 'free': 2})
        for mode in ('', 'fleet', 'github'):
            with self.subTest(mode):
                code, outputs, log = self.run_step(self.pull_request(head='someone/example-app'), 'pull_request',
                                                   mode=mode, vars=json.dumps({'FLEET_BEACON_DESKTOP': beat}))
                self.assertEqual(code, 0, log)
                self.assertEqual(set(json.loads(outputs['runners']).values()), {'ubuntu-latest'})

    def test_a_missing_or_broken_config_or_setting_fails_and_puts_out_nothing(self):
        broken = os.path.join(self.d, 'broken.json')
        with open(broken, 'w') as f:
            json.dump({**self.cfg, 'hosted': True}, f)
        cases = {'missing': {'config': os.path.join(self.d, 'nope.json')}, 'broken': {'config': broken},
                 'a typo in the setting': {'mode': 'fleeet'}}
        for name, given in cases.items():
            with self.subTest(name):
                code, outputs, log = self.run_step({'repository': {'full_name': self.cfg['repository']}}, **given)
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
                         {k: f'${{{{ steps.route.outputs.{k} }}}}' for k in ('runners', 'mode', 'attempt', 'watch')})

    def test_the_router_ships_in_every_release(self):
        """A release commit holds only the configured paths: the router must be one of them."""
        with open(os.path.join(HERE, '..', '..', '..', '.github', 'fork-ci-release.json')) as f:
            paths = json.load(f)['paths']
        self.assertIn('.github/scripts/fork-fleet.py', paths)


if __name__ == '__main__':
    unittest.main()
