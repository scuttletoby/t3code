# pressure-note

A job's memory pressure numbers, for the log and for the one automatic re-run of an infrastructure failure
([.github/fork-ci-rerun.md](../../../.github/fork-ci-rerun.md)). It runs the release's own
`.github/scripts/fork-ci-pressure.py` (every release commit holds it), so the numbers and the notice are the ones the
re-run decision reads: `start` saves a sample to `$RUNNER_TEMP`, `end` prints one line with the numbers since then
(memory stall share, the kernel's averages, available memory and swap, OOM kills, the job's cgroup and each box with a
memory limit) and, when the job did not succeed, the same numbers as one notice annotation titled `fork-ci-pressure`.

Facts only, and it never fails the job: a bad `phase` or `time-limit`, a release without the script, a probe that
fails or one that runs past `time-limit` (a stalled machine) each print a warning and the step ends green.

```yaml
steps:
  - uses: actions/checkout@<sha>
  - name: Note memory pressure at the start
    uses: scuttletoby/t3code/fork-ci/actions/pressure-note@<release sha> # ci/vN
    with:
      phase: start
  # ... the job's own steps ...
  - name: Note memory pressure at the end
    if: always()
    uses: scuttletoby/t3code/fork-ci/actions/pressure-note@<release sha> # ci/vN
    with:
      phase: end
      job-status: ${{ job.status }}
```

The re-run decision finds the two steps by name (`probe_start_step` and `probe_end_step` in its config): keep the
names it is configured with.

| Input        | Required | What it is                                                                                  |
| ------------ | -------- | ------------------------------------------------------------------------------------------- |
| `phase`      | yes      | `start` after the checkout, `end` as the job's last step with `if: always()`.               |
| `job-status` | no       | The job's status for `end` (`${{ job.status }}`); anything but `success` leaves the notice. |
| `time-limit` | no       | Seconds the probe may take (default 30); past them it is stopped with a warning.            |

No outputs. Needs `python3` and `timeout` on the runner (Linux; on another system every number reads as null).
Tests: `pressure_note.test.py`; the numbers themselves: `.github/scripts/fork-ci-pressure.test.py`.
