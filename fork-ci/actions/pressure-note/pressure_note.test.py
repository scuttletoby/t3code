"""pressure-note (action.yml, pressure_note.sh). Questions: does start then end leave the numbers in the log and,
for a job that did not succeed, the one notice the re-run decision reads? Does every failure (bad phase, bad time
limit, a release without the script, a probe that hangs) end as a warning with exit 0? Does action.yml pass its
inputs, and does a release commit carry the script the action runs?"""
import json, os, re, shutil, subprocess, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
WRAPPER = os.path.join(HERE, 'pressure_note.sh')


def run(phase, action_path=HERE, status='', limit='30', temp=None):
    env = {'PATH': os.environ['PATH'], 'GITHUB_ACTION_PATH': action_path, 'PHASE': phase,
           'JOB_STATUS': status, 'TIME_LIMIT': limit, 'RUNNER_TEMP': temp or tempfile.mkdtemp()}
    return subprocess.run(['bash', WRAPPER], env=env, capture_output=True, text=True, timeout=60)


class PressureNote(unittest.TestCase):
    def test_start_then_end_of_a_failed_job_leaves_the_notice(self):
        temp = tempfile.mkdtemp()
        self.assertEqual(run('start', temp=temp).returncode, 0)
        out = run('end', status='failure', temp=temp)
        self.assertEqual(out.returncode, 0)
        self.assertIn('fork-ci-pressure: job failure', out.stdout + out.stderr)
        self.assertRegex(out.stdout + out.stderr, r'::notice title=fork-ci-pressure::fork-ci-pressure v1 \{')

    def test_a_job_that_succeeded_gets_the_log_line_and_no_notice(self):
        temp = tempfile.mkdtemp()
        run('start', temp=temp)
        out = run('end', status='success', temp=temp)
        self.assertEqual(out.returncode, 0)
        self.assertIn('fork-ci-pressure: job success', out.stdout + out.stderr)
        self.assertNotIn('::notice', out.stdout + out.stderr)

    def test_a_bad_phase_or_time_limit_is_a_warning_never_a_failure(self):
        out = run('middle')
        self.assertEqual(out.returncode, 0)
        self.assertIn('::warning::pressure-note: phase must be start or end', out.stdout)
        out = run('start', limit='soon')
        self.assertEqual(out.returncode, 0)
        self.assertIn("time-limit 'soon' is not a whole number", out.stdout)

    def fake_release(self, script_text):
        root = tempfile.mkdtemp()
        action = os.path.join(root, 'fork-ci', 'actions', 'pressure-note')
        os.makedirs(action)
        if script_text is not None:
            os.makedirs(os.path.join(root, '.github', 'scripts'))
            with open(os.path.join(root, '.github', 'scripts', 'fork-ci-pressure.py'), 'w') as f:
                f.write(script_text)
        return action

    def test_a_release_without_the_script_is_a_warning(self):
        out = run('start', action_path=self.fake_release(None))
        self.assertEqual(out.returncode, 0)
        self.assertIn('fork-ci-pressure.py is not in this release', out.stdout)

    def test_a_probe_that_hangs_is_stopped_at_the_time_limit(self):
        began = time.monotonic()
        out = run('start', action_path=self.fake_release('import time\ntime.sleep(30)\n'), limit='1')
        self.assertLess(time.monotonic() - began, 15)
        self.assertEqual(out.returncode, 0)
        self.assertIn('::warning::pressure-note: start stopped (exit 124)', out.stdout)

    def test_a_probe_that_fails_is_a_warning(self):
        out = run('end', action_path=self.fake_release('raise SystemExit(3)\n'))
        self.assertEqual(out.returncode, 0)
        self.assertIn('end stopped (exit 3)', out.stdout)

    def test_action_yml_passes_its_inputs_to_the_wrapper(self):
        with open(os.path.join(HERE, 'action.yml'), encoding='utf-8') as f:
            text = f.read()
        for line in ('PHASE: ${{ inputs.phase }}', 'JOB_STATUS: ${{ inputs.job-status }}',
                     'TIME_LIMIT: ${{ inputs.time-limit }}', 'run: exec bash "$GITHUB_ACTION_PATH/pressure_note.sh"'):
            self.assertIn(line, text)
        self.assertTrue(re.search(r'^  using: composite$', text, re.M))

    def test_a_release_commit_carries_the_script_the_action_runs(self):
        with open(os.path.join(ROOT, '.github', 'fork-ci-release.json'), encoding='utf-8') as f:
            paths = json.load(f)['paths']
        self.assertIn('fork-ci', paths)
        self.assertIn('.github/scripts/fork-ci-pressure.py', paths)
        self.assertTrue(os.path.isfile(os.path.join(HERE, '..', '..', '..', '.github', 'scripts', 'fork-ci-pressure.py')))


if __name__ == '__main__':
    unittest.main()
