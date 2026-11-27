"""The temp folders of .github/scripts/fork-ci-release.test.py (tens of MB a run): a run stopped by SIGTERM removes its
own, and a run removes the folders earlier runs left behind only when they are a day old and no live run holds them.

    python3 .github/scripts/fork-ci-release-cleanup.test.py

Starts the release test once under a private TMPDIR, stops it with SIGTERM after its first ok line, and checks the
folders it found there.
"""
import fcntl
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "fork-ci-release.test.py"
PREFIX = "fork-ci-release-test-"
DAYS_AGO = time.time() - 2 * 24 * 3600


def aged(path):
    os.utime(path, (DAYS_AGO, DAYS_AGO))


class StoppedRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="release-cleanup-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.outside = cls.tmp / "outside"
        (cls.outside / "keep").mkdir(parents=True)
        cls.made = {}
        for name in ("old-free", "old-held", "old-unlocked", "new-free"):
            d = cls.tmp / f"{PREFIX}{name}"
            (d / "inside").mkdir(parents=True)
            if name != "old-unlocked":
                (d / ".lock").touch()
            cls.made[name] = d
        cls.held = os.open(cls.made["old-held"] / ".lock", os.O_RDWR)   # an earlier run that still lives
        fcntl.flock(cls.held, fcntl.LOCK_EX)
        cls.addClassCleanup(os.close, cls.held)
        other = cls.tmp / "other-test-old"
        other.mkdir()
        cls.made["other-name"] = other
        link = cls.tmp / f"{PREFIX}old-link"
        link.symlink_to(cls.outside)
        cls.made["old-link"] = link
        for name in ("old-free", "old-held", "old-unlocked", "other-name"):
            if (cls.made[name] / ".lock").exists():
                aged(cls.made[name] / ".lock")
            aged(cls.made[name])
        os.utime(link, (DAYS_AGO, DAYS_AGO), follow_symlinks=False)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env["TMPDIR"] = str(cls.tmp)
        proc = subprocess.Popen([sys.executable, "-B", "-u", str(SCRIPT)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env)
        cls.lines, cls.own_lock_held = [], None
        try:
            for line in proc.stdout:
                cls.lines.append(line)
                if line.startswith(("ok   ", "FAIL ")):
                    break
            own = [p for p in cls.tmp.iterdir() if p.name.startswith(PREFIX) and p not in cls.made.values()]
            cls.own = own
            if len(own) == 1 and (own[0] / ".lock").exists():
                fd = os.open(own[0] / ".lock", os.O_RDONLY)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    cls.own_lock_held = False
                except BlockingIOError:
                    cls.own_lock_held = True
                finally:
                    os.close(fd)
            proc.send_signal(signal.SIGTERM)
            cls.lines += proc.stdout.readlines()
            cls.code = proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()

    def test_the_run_started(self):
        self.assertTrue(any(line.startswith("ok   ") for line in self.lines), "".join(self.lines)[-2000:])
        self.assertEqual(len(self.own), 1, [p.name for p in self.own])

    def test_a_live_run_holds_its_folder_lock(self):
        self.assertIs(self.own_lock_held, True)

    def test_sigterm_exits_through_the_cleanup(self):
        self.assertEqual(self.code, 128 + signal.SIGTERM, "".join(self.lines)[-2000:])
        self.assertFalse(self.own and self.own[0].exists(), "the stopped run left its folder")

    def test_old_folders_no_live_run_holds_are_removed(self):
        for name in ("old-free", "old-unlocked"):
            self.assertFalse(self.made[name].exists(), name)

    def test_other_folders_are_kept(self):
        for name in ("old-held", "new-free", "other-name"):
            self.assertTrue(self.made[name].is_dir(), name)
        self.assertTrue((self.made["old-held"] / "inside").is_dir())
        self.assertTrue(self.made["old-link"].is_symlink())
        self.assertTrue((self.outside / "keep").is_dir(), "a link's target was touched")


if __name__ == "__main__":
    unittest.main()
