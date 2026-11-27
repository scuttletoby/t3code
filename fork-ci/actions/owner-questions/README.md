# owner-questions

Lists the open **owner questions** of a project's GitHub repositories, the issues that wait for the owner's decision,
and in `label` mode keeps the rule's label on exactly the ones still waiting. With the label on every waiting question,
a listing by label finds them all, also in a repository with too many open issues to read in full (T3's "Waiting on
you" page reads up to 200 per repository, plus every labelled one).

## The rule

An issue (not a pull request) **asks the owner** when it carries the label, or its title starts with one of the title
prefixes as a word (`Owner question: ...`, not `Owner questions are ...`), or, with `topicQuestionTitles`, it reads
`<Topic> Q<n>: ...` (`Issues page Q1: how far?`). Case does not matter. A comment **answers** it when it starts with
the decision prefix and then `:` or ` (` (`Owner decision: yes`, `Owner decision (in chat): yes`, bold allowed);
`Owner decision needed ...` does not. The defaults:

| Key                   | Default                       |
| --------------------- | ----------------------------- |
| `label`               | `status:needs-owner-decision` |
| `titlePrefixes`       | `["Owner question"]`          |
| `topicQuestionTitles` | `true`                        |
| `decisionPrefix`      | `Owner decision`              |

This is the rule of T3's own pages and tools (`packages/contracts/src/forkOwnerQuestions.ts`); both copies run the
cases in [`rule-cases.json`](rule-cases.json), so they cannot drift.

## What it does

- Reads every open issue of each repository (all pages) and the comments of each owner question that has any.
- Writes the waiting questions to the job summary (a table) and the log, and sets the outputs.
- **`label` mode:** puts the label on a waiting question that lacks it (a question asked by title), and takes it off an
  answered one that still has it. Nothing else is written, and a second run changes nothing. GitHub creates the label
  on its first use if the repository has none.
- **Fails the step** (exit 1, an `::error::` line, no outputs) when the config cannot be read or is not valid, or a
  read or label write fails. It never guesses: a question whose comments could not be read is neither listed nor
  relabelled.

## Config

Optional, in the project's repository (default `.t3code/issues.json`, the file T3's server reads for the same
project). Without it, the action reads the running repository with the defaults.

```json
{
  "repositories": ["owner/web", "owner/docs"],
  "ownerQuestions": { "label": "needs-decision", "titlePrefixes": ["Decision needed"] }
}
```

- `repositories`: the `owner/repo` names to read; default the running repository (`repo` input).
- `ownerQuestions`: any of the rule's keys, replacing the default; an unknown key or a wrong type is refused. T3's
  pages use the defaults until they read this key, so rename only a project that does not rely on them.

## Inputs and outputs

| Input    | Default                    | Meaning                                                              |
| -------- | -------------------------- | -------------------------------------------------------------------- |
| `config` | `.t3code/issues.json`      | the config's path in the checkout                                    |
| `mode`   | `list`                     | `list` reads only; `label` also changes labels                       |
| `repo`   | `${{ github.repository }}` | the repository read when the config names none                       |
| `token`  | `${{ github.token }}`      | reads issues; in `label` mode it needs `issues: write` on every repo |

| Output      | Meaning                                                                       |
| ----------- | ----------------------------------------------------------------------------- |
| `count`     | how many owner questions wait                                                 |
| `changed`   | how many labels `label` mode put on or took off (`0` in `list` mode)          |
| `questions` | the waiting questions, a JSON array of `{repo, number, title, url, labelled}` |

All three are empty when the step failed.

## Usage: a thin adapter

A project adds one small workflow; its facts (repositories, wording) stay in its config file. Pin a release commit by
its full SHA ([why](../../README.md#pinning)). The token of a run reaches only its own repository: to label in
another one, pass a token that may write its issues.

```yaml
name: Owner questions
on:
  schedule:
    - cron: "23 * * * *"
  workflow_dispatch:
permissions: {}
concurrency:
  group: owner-questions
  cancel-in-progress: false
jobs:
  label:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      contents: read
      issues: write
    steps:
      - uses: actions/checkout@<sha> # v6
        with:
          sparse-checkout: /.t3code/issues.json
          sparse-checkout-cone-mode: false
      - uses: scuttletoby/t3code/fork-ci/actions/owner-questions@<release sha> # ci/vN
        with:
          mode: label
```

A project may also run it on `issues` and `issue_comment` events, so a new question is labelled at once; the
workflow then runs the default branch's code, and issue text is only data to the action.

Tests: `owner_questions.test.py` (the shared rule cases, the config, both modes and every failure against a fake
`gh api`).
