#!/usr/bin/env python3
"""git_retry (fork-git-retry.sh): every git fetch in the fork's workflows, fork scripts and fork-ci actions is tried again
after a connection error and never after another error, through the one helper (copies of it held to its text, since a
checkout step runs before there are files to source). A run's Checkout once failed on one refused connection after
132 s (`Failed to connect to github.com port 443`). End to end: the real checkout steps, run as Actions runs them, against
a local remote that refuses connections first (a git on PATH points the step's own fetch at a bound port nobody listens
on, then at a local repository). Nothing reaches GitHub. Run: python3 <this>. Needs PyYAML."""
import os, pathlib, re, shutil, socket, subprocess, sys, tempfile, time, unittest

import yaml

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
HELPER = ROOT / ".github/scripts/fork-git-retry.sh"
HELPER_TEXT = HELPER.read_text()
FUNCTION = HELPER_TEXT[HELPER_TEXT.index("git_retry() {"):].rstrip("\n")
REAL_GIT = shutil.which("git")
SOURCES = ('. .github/scripts/fork-git-retry.sh', '. "$here/fork-git-retry.sh"',
           '. "$(dirname "${BASH_SOURCE[0]}")/fork-git-retry.sh"')
# A git fetch or clone in code, after any `-c name=value`.
GIT_FETCH = re.compile(r"(?<![\w-])git((?:\s+-c\s+\S+)*)\s+(fetch|clone)\b")
# The fetches that keep their own handling, each with the reason (a part of its command line, as written). One that is gone fails, so
# the list cannot keep an entry for a fetch that no longer is.
OWN_HANDLING = {
    ("fork-follow.sh", "\\`git fetch origin fork-follow/candidate main\\`"): "words of a message, not a command",
    ("fork-follow.sh", "To follow by hand: \\`git fetch https://github.com/$UPSTREAM.git tag $target_tag\\`"):
        "words of a message, not a command",
    ("fork-secret-scan.sh", 'if err="$(timeout 300 git fetch --no-tags --quiet --filter=blob:none origin "$1" 2>&1)" '
                            '&& have "$1"; then'):
        "fetch_earlier: its own 4 tries, telling a tip origin no longer has from any other failure",
    ("fork-secret-scan.sh", 'timeout 600 git fetch --no-tags --quiet --filter=blob:none origin "${missing[@]}" '
                            '2> /dev/null || true'):
        "the batch that may fail: each tip is then fetched by fetch_earlier, with its tries",
    ("fork-secret-scan.yml", '&& ! timeout 600 git fetch --quiet --no-tags --filter=blob:none origin "${shown[@]}" '
                             '2> /dev/null; then'):
        "the sweep's batch that may fail: each tip is then fetched alone (fork-contracts.test.py pins the lookup)",
    ("fork-secret-scan.yml", 'timeout 300 git fetch --quiet --no-tags --filter=blob:none origin "$tip" 2> /dev/null \\'):
        "a tip git may no longer serve: asked again below and counted unserved, never a failed job",
    ("fork-secret-scan.yml", 'if ! err="$(timeout 300 git fetch --quiet --no-tags --filter=blob:none origin "$tip" '
                             '2>&1)"; then'):
        "told apart by git's words already: a tip origin no longer has, else the job fails with a re-run hint",
}


def dedent(text):
    lines = text.split("\n")
    pad = min((len(l) - len(l.lstrip(" ")) for l in lines if l.strip()), default=0)
    return "\n".join(l[pad:] for l in lines)


def code_lines(text):
    """Each command line with backslash continuations joined, comments left out."""
    out, cur = [], ""
    for line in text.split("\n"):
        if not cur and line.strip().startswith("#"):
            continue
        cur += line.strip() + " " if line.endswith("\\") else line.strip()
        if not line.endswith("\\"):
            out.append(cur)
            cur = ""
    return [l for l in out if l]


