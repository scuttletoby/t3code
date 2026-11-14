# Gate trust: what the head gate proves, and the owner's steps

Fork only. This page is the trust story of the head gate
([issue 34](https://github.com/scuttletoby/t3code/issues/34)): what it proves, what it does not stop,
the repository owner's one-time steps as paste-ready commands, key rotation, and what an agent on a
work machine can still do. How the gate works (caller, guard job, bare mode, the one check run) is in
the head-check README, section [The gate](../../fork-ci/actions/head-check/README.md#the-gate); this
page does not repeat it.

The owner's commands live **only here**. Other pages link to this one. A test runs the commands'
`jq` parts and the `gh` calls against a fake GitHub
([`fork-gate-trust-docs.test.py`](../../.github/scripts/fork-gate-trust-docs.test.py)).

## What the gate proves

When the required check is bound to the gate App (owner step 6) and the queue counts only that App's
check runs (`head_check_app`, [pr-queue](../../fork-ci/actions/pr-queue/README.md)):

- **The check was judged by the default branch's code.** `pull_request_target` always runs the
  workflow file of the default branch, and the gate's code comes from a pinned fork release. A pull
  request that edits the caller, the gate, the queue config or the lens list changes nothing for its
  own run: the config is read from the base tip.
- **Only the gate App could post it.** Only GitHub Apps can create check runs; a user token gets 403
  (a commit status is a different thing, and a status never meets an App-bound check). A job of the
  pull request's own workflow reports as the GitHub Actions App (id 15368), not as the gate App.
- **Only the default branch could make the App's token.** The App key is a secret of an Environment
  that only the default branch may deploy to. A run for a pull request branch is refused the
  Environment and its secrets. When a repository or organisation secret of the key's name exists
  (the guard sees one only because the caller passes `secrets: inherit`), or the Environment allows
  more than the default branch, the guard refuses and the gate posts only failures, on every head
  it runs for.
- **The judged head is the change the review read**, by the same rules as the head-check action
  (the README's "The rules").

## What it does not stop

- **An admin login.** Anyone (person or agent) who can change the default branch, the ruleset or
  branch protection, the Environment, or the App can change the gate. Today an agent that runs with
  the owner's own GitHub login (repository admin) can do all of that, and can post the override
  comment "as the owner". The gate stops a pull request from faking its gate; it does not stop the
  holder of an admin login. Closing that is stage 2 (below), an owner decision.
- **An admin bypass.** A ruleset bypass ("always") lets its holder merge or push past the gate.
  Remove it together with stage 2a.
- **Lens comments.** A lens comment is only as good as the people allowed to post it (the config's
  `lens_author_associations`). Who may post them once agents use App tokens is open (lens identity).
- **Stale answers.** The gate judges the comments that exist when it runs. Reviews and inline diff
  comments do not start it (see the README's "Run again after a review").
- **Ordinary CI jobs.** A pull request can still weaken an ordinary job of its own workflow. The gate
  catches that only through review: the reviewers read the exact head, and the gate cannot be
  rewritten.
- **The head-check action on a reused self-hosted workspace.** In checkout mode, an earlier job of the
  same pull request can leave `.git/config` entries that run its code inside the check
  ([issue 83](https://github.com/scuttletoby/t3code/issues/83)). The gate is not affected: it runs on
  a GitHub-hosted runner and fetches into a new bare repository each run.
- **The host.** The gate keeps its own key off every machine. The agent Apps' keys, their token cache
  and the `gh` tool on the agent machine are a separate part (the host install kit, not in the fork
  yet). Until it lands, an agent can read those keys.

## What an agent can still do

"Agent" means a process with a shell as the agent user. Today that user often also holds the owner's
GitHub login. Stage 1 is the gate plus the host install kit. Stage 2 replaces the owner's login on the
agent user: 2a with a fine-grained token without Administration, Secrets and Environments rights, 2b
with no owner credential at all (agents only through App tokens).

| What the agent tries                                          | Without the gate   | Stage 1                                                                                          | Stage 2a                           | Stage 2b  |
| ------------------------------------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------ | ---------------------------------- | --------- |
| A pull request changes the workflow so its gate passes        | yes                | **no**                                                                                           | no                                 | no        |
| A same-named job, or a status, under the required name        | yes                | **no**                                                                                           | no                                 | no        |
| Read the gate App key                                         | (no key)           | only by first changing the Environment rule (row 7) or pushing past the gate (row 8)             | **no**                             | no        |
| Read an agent App key or a cached token                       | yes                | **no** (host kit)                                                                                | no                                 | no        |
| Change the `gh` tool or its guard                             | yes                | **no** (host kit, root-owned)                                                                    | no                                 | no        |
| Get a token out of a command                                  | yes                | **hard** (host kit): never printed, output scrubbed; worst case 1 hour, one repository, no admin | same                               | same      |
| Change the ruleset, branch protection or the Environment rule | yes                | yes                                                                                              | **no**                             | no        |
| Push to the default branch through an admin bypass            | yes, if one exists | yes                                                                                              | **no**, once the bypass is removed | no        |
| Post the override comment "as the owner"                      | yes                | yes                                                                                              | yes                                | **no**    |
| Post a lens comment "as a reviewer"                           | yes                | yes                                                                                              | yes                                | lens work |

Stage 2b needs two things first: git push without a readable token, and a rule for who may post lens
comments (a reviewer identity the author cannot use).

## Owner steps (paste-ready)

Placeholders are in CAPITALS: replace each one before you paste. No step shows a secret. Run steps 3
and 6 with your own `gh`, on a machine agents do not use. Steps 4 and 5 run as root, in a shell no
agent sees (not a terminal an agent can read).

| Placeholder                | Meaning                                                                                   |
| -------------------------- | ----------------------------------------------------------------------------------------- |
| `OWNER/REPO`               | the project repository                                                                    |
| `DEFAULT_BRANCH`, `BRANCH` | its default branch (the one the gate guards)                                              |
| `GATE_APP_NAME`, `APP_ID`  | the gate App's name and its App ID (step 1)                                               |
| `CHECK_NAME`               | the caller's `check-name`, the name of the gate's check run                               |
| `RULESET_ID`               | the id of the ruleset that requires the check (`gh api repos/OWNER/REPO/rulesets`)        |
| `ANY_SOURCE_APP_ID`        | classic protection only: `15368` (GitHub Actions) or `-1` (keep "any source"), see step 6 |
| `PATH_TO_DOWNLOADED_KEY`   | the `.pem` file the App's page downloaded                                                 |
| `POLICY_ID`                | step 3 on an existing Environment: the id of a deployment rule to delete                  |
| `FORK_OWNER/FORK`          | step 4: the fork that ships the host install kit                                          |
| `RELEASE_SHA`, `KIT_PATH`  | step 4: the kit's release commit and its folder in the fork                               |
| `AGENT_USER`               | step 4: the system user the agents run as                                                 |
| `APP_NAME`, `AGENT_APP_ID` | step 5: an agent App's name and its App ID                                                |

**1. Make the gate App** (browser): Settings, Developer settings, GitHub Apps, New GitHub App. Name
`GATE_APP_NAME`; webhook off; repository permission **Checks: Read and write** and nothing else;
"Only on this account"; no user sign-in. Note the **App ID**. Generate a private key (a `.pem` file
downloads).

**2. Install it** (browser): the App's page, Install, only `OWNER/REPO`. One gate App may be
installed on several repositories: it can only write check runs.

**3. Environment, App id and key.** The Environment's deployment branches are one rule, the default
branch; for anything else the guard refuses and the gate posts only failures. Safe to run again:
the second `POST` finds the rule already there (GitHub answers with the existing one or an error,
and adds nothing), and the other lines set the same values.

<!-- owner-step: environment -->

```sh
gh api -X PUT repos/OWNER/REPO/environments/head-gate \
  -F 'deployment_branch_policy[protected_branches]=false' -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X POST repos/OWNER/REPO/environments/head-gate/deployment-branch-policies -f name=DEFAULT_BRANCH -f type=branch
gh variable set HEAD_GATE_APP_ID --env head-gate --repo OWNER/REPO --body APP_ID
gh secret set HEAD_GATE_APP_KEY --env head-gate --repo OWNER/REPO < PATH_TO_DOWNLOADED_KEY
```

These lines add, they never delete: on an Environment that already existed, other rules (a
pattern, a tag, a second branch) stay, and the guard then posts a failure on every head. Read the
rules back; the answer must be exactly one line, `branch DEFAULT_BRANCH`:

<!-- owner-step: environment-rules -->

```sh
gh api repos/OWNER/REPO/environments/head-gate/deployment-branch-policies \
  --jq '.branch_policies[] | "\(.id) \(.type) \(.name)"'
```

Delete every other line's rule by its id (the first number), one at a time:

<!-- owner-step: environment-rule-delete -->

```sh
gh api -X DELETE repos/OWNER/REPO/environments/head-gate/deployment-branch-policies/POLICY_ID
```

Then delete the downloaded `.pem`. Keep no repository or organisation secret named
`HEAD_GATE_APP_KEY`. Then add the caller workflow on the default branch and dispatch it once without a
number (the backfill), as the README's setup says.

**4. Host install** (as root; not available yet). The host install kit is not in the fork yet. Its
planned shape, which the kit's pull request fixes exactly. The first line deletes a file an earlier
try left, and `&&` stops the chain at the first failure, so a failed download never runs an older
script:

```sh
rm -f /root/gh-apps-bootstrap.sh &&
curl -fsSL https://raw.githubusercontent.com/FORK_OWNER/FORK/RELEASE_SHA/KIT_PATH/bootstrap.sh -o /root/gh-apps-bootstrap.sh &&
sh /root/gh-apps-bootstrap.sh --repo FORK_OWNER/FORK --sha RELEASE_SHA --agent-user AGENT_USER
```

It is to print the sha and file hashes it installed; compare the sha with the release page.

**5. New agent App keys** (as root; not available yet, comes with step 4). Keys an agent could ever
read count as leaked: per agent App, in the browser, generate a new key and delete the old one. Then
once per App, with the key text on stdin, and delete the download:

```sh
gh-apps-admin add-key APP_NAME --app-id AGENT_APP_ID --repo OWNER/REPO < PATH_TO_DOWNLOADED_KEY
```

**6. Bind the check to the gate App**, only after the backfill shows the gate's check on every open
pull request (otherwise those pull requests wait until their next event). The one exception is a gate
App made again: there this step comes **first**, before the gate posts as the new App (Rotation and
repair, "App made again"). Each block reads the rule, rewrites only the gate's entry with `jq` into a
temporary folder, and writes it back; a failed step stops the chain before the write. Safe to run
again: the second run writes the same rule.

With a ruleset (the entry for `CHECK_NAME` is replaced by one bound to `APP_ID`; every other check and
rule stays as it is). It refuses a ruleset with no or two required-checks rules, and one whose bypass
list your login cannot see (run it as a repository admin):

<!-- owner-step: bind-ruleset -->

```sh
d=$(mktemp -d) &&
gh api repos/OWNER/REPO/rulesets/RULESET_ID > "$d/old.json" &&
jq --arg check 'CHECK_NAME' --argjson app APP_ID '
  if ([.rules[]? | select(.type == "required_status_checks")] | length) != 1
  then error("this ruleset needs exactly one required status checks rule") else . end
  | if has("bypass_actors") then . else error("no bypass_actors: read the ruleset as a repository admin") end
  | {name, target, enforcement, conditions, bypass_actors, rules}
  | (.rules[] | select(.type == "required_status_checks").parameters.required_status_checks)
    |= (map(select(.context != $check)) + [{context: $check, integration_id: $app}])
' "$d/old.json" > "$d/new.json" &&
gh api -X PUT repos/OWNER/REPO/rulesets/RULESET_ID --input "$d/new.json"
```

With classic branch protection instead. Classic protection sets every required check's source in one
list, so this also sets each check that allows "any source" to `ANY_SOURCE_APP_ID`. `15368` pins them
to GitHub Actions, which also stops a status posted with a user token from meeting them: first open a
recent pull request and confirm each of those checks is a GitHub Actions job, or merges wait. `-1`
keeps "any source" as before.

<!-- owner-step: bind-classic -->

```sh
d=$(mktemp -d) &&
gh api repos/OWNER/REPO/branches/BRANCH/protection/required_status_checks > "$d/old.json" &&
jq --arg check 'CHECK_NAME' --argjson app APP_ID --argjson other ANY_SOURCE_APP_ID '
  {strict, checks: ((.checks | map(select(.context != $check)) | map(.app_id //= $other))
    + [{context: $check, app_id: $app}])}
' "$d/old.json" > "$d/new.json" &&
gh api -X PATCH repos/OWNER/REPO/branches/BRANCH/protection/required_status_checks --input "$d/new.json"
```

With a ruleset, the next gate run's log says the check is bound to the gate App; until then it
warns. Classic branch protection cannot be read with the gate's job token, so with it the log always
warns, also after this step. Check it by hand: the gate's entry must show `app_id` `APP_ID`:

<!-- owner-step: classic-check -->

```sh
gh api repos/OWNER/REPO/branches/BRANCH/protection/required_status_checks --jq '.checks'
```

Roll back: run the block again with the old source, or remove the entry in the settings page.

**7. The queue key.** Set `head_check_app` to `APP_ID` in the queue config, in a pull request, after
the queue's `head_check` names `CHECK_NAME` (Rollout order step 4) and after the fork release that
knows the key is pinned everywhere (an older queue refuses a key it does not know). With
`head_check` still on the old name, the queue counts only the gate App's runs of that old name,
finds none, and stops merging: it fails closed, but looks broken.

**Later decision: stage 2.** Take the owner's broad login off the agent user (2a: a fine-grained
token without Administration, Secrets and Environments rights, and no admin bypass; 2b: App tokens
only, after lens identity).

## Rollout order

1. Ship the gate under a **new** check name; the old head check stays as it is (shadow).
2. Backfill (owner step 3's dispatch): every open pull request shows the new check.
3. Bind the new check (owner step 6). Both checks are now required, which is only stricter.
4. A pull request sets the queue's `head_check` to the new name and removes the old step; then
   `head_check_app` (owner step 7).

Roll back at any point: remove the one ruleset or protection entry. After owner step 7, also take
`head_check_app` out of the queue config (a pull request, merged by hand), or the queue keeps waiting
for the gate App's check.

## Rotation and repair

- **Gate key:** add first, then delete. In the App's page add a new key; set the Environment secret
  again (step 3's last line) and delete the new downloaded `.pem`; dispatch the backfill and see the
  check on every open pull request; then delete the old key. The wrong order is **not** fail closed:
  with the old key deleted first, no token is made and nothing is posted (the README's "Stops before
  start"). New heads get no check and wait, but a head that already passed stays passed, even after an
  event that should fail it (a deleted override or lens comment), until the gate can post again and
  the backfill has run. Old check runs stay valid: they belong to the App, not to the key.
- **App reinstalled:** fine, the installation is looked up on every run.
- **App made again** (a new App has a new id), in this order: make the new App and install it (steps 1
  and 2); bind the check to the new id (step 6) **first**; only then set `HEAD_GATE_APP_ID` and
  `HEAD_GATE_APP_KEY` to the new App's (step 3's last two lines, and delete the downloaded `.pem`);
  dispatch the backfill; set `head_check_app` to the new id (step 7); then delete the old App. The
  queue stays blocked at every point. From step 6 until the backfill has posted, nothing can merge:
  the required check now wants the new App, which has posted nothing yet (an admin bypass excepted,
  row 8). From the switch until step 7, a head needs both the new App's pass (the required check) and
  the old App's pass (the queue still counts only the old App), and the old App no longer posts, so a
  new head waits; a failure the new App posts blocks the merge. Merge the step 7 config change by hand
  once the new App's check on it is green. The other order (switch first, bind later) is **not** fail
  closed: until the bind, the new App's failures count nowhere and an old pass of the old App still
  merges.
- **Agent App keys** (host kit): the next token request fails with a clear error naming the App; a
  cached token works until it ends (at most one hour). Rotate as in step 5.
- **Environment widened** (a second branch rule, a tag or pattern rule, or "protected branches"): the
  guard refuses and the gate posts a failure on every head it runs for until the one default-branch
  rule is back. Run step 3 again (its first line turns "protected branches" off, which the read-back
  cannot show), then its read-back and delete. Then dispatch the backfill. With "protected branches"
  on, the gate may post nothing instead of failures: when GitHub does not count the default branch as
  protected, the gate job is refused the Environment (the README's "No token can be made"), and as
  with a deleted key, a head that already passed stays passed.
- **Environment deleted, or the guard cannot see it:** the guard job fails and nothing is posted (a
  gate job that named a missing Environment would make GitHub create it, open to every branch). As
  with a deleted key, a head that already passed stays passed. Deleted: make it again (step 3), then
  dispatch the backfill. Not seen (GitHub does not answer, or a rate limit): once GitHub answers
  again, comment on the pull request or dispatch the gate (the README's "Stops before start").

## General vs project

- **General (this fork):** the gate workflow, its guard and poster, the queue key, this page and its
  commands. Any project, any repository owner; the gate itself needs no machine.
- **Project (config, not code):** the repository, default branch, check name, Environment name, App
  ids, ruleset id or classic protection, required lenses, the agent user. They live in the project's
  caller workflow, its queue config, the Environment's variable, and later the host's root-owned
  config.
- **Thin adapter:** the caller workflow and the release pin in the project's CI.
