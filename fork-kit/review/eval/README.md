# Review model eval

This measures which model and effort finds the **confirmed** blocker and major bugs of past pull requests, so a
project picks its lens runners (the kit settings' `runner` and `lens_runners`, [../REVIEW.md](../REVIEW.md)) from
numbers. Run it again when new models arrive, or with more cases. Everything about the project (its repo, cases,
work folder, the configs to compare, prices, where transcripts are) is in one eval config file; the harness is
[../lib/review_eval.py](../lib/review_eval.py).

## What one earlier run showed

One project ran 40 reviews (5 model and effort settings x 4 cases x 2 runs) of database and server-tooling code. It
is a small sample, and 1-2 bugs between settings is noise, but three results shaped the kit's defaults:

- **No setting found the two rerun and empty-state bugs** (a migration applied twice on a rerun; data migrations run
  again on an empty history) without being asked. That is why every lens prompt carries the state question.
- **Two independent runs of a strong setting miss different bugs**; their union had the best worst case. That is why
  critical paths get two independent lenses.
- **The highest effort cost about 3x for no reliable gain**, and a cheaper model missed a blocker in both its runs.

## Method

1. **Truth set** (`truth`): one case per reviewed commit of a real pull request, with only findings the author
   confirmed (a red-to-green test or a fix commit):

   ```json
   {
     "cases": {
       "A": {
         "reviewed_sha": "<sha>",
         "base": "<sha>",
         "blocker_major": [{ "id": "A-M1", "loc": "src/x.py:10", "bug": "one line" }],
         "minor": [],
         "known_other_lens": []
       }
     }
   }
   ```

   `known_other_lens` holds issues found by other means. The truth set is never shown to runs.

2. **Case folders:** `review_eval.py case --config eval.json` builds `<workdir>/cases/<case>/` from `repo` (any git
   repository, a bare clone is enough): `src/` (the tree at `reviewed_sha`), `diff.patch`, `diff.stat`, and
   `brief.md` from `briefs/<case>.md` when there is one. A brief holds the problem as reported, the files, and the
   lens text ([lens.md](lens.md) is a general one); never the author's summary or earlier findings.
3. **Runs:** `review_eval.py runs` writes `<workdir>/gt/runmap.json` (every case x config x rep; the ids and the
   order come from `seed`, so the same config gives the same map) and lists the runs. Start each run as its own
   read-only agent with `review_eval.py prompt <id>` ([prompt.txt](prompt.txt) filled in), the model and the effort
   it names. Mind the machine's limits on parallel agents.
4. **Judging:** when every run of a case has written `<workdir>/runs/<id>.md`, `review_eval.py judge-prep <case>`
   copies them under neutral names (`review-NN.md`, an order fixed by `seed`) and records the names in
   `gt/judgemap.json`. One blind judge per case (a strong setting, not one under test) runs
   `review_eval.py judge-prompt <case>` ([judge_prompt.txt](judge_prompt.txt)) and writes
   `judge/<case>/scores.json`. A finding counts only for the same root defect: same code path, same failure
   mechanism.
5. **Scores:** `review_eval.py cost` reads tokens, $ (from `prices`, $ per million tokens; check the current price
   list) and active minutes (gaps over `idle_gap_seconds` dropped) from the transcripts that `transcripts` globs
   find, matching each to its run by the `RUN-ID:` first line of the session's first message. `review_eval.py
   analyze` gives per config the blocker and major recall (mean and worst run), recall of all findings, false
   positives, $ and minutes, the union of every pair of configs, and which known bugs each config missed.
6. **Rule:** use the cheapest setting whose worst-case blocker and major recall equals the best setting's. Check it
   again with more cases before changing the project's runners.

[eval.example.json](eval.example.json) shows a config; paths in it are relative to the config file. Keep the work
folder, results and transcripts out of the repository; put the summary in the pull request that changes the
project's runners.

Safe to run again and to stop at any point: files are written by temporary name and rename, a case folder is built
beside its final name and renamed when complete (a stopped build is started again), and a run map or case folder
that would change under existing results is refused, never overwritten.