def run_texts():
    """(file name, label, text) for every fork workflow step's run, every fork script and every fork-ci action script."""
    found = []
    for path in sorted(WORKFLOWS.glob("fork-*.y*ml")):
        for job_id, job in (yaml.safe_load(path.read_text()).get("jobs") or {}).items():
            for i, step in enumerate(job.get("steps") or []):
                if "run" in step:
                    found.append((path.name, f"{path.name}/{job_id}/{step.get('name') or i}", str(step["run"])))
    for path in sorted(ROOT.glob(".github/scripts/fork-*.sh")) + sorted((ROOT / "fork-ci").rglob("*.sh")):
        if path != HELPER and not path.name.endswith(".test.sh"):
            found.append((path.name, str(path.relative_to(ROOT)), path.read_text()))
    for path in sorted((ROOT / "fork-ci").rglob("action.y*ml")):
        for i, step in enumerate((yaml.safe_load(path.read_text()).get("runs") or {}).get("steps") or []):
            if "run" in step:
                found.append((path.name, f"{path.relative_to(ROOT)}/{step.get('name') or i}", str(step["run"])))
    return found


def function_copies(text):
    """Each `git_retry() {` ... `}` in text, dedented."""
    lines, copies = text.split("\n"), []
    for i, line in enumerate(lines):
        if line.strip() == "git_retry() {":
            pad = line[:len(line) - len(line.lstrip(" "))]
            end = next(j for j in range(i + 1, len(lines)) if lines[j] == pad + "}")
            copies.append("\n".join(l[len(pad):] if l.startswith(pad) else l for l in lines[i:end + 1]))
    return copies


class Copies(unittest.TestCase):
    def test_every_copy_is_the_helper(self):
        n = 0
        for _, label, text in run_texts():
            for copy in function_copies(text):
                n += 1
                with self.subTest(label):
                    self.assertEqual(copy, FUNCTION, f"{label}: git_retry differs from fork-git-retry.sh")
        # The shared checkout (13 jobs), fork-checks.yml's own two, the secret scan's step and its script.
        self.assertEqual(n, 17)

    def test_every_caller_has_it(self):
        for _, label, text in run_texts():
            if "git_retry " not in text.replace("git_retry() {", ""):
                continue
            with self.subTest(label):
                first_use = min(m.start() for m in re.finditer(r"\bgit_retry (?:fetch|clone|-c)\b", text))
                defined = text.find("git_retry() {")
                sourced = min((text.find(s) for s in SOURCES if s in text), default=-1)
                self.assertTrue(0 <= defined < first_use or 0 <= sourced < first_use,
                                f"{label}: calls git_retry before defining or sourcing it")


class Census(unittest.TestCase):
    def test_every_fetch_goes_through_git_retry(self):
        seen, bare = set(), []
        for name, label, text in run_texts():
            for line in code_lines(text):
                if not GIT_FETCH.search(line):
                    continue
                keys = {k for k in OWN_HANDLING if k[0] == name and k[1] in line}
                seen |= keys
                if not keys:
                    bare.append(f"{label}: {line}")
        self.assertEqual(bare, [], "a git fetch or clone not through git_retry (or listed in OWN_HANDLING)")
        self.assertEqual(sorted(set(OWN_HANDLING) - seen), [], "OWN_HANDLING lists a fetch that is gone")

    def test_census_sees_a_bare_fetch(self):
        self.assertTrue(GIT_FETCH.search('git -c a=b fetch -q origin "$src"'))
        self.assertTrue(GIT_FETCH.search("timeout 9 git clone x"))
        self.assertFalse(GIT_FETCH.search('git_retry fetch -q origin "$src"'))
        self.assertFalse(GIT_FETCH.search("git cat-file -e x"))


def fake_git(tmp, plan):
    """A git on PATH that answers try by try from plan: (stderr, exit status, seconds to sleep) each, the last one
    repeated; it counts its tries in tmp/tries and copies its stdin to tmp/stdin.<try>."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    for i, (err, code, sleep) in enumerate(plan, 1):
        (tmp / f"plan.{i}").write_text(f"{code} {sleep}\n{err}")
    (bin_dir / "git").write_text(f"""#!/bin/bash
