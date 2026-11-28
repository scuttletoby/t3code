#!/usr/bin/env python3
"""Every pinned release download in the fork's workflows (gitleaks, actionlint, shellcheck): one curl with the same
bounded retries on every error (GitHub's release downloads now and then answer 403, 429 or 5xx for a moment, which
curl's plain --retry does not retry for a 403), then the pinned SHA256 checked before anything is unpacked, so a
mismatch fails the step. End to end: each step's own run text, against a local HTTP server that answers with errors
first (a curl on PATH sends the step's own flags there instead of to github.com). Nothing reaches GitHub.
Run: python3 <this>. Needs PyYAML."""
import concurrent.futures, functools, gzip, hashlib, lzma, http.server, io, os, pathlib, re, shutil, subprocess, sys, tarfile, tempfile
import threading, unittest

import yaml

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
# The one download line every such step uses, flags in this order.
CURL = ('curl -fsSL --retry 5 --retry-all-errors --retry-max-time 120 --connect-timeout 20 --max-time 120 \\\n'
        '-o "$RUNNER_TEMP/$archive" \\\n')
CHECK = 'echo "$SHA256  $RUNNER_TEMP/$archive" | sha256sum -c -\n'
REAL_CURL = shutil.which("curl")
FAKE_TOOL = b"#!/bin/sh\nexit 0\n"
VERSION = "9.9.9"


def download_steps():
    """(label, step) for every step of a fork workflow whose run downloads a GitHub release."""
    found = []
    for path in sorted(WORKFLOWS.glob("fork-*.y*ml")):
        wf = yaml.safe_load(path.read_text())
        for job_id, job in (wf.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if "releases/download/" in str(step.get("run", "")):
                    found.append((f"{path.name}/{job_id}/{step.get('name')}", step))
    return found


def dedent(run):
    return "\n".join(line.strip() if line.startswith(" ") else line for line in run.split("\n"))


@functools.cache
def archive_bytes(xz):
    """The same bytes every call (the checksum the step checks is taken from them)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in ("gitleaks", "actionlint", f"shellcheck-v{VERSION}/shellcheck"):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(FAKE_TOOL), 0o755
            tar.addfile(info, io.BytesIO(FAKE_TOOL))
    return lzma.compress(buf.getvalue()) if xz else gzip.compress(buf.getvalue(), mtime=0)


class Server:
    """Answers the given status codes first, one per request, then the archive (by the path's extension)."""

    def __init__(self, errors):
        self.errors, self.requests = list(errors), 0
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests += 1
                if server.errors:
                    code = server.errors.pop(0)
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = archive_bytes(self.path.endswith(".xz"))
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def run_step(run, errors, sha=None):
    """Runs a step's run text as GitHub's default bash shell does. Returns (process, server requests, temp dir)."""
    xz = ".tar.xz" in run
    server = Server(errors)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="fork-release-downloads."))
    try:
        (tmp / "bin").mkdir()
        (tmp / "runner").mkdir()
        wrapper = tmp / "bin/curl"
        # Sends github.com's URLs to the local server, every flag as the step has it.
        wrapper.write_text('#!/bin/bash\nargs=()\nfor a in "$@"; do\n'
                           f'  args+=("${{a/#https:\\/\\/github.com\\//http://127.0.0.1:{server.port}/}}")\n'
                           f'done\nexec {REAL_CURL} "${{args[@]}}"\n')
        wrapper.chmod(0o755)
        env = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "RUNNER_TEMP": str(tmp / "runner"),
               "VERSION": VERSION, "SHA256": sha or hashlib.sha256(archive_bytes(xz)).hexdigest(),
               "HOME": str(tmp)}
        p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run], cwd=tmp, env=env,
                           capture_output=True, text=True, timeout=180)
        return p, server.requests, tmp
    finally:
        server.close()


def tools(tmp):
    return sorted(str(p.relative_to(tmp / "runner")) for p in (tmp / "runner").rglob("*")
                  if p.is_file() and not p.name.endswith((".gz", ".xz")))


class Flags(unittest.TestCase):
    def test_found(self):
        names = [label for label, _ in download_steps()]
        self.assertEqual(len(names), 4, names)  # two gitleaks (scan, tests), actionlint, shellcheck

    def test_each_step_retries_and_checks_before_unpacking(self):
        for label, step in download_steps():
            with self.subTest(label):
                run = dedent(step["run"])
                self.assertEqual(run.count("curl "), 1, run)
                self.assertIn(CURL, run)
                self.assertRegex(str(step["env"]["SHA256"]), r"^[0-9a-f]{64}$")
                self.assertTrue(step["env"]["VERSION"])
                # The checksum right after the download, before any tar (fail closed).
                after = run[run.index(CURL):]
                self.assertRegex(after, r'^[^\n]*\n[^\n]*\n"https://github\.com/[^"\s]+/releases/download/v\$\{VERSION\}/'
                                        r'\$\{archive\}"\n' + re.escape(CHECK) + r"tar ")
                self.assertNotIn("--retry-connrefused", run)
                self.assertNotIn("continue-on-error", step)


class EndToEnd(unittest.TestCase):
    def test_passing_errors_retried(self):
        # 403 (what failed #1163's run), then 503: each step still installs its tool. All four at once (each waits
        # curl's backoff, about 3 s).
        steps = download_steps()
        with concurrent.futures.ThreadPoolExecutor(len(steps)) as pool:
            results = list(pool.map(lambda s: run_step(s[1]["run"], [403, 503]), steps))
        for (label, _), (p, requests, tmp) in zip(steps, results):
            with self.subTest(label):
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(requests, 3)
                self.assertTrue(tools(tmp), "no tool unpacked")
            shutil.rmtree(tmp)

    def test_checksum_mismatch_fails_closed(self):
        for label, step in download_steps():
            with self.subTest(label):
                p, requests, tmp = run_step(step["run"], [], sha="0" * 64)
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn("FAILED", p.stdout + p.stderr)
                self.assertEqual(requests, 1)  # a wrong archive is not fetched again
                self.assertEqual(tools(tmp), [], "unpacked although its checksum did not match")
                shutil.rmtree(tmp)

    def test_without_retry_on_all_errors_a_403_fails(self):
        # The red: the old line (--retry 3 alone) and the new one without --retry-all-errors give up at the first 403.
        label, step = download_steps()[0]
        old = re.sub(r"curl -fsSL [^\n]*\\\n\s*-o", "curl -fsSL --retry 3 -o", step["run"])
        self.assertNotEqual(old, step["run"])
        for name, run in [("the old line", old), ("no --retry-all-errors", step["run"].replace(" --retry-all-errors", ""))]:
            with self.subTest(name):
                p, requests, tmp = run_step(run, [403])
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(requests, 1)
                self.assertEqual(tools(tmp), [])
                shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
