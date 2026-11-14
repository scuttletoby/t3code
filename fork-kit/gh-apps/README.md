# gh-apps: GitHub App tokens for agents, without giving agents the keys

`gh-as` lets an agent run a short list of `gh` commands as a GitHub App, on one repository, while the agent's
own user can never read the App's private key or a token. It is option H-B of the gate-trust design: two
service users, one sudoers file, code in a root-owned folder.

Linux only for now (v1). It needs `python3`, `sudo` (with `@includedir /etc/sudoers.d`), `visudo`, `useradd`,
`openssl`, `git`, `gh`, and for the bootstrap `curl` and `tar`. The code is plain Python with no packages, so a
macOS or BSD layer later only needs other user and sudoers steps.

## How it works

```
agent user                 ghrun (sudo, one fixed command)           ghkeys (sudo from ghrun only)
/usr/local/bin/gh-as  -->  /usr/local/libexec/gh-apps/gh-as     -->  /usr/local/libexec/gh-apps/gh-mint
 finds the repo with        checks app, repo and the command          reads the key (root:ghkeys 0640)
 /usr/bin/git, opens        against root-owned config, gets a         makes a 1-hour token for exactly
 any file you name and      token, runs gh in an empty folder         one repository, caches it (ghkeys
 sends it on stdin          with a clean environment, scrubs          0600), prints it to ghrun only
                            the token out of the output
```

- **The agent** runs `gh-as [--repo OWNER/NAME | --app NAME] gh <command>`. Without `--repo` the wrapper reads
  the `origin` remote with `/usr/bin/git` (never a `git` found in `PATH`). A file flag (`-F body.md`,
  `--body-file`, `--input`, `-F key=@file`) is opened by the wrapper **as the agent**, so only files the agent
  could read anyway can be sent; gh then reads them from stdin.
- **ghrun** runs the privileged `gh-as` and `gh`. It refuses every file path (only stdin), every command that is
  not on the list in `lib/ghapps/policy.py`, unknown flags, full URLs, `--hostname`, `--verbose`, jq that reads
  the environment, and owner work (rulesets, branch protection, Environments, secrets, collaborators, disabling
  workflows or Actions). Every `gh api` write must go to a path inside the App's own repository
  (`repos/OWNER/NAME/...`): never the repository itself, its numeric alias `repositories/ID`, or org, team, user or
  member endpoints. The one write to Actions settings that passes turns Actions back on (`enabled=true` and no
  other field). The token is in `gh`'s environment only; anything that looks like a token or key is
  replaced by `[redacted]` in the output.
- **Plain text only.** The guard checks what GitHub will see, so it refuses anything gh or GitHub would rewrite
  first: a `gh api` endpoint with `%`, `\`, `#`, spaces, an empty, `.` or `..` part, `{branch}`, or a query string
  on a write; any `-H` header but `Accept`, `X-GitHub-Api-Version`, `GraphQL-Features`, `If-None-Match` and
  `If-Modified-Since`.
- **GraphQL: queries only.** The agent tools only read through GraphQL (review threads, pull request and check
  state); every write goes through a listed `gh` command. The body is decoded the way GitHub reads it (`-f`/`-F`
  fields, or strict JSON from `--input` with `\u` escapes decoded) and every `mutation` or `subscription` is
  refused; a body that cannot be decoded that way is refused too.
- **ghkeys** owns the token cache and is the only user (besides root) that can read the keys. Agents cannot run
  `gh-mint`; ghrun can, but it only gets a token on stdout, never the key. sudo's I/O logging is off for `gh-mint`
  (`!log_output`), so a token never lands in a sudo log.
- Each call runs in its own folder under `/var/lib/gh-apps/run`, locked by an open file that `gh` holds too, so
  killing the wrapper (even with `kill -9`) never lets a later call delete a folder `gh` still uses.

## What it stops, and what it does not