n=$(( $(cat "{tmp}/tries" 2>/dev/null || echo 0) + 1 )); echo "$n" > "{tmp}/tries"
p="{tmp}/plan.$n"; [[ -f "$p" ]] || p="{tmp}/plan.{len(plan)}"
read -r code sleep < "$p"
[[ -t 0 ]] || cat > "{tmp}/stdin.$n"
echo "out $n"
tail -n +2 "$p" >&2
sleep "$sleep"
exit "$code"
""")
    (bin_dir / "git").chmod(0o755)
    return bin_dir


def run_helper(plan, env=None, stdin=None):
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="fork-git-retry."))
    bin_dir = fake_git(tmp, plan)
    started = time.monotonic()
    p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
                        f'. "{HELPER}"; git_retry fetch -q origin x'], capture_output=True, text=True, timeout=120,
                       env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "GIT_RETRY_WAITS": "0 0 0", **(env or {})},
                       input=stdin or "")
    took = time.monotonic() - started
    tries = int((tmp / "tries").read_text()) if (tmp / "tries").exists() else 0
    stdins = [(tmp / f"stdin.{i}").read_text() for i in range(1, tries + 1) if (tmp / f"stdin.{i}").exists()]
    shutil.rmtree(tmp)
    return p, tries, took, stdins


OK = ("", 0, 0)
NET = [
    "fatal: unable to access 'https://github.com/o/r/': Failed to connect to github.com port 443 after 132508 ms: "
    "Couldn't connect to server",
    "fatal: unable to access 'https://github.com/o/r/': Could not resolve host: github.com",
    "fatal: unable to access 'https://github.com/o/r/': Recv failure: Connection reset by peer",
    "fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 503",
    "fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 429",
    "fatal: unable to access 'https://github.com/o/r/': gnutls_handshake() failed: The TLS connection was non-properly "
    "terminated.",
    "fatal: unable to access 'https://github.com/o/r/': OpenSSL SSL_read: SSL_ERROR_SYSCALL, errno 0",
    "error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly: CANCEL (err 8)\nfatal: early EOF",
    "fatal: unable to access 'https://github.com/o/r/': Empty reply from server",
]
REAL = [
    "fatal: couldn't find remote ref refs/heads/gone",
    "fatal: remote error: upload-pack: not our ref 1111111111111111111111111111111111111111\n"
    "fatal: the remote end hung up unexpectedly",
    "remote: Repository not found.\nfatal: repository 'https://github.com/o/r/' not found",
    "fatal: Authentication failed for 'https://github.com/o/r/'",
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 403",
    "fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 404",
    "fatal: invalid refspec 'x:y:z'",
    "error: unknown option `nope'",
]


class Helper(unittest.TestCase):
    def test_connection_errors_are_tried_again(self):
        for err in NET:
            with self.subTest(err):
                p, tries, _, _ = run_helper([(err, 128, 0), OK])
                self.assertEqual((p.returncode, tries), (0, 2), p.stderr)
                self.assertIn(err.split("\n")[0], p.stderr)  # git's own words reach the log
                self.assertIn("::warning::git fetch: a connection error on try 1 (exit 128)", p.stderr)

    def test_other_errors_fail_at_once(self):
        for err in REAL:
            with self.subTest(err):
                p, tries, _, _ = run_helper([(err, 128, 0), OK])
                self.assertEqual((p.returncode, tries), (128, 1), p.stderr)
                self.assertIn(err.split("\n")[0], p.stderr)
                self.assertNotIn("::warning::", p.stderr)

    def test_a_failure_with_no_words_fails_at_once(self):
        p, tries, _, _ = run_helper([("", 1, 0), OK])
        self.assertEqual((p.returncode, tries), (1, 1))

    def test_four_tries_then_the_last_status(self):
        p, tries, _, _ = run_helper([(NET[0], 128, 0)])
        self.assertEqual((p.returncode, tries), (128, 4), p.stderr)
        self.assertEqual(p.stderr.count("::warning::"), 3)
        self.assertIn("::error::git fetch: a connection error on try 4, the last one", p.stderr)

    def test_a_try_over_its_limit_is_cut_and_tried_again(self):
        p, tries, took, _ = run_helper([("", 0, 30), OK], {"GIT_RETRY_TRY": "1"})
        self.assertEqual((p.returncode, tries), (0, 2), p.stderr)
        self.assertLess(took, 15)
        self.assertIn("(exit 124)", p.stderr)

    def test_the_total_bounds_every_try_and_wait(self):
        # Each try hangs: cut at 2 s each, 12 s in all; the 100 s wait would pass the total, so it is never waited.
        p, tries, took, _ = run_helper([("", 0, 60)], {"GIT_RETRY_TRY": "2", "GIT_RETRY_TOTAL": "12",
                                                       "GIT_RETRY_WAITS": "1 100 1"})
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertEqual(tries, 2)
        self.assertLess(took, 12)
        self.assertIn("no tries or time left", p.stderr)

    def test_stdout_passes_and_each_try_reads_the_whole_stdin_file(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="fork-git-retry-stdin."))
        try:
            (tmp / "ids").write_text("a\nb\n")
            p, tries, _, stdins = run_helper([(NET[2], 128, 0), OK], {"GIT_RETRY_STDIN": str(tmp / "ids")})
        finally:
            shutil.rmtree(tmp)
        self.assertEqual((p.returncode, tries), (0, 2), p.stderr)
        self.assertEqual(p.stdout, "out 1\nout 2\n")
        self.assertEqual(stdins, ["a\nb\n", "a\nb\n"])

    def test_default_bounds_fit_every_checkout(self):
        """The defaults: waits 5, 10, 20 s, 90 s a try, 150 s in all. With the 5 s kill grace, the checkout's worst case
        stays inside a bounded checkout step and leaves at least 2 minutes of every job's timeout to its other steps."""
        self.assertIn('${GIT_RETRY_WAITS:-5 10 20}', FUNCTION)
        self.assertIn('${GIT_RETRY_TRY:-90}', FUNCTION)
        self.assertIn('${GIT_RETRY_TOTAL:-150}', FUNCTION)
        worst = 150 + 5
        for path in sorted(WORKFLOWS.glob("fork-*.y*ml")):
            for job_id, job in (yaml.safe_load(path.read_text()).get("jobs") or {}).items():
                for step in job.get("steps") or []:
                    if "git_retry fetch" not in str(step.get("run", "")) or "GIT_RETRY_TOTAL=" in str(step["run"]):
                        continue
                    with self.subTest(f"{path.name}/{job_id}"):
                        if "timeout-minutes" in step:
                            self.assertLess(worst, step["timeout-minutes"] * 60)
                        self.assertLessEqual(worst + 120, job["timeout-minutes"] * 60)


