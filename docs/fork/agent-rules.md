# Agent rules (general)

Fork only. The rules for coding agents that hold in every project and on every machine, each written once. A
project's `AGENTS.md` and a machine's instruction file link here and add only their facts; they never copy a rule.
Where a tool enforces a rule, the tool is the authority.

## Facts each place supplies

- The machine's instruction file: its capacity check and sizes, agent limits, how the owner runs a command there.
- The project's `AGENTS.md`: build slot, test servers, where owner questions go, the state file.
- The [model-guard](../../fork-kit/model-guard/README.md) policy file: allowed models and efforts.
- The project's [review](../../fork-kit/review/SKILL.md) and [doc-check](../../fork-ci/actions/doc-check/README.md)
  configs: review weights and rounds; instruction file budgets.

## Instruction files

- Only what every reader needs every time; the rest is a skill or doc, named in one line with when to read it.
  _Why: they are re-read on every call._
- Current rules only: update in place, delete what you replace. _Why: git keeps history; old text misleads._
- Decisions go in issue comments; history in git, issues and releases; live state in a state file; lessons in
  memory until a tool enforces them; what code or git can tell, nowhere. _Why: one place each._
- A change that adds a guard (check, hook, test) deletes the rule text it replaces; a line outside the repository
  is named in the pull request. _Why: no stale copies._
- A rule that looks wrong goes to the owner with the reason. _Why: working around it hides the flaw._

## General first

- CI, review, agent and ops tooling is general by default: project facts in a project config file, project-only
  parts in a thin adapter, a "General vs project" section in each pull request. _Why: the next project gets it._
- A check that refuses beats a rule in a doc; call a guard a security boundary only when it is one.
  _Why: agents forget; checks do not._
- No guessed waits, no time gates: wait on real progress, advance when the check passes. _Why: guesses fail._

## Machine safety

- Before a build, a test server or several agents, run the capacity check with the job's size. On no: do not start,
  do lighter work, check again on the next wake. Gate with `check && cmd`, never pipe it.
  _Why: overload kills agents; a pipe hides the no._
- Builds and test servers only through the project's build slot and the capped slice (`heavy`). _Why: one cap._
- Stop every server, watcher, daemon and browser tab you started; list what is left to prove it.
  _Why: leftovers starve the next job._
- Deletes: guarded variables (`rm -rf "${DIR:?}/${NAME:?}"`) or literal paths; look at the target first.
  _Why: an empty variable deletes the wrong tree._
- Never print, commit or paste a key, token or credential file; use it only through its command, and ask for one
  only through the secret request tool. _Why: chat and logs are kept._

## Work and pull requests

- Re-check the live state before acting on a brief, a summary or an old status. _Why: facts go stale._
- CI decides: fix the cause of red CI, never bypass it. No CI-skip marker in a commit message, even quoted.
  _Why: it silently skips the real CI._
- Small, focused pull requests; one push per fix round. _Why: each push restarts CI._
- A test that fails the same way twice is known-failing: its fix goes first, no reruns. _Why: reruns waste CI._
- "Fixes #N" only when the fix is proven; else "Part of #N". _Why: merging closes the issue._
- Update a pull request branch from its base (`gh pr update-branch`, Update branch) only for the one you merge next,
  at most once, right before it merges; never the others that wait, never on each move of the base, never by hand
  when a merge queue does it. _Why: each update is a full CI run; they were a quarter of all extra pushes._
- Reports, handoffs and review notes go in the pull request or an issue. _Why: the repository holds current text._
- Reviews follow the [review skill](../../fork-kit/review/SKILL.md) (weights, the state question, round limit);
  never restate it; its `plan` and `check` take `--settings .github/fork-review-kit.json` here. _Why: one source;
  without it the kit reads no queue config and plans one lens._
- No `git stash`; edit tracked files in place. _Why: worktrees share the stash; a move drops `+x`._
- In a fork: build on upstream's mechanisms, check upstream first, never push, comment or open anything upstream.
  _Why: thin forks rebase cleanly; upstream is the owner's._
- Public repositories hold nothing personal (names, hosts, addresses, home paths, tokens). _Why: anyone reads it._

## Agents and orchestrators

- Each task gets a chosen model and effort from the policy file, never the launch default. _Why: cost is a choice._
- One job per agent. New work is a new agent with a short brief naming the old thread; a running agent gets only
  what changes its job. _Why: a long context costs on every call._
- Never end a turn on your own background run, or on remote work with nothing to wake you; wait in the turn or
  register a wake-up and say what will wake you. _Why: nothing else wakes you._
- Ask tools for small outputs: list limits, JSON fields, `git diff --stat`, test output through `tail`.
  _Why: output stays in context._
- An orchestrator stays responsive: longer work goes to a sub-agent, no sleep or poll loops; it keeps a terse
  state file outside `/tmp`. _Why: owner messages wait otherwise._
- Post first, read the link back from the output, then pass it on. _Why: guessed links are wrong._
- Write briefs with the Write tool or a quoted heredoc (`<<'EOF'`). _Why: backticks in `<<EOF` run._

## The owner

- Automate what does not need the owner. Only real decisions go to them: plain English, recommendation first.
- Before asking, read the issue and its comments; ask only what is open. One issue per question, plus a short
  list in chat. A sub-agent asks its orchestrator, never the owner. _Why: repeat questions waste their time._
- A command the owner must run goes at the top of the reply, ready to paste, for a machine they can reach.
- Make every tool call first and write the reply last. _Why: T3 hides the user's message otherwise._
- Legal, licensing and privacy decisions are the owner's: point them out, never conclude.