| What an agent tries                                                                        | With gh-apps                                                                                    |
| ------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| Read an App key or a cached token                                                          | **no**: root:ghkeys 0640 and ghkeys 0700; the agent is in neither group                         |
| Change the tool, its config or the guard                                                   | **no**: everything is root-owned; `gh-as --self-check` reports drift                            |
| Run `gh-mint`, or sudo with its own environment (`LD_PRELOAD`, `GH_*`, `GIT_*`, `PYTHON*`) | **no**: one sudoers line, `env_reset`, `!setenv`, fixed `secure_path`, Python started with `-I` |
| Plant a `git` or `gh` in `PATH`                                                            | **no effect**: fixed paths only                                                                 |
| Upload the key through a file flag (`--body-file /etc/gh-apps/keys/x.pem`)                 | **no**: the agent itself opens the file, and cannot read it                                     |
| Use a token on another repository or host                                                  | **no**: one-repository tokens, `GH_HOST` fixed, `-R` must name the App's repository             |
| Rulesets, protection, Environments, secrets, members, disabling workflows                  | **no**, through `gh-as`                                                                         |

What it does **not** stop:

- **A token leaking through an allowed command.** gh-as never prints the token and scrubs output, but an allowed
  command could still carry it somewhere in a way no filter sees. The worst case is one token: one hour, one
  repository, the App's own permissions (never admin).
- **Anything the agent does with its own login.** If the agent user also has a person's `gh` or git login, that
  login can do all of the above itself. Stage 2 of the gate-trust design removes it; this kit does not.
- **git push.** git cannot use these tokens yet; it stays on whatever login the agent user has.
- **Root.** Anyone with root on the host can read the keys. Run the install and `gh-apps-admin` in a shell no
  agent sees.
- **The App's permissions.** gh-as limits commands, not the App: each token gets all of the App's permissions
  (on one repository). Give each App only what agents need.
- **One agent user from another.** Every agent user named at install can use every App set up on the host. Keep
  agents that must not share an App on different hosts.
- **Comments and reviews as the App.** An agent can post as the App bot on the App's repository. Rules that
  trust a comment must look at who posted it (the gate does).

## Files on the host

| Path                                          | Owner, mode                  | What                                                     |
| --------------------------------------------- | ---------------------------- | -------------------------------------------------------- |
| `/usr/local/bin/gh-as`                        | link                         | the wrapper the agent runs                               |
| `/usr/local/libexec/gh-apps/versions/<sha>/`  | root 755                     | the code of one release, with `MANIFEST.sha256`          |
| `/usr/local/libexec/gh-apps/current`          | link                         | the release in use (switched in one step on upgrade)     |
| `/usr/local/libexec/gh-apps/gh-as`, `gh-mint` | links                        | the two commands sudo allows                             |
| `/usr/local/sbin/gh-apps-admin`               | link                         | the owner's command (root)                               |
| `/etc/gh-apps/config.json`                    | root 644                     | `gh` path and the agent users                            |
| `/etc/gh-apps/apps/<name>.json`               | root 644                     | app id, installation id, repository                      |
| `/etc/gh-apps/keys/<name>.pem`                | root:ghkeys 640 (folder 750) | App private keys                                         |
| `/etc/gh-apps/installed.tsv`                  | root 644                     | every path, owner, mode and hash (what self-check reads) |
| `/etc/sudoers.d/gh-apps`                      | root 440                     | the one sudoers file                                     |
| `/var/cache/gh-apps/`                         | ghkeys 700                   | cached tokens (renewed when under 45 minutes are left)   |
| `/var/lib/gh-apps/run/`, `empty/`             | ghrun 700, root 555          | per-call folders, and gh's working folder                |

## Owner steps

Placeholders in CAPITALS. As root, in a shell no agent sees.

1. **Install** (gate-trust design, owner step 4). `RELEASE_SHA` is a commit on the fork's protected
   `ci-release` branch; the bootstrap refuses anything else.

   ```
   curl -fsSL https://raw.githubusercontent.com/FORK_OWNER/FORK/RELEASE_SHA/fork-kit/gh-apps/bootstrap.sh -o /root/gh-apps-bootstrap.sh
   sh /root/gh-apps-bootstrap.sh --repo FORK_OWNER/FORK --sha RELEASE_SHA --agent-user AGENT_USER
   ```

   It prints the sha, the hash of the download and of every installed file. Compare the sha with the release.
   Add `--gh-path /path/to/gh` if gh is not `/usr/bin/gh`. With no App set up yet, `gh-as` refuses every call
   (there is no fallback to another login).