# --- End to end: the real steps against a local remote ------------------------------------------------------------
ENV_DROP = {"GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ACTIONS", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
            "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY", "GIT_RETRY_TRY", "GIT_RETRY_TOTAL", "GIT_RETRY_WAITS"}


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = pathlib.Path(tempfile.mkdtemp(prefix="fork-git-retry-e2e."))
        cls.env = {k: v for k, v in os.environ.items() if k not in ENV_DROP}
        cls.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(cls.work / "gitconfig"), GIT_TERMINAL_PROMPT="0",
                       GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                       GIT_COMMITTER_EMAIL="t@example.invalid", LC_ALL="C")
        origin = cls.work / "gh/me/t3code"
        origin.mkdir(parents=True)
        cls.git(origin, "init", "-q", "-b", "main")
        cls.git(origin, "config", "uploadpack.allowFilter", "true")
        cls.git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
        (origin / ".github/scripts").mkdir(parents=True)
        (origin / ".github/scripts/fork-x.sh").write_text("echo x\n")
        cls.git(origin, "add", "-A")
        cls.git(origin, "commit", "-q", "-m", "first")
        cls.sha = cls.git(origin, "rev-parse", "HEAD")
        # Bound, never listening: every connection to it is refused, and no other program can take the port.
        cls.closed = socket.socket()
        cls.closed.bind(("127.0.0.1", 0))
        port = cls.closed.getsockname()[1]
        # The git a step finds on PATH: its first REFUSE fetches go to the closed port, the rest to the local origin.
        bin_dir = cls.work / "bin"
        bin_dir.mkdir()
        (bin_dir / "git").write_text(f"""#!/bin/bash
to="file://{cls.work}/gh/"
for a in "$@"; do
  if [[ "$a" == fetch ]]; then
    n=$(( $(cat "$FETCH_COUNT") + 1 )); echo "$n" > "$FETCH_COUNT"
    (( n > REFUSE )) || to="http://127.0.0.1:{port}/"
    break
  fi
done
exec {REAL_GIT} -c "url.$to.insteadOf=https://github.example/" "$@"
""")
        (bin_dir / "git").chmod(0o755)
        cls.env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
        tests = yaml.safe_load((WORKFLOWS / "fork-tests.yml").read_text())
        cls.checkout = next(s for s in tests["jobs"]["fork_checks_ran"]["steps"] if s.get("name") == "Checkout")
        checks = yaml.safe_load((WORKFLOWS / "fork-checks.yml").read_text())
        cls.checks_step = next(s for s in checks["jobs"]["no_schema_changes"]["steps"]
                               if str(s.get("name", "")).startswith("Check out the default branch"))

    @classmethod
    def tearDownClass(cls):
        cls.closed.close()
        shutil.rmtree(cls.work)

    @classmethod
    def git(cls, cwd, *args):
        return subprocess.run([REAL_GIT, *args], cwd=cwd, env=cls.env, capture_output=True, text=True,
                              check=True).stdout.strip()

    def run_step(self, run, env, refuse):
        dest = pathlib.Path(tempfile.mkdtemp(prefix="runner.", dir=self.work))
        count = dest.parent / f"{dest.name}.fetches"
        count.write_text("0")
        started = time.monotonic()
        p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run], cwd=dest, timeout=170,
                           stdin=subprocess.DEVNULL,
                           env={**self.env, "GITHUB_SERVER_URL": "https://github.example", "GITHUB_REPOSITORY": "me/t3code",
                                "FETCH_COUNT": str(count), "REFUSE": str(refuse), **env},
                           capture_output=True, text=True)
        return p, int(count.read_text()), time.monotonic() - started, dest

    def head(self, dest):
        return subprocess.run([REAL_GIT, "rev-parse", "HEAD"], cwd=dest, env=self.env, capture_output=True,
                              text=True).stdout.strip()

    def test_checkout_refused_once_then_checks_out(self):
        for history, ref in (("tip", self.sha), ("full", "main")):
            with self.subTest(history):
                p, fetches, took, dest = self.run_step(self.checkout["run"], {"REF": ref, "HISTORY": history,
                                                                              "SPARSE": "/.github/scripts/"}, 1)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(fetches, 2)
                self.assertEqual(self.head(dest), self.sha)
                self.assertTrue((dest / ".github/scripts/fork-x.sh").exists())
                self.assertRegex(p.stderr, r"Failed to connect to 127\.0\.0\.1 port \d+")
                self.assertIn("::warning::git fetch: a connection error on try 1 (exit 128); trying again in 5 s",
                              p.stderr)
                self.assertGreaterEqual(took, 5)

    def test_checkout_bad_ref_fails_at_once(self):
        for label, ref in (("an unknown commit", "1" * 40), ("an unknown branch", "gone")):
            with self.subTest(label):
                p, fetches, took, _ = self.run_step(self.checkout["run"], {"REF": ref, "HISTORY": "tip",
                                                                           "SPARSE": ""}, 0)
                self.assertNotEqual(p.returncode, 0)
                self.assertEqual(fetches, 1, p.stderr)
                self.assertLess(took, 5)
                self.assertNotIn("::warning::", p.stderr)

    def test_old_checkout_fails_on_the_first_refusal(self):
        """The step as it was (one plain git fetch) fails where the step now passes: the fixture reproduces the flake."""
        run = self.checkout["run"]
        old = run[:run.index("# git_retry: a copy")] + run[run.index("\n}\n", run.index("git_retry() {")) + 3:]
        old = old.replace("git_retry fetch", "git fetch")
        self.assertNotIn("git_retry", old)
        p, fetches, _, _ = self.run_step(old, {"REF": self.sha, "HISTORY": "tip", "SPARSE": ""}, 1)
        self.assertEqual(p.returncode, 128, p.stderr)
        self.assertEqual(fetches, 1)
        self.assertRegex(p.stderr, r"Failed to connect to 127\.0\.0\.1 port \d+")

    def test_fork_checks_checkout_refused_once_then_checks_out(self):
        env = {"DEFAULT_SHA": self.sha, "BASE_SHA": self.sha, "HEAD_SHA": self.sha}
        p, fetches, _, dest = self.run_step(self.checks_step["run"], env, 1)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(fetches, 2)
        self.assertEqual(self.head(dest), self.sha)
        p, fetches, _, _ = self.run_step(self.checks_step["run"], {**env, "HEAD_SHA": "1" * 40}, 0)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(fetches, 1, p.stderr)


if __name__ == "__main__":
    unittest.main()