2. **App keys** (owner step 5). Per App, in the browser: generate a new key and delete the old one. Then, with
   the key text on stdin, and delete the download afterwards:

   ```
   gh-apps-admin add-key APP_NAME --app-id AGENT_APP_ID --repo OWNER/REPO < DOWNLOADED_KEY.pem
   ```

   It stores the key, finds the installation, and makes one test token. Run it again with a new key to rotate.
   One App per repository: `add-key` refuses a second App for a repository, and also while a broken app file that
   is (or may be) for it exists. `gh-as --app NAME` refuses while any other app file is, or could be, for the same
   repository (a broken one could), so an agent can never pick a stronger App.
   `gh-apps-admin list`, `gh-apps-admin remove-app APP_NAME` and `gh-apps-admin self-check` do the rest.

Old copies: the installer removes each agent's old `~/.cache/gh-apps`, `~/.local/bin/gh-as` and
`~/.local/bin/gh-app-token`. Keys in `~/.config/gh-apps` were readable by the agent, so they count as leaked: they
are **not** imported, only reported. Make new keys (step 2), delete the old ones on GitHub, then the folder.

Upgrade: run the bootstrap again with the new sha. Keys, apps and config (the gh path and the agent users) stay
unless a flag names a new value. The new version is copied in next to the old one and self-tested (its files
against their hashes, and one test token per App from the new `gh-mint`) before `current` switches to it in one
step; if the self-test fails, the old version stays live (and if the links fail their check right after the
switch, `current` and `config.json` both go back). The old version folder stays for rollback (run the
bootstrap with the old sha). Remove: `gh-apps-install uninstall` (from `/usr/local/libexec/gh-apps/current/sbin/`)
removes the sudoers file first, then everything above, keys included, and the two users. There is no command to
take away one agent user: uninstall, then install again for each user to keep, then add the keys again.

## Safe to run again, stopped half-way, old or empty state

- **Runs again:** every step checks what is there and only fixes what differs. A second run changes nothing
  (the sudoers file is not even rewritten).
- **Stops half-way:** the steps run in this order: users, folders, tool, self-test, switch, sudoers, wrapper,
  cleanup, record. Nothing points at a new version until its self-test passed, so a stop or a failed self-test
  during an upgrade leaves the old version live. The sudoers file, the only thing that lets an agent reach the
  tool, is placed only after the self-test passed, and only after `visudo -cf` accepts it (then `visudo -c` for the whole config, or it is removed again).
  If sudo's config already fails `visudo -c` before the kit touches it (another file is wrong), the installer
  shows visudo's message and places nothing. Files are written to a temporary name and renamed; a version folder is copied to a temporary folder and renamed;
  a half-copied or changed version folder is made again. Running the same command again finishes the job.
- **Old state:** see "Old copies" above. Existing keys, apps and agent users in `/etc/gh-apps` are kept. A link
  or file where the kit expects its own folder or link is refused with a message, never overwritten. Existing
  `ghrun` or `ghkeys` users are reused only if they are plain service users (a no-login shell, a locked password,
  only their own group, no other members, counting users whose primary group it is); otherwise the installer says
  what is wrong and stops. Every agent user, the one named now and each one kept from the installed config, is
  checked again on every run: one in group `ghrun` or `ghkeys` (its primary group too) stops the install. One
  broken app file does not lock the owner out: `remove-app` works, `list` names it, and `add-key` works for other
  repositories (it refuses only while the broken file is, or may be, for the same one). `gh-as --app` refuses
  while any other app file is broken.
- **Self-check as an agent** (`gh-as --self-check`) cannot read the sudoers file; it says so ("not checked")
  instead of passing it silently. `gh-apps-admin self-check` as root checks it.
- **Empty state:** a fresh machine with zero Apps installs fine; `gh-as` then says no App is set up and fails.
- **Rotated key:** gh-mint fails with a message that names the App and says to run `add-key`; a token already
  cached keeps working until it is within 45 minutes of its end (at most 1 hour).

## Tests

`tests/*.test.py`, run with `python3 -I -B fork-kit/gh-apps/tests/NAME.test.py` from the repository root, on a
fake host in a temporary folder (no root, no network). `tests/root.test.py` runs the real install with real users
and sudo; it runs only in CI on a throw-away runner (it refuses anywhere else).
