# Host tools: agents on a person's own computer (design, no code)

Status: design for review, revision 3. Fork feature, not upstream. Nothing here names a real machine, person or
address; those live in each install's local settings.

## In short

- **What it lets agents do:** you give an agent on one of your machines (for example a headless server) a goal on
  another of your computers: "I want X done". The agent works out the fastest, best route itself: files, commands, the
  apps' own automation, app-side bridges, accessibility elements, and the mouse and keyboard only as the last resort.
- **Scope (owner decision 2026-11-13):** the agent may use the **whole computer except sensitive things**: keys, `.env`
  files, credentials, password fields and secret stores. It looks only at what the task needs. When it needs something
  more specific (the short high-risk list in §4.2), it **asks once with a list**: "Can I use X, Y and Z?". Your answers
  are remembered for the scope you choose. This replaces a per-app allow list.
- **Speed first:** the computer runs a whole list of steps locally, with waits like "until this button appears", and
  sends back only what changed. One network hop and one model turn do the work that takes classic computer use dozens
  of screenshot-and-click turns (§7, §8). When the agent is unsure which of two ways works, it tries both at once on
  private copies and keeps the one that worked (§7.4).
- **Your mouse stays yours:** the agent clicks and types through the least intrusive way that works: element actions,
  then events sent to one window, then its own screen, and only last the real mouse and keyboard on your desktop (§6.4).
- **Invisible by default:** commands, scripts and batches run in the background with no window shown and no focus
  taken. Only UI work you asked for touches the visible screen, and a test checks that nothing else ever does (§6.8).
- **How it is kept safe:** commands run as a separate, limited **agent account** that the OS keeps out of your
  sensitive files and away from T3's own data (T3 itself runs as a second account). Nothing the agent writes is ever run
  as you. The rules live in a store the agent cannot change; widening them needs you.
- **While the agent uses your screen:** a clear on-screen sign shows it; any real mouse or keyboard input from you takes
  control back at once; everything is logged.
- **What you click:** the one batched ask, when the agent needs something from the high-risk list (installs and admin
  are on it). Nothing else asks.
- **What you can always do:** from the tray icon: pause, **wind down** (start nothing new, finish or park what runs,
  then "safe to shut down"), the kill switch, and revoke every link; undo the agent's file changes of the last 24 hours.
- **Power can go at any time:** every part recovers on the next start (§12.3, §15).
- **Links between your machines** last long and extend with one click or one command, without new pairing codes; the
  strong controls are against outsiders (§10).
- **Background runners** (CI jobs, builds) use the hardware you leave free, minus a small buffer, and shrink when you
  need it back (§11). Host tools themselves never slow down because you are busy.

## 1. Parts

| Part                         | Where it runs                                       | Built from                                                       | What it does                                                                                                 |
| ---------------------------- | --------------------------------------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Host environment             | the computer, as the **host account** (§3)          | upstream T3 server, installed with upstream's background service | serves the host tools over the link; owns T3's data; runs no agent code                                      |
| Command broker               | the computer, as the **agent account** (§3)         | **fork**, small, per OS                                          | starts every agent command as the agent account; accepts requests only from the host account                 |
| Linked environment           | between the agent machine and the computer          | upstream peer links (`docs/internals/remote.md`, "Peer links")   | transport, auth, routes (Tailscale, LAN), revocation, access ceiling, expiry (§10)                           |
| `host` toolkit               | inside T3's MCP server                              | **fork**, one toolkit like `toolkits/environment/`               | typed tools (§9); each takes `environmentId`, so upstream forwards it to the computer                        |
| Session helper               | the computer, in **your** desktop session, as you   | **fork** add-on to the T3 desktop app (main process)             | tray (§12), the batched ask (§4.2), policy view; starts the engine; hosts the bridge endpoint                |
| Engine                       | the computer, as you, started by the session helper | **fork**, new, one native binary per OS (§13)                    | the seeing and acting layer (§6-§8): app state, accessibility tree cache, batches, waits, input, screenshots |
| Guard service                | the computer, as the system (admin)                 | **fork**, per OS, installed once                                 | owns the protected store, snapshots, the authoritative log, the kill switch, permission re-application       |
| Protected store              | the computer, owned by the guard service            | **fork**, JSON, shown in T3's settings UI and the tray           | reach map, secret patterns, ask answers, recipes, approved scripts, named commands, bridges                  |
| Availability, project grants | the agent machine's T3 settings                     | **fork**, server settings                                        | per linked computer: on / off / off until; per project: may this project use host tools (§4.3 rule 9)        |
| Activity service             | the computer, in your session (part of the helper)  | **fork** (§11)                                                   | measures free hardware for background runners and for sizing speculative runs; never gates host tools        |

The **core is OS-neutral**: tool schemas, the policy format and checks, the step language, the input ladder logic,
recipes, snapshot and diff format, secret patterns, the log format, pause and wind-down logic, the undo index. The
**per-OS layer** is named in §3, §6 and §13: making the accounts, folder permissions, link-safe file operations, the
service install, snapshots, the accessibility and input back ends, screenshots, refusal checks, and the kill switch.
Windows is built first; nothing Windows-only goes into the core.

## 2. Upstream fit

Read at upstream `main` 8cd8a3ca5a (2026-11-13), with open PRs and issues searched the same day.

**Owner decision (2026-11-13): upstream is used for transport and auth only.** The seeing and acting layer is the
fork's own, written from scratch and speed first (§5). Upstream's computer-use work is listed below so it is clear what
was considered and why it is not the base.

| Need                                  | Upstream mechanism we use as is                                                                                                                                              | What the fork adds (thin)                                                                                                 |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Run T3 on the computer, start at boot | background service: `t3 service install` (Windows Task Scheduler task, macOS login item, Linux systemd user unit; `docs/user/background-service.md`)                         | an option to install it **for another account** (the host account, §3), since upstream installs it for the signed-in user |
| Reach the computer, auth, revoke      | linked environments: pairing code, OAuth session in the secret store, routes over Tailscale/LAN, revoke in Sharing & access (`apps/server/src/peer/PeerLinks.ts`)            | a longer link lifetime setting, one-click extension, device binding and revoke-all (§10), in a form upstream could take   |
| Send a tool call to the computer      | tools whose parameters take `OrchestratorMcpEnvironmentTarget` are forwarded by `McpToolAccess.toLayer` after the caller is checked (`apps/server/src/mcp/McpToolAccess.ts`) | the `host` toolkit, declared like `t3_environment_read` (`apps/server/src/mcp/toolkits/environment/tools.ts`)             |
| Limit what a link may do              | link access (read-only or a runtime-mode ceiling), `T3-Mode-Limit` on every forwarded call, `linkOrigin` fence below full access                                             | host tools are declared read or write with upstream's access declarations; a read-only link gets only the read tools      |
| A full agent working on the computer  | `delegate_task` with `environmentId`; the child is an ordinary thread there (`apps/server/src/peer/RemoteDelegation.ts`)                                                     | **off in v1** for host-tools computers: its provider process would run as the host account (§3.2)                         |
| Settings and their UI                 | `ServerSettings` / `ServerSettingsPatch` (`packages/contracts/src/settings.ts`), settings pages, settings search                                                             | availability and project grants (§4.3), link lifetime (§10), a read view of the policy                                    |
| Keep the tool list small              | `McpToolExposure` and `tool_search` (deferred tools); link tools are hidden while the beta is off (merged #30371)                                                            | host tools are deferred and hidden unless a linked computer reports host tools on                                         |

**Upstream computer-use work, considered and not used as the base:**

- Codex's built-in Computer Use is only _shown_ by T3 (`apps/server/src/provider/CodexToolPresentation.ts:195`,
  `packages/shared/src/toolActivity.ts:31`); it runs where Codex runs, not on another computer.
- The Cua Driver stack, open PRs #16527 to #16532 (bundle Cua Driver 0.38 on all three OSes, attach `cua-driver` as an
  MCP server to every provider session, settings, a floating preview of the driven window). It drives the host the
  provider runs on, one action per tool call, screenshot-led.
- agentcast, open draft #28050: remote desktops over SSH, reads UI trees, background input, refuses on a locked PC
  or an elevation prompt, journals every action so a lost reply never runs twice. Its code is in a private repository;
  we take **ideas only** (the refusals and the action journal, §6.7 and §7.2), per the fork rule of own
  implementations.
- The device toolkit (`apps/server/src/mcp/toolkits/device/tools.ts`) drives phone simulators and emulators, not a
  desktop.

None of these batches steps on the far side, keeps a diffed tree cache, or puts a person's policy between the agent
and the desktop, which is what §5 needs. If upstream later ships a fast, policy-gated desktop layer, we compare and
drop ours where theirs covers it (fork rule: build on upstream where it fits).

**Custom, and why.** The command broker, the session helper, the engine, the guard service, the snapshots and the
protected store are custom because upstream has no notion of limited OS accounts, of the person's desktop session as a
separate trust zone, or of a batched agent-native desktop layer. Each is an add-on module with no change to upstream
files except one registration line per toolkit and setting, plus the small link changes of §10.

**When upstream changes the Linked environments beta:**

- **The beta becomes default-on or the flag goes away:** our check of `linkedEnvironmentsEnabled` is dropped; nothing
  else changes.
- **Forwarding changes shape:** the `host` toolkit is built exactly like upstream's own forwarded tools, so it breaks
  where theirs break and takes the same fix. The fork's follow job (nightly) shows it the same day.
- **Linked environments are removed or redesigned without forwarding:** fallback without new transport code: the
  agent machine adds the computer's `/mcp` as an outside MCP client (`docs/internals/environment-auth.md`, "MCP clients
  are a separate audience"). Same tools, same rules on the computer; only the settings in §4.3 would move.
- **Upstream adds its own remote file or command tools:** we compare and drop ours where theirs cover a tool, keeping
  only the OS-account rules, the helper and the snapshots.
- **Upstream ships link renewal or a lifetime setting (#33183):** we drop ours (§10).

Related upstream work, not merged on 2026-11-13: #36102 (moving work carries its files), #32639 (re-linking revokes the
old session), #35519 (a silent first address costs 10 s per call), #32158 (OpenShell worker fleet), #35826 (read-only
clients get a 403 for write tools), #34293 (host CPU and memory load in `t3_environment_read`; §11 can read it),
#26757 (Tailscale identity sign-in; §10 reuses the idea of checking the caller's tailnet node).

**Next to the fork's fleet runners (#13).** Separate features. Fleet runners run the fork's own CI jobs on fleet hosts
(root dispatcher, one Linux user per slot, one-job runners, `.github/fork-fleet.json`). Host tools give agents typed
access to a person's own computer. They share Tailscale, the tray (§12) and the activity service (§11), and no other
code or config in v1. A computer that is both a fleet host and a host-tools computer keeps them in **separate
accounts**, and neither account can reach the other's folders.

## 3. The two accounts (per OS)

Two local accounts per computer. Both are standard users, never administrators, and have no password anyone types (a
long random one held by the OS's task or service store, or none where the OS allows it).

- **Host account** (for example `t3-host`): runs the computer's T3 server. It owns T3's data (auth database, issued
  sessions, link state, its log). It runs no command, script or provider the agent supplies.
- **Agent account** (for example `t3-agent`): runs every command, build and script the agent starts, through the
  **command broker**, which runs as this account and accepts requests only from the host account. It cannot read or
  write T3's data, T3's secrets or the T3 install.
- The **T3 install** (program files) is owned by an administrator and read-only to both accounts. The computer's T3
  holds **no outbound link** to the agent machine, so nothing on the computer can reach back with T3's credentials.

Why two accounts (lens M1): if commands ran as the T3 server's own account, one hostile build script could raise a
link's ceiling, mint sessions, read link credentials, patch T3 to run its code at boot and call the engine with fake
caller details.

### 3.1 Per OS

|                                     | Windows (first)                                                                                                                                     | macOS                                                                                                         | Linux                                                                     |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Create both accounts                | local standard users, not in Administrators or Remote Desktop Users; "deny log on locally" and "deny log on through RDP" so nobody signs in as them | standard users, hidden from the login window (`IsHidden`), no admin group                                     | system users with a home, no sudo, shell login disabled for people        |
| Run T3 as the host account          | a Task Scheduler task "run whether the user is signed in or not", at boot (upstream installs "at logon, as me"; the fork adds `--account`)          | a LaunchDaemon with `UserName` (a login item would only run when that user logs in)                           | a systemd system unit with `User=`                                        |
| Run the broker as the agent account | a second such task; the broker's named pipe ACL admits only the host account                                                                        | a second LaunchDaemon; Unix socket owned by the host account's group, mode 0660                               | a second unit; same socket rule                                           |
| Reach of the agent account          | Modify on the reach map's folders (§4.3 rule 1), inherited, applied by the helper as the folders' owner                                             | ACL entries (`chmod +a`) by the folders' owner                                                                | POSIX ACLs (`setfacl`) or a shared group                                  |
| Secret and auto-run paths           | an explicit Deny ACE for the agent account on each match (§4.3 rules 2-3)                                                                           | a deny ACL entry                                                                                              | an ACL with no rights for the account (or the file left out of the group) |
| Disk quota (agent account)          | NTFS per-user quota on each volume it can write                                                                                                     | APFS has no per-user quota: the agent account's scratch lives on its own APFS volume with a quota (to verify) | `quota` / XFS or ext4 project quota                                       |
| Network                             | §3.5                                                                                                                                                | §3.5                                                                                                          | §3.5                                                                      |
| Screen, apps, clipboard             | none: neither account is in your session. Only through the engine (§6), which admits only the host account                                          | same                                                                                                          | same                                                                      |

### 3.2 How commands run

`host_run` asks the broker to start the program as the agent account, with `shell=false` by default and a shell only
when the caller asks for one, in a job (Windows job object / process group / cgroup) that is killed on timeout, on
pause and when T3 stops. The working folder must be inside the reach map. Either way, what a command can touch is what
the agent account can touch.

**Delegated full agents on the computer are off in v1.** Upstream starts provider processes from the T3 server, so a
delegated agent would run as the host account and could reach T3's data. They are enabled once the fork starts provider
and terminal processes through the broker (one spawn point, to locate in upstream code when built). Host tools need no
provider on the computer. This changes the r2 statement that delegated agents need nothing added.

### 3.3 Install verify (host tools stay off until it passes)

The install, and every later start, checks: both accounts exist and are in no admin or remote group; the install is
admin-owned and read-only to both; the broker's and the engine's channel ACLs are as stated; the reach map's grants,
the secret deny pass and the auto-run denies are applied and current; the firewall rules exist and each network class
of §3.5 is probed from the agent account; the disk quota is set. **Host tools stay off until every check passes**, and
`host_status` reports which one failed. `host_status` re-runs the quick checks on every call and the full set daily; a
failure turns host tools off again with the reason.

**Reach check.** Some places are readable by every user by default (Windows: folders on extra drives, `C:\Users\Public`;
macOS and Linux: world-readable shared folders). The install walks every fixed drive as the agent account and lists
what it can reach outside the reach map. You remove access there or accept it. The daily check reports new finds.

### 3.4 File operations never follow planted links (lens B2)

The agent account can create symbolic links, junctions and (where the OS lets it) hard links in folders it can write.
Anything that runs as the system, root, the host account or you and touches a path inside an agent-writable folder
(the guard's permission re-application, deny entries, snapshot restore, `host_undo`, the helper's copies, sandbox
copies) follows these rules:

1. It opens each path component without following reparse points or symlinks: Windows `FILE_FLAG_OPEN_REPARSE_POINT`
   and a check of the opened handle's attributes; Linux `openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
   RESOLVE_NO_MAGICLINKS`; macOS `O_NOFOLLOW_ANY`.
2. It works on the open handle (Windows `SetSecurityInfo` on the handle; `fsetxattr` / `acl_set_fd`), never on a path
   it looked up earlier.
3. It refuses a file whose link count is above 1 (a hard link to one of your files).
4. Before it writes or sets an ACL it checks the handle's final path (Windows `GetFinalPathNameByHandle`, `/proc/self/fd`
   or `F_GETPATH`) is under the allowed root.
5. Recursive permission application is our own walk that stops at every reparse point and symlink; it never uses an OS
   call that follows them.

Test (per OS): a planted junction to a system folder, a symlink to your profile and a hard link to one of your files do
not widen any grant, do not redirect any restore or copy, and are reported.

### 3.5 Network for the agent account (lens M3)

Default deny for the agent account, with these classes listed explicitly: loopback (`127.0.0.0/8`, `::1`), RFC 1918,
CGNAT and tailnet (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), link-local (`169.254.0.0/16`, `fe80::/10`, which includes
cloud metadata `169.254.169.254`), ULA (`fc00::/7`), multicast and broadcast. Allowed: the public internet (builds,
git, package downloads), and explicit entries you accept through the batched ask (for example a git server on your
LAN).

- **Loopback:** the agent account may reach only listeners of its own (tests that start a local server). Linux: each
  command runs in its own network namespace with its own loopback and a user-mode route out (`pasta`); macOS: `pf`
  `user` rules on `lo0` (to verify); Windows: a WFP filter on loopback that matches the connecting and the listening
  user (to verify). It never reaches your dev servers, a browser's debug port, a desktop T3, container APIs or sync UIs.
- **Per OS:** Windows Firewall / WFP rules scoped to the account's SID; macOS `pf` with `user`; Linux `nftables`
  `meta skuid`.
- The computer's T3 gives loopback callers **no** trust or bootstrap: other local accounts are remote clients that must
  sign in.
- The install verify (§3.3) probes one address of each class from the agent account and refuses to enable host tools
  if any answers.

## 4. Scope and rules

### 4.1 Goal-driven scope (owner decision 2026-11-13)

You say what you want done. The agent picks the route (§5). It may use the **whole computer except sensitive things**,
and it looks only at what is relevant to the task.

**Open by default:**

- Your data folders (the reach map, §4.3 rule 1): read and write, with undo.
- Every app and window, for seeing and acting, through the routes of §5 and the input ladder of §6.4.
- The clipboard (with the limits of §6.7).

**The sensitive set: never usable, no answer can open it** (hard refusals):

- Secret files by built-in pattern (keys, `.env`, `*.pem`, `*.kdbx`, cloud and CLI credential files, …) and every
  secret store: OS keychains and credential managers, browser login and cookie stores, password managers, wallets.
- Password and credential fields and dialogs, the lock screen and the secure desktop, elevation prompts.
- The tray, the helper, the ask dialog, T3's host-tools settings and the protected store (§4.3 rule 5).

**Relevance.** The agent names the windows and folders a step needs. The window list gives ids, apps and titles (a
title that matches a secret pattern is redacted). Trees and screenshots come only for named windows; other windows are
blacked out of a crop. A whole-screen capture needs a reason in the batch and is logged. Searches run in the folders
the task names, not across every drive.

**More specific: the narrow high-risk list** below needs the batched ask (§4.2).

### 4.2 The one batched ask

When the agent's plan needs anything from this list, it asks once for all of it: `host_ask {items, reason}`. The
helper shows one dialog: **"Can I use X, Y and Z?"**, each item with what it is and why it is on the list, a scope
(**this task** / **this project** / **always**) and a tick box; then Allow and Deny. Facts come first, the agent's
reason last and marked unverified; Deny is the default; one ask is open at a time; unanswered after 2 minutes it is
denied.

| On the high-risk list                                                                                                                                                                  | Why                                                                         | Default scope            |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------ |
| **Full-user-rights apps** for acting: shells and terminals, the Run dialog, the file manager, system settings, the task scheduler, the registry editor, script hosts, browser devtools | acting there runs any code as you, the whole policy depends on it (lens B3) | this task                |
| **Privilege elevation:** a command run as admin, a machine-wide install                                                                                                                | changes the OS for everyone                                                 | this task (one command)  |
| Installs for you (winget / Homebrew / the distro's package manager)                                                                                                                    | new code that runs as you                                                   | this task                |
| Your profile's config and app-data areas (`AppData`, `~/Library`, `~/.config`, dot-folders) beyond the reach map                                                                       | they hold tokens and sessions outside the built-in patterns                 | this project             |
| Auto-run paths in a folder (git hooks and config, IDE task files, mods and plug-in folders; §4.3 rule 3)                                                                               | code there runs as you without you choosing to run it                       | this project, per folder |
| Other users' data on the computer                                                                                                                                                      | not yours to give                                                           | this task                |
| A new app-side bridge, with its verbs listed (§6.6)                                                                                                                                    | a bridge acts inside an app with your rights                                | always                   |
| A network address in a blocked class (§3.5)                                                                                                                                            | your LAN and tailnet services                                               | this project             |

**Remembered.** Answers live in the protected store, per scope: the agent never asks twice for what you already
answered in that scope. A "this task" grant ends with the task (the T3 turn chain that asked), on pause, and on the
kill switch.

**Not worn down (lens m4).** After a Deny, the same item is not shown again in that task; a new ask with only items
denied in the last 10 minutes is dropped without a dialog; the dialog shows how many asks this task already made.

**Confirmed where the agent cannot click.** The engine refuses to see or act on the ask dialog. Items that grant
**full user rights or elevation** are also confirmed through the OS's own consent (Windows UAC on the secure desktop;
macOS authorization through `SecurityAgent`; Linux polkit), because once a full-rights grant is active, code with your
rights exists and could click a dialog in your session. For the same reason, **while a full-rights grant is active,
every widening of the policy needs that OS consent**; narrowing (Deny, pause, revoke) never does. This is one extra OS
prompt on top of A, forced by blocker B3.

### 4.3 Rules the computer enforces

Normal work needs no click. Each rule says **who enforces it**, because a command run as the agent account can change
anything that account owns. Nothing that must hold is enforced only by the T3 server.

1. **Reach map** (OS). The agent account gets read-write on your data folders: by default the profile's document,
   desktop, download, media and source folders and the data folders of extra drives, set at install and editable in
   the tray. Your profile root, its config and app-data areas, other profiles and system folders are not granted (they
   are on the ask list). The helper applies grants as the folders' owner; the guard service re-applies them daily
   (§3.4 rules) and reports drift. Removing a folder ends every agent-account process with an open handle under it
   (Windows Restart Manager, `lsof`/`/proc`), because an open handle keeps its access after an ACL change.
2. **Secret deny list** (OS, applied by the helper and the guard). Built-in patterns, one OS-neutral list in the core;
   the policy can add patterns, never remove a built-in one. Every match inside the reach map gets a deny entry. A
   watcher denies a matching file you create within about a second. **Fail closed:** on helper start, after a watcher
   overflow and after the daily re-apply, a full deny pass runs **before** file tools are enabled, and the server
   refuses file reads while the pass is not current (lens M8b). Token shapes (`ghp_`, `sk-ant-`, `AKIA…`, JWTs, …) are
   redacted in every output path and every stored record (§6.7).
3. **Auto-run paths are read-only to the agent** (OS deny entries, lens B1e). Inside writable folders: `.git/config`,
   `.git/hooks/`, `.git/info/`, `.gitattributes` filter lines are left alone but `.git/config` is where filters run;
   IDE task and settings files (`.vscode/`, `.idea/`), `.envrc`, shell profiles, the Startup folder and login items,
   and the mod and plug-in folders of apps you run. Code there runs as you when you merely open or `git status` the
   folder. Unlocked per folder through the ask. Git commands that write repo config (`push -u`, `remote add`) fail for
   the agent until then; the tool descriptions say so. Package manifests (`package.json`, …) stay writable: they run
   only when you choose to run them, and agent work is code (see §14.1).
4. **Nothing the agent wrote runs as you** (engine and helper, lens B1).
   - The engine never opens, launches or Shell-executes a file in an agent-writable folder if the type is executable
     or a script: `PATHEXT` types, `.lnk`, `.url`, `.scf`, `.hta`, `.msi`, `.desktop`, `.command`, `+x` files,
     `.app` bundles, and Office files with macros. Data files open in any app. COM servers (Office and similar) are
     created with macros forced off (`AutomationSecurity = msoAutomationSecurityForceDisable`).
   - Every launch, approved script, named command and admin command runs in a working folder the agent account cannot
     write (so no planted DLL is picked up).
   - Approved scripts, named commands and admin commands may not run files from agent-writable folders. A command that
     needs one (a deploy from a repo) runs from a **frozen copy**: the helper copies the referenced files, or makes a
     git worktree at a commit, into a protected folder; the ask shows the hash or commit; the copy runs. The same hash
     later needs no new ask.
   - A bridge whose file is in an agent-writable folder must be listed with its hash (§6.6).
5. **Protected store** (guard service, lens B3). The policy, ask answers, recipes, approved scripts, named commands,
   bridges and pause state live in a store owned by the guard service. Your account and both service accounts can only
   read it. Changes go through the guard: the helper asks, the guard writes, and widening follows §4.2. A store file
   edited outside the guard (by an admin) fails the guard's signature check on load and is ignored until you confirm it
   in the tray with OS consent. The engine refuses acting on any window whose document is in the store's folder and
   refuses file-dialog navigation into it.
6. **Undo** (guard service).
   - **Snapshots:** OS snapshots of the volumes in the reach map, taken by the guard before an agent write or run when
     the last one is older than 30 minutes (at most 48 a day, under the Windows Volume Shadow Copy limit of 64). Kept
     **at most 24 hours** (setting, shorter allowed) within a **tight size cap** (setting; default the smaller of 5 GB
     and 2 % of the volume). Windows: Volume Shadow Copy; macOS: APFS local snapshots (`tmutil localsnapshot`), which
     macOS keeps about 24 h anyway; Linux: btrfs/ZFS/LVM-thin snapshots where the folder lives on one, otherwise a
     root-owned `rsync --link-dest` copy of only the files the agent changed.
   - **Pre-images:** T3's own file tools (`host_write`, `host_move`, `host_delete`) keep the previous version of each
     file they change in the undo store, so their changes undo exactly, not only to the last snapshot.
   - **Writer recorded:** the guard records which account changed each file (Windows ETW kernel file events, macOS
     Endpoint Security, Linux `fanotify`). Where the writer cannot be told, the change counts as yours.
   - **Fail closed (lens M8a):** if a snapshot cannot be taken, or keeping it would push out one younger than 1 hour
     (setting), agent writes and runs are refused and the tray says why. The agent account's disk quota stops it
     filling the disk to push snapshots out.
   - **Agent undo restores only agent changes (lens M9):** an agent-called `host_undo` restores only files whose
     changes in the window came from the agent account, and only if the file still has the hash the agent left it
     with. Files you changed are listed, not touched; restoring them is offered only in the tray.
   - The guarantee: any file can go back to its state at the last snapshot before the change (exactly, for T3 file
     tool writes), within 24 hours. Changes **inside apps** are not covered unless the app saves into the reach map.
7. **Log** (guard service is the authority, lens m2). The guard's log records every file change with its writer,
   every batch and step the engine ran (app, target, action, rung, result, timing), every ask and answer, take-over,
   pause, wind-down and kill switch. The T3 log (host account) records every host tool call and its caller (§9): who,
   tool, arguments without file contents, result, bytes, time. Each line carries the hash of the line before; every 5
   minutes the guard countersigns the T3 log's head into its own log, so a forged or cut T3 log shows. A call whose
   log write fails is refused.
8. **Pause, wind down, kill switch, revoke all** (tray, §12). _Pause_: every host tool and every new turn is refused
   with "paused by owner"; **every agent-account process** is ended (the T3 server is the host account, so nothing it
   needs survives; daemonised children and scheduled runs go too, lens m1); batches stop; held keys and buttons are
   released; "this task" grants end. _Wind down_: §12.2. _Kill switch_: the guard ends every agent-account process,
   stops the engine and the host account's T3, and disables both accounts until you turn them back on. _Revoke all_:
   every link to and from this computer is revoked (§10). Pause and wind-down rely on the guard and the helper; the
   kill switch relies only on the OS.
9. **Availability and who may call** (agent machine, then the computer; lens M6).
   - Per computer, on the agent machine: Settings → Linked environments → _computer_ → Host tools: **on** (default),
     **off**, or **off until** a time. Stored in `ServerSettings`.
   - Per project, on the agent machine: **host tools for this project** on or off (a project setting, default off for
     new projects, one toggle each). Your own machines and projects are trusted; this keeps them away from projects
     that mostly read outside text.
   - Per thread: a thread marked **outside input** (set by you, or by a launcher for work on outside contributions,
     public issues or web research) gets the read tools at most.
   - The agent machine forwards the calling project and thread id with each call; the computer logs it and the tray
     shows it while acting.
   - These are convenience switches on the agent machine; the computer's own pause, wind-down and kill switch win.

**No activity gate.** Host tools do not check whether you are gaming or busy. Work you asked an agent to do runs while
you do anything else. Only §11's background runners adapt to the free hardware.

## 5. Seeing and acting: principles (owner decision, speed first)

The agent works in your apps through our own layer, built from scratch for agents, not by imitating a person with a
mouse and screenshots. Seven rules:

1. **Most suitable tool first, pixels last.** For each job the agent uses the first route that works:
   1. **shell, CLI and files** as the agent account (`host_run`, file tools): a build, a git command, a file export;
   2. **app and OS automation APIs**, through typed adapters in the engine: Windows COM / WinRT / Shell, macOS Apple
      Events (AppleScript, JXA) and app scripting dictionaries, Linux D-Bus (desktop portals, MPRIS, app interfaces);
   3. **app-side bridges** (§6.6): a game mod, a browser over the DevTools protocol (CDP), an editor extension;
   4. **accessibility-tree actions on elements by id**: invoke, toggle, set value, select, expand, scroll into view,
      with no mouse movement;
   5. **input**, down the input ladder of §6.4: window-targeted events, then the agent's own screen, then real
      synthetic input on your desktop only last.
2. **Few round trips.** One `host_do` call carries a list of steps (§7), with waits on conditions that the engine checks
   **on the computer**, and returns only the results and what changed. No screenshot polling over the network.
3. **Compact observations.** Filtered text snapshots, then **only what changed** (§6.2). Screenshots only when asked
   for or when an app has no structure, cropped and scaled down.
4. **A latency budget with numbers** (§8), measured against a mouse-and-screenshot baseline.
5. **Reusable named recipes** (§7.3), replayed at engine speed, each remembering which input rung works.
6. **Invisible by default** (owner decision 2026-11-13, §6.8).
7. **Try alternatives at once when unsure** (owner decision 2026-11-13, §7.4), on private copies.

## 6. Seeing and acting: the engine

### 6.1 Who runs it, and how that fits the accounts

Files and commands run as the **agent account** (§3). Seeing and using your apps cannot work that way: only a process
in **your** desktop session, as you, can read your windows' accessibility trees, capture your screen and send input to
your apps (Windows session isolation and UIPI; macOS grants Accessibility and Screen Recording to an app in your GUI
session; Wayland gives these only to the session's own clients). So:

- The **engine runs as you**, started by the session helper, and is the only part with that power.
- It has a **local channel** with a fixed set of verbs: a Windows named pipe or a Unix socket whose ACL admits **only
  the host account** (the T3 server). The agent account, its commands and speculative runs cannot connect (lens M1,
  M2). The engine still **enforces the policy itself**: pause, wind-down, the sensitive set, the high-risk list, the
  file-type rule (§4.3 rule 4), limits, and its own log line to the guard. The T3 server's checks come first but are
  not relied on.
- The engine runs **no free-form script as you**. Automation adapters are typed verbs shipped with the fork. A raw
  script (PowerShell, AppleScript, a shell line) runs as you only if it is an **approved script** in the protected
  store, from a frozen copy (§4.3 rule 4). Agent-written scripts run as the agent account through `host_run`.
- The engine never runs elevated. Windows: no `uiAccess`, medium integrity, so admin windows are out of reach by UIPI.
  macOS: no root helper. Linux: no root.
- **Heartbeat (lens M8d):** the helper draws the indicator; the engine stops acting within 500 ms when the helper's
  heartbeat stops, releases held input, and refuses acting until the helper is back. It never acts without an
  indicator.

**What this means plainly:** with A's open scope, the agent can do in a non-sensitive app what you could do there by
hand, with your rights. An app that runs code (a terminal, the Run dialog, the file manager) is the way around every
other rule, which is why those need the batched ask (§4.2) and why files the agent wrote never run as you.

### 6.2 Seeing: sources, which wins, and how the agent finds them

`host_observe` returns, in one call:

- **Windows and focus:** every visible top-level window (id, app, process, redacted title, bounds, monitor,
  minimised, focused), and which element has keyboard focus. Cheap; always included.
- **One structured snapshot per named window**, from the best source that has data for it:

| Order | Source                     | Used when                                                                                    | What it gives                                                                 |
| ----- | -------------------------- | -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| 1     | **app-side bridge** (§6.6) | a bridge is allowed for that app and answers                                                 | the app's own state, exact (what the player looks at, inventory, tooltips, …) |
| 2     | **automation adapter**     | the app has a typed adapter (§5) whose read verbs cover the request                          | documents, selections, open items, as data                                    |
| 3     | **accessibility tree**     | the app exposes one (most normal apps, Electron with accessibility on, browsers' chrome)     | elements with id, role, name, value, state, bounds, available actions         |
| 4     | **screenshot**             | asked for, or sources 1-3 give a **sparse** result (games, canvases, remote-desktop windows) | a cropped, scaled image of the window or a region                             |

Sources 1 and 3 are combined when both have data (a browser: CDP for the page, the tree for the browser's own UI).
A tree is **sparse** when a visible window of more than a small area has fewer than a few named elements; the
snapshot then says `sparse: true` and suggests a bridge or a screenshot.

**Snapshot format.** Indented text, one element per line, `[id] role "name" = value {states} @x,y,w,h`. Filtered by
default: only visible, named or actionable elements; containers with one child are folded; long lists are cut to the
visible part with a count. A size cap (default 6,000 tokens per window) cuts deepest-first and says so. Element ids
are stable for as long as the element lives, so a step can name `[e412]` minutes later. **Selectors**
(`role=button name="Save" in window title~"Report"`) are the stable form for recipes.

**Diffs.** Every snapshot has an id. `host_observe {since: <id>}` returns only added, removed and changed lines. The
engine keeps a live cache of each watched window's tree, updated from the OS's change events (UI Automation event
handlers with a cache request; macOS `AXObserver` notifications; AT-SPI events over D-Bus), so a diff is a read from
memory, not a new walk of the app.

**Screenshots.** Window or region crop, scaled to a maximum edge (default 1280 px), WebP or JPEG. A `zoom` region
returns a sharper crop. Windows of the sensitive set, and windows not named, are blacked out. The on-screen indicator
is excluded from captures (Windows `WDA_EXCLUDEFROMCAPTURE`; macOS window `sharingType = none`; Linux: left out of the
crop where the compositor allows, to verify).

**Untrusted marking.** Everything read from an app, a page, a bridge or a file is marked in the result as untrusted
content, so the agent and its tool descriptions treat instructions in it as data.

**Discovery.** `host_status` lists what the computer offers: the OS and its accessibility back end and permission state
(macOS Accessibility and Screen Recording granted or not), the input rungs available (§6.4), the allowed bridges and
their verbs, the automation adapters, the high-risk grants in force and their scopes, and the saved recipes.
`host_observe` names the source used per window.

### 6.3 Acting

All acting goes through `host_do` (§7). The step kinds, in the order agents should prefer them:

| Step kind   | What it does                                                                                            | Per-OS back end (Windows / macOS / Linux)                                                                                                            |
| ----------- | ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run`       | a command as the agent account (same as `host_run`)                                                     | broker; job object / process group / cgroup                                                                                                          |
| `app`       | a typed automation verb (open a file in an app, read a document, menu command)                          | COM, WinRT, Shell / Apple Events, scripting dictionaries / D-Bus, portals                                                                            |
| `bridge`    | a verb of an app-side bridge (§6.6)                                                                     | the bridge's own channel                                                                                                                             |
| `el`        | an element action by id or selector: invoke, toggle, set value, select, expand, focus, scroll into view | UIA patterns (Invoke, Toggle, Value, SelectionItem, ExpandCollapse, ScrollItem) / AX actions and attributes / AT-SPI Action, EditableText, Selection |
| `window`    | focus, move, resize, minimise, close a window; launch an app                                            | Win32 / AppKit / X11 EWMH or the compositor's portal                                                                                                 |
| `clipboard` | read or set the clipboard (§6.7)                                                                        | Win32 / NSPasteboard / X11 selections, Wayland data-control                                                                                          |
| `input`     | keys, text, clicks, drags, scrolls at a window-relative point or element, down the ladder of §6.4       | §6.4                                                                                                                                                 |
| `wait`      | until a condition holds or a timeout (§7.1)                                                             | event-driven where the OS sends events, else a 25 ms poll                                                                                            |
| `observe`   | add a snapshot or diff (or a crop) to the result at this point                                          | §6.2                                                                                                                                                 |

`app open` and `window launch` follow the file-type rule of §4.3 rule 4. `input` steps aim at an element (its centre,
or an offset in it) or at a point relative to a window, never at a bare screen point.

### 6.4 The input ladder (owner decision 2026-11-13)

The agent **may** simulate mouse and keyboard when it needs to, but must **never take over your real cursor or
keyboard when a lower-level way exists**. Each `input` step (and each `el` action) runs on the fastest, least intrusive
rung that works for that app; the engine steps down only when a rung did not work.

| Rung | How                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Reaches                                                                                                                                                                                            | Cannot reach                                                                                                                                                                                                                                                                                  | Speed (p50)                                                                   | Your cursor and keys                                  |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------- |
| 1    | **Actions with no coordinates:** UIA Invoke / Value / Toggle patterns; macOS AX actions and `AXValue`; AT-SPI Action and EditableText; app bridges; CDP `Input.dispatchMouseEvent`, `Input.dispatchKeyEvent`, `Input.insertText` for the agents' browser                                                                                                                                                                                                                                                                                    | standard controls in most apps, browser pages, any app with a bridge, background windows                                                                                                           | custom-drawn UIs, canvases and games without a bridge; controls that expose no pattern                                                                                                                                                                                                        | dispatch ≤ 10 ms; 1,000 chars of text in one `set_value`/`insertText` ≤ 20 ms | untouched                                             |
| 2    | **Events sent to one window** at window coordinates, cursor not moved: Windows `PostMessage`/`SendMessage` of `WM_*BUTTON*`, `WM_MOUSEWHEEL`, `WM_KEYDOWN`/`WM_CHAR` to the window handle; macOS `CGEventPostToPid`; X11 `XSendEvent` (xdotool `--window`)                                                                                                                                                                                                                                                                                  | classic Win32 and many toolkit apps, many macOS apps for keys, X11 apps that accept sent events                                                                                                    | games using Raw Input, DirectInput or `GetAsyncKeyState`; some Chromium content areas, UWP/WinUI windows and apps that check real key state; windows of higher integrity (UIPI blocks them); X11 apps that drop `send_event` events (xterm by default); native Wayland apps (no such channel) | ≤ 1 ms per event; 1,000 chars ≤ 60 ms                                         | untouched                                             |
| 3    | **The agent's own screen and input.** Windows: a hidden desktop in your session (`CreateDesktop`) for apps that need your logins, or the agent account's own non-interactive window station for apps that do not; a loopback RDP session of the agent account on editions that allow several sessions. macOS: a virtual display or a second background GUI session (both to verify; weakest OS here). Linux: Xvfb or a headless compositor (weston or wlroots headless, gamescope headless) as the agent account, or nested in your session | apps the agent opens for its own use; on Linux real XTest/libei input into its own display (games included); on Windows and macOS rungs 1-2 inside it (`SendInput` reaches only the input desktop) | apps already open on your screen (they would have to be reopened there); on Windows and macOS, apps that need real hardware input; GPU apps where the hidden display has no GPU (to verify per OS)                                                                                            | as the rung used inside it, plus the app's start                              | untouched                                             |
| 4    | **Real synthetic input on your desktop:** `SendInput` / `CGEventPost` / XTest on X11, libei through the RemoteDesktop portal on Wayland (`uinput` only through a root-owned guard device as a last fallback, to verify)                                                                                                                                                                                                                                                                                                                     | everything you can reach by hand                                                                                                                                                                   | elevated and secure-desktop windows (UIPI, refused anyway)                                                                                                                                                                                                                                    | ≤ 2 ms per event; 1,000 chars ≤ 300 ms with the checks below                  | **used**: needs `visible: true`, indicator, take-over |

**How the engine knows a rung did not work, and steps down:**

- Each action has an **expected effect**, given by the step or defaulted per verb: invoke → a tree event or diff on
  the target; set value / typed text → the element's value; a click → a focus, selection or tree change, or, with no
  tree, a pixel change in the target's crop; a bridge verb → its own result. The engine checks it within the rung's
  settle time (event-driven; default at most 300 ms).
- **Immediate step-down:** the rung's API fails (UIA `E_ACCESSDENIED`, no pattern, `SendMessage` blocked by UIPI,
  `CGEventPostToPid` refused, a native Wayland window for rung 2), or the app is on the engine's built-in list for that
  rung (for example Raw Input games skip rung 2).
- **No double apply.** Before stepping down after no effect, the engine waits until the app is idle (Windows
  `SendMessageTimeout(WM_NULL)` returns, `WaitForInputIdle`; macOS and Linux: no tree events for the settle time) and
  checks again. For an action that is not safe to repeat (typed text, invoke of a button that sends or deletes) it
  steps down only if the check is clearly negative (value unchanged, no event); otherwise the step returns `unknown`
  and the agent decides from a fresh diff.
- **Rung memory.** The rung that worked is recorded per app and app version (and in recipes), so the next step starts
  there. A step can pin a rung or cap the lowest rung (`maxRung: 3` = never touch the real mouse).
- Rung 4 is used only in a batch marked `visible: true` (§6.8); otherwise the step fails as `needs_visible` with the
  rungs tried.

**Security fixes fitted to each rung (lens M5):**

| Rung | Focus and target race                                                                                                                                                                                                                                                                                           | Typing                                                                                                      |
| ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| 1    | none: actions name the element; the engine checks before each action that the element still belongs to the same window and process (runtime id + pid)                                                                                                                                                           | `set_value` / `insertText` in one call; refused on password fields                                          |
| 2    | none from focus (events go to a handle, not the foreground); between chunks the engine checks the handle still belongs to the same process, thread and class (handles are reused)                                                                                                                               | `WM_CHAR` in chunks of 32 characters with that check between chunks                                         |
| 3    | none with your desktop; inside the agent's own display the same checks as the rung used there                                                                                                                                                                                                                   | as the rung used there                                                                                      |
| 4    | Windows `LockSetForegroundWindow(LSFW_LOCK)` for the burst; events in chunks of at most 8 with a foreground, target and pid check between chunks; abort on a foreground change (`EVENT_SYSTEM_FOREGROUND`, the `NSWorkspace` activation notification, `_NET_ACTIVE_WINDOW`), release held input, return `stale` | chunks of at most 8 key events with the same checks; text goes through rung 1 whenever the target allows it |

**Take-over by rung.** Rungs 1-3 never move your pointer or type into the window you use, so the agent keeps working
while you use other windows. Take-over (§6.7) stops a batch on rungs 1-2 only when your real input goes to the same
window the agent is acting in. Rung 4 stops at any real input.

**Rules that hold on every rung:** the sensitive set, the high-risk list (keys sent to a terminal by `PostMessage` are
still acting in a full-user-rights app), the file-type rule, the indicator (for rungs 1-3 a quiet banner "agent working
in _app_"; rung 4 the full border), the log.

### 6.5 Browsers (lens M10)

- **The agents' browser, headless:** runs as the **agent account** on its own display (rung 3), so a page it visits has
  the agent account's file rights, not yours. CDP over a pipe only the engine holds (`--remote-debugging-pipe`), no
  local port.
- **The agents' browser, visible** (a task needs it on your screen): runs as you with browser policy that blocks
  `file://` and other local schemes, pins downloads to a folder in the reach map, allows no extensions and no sync
  sign-in, and turns off the password manager and autofill.
- **CDP is never passed through:** the engine exposes typed verbs (navigate, read the page tree, element actions, the
  `Input.*` calls of rung 1, screenshot), not raw protocol.
- **Your own browser** (your logins): through its accessibility tree, or a bridge extension you allow (§6.6), which
  also offers typed verbs only. Its password manager pages, autofill and saved-login screens are in the sensitive set.

### 6.6 App-side bridges

For apps with no useful accessibility data (games, canvases, custom-drawn editors), the app itself can expose its state
and verbs. A **bridge** is a plug-in inside the app (a game mod, an editor extension) that connects to the engine's
**bridge endpoint** (a local pipe/socket for processes running as you) and speaks **MCP**: its tools are its verbs,
each marked read or act, with a JSON schema.

- **Allowing.** A new bridge shows its name, app and verb list in the batched ask (§4.2), one tick box per verb (all
  ticked by default); verbs you untick stay off. A bridge whose file is in an agent-writable folder must be listed with
  its hash, and a changed hash puts it back to "waiting".
- **Use.** Read verbs feed `host_observe` (source 1 in §6.2); act verbs follow the same rules as other acting,
  including take-over: while you are using that app, its act verbs wait. Bridge read data (game chat, page text) is
  marked untrusted.
- **Why preferred:** exact, fast (one local call), needs no window in front, never touches your cursor (rung 1).
- **Files** the app uses (worlds, saves, configs) are reached through the reach map and the file tools.

### 6.7 Safety rules for seeing and acting

- **The sensitive set is refused** (§4.1): lock screen and secure desktop (Windows: input desktop is not `Default`,
  UAC, Ctrl+Alt+Del; macOS: `loginwindow`, `SecurityAgent`; Linux: the screen locker and polkit agents); password
  fields (UIA `IsPassword`, AX secure text fields and `IsSecureEventInputEnabled`, AT-SPI `password text`): never read,
  never typed into; credential dialogs; elevated windows; password managers and secret stores in the built-in list; a
  window whose document matches a secret pattern; the tray, helper, ask dialog and T3's host-tools screens; windows and
  file dialogs inside the protected store's folder. Seeing and acting return `refused` with the reason.
- **Full-user-rights apps** act only under an active grant (§4.2); without it they can be seen, not acted on.
- **Inactive session (lens m3):** acting on your desktop (rungs 1, 2, 4) is refused while your session is locked,
  disconnected, switched away or remote; rung 3 work in the agent account's own display goes on.
- **On-screen indicator.** While a batch acts, the tray icon changes and a small banner names the app, the step and the
  rung. On rung 4 a coloured border runs around the screen and the agent's pointer is drawn. Drawn by the helper; the
  engine stops without the helper's heartbeat (§6.1).
- **You take over at once.** The engine watches real input and tells it apart from its own (Windows: low-level hooks,
  `LLMHF_INJECTED` / `LLKHF_INJECTED`; macOS: an event tap, our events carry a source tag; X11: XInput2 raw events by
  device; Wayland: the compositor's idle and input signals, to verify per compositor). On take-over (rules per rung in
  §6.4): the running step stops, every key and button the engine holds is released, queued steps are dropped, and the
  batch returns `interrupted_by_user` with what ran and a fresh diff. A new acting batch on that window starts when you
  have been idle for a set time (default 5 s; open point 1) or press "let the agent continue".
- **Clipboard (lens M4).** Read is refused when the content carries a concealed or excluded marker (Windows
  `ExcludeClipboardContentFromMonitorProcessing` or `CanIncludeInClipboardHistory = 0`; macOS
  `org.nspasteboard.ConcealedType`; KDE `x-kde-passwordManagerHint`), and token shapes are redacted. A write counts as
  acting: logged, indicator on, and the previous content is **put back when the batch ends** (unless the step says the
  result is for you), so nothing the agent placed waits to be pasted into a terminal.
- **Redaction everywhere (lens M4):** token-shape redaction applies to every output path and every stored record:
  observe text, titles, diffs, bridge results, journalled results returned on retry, `host_run` arguments and output,
  both logs. Typed values are logged as length only when the target is a field marked sensitive or the text matches a
  token shape.
- **Limits.** Per batch: steps (default 200), wall time (default 2 min), typed characters, and waits (each needs a
  timeout). A batch is checked as a whole before it starts and each step again when it runs.

### 6.8 Invisible by default (a tested requirement)

**Owner decision (2026-11-13):** everything the agent runs on the computer (commands, scripts, batches, adapters,
helpers) runs in the background: no window is shown, none comes to the front, focus is never taken, and no console
flashes. Only UI work you asked for touches the visible screen. **Requirement: no window ever pops in front of you
unless a step you asked for put it there.**

**Visible steps are declared.** A `host_do` batch may contain visible steps (rung 4 input; `window` focus, move, launch
shown; `el` actions that need the window in front; `app` verbs marked `visible`) only if the batch says
`visible: true`. That flag turns on the full indicator and is logged. Without it, visible steps are refused before the
batch starts, and every other step runs off-screen (rungs 1-3 need no window in front).

**How each part stays off-screen:**

| Part                             | Windows                                                                                                                                                                                                                                                                     | macOS                                                                                                                                                                 | Linux                                                                                                                                      |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `host_run` (agent account)       | the broker runs from a "whether signed in or not" task, so its processes live in a non-interactive window station and **cannot** draw on your desktop; children are started with `CREATE_NO_WINDOW` anyway, a pseudo-console (ConPTY) only when a program needs a terminal  | a LaunchDaemon has no GUI session; processes cannot reach the window server                                                                                           | no `DISPLAY`, `WAYLAND_DISPLAY` or session bus is passed; the unit has no access to your session's sockets                                 |
| engine and helper (as you)       | engine is a GUI-subsystem binary with no window; every child: `CREATE_NO_WINDOW` for console programs, `STARTF_USESHOWWINDOW` + `SW_HIDE` for GUI ones; PowerShell `-NoProfile -NonInteractive -WindowStyle Hidden` started without a console, never through `cmd /c start` | engine and helper are `LSUIElement` / background-only; apps opened with `open -g -j` or Launch Services with "don't activate"; scripts use `launch`, never `activate` | apps started without an activation token (`xdg-activation` on Wayland; no `_NET_WM_USER_TIME` on X11), so compositors refuse to focus them |
| automation adapters (§5 route 2) | COM servers created with `Visible = false` / `DisplayAlerts = false` and macros forced off; WinRT and Shell verbs that need no UI                                                                                                                                           | Apple Events to a running app or a hidden launch; scripting dictionaries without `activate`                                                                           | D-Bus calls, no window                                                                                                                     |
| browser work                     | the agents' browser runs headless as the agent account unless the batch is `visible` (§6.5)                                                                                                                                                                                 | same                                                                                                                                                                  | same                                                                                                                                       |
| rungs 1-3                        | UIA patterns and window messages work on windows that are not in front; the engine does not call `SetForegroundWindow`; rung 3 windows are on a desktop you do not see                                                                                                      | AX actions without `AXRaise`; `CGEventPostToPid` does not activate                                                                                                    | AT-SPI actions without focus; rung 3 on its own display                                                                                    |

**Watched while it runs.** While a non-visible batch or `host_run` runs, the engine watches for new shown top-level
windows and for foreground changes (Windows `SetWinEventHook` for `EVENT_OBJECT_SHOW` and `EVENT_SYSTEM_FOREGROUND`;
macOS `NSWorkspace` activation notifications plus the window list; X11 `MapNotify` and `_NET_ACTIVE_WINDOW`; Wayland:
per compositor, to verify). A window shown by a process the agent started: the engine hides it, gives the front back to
the window you had, logs it, and the step's result carries `visible_side_effect` with the program name. A program that
keeps showing windows when hidden is stopped and the step fails with `visible_side_effect`. Windows from your own
programs are never touched.

**Tested.** A "no pop-in" test runs on each OS in fork CI (where a runner with a desktop session exists) and before an
OS is enabled: with a test window in front and focused, it runs every host tool, every adapter verb, every input rung
1-3, a set of console and GUI programs (including ones that try to show a window, a console program under Windows
Terminal as the default terminal, PowerShell, a script interpreter) and every saved test recipe, while the same watcher
records events. Any shown window, focus change or cursor movement not caused by a step marked visible fails the test.
Each new adapter verb or bridge must pass it before it ships.

## 7. Batches, waits and recipes

### 7.1 The step list

`host_do` takes a JSON list of steps (§6.3), not a programming language, so the engine can check the whole list against
the policy before it starts and the log shows exactly what ran. Each step can name an `id`, a `timeout`, an
`onFail` (`stop` by default, `continue`, or `goto` a later step), an `expect` (§6.4), a `maxRung`, and can bind a
result to a name that later steps use (`${found.id}`). There are no loops beyond `wait` and a bounded `repeat` (count
cap).

**Conditions** for `wait`, `assert`, `expect` and `goto`: an element exists / is gone / has a value or state (by id or
selector); a window with a title or app is open / in front / closed; a process runs / exits; a file exists / changed;
the clipboard matches; a bridge read verb's result matches a JSON path; any of / all of these.

**Result:** per step `ok` / `failed` / `refused` / `stale` / `unknown` / `needs_visible` / `interrupted_by_user` /
`skipped`, its outputs, the rung used, its time; then a diff of the touched windows since the batch started (or since
the snapshot id given), and crops only if asked.

Example (placeholders): export a report from an app and wait for the file, in one call:

```json
[
  { "app": "open", "app_id": "<app>", "path": "<reach map folder>/report.src" },
  { "wait": { "window": { "title~": "report" } }, "timeout": 5000 },
  { "el": "invoke", "selector": "role=menuitem name=\"Export…\"" },
  { "wait": { "element": "role=dialog name~\"Export\"" }, "timeout": 3000 },
  {
    "el": "set_value",
    "selector": "role=edit name=\"File name\"",
    "value": "<reach map folder>/report.pdf"
  },
  { "el": "invoke", "selector": "role=button name=\"Save\"" },
  { "wait": { "file": "<reach map folder>/report.pdf" }, "timeout": 10000 }
]
```

### 7.2 Idempotency

Every `host_do` carries an idempotency key. The engine journals each step (planned, started, done, with its result)
before and after it runs, with an fsync, so the journal survives a power cut. A retry with the same key returns the
journalled result and never runs a done step again; a step that was started but not confirmed is reported as `unknown`
and is not re-sent (idea from agentcast, #28050). The agent decides from a fresh diff.

### 7.3 Recipes

A recipe is a **named, parameterised step list** saved on the computer, for example `<app>.export-pdf(path)`.

- **Save.** `host_recipe save` (name, description, parameters, steps, the app and its version, the rung per step). The
  guard stores it in the protected store, versioned, with a content hash; saving the same content again is a no-op.
- **Run.** `host_recipe run` or a `recipe` step: the same checks as a fresh batch, at engine speed, one network hop.
  Recipes use selectors, not element ids.
- **Repair.** When a step fails because the app changed, the run stops there and returns the diff; the recipe is marked
  `needs repair` until a new version passes.
- **Review.** Recipes show in the tray with their steps; you can delete or pin them. A recipe never widens the policy.
- Recipes are per computer in v1.

### 7.4 Speculative parallel batches

**Owner decision (2026-11-13):** when the agent is unsure which of two or more ways works (which command finds the
file, which selector the app uses, which build flag is right), it may run the alternatives **at the same time** and
keep the one that works.

**Shape.** `host_do { alternatives: [steps, steps, …], pick, check }`, one idempotency key for the whole call.

**Private copies only (design choice, owner left it to us).** Every alternative works on its own private copy; the r2
option of writing to real folders and restoring the losers from a snapshot is dropped. Why: restoring real folders
races with your own edits (the same problem as lens M9), depends on snapshots that are now small and at most 24 hours
old, and needs disjoint-path bookkeeping that a crash can leave half done. Large folders do not need a full copy: each
alternative sees the real folders **read-only** plus a private writable layer (Linux overlayfs; macOS APFS `clonefile`
of only the declared write paths; Windows a copy of only the declared write paths, block clone on ReFS/Dev Drive).

**Which steps may run speculatively.** The engine classes every step before anything starts and refuses the call if
any alternative has a step outside these classes:

1. **read-only:** `observe`, `wait`, `assert`, element and bridge **read** verbs, `app` read verbs, file reads, and
   `run` steps (which see the read-only view);
2. **writes into the alternative's own private layer:** its `run` and file steps write only there.

**Never speculative:** `input` on any rung, element and bridge **act** verbs, `window` and `clipboard` changes, `app`
act verbs, `host_ask`, `host_named`, approved scripts, and anything that sends over the network.

**The OS enforces the sandbox (lens M2),** not the agent's claim:

- Linux: a mount namespace with the real folders bound read-only and only the private layer writable; a fresh network
  namespace with no interfaces; no access to `/run/user`, abstract sockets or any host socket.
- macOS: a sandbox profile with file-write limited to the private layer, network denied, and no Mach or Unix-socket
  IPC to the engine, the broker or T3; each run under a per-slot hidden user from a small pool, so cleanup ends every
  process of that user (process groups do not hold `setsid` daemons).
- Windows: an AppContainer with no capabilities and ACEs only on the private layer; the engine's and broker's pipes
  carry no AppContainer ACE, so they refuse it.
- The private layer is made **by the agent account**, so denied files cannot be read and are skipped, and copies of
  secret-pattern files are never made. A denied `.env` never becomes readable in a copy.
- Speculative processes cannot reach the engine, the T3 server or any local socket (also because the engine admits only
  the host account, §6.1).

**Sized from the task and the free hardware (owner decision 2026-11-13), no fixed numbers.** Each alternative's need
(peak memory, CPU time, disk I/O) comes from that command's history on this computer, or a default estimate the first
time. The engine starts as many alternatives at once as fit into the free hardware measured by the activity service
(§11) minus its buffer, and queues the rest within the same call. If not even one fits, they run one after another.
This sizes parallelism only; it never delays work you asked for (no activity gate).

**Picking the winner and comparing results.**

- `pick: "first"` (default): the first alternative whose steps all end `ok` **and** whose `check` holds wins. `check`
  is a §7.1 condition evaluated against that alternative's results and private layer. With no `check`, "all steps ok"
  is the test. Ties go to the earlier one in the list.
- `pick: "all"`: every alternative runs to its end or the batch timeout; the result lists each with its outcome, a short
  redacted output digest and the diff of its layer, so the agent compares them in one look. Layers are kept until the
  agent applies one (`host_do { apply: <alternative> }`) or 10 minutes pass.
- The result says which alternatives ran, which won, why each loser lost (`failed`, `check_false`, `cancelled`,
  `timeout`), and the time each took.

**Cancelling and cleaning up losers.** As soon as a winner is known (`first`) or the agent applies one (`all`): each
loser's processes are ended (job object / cgroup / per-slot user: a polite stop, then a hard kill after 2 s); its
private layer is deleted; its waits, watchers and cached trees are dropped; it is logged as `cancelled_speculative`.

**Applying the winner.** Read-only winners just return their results. A winner's changes are applied to the real
folders as one write burst: snapshot first (§4.3 rule 6), then each file compare-and-swapped against the hash it had
when the layer was made, through the link-safe operations of §3.4. If any real file changed meanwhile, nothing is
applied and the result is `conflict` with the list.

## 8. Speed: budget, measurement, and why not classic computer use

### 8.1 Latency budget

Targets on a normal desktop PC, measured inside the engine and on the agent machine:

| Item                                                      | p50 target                                                | p95 target | Note                               |
| --------------------------------------------------------- | --------------------------------------------------------- | ---------- | ---------------------------------- |
| network hop, agent machine ↔ computer (tailnet, same LAN) | ≤ 5 ms                                                    | ≤ 20 ms    | through a relay: tens of ms        |
| batch overhead (policy check, journal, log)               | ≤ 3 ms                                                    | ≤ 10 ms    | per batch                          |
| window list + focus                                       | ≤ 5 ms                                                    | ≤ 20 ms    |                                    |
| snapshot diff of a watched window (warm cache)            | ≤ 15 ms                                                   | ≤ 60 ms    | read from the event-fed cache      |
| first snapshot of a window (cold)                         | ≤ 150 ms                                                  | ≤ 400 ms   | one cached tree walk               |
| rung 1 action dispatch (invoke, set value)                | ≤ 10 ms                                                   | ≤ 40 ms    | plus the app's own reaction        |
| rung 2 window event                                       | ≤ 1 ms                                                    | ≤ 5 ms     | 1,000 characters ≤ 60 ms           |
| rung 4 synthetic input event                              | ≤ 2 ms                                                    | ≤ 10 ms    | 1,000 characters ≤ 300 ms, checked |
| step-down to the next rung (no effect seen)               | ≤ 300 ms                                                  | ≤ 600 ms   | once per app version (rung memory) |
| bridge verb, local                                        | ≤ 5 ms                                                    | ≤ 30 ms    | plus the bridge's own work         |
| wait reacting to its condition                            | ≤ 30 ms                                                   | ≤ 60 ms    | event-driven; 25 ms poll otherwise |
| crop + scale + encode screenshot (1280 px edge)           | ≤ 40 ms                                                   | ≤ 100 ms   | only when asked                    |
| observation size                                          | diff ≤ 1.5k tokens typical; window snapshot cap 6k tokens |            | screenshot about 1-1.5k tokens     |

**Task level:** the model turn (seconds) is the cost that matters. Target: a typical UI task in **1-3 model turns**
(observe, one batch, sometimes one fix), against 10-40 turns for classic computer use.

### 8.2 How it is measured

- **Engine micro-benchmarks** (no model): every item in §8.1 against stock OS apps per OS and test apps the fork ships
  (a small accessibility-rich app, a canvas app with a test bridge, a test web page, a test app that accepts only real
  input). They run in fork CI on each OS where a runner with a desktop session exists, nightly; a p95 more than 25 %
  worse than the last baseline turns the job red.
- **Task benchmark** (with a model, run by hand or weekly, since it costs tokens): about 20 general tasks (change a
  setting, fill and save a form, export a file from an app, find a value in a long list, a multi-page web flow, read and
  act on a canvas app through its test bridge), each in three modes on the same computer, model and link:
  1. **native**: everything in §5;
  2. **tree only**: no adapters, bridges or recipes;
  3. **pixels**: screenshots and rung 4 input only, one action per model turn: the classic computer-use baseline.
     Recorded per task: success, wall time, model turns, input and output tokens, the rungs used, and the engine's own
     times. Results go in a table in the PR that ships the engine, and the targets above are adjusted from them.

### 8.3 Why not classic computer use

Classic computer use (Anthropic's computer-use toolset, Codex Computer Use, Cua-style drivers attached as one tool per
action) has the model act like a person: take a screenshot, pick a pixel, click, take another screenshot. Each action
costs a full model turn plus an image. In practice that is about **one action every 3-6 seconds** (owner's measure:
about 5 s), and a 20-action task is 20 turns, 1.5-2 minutes and roughly 20-30k image tokens, before any mistake. It
also takes your mouse for the whole time.

The same task in this design: one observe (a text snapshot, a few thousand tokens), one `host_do` batch that the
computer runs in well under a second plus the app's own time, one check of the diff: **2-3 model turns, about 10-20
seconds**, a fraction of the tokens, exact element targets instead of pixel guesses, and your mouse untouched unless
nothing else works. The §8.2 benchmark checks this estimate before the owner relies on it.

## 9. Tools

All take `environmentId` (the computer) and are logged with the calling project and thread (§4.3 rule 9). Read tools
work over a read-only link; the others need write access on the link and a non-plan caller (upstream checks both), and
a project grant.

| Tool                                                   | Kind  | What it does                                                                                                                                                 | Limits                                                                          |
| ------------------------------------------------------ | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| `host_status`                                          | read  | online, paused or winding down, install verify, availability, policy hash, reach map, grants in force, snapshot and log state, link expiry, discovery (§6.2) |                                                                                 |
| `host_list`, `host_stat`, `host_read`, `host_search`   | read  | files in the reach map; a secret shows as name + `secret` only                                                                                               | 1 MB per read, 2,000 entries, search 20 s; refused while the deny pass is stale |
| `host_write`, `host_mkdir`, `host_move`, `host_delete` | write | in the reach map; compare-and-swap on the old hash for writes; pre-image kept                                                                                | snapshot rule of §4.3 rule 6                                                    |
| `host_run`                                             | write | a program with arguments, a working folder in the reach map, a timeout, as the agent account through the broker                                              | output 64 KB tail, redacted                                                     |
| `host_undo`                                            | write | list changes since a time; restore the agent's own changes (§4.3 rule 6)                                                                                     | your changes: listed, restored only from the tray                               |
| `host_observe`                                         | read  | windows and focus; snapshots or diffs per named window from the best source; optional crops; clipboard if asked (§6.2, §6.7)                                 | sensitive set refused                                                           |
| `host_do`                                              | write | a step list run by the engine (§7), or speculative alternatives (§7.4)                                                                                       | sensitive set; high-risk grants; batch limits; take-over; `visible` flag (§6.8) |
| `host_recipe`                                          | write | list, show, save, run, delete recipes (§7.3); list and show work over a read-only link                                                                       | run = same checks as `host_do`                                                  |
| `host_ask`                                             | write | the one batched ask of §4.2 (high-risk items, installs and admin commands included)                                                                          | one open at a time; wear-down rules                                             |
| `host_named`                                           | write | a named command from the protected store, run as you from a frozen copy (§9.1)                                                                               |                                                                                 |

Nine tools (plus the file group). The file and run tools exist so agents on another machine need no full agent on the
computer.

### 9.1 Pop-ups: only the batched ask

The batched ask (§4.2) is the only dialog. Two of its item kinds carry a little more:

1. **Installs.** The item names a package (winget / Homebrew / the distro's package manager), its source and size, and
   for whom. On Allow the helper installs as **you**; Windows UAC or the OS password prompt follows when it is
   machine-wide. Installing tools for the agent account only (into its own folders) needs no ask.
2. **Admin commands.** The item shows the exact command. The helper copies every file the command references into a
   protected folder and shows its hash or content; on Allow the OS's own elevation prompt follows and the **copy** runs,
   in a protected working folder, so nothing can be swapped between the ask and the run (lens B1).

**Named commands and approved scripts** are entries in the protected store, run by the helper as you, with output
scrubbed of every secret value: things that need **your** secrets (a deploy with your SSH key) or your session. They
need no ask because you wrote or approved them, and they run from a frozen copy (§4.3 rule 4): a deploy from a repo
runs at the commit you approved; a new commit is one ask showing the commit.

## 10. Network, auth and links

- All traffic goes over the linked environment: upstream's auth, routes and revocation. Tailscale is the expected route
  between machines; the computer's T3 service answers on loopback, LAN and tailnet only (upstream behaviour), and gives
  loopback no special trust (§3.5).
- The agent account's outgoing network: §3.5.

**Link expiry, what upstream does today** (upstream `main` 8cd8a3ca5a):

- A peer link signs in with OAuth `authorization_code` against the other side's MCP OAuth
  (`apps/server/src/peer/PeerLinks.ts:308`, token request at `:379`) and stores `expiresAt = now + expires_in`
  (`PeerLinks.ts:536`).
- The token is an MCP-client session with a fixed 30-day lifetime (`apps/server/src/auth/EnvironmentAuth.ts:79`,
  issued at `:1347`, called from `apps/server/src/auth/McpOAuth.ts:546`; `expires_in` at `McpOAuth.ts:564`).
- The MCP OAuth server supports only `authorization_code` (`McpOAuth.ts:97`) and refuses every other grant
  (`McpOAuth.ts:510-511`): there is no refresh token and no renewal.
- After 30 days the link is `expired` (`PeerLinks.ts:593`, `:645-648`: "Link it again with a new pairing code"), and the
  linking side checks its own stored expiry before every call (`apps/server/src/peer/PeerLinkRequests.ts:106-112`).
- Upstream issues and PRs: issue #33183 (open: sessions expire after a hard-coded 30 days; asks for a setting or sliding
  renewal), issue #9884 (open: session lifetimes), PR #31755 (closed unmerged: a TTL setting for pairing-redeemed
  sessions), PR #26757 (open: Tailscale identity sign-in for clients), PR #25024 (open: the desktop app renews its own
  local session only). No open upstream PR adds renewal of peer links.

**Owner decision (2026-11-13): a long link lifetime, easy to extend; no refresh tokens.** You trust your own computers;
the goal is to keep **outside people** out, not to add friction between your machines.

1. **Lifetime setting.** `peerLinkSessionDays` on the computer, for the MCP-client sessions it issues to peer links:
   default **365 days**, any number of days, or **never expires** (open point 2). It replaces the fixed constant at
   `EnvironmentAuth.ts:79` for peer-link sessions only, and the issued `expires_in` follows it, so the linking side's
   own expiry check keeps working unchanged. Written as a small change upstream could take for #33183.
2. **Extending is one action, no pairing code.** On the computer, the tray's **Extend links** sets every link to now +
   the lifetime, including links that expired in the last 30 days (older ones need pairing again). From any of your
   already-linked machines, one command (`t3` link extend for a named computer, also a button in Sharing & access)
   asks over its live link; the computer extends that link, and, when the caller's link has write access, any other
   link you name. The extension happens on the computer, in the same transaction that checks revocation, so a revoke
   always wins over an extension in flight.
3. **Heads-up** 7 days before any link expires: tray on both machines, `host_status`, and a T3 notification on the
   agent machine, each with the extend action.

**Strong against outsiders:**

- **Device binding.** At pairing the computer records the linking device: over the tailnet, its Tailscale node
  identity (the computer asks its local Tailscale who the connection comes from, the idea of #26757); on other routes,
  a key pair the linking side makes at pairing, kept in its secret store, which signs each request (proof of
  possession, RFC 9449 style). A token presented from another node or without a valid signature is refused, the link
  is marked suspect, and the tray alerts "a link was used from an unknown device", with Revoke.
- **No new devices without you.** Pairing codes stay upstream's short-lived single-use codes; a new device is always a
  pairing you start, and the tray shows each new link.
- **Visible use.** The computer tracks each link's last use and shows it in the tray and Sharing & access; each
  extension and each refused device is logged.
- **Revoke all** in the tray ends every link to and from the computer at once.

**Light between your own machines:** no per-call confirmation, no token rotation, no re-pairing to extend.

**Residual risk, in one line:** an infected machine of yours can use its links to your other machines until you press
the kill switch or **revoke all** on them.

## 11. Background runners: use the free hardware (runner side, not host tools)

The PC also runs background work moved there from the server: CI jobs on fleet runners (#13), builds, other project
runners. **Owner decision (2026-11-13): no fixed levels or slot counts.** The runners measure what the hardware has
free while you play or work, keep a small safety buffer, and use the rest; when free resources drop, they shrink to
fit. Host tools do not use this to gate work (§4); speculative batches use the same measurement only to size their
parallelism (§7.4).

**Measured** (the activity service, inside the helper in your session, every 2 s, excluding the runners' own use):

| Resource      | Windows                                                               | macOS                                         | Linux                                         |
| ------------- | --------------------------------------------------------------------- | --------------------------------------------- | --------------------------------------------- |
| memory free   | available memory (PDH) and the low-memory resource notification       | `host_statistics64`, memory pressure level    | `MemAvailable`, PSI `/proc/pressure/memory`   |
| CPU free      | PDH processor time per core, minus runner processes                   | `host_processor_info`, minus runner processes | `/proc/stat`, PSI cpu, minus the runner slice |
| GPU free      | PDH "GPU Engine" utilisation and "GPU Adapter Memory"                 | IOKit accelerator statistics                  | NVML, or `gpu_busy_percent` and VRAM in sysfs |
| disk I/O free | PDH disk queue length and latency                                     | IOKit block storage statistics                | PSI io, `/proc/diskstats` latency             |
| your override | tray: "leave me X more" or "runners off" for 1 h, 4 h, until tomorrow | same                                          | same                                          |

**Budget = free − buffer**, per resource. The buffer is small and adapts: default the larger of 2 GB and 10 % of
memory, one core, 10 % GPU and VRAM, and a disk latency ceiling; it grows for a while after you had a spike (your game
loading a new area), so short peaks do not hit a runner job. Example: a game leaves 15 GB of memory free, so the
runners may use about 13 GB.

**Admitting work.** Each job kind has a need (peak memory, cores, GPU, disk I/O), from its own history on this
computer or a declared default. The dispatcher starts a job only when its need fits into the budget, and caps it at
that size (Windows job object memory and CPU rate limits, `BELOW_NORMAL`/EcoQoS; macOS `taskpolicy -b`; Linux
`memory.high`/`memory.max`, `cpu.max`, `cpu.weight`, `io.weight` on the runner slice). Runner processes always run at
low priority. For fleet runners (#13) the dispatcher's heartbeat reports `free` as the number of standard jobs that fit
the budget, so `auto` routing keeps working with no config change; the rest go to the server pool or GitHub-hosted
runners.

**Shrinking to fit.** When the budget drops below what runs:

1. **CPU, GPU or disk short:** lower the runners' weights and caps first (instant, nothing lost).
2. **Still short, or memory short:** pick jobs to remove until the rest fit, choosing per job the cheapest of:
   **finish** (expected remaining time is short and the job fits throttled), **park** (the job can checkpoint: stop at
   its next checkpoint, resume later here or elsewhere), or **move** (a pure job such as tests: cancel and re-run
   elsewhere, losing only its work so far). Cost = CI time wasted plus agent usage it causes later; the job with the
   least waste that frees enough goes first.
3. **Memory falls below the buffer fast** (risk of your game swapping): the newest runner job is moved at once, before
   your work suffers. Memory caps stop a runner job from growing into your share.

Growing back: more work is admitted when the larger budget has held for 2 minutes (setting), so it does not flap. If
the activity service stops reporting while someone is signed in, runners admit nothing new and the tray says why; with
nobody signed in, the budget is the whole machine minus the buffer.

**How runners follow it.** The service writes `activity.json` (`at`, `free`, `buffer`, `budget`, `reasons`) where only
you write and the runner dispatcher (system) reads. Other runner kinds read the same file. Jobs with outside effects
(deploys, releases) never run on a personal PC. Suspending a CI job (freezing its processes) is not used: a frozen
runner loses its connection and the job fails; it is an option only for local jobs with no connection to keep.

## 12. Tray icon, wind down, kill switch and power loss

One tray icon per computer, from the session helper, covering host tools and runner work: in the Windows notification
area (first run tells you how to pin it), the macOS menu bar, and the Linux status area (StatusNotifierItem /
AppIndicator; GNOME needs the AppIndicator extension, to verify). Built on Electron's `Tray`; upstream's desktop app has
no tray today.

### 12.1 The menu

- **Status:** host tools (idle, working, acting in _app_ on rung _n_, paused, winding down, safe to shut down), the
  calling project and thread while acting, link expiry and last use, runners (jobs running, budget and why).
- **Pause / Resume.**
- **Wind down** and **Shut down when safe** (wind down, then the OS shutdown).
- **Kill switch** (asks once: "stop every agent process on this computer now?"), and **Revoke all links**.
- **Extend links** (§10), grants in force (with Revoke), runner override (§11), open log, open policy, recipes, bridges
  waiting.

### 12.2 Wind down (owner decision 2026-11-13: no fixed timer)

1. The guard writes `winding_down` (time and what was running) to the protected store. Nothing new starts: host tools
   and new turns are refused with "winding down", runners take no new jobs.
2. Each running item goes to a safe point **as fast as possible without wasting work**, chosen per item by remaining
   cost: a batch finishes its current step and releases input; a `host_run` job finishes if it is expected to end soon,
   otherwise it is stopped (its changes stay, with undo); runner jobs finish, park at their next checkpoint or move
   elsewhere, by the cost rule of §11; a take-over-style stop is never used where finishing is cheaper. A snapshot is
   taken of folders changed since the last one.
3. The tray lists each item with its expected time and choice, and "kill it now" per item. When nothing is left it
   shows **"Safe to shut down"** (and shuts down if you chose that).
4. Wind-down ends when you choose Resume, or at the next sign-in after a restart, back to the state before it (unless
   "stay paused after restart" is ticked).

The **kill switch** stays the immediate stop (§4.3 rule 8).

### 12.3 Power loss and hard reset (owner decision 2026-11-13)

The computer may be switched off at any moment. Every part writes its state so that the next start recovers:

- **Atomic and durable writes:** the protected store, recipes, journals, ledgers and state files are written to a temp
  file, fsynced, then renamed; logs are fsynced per line.
- **On the next start, in order:** the guard checks the protected store's signature (falls back to the last good
  version) and the log tails (a half line gets a `truncated` marker); the helper runs the full deny pass and the
  install verify before host tools come back (§3.3); the engine drops held-input files from before the boot (the OS
  reset input) and marks journalled started-but-unconfirmed steps `unknown`; speculative layers listed in the journal
  are deleted; snapshots the OS did not finish are discarded; the runner dispatcher re-asks for every job in its ledger
  and for jobs that were running on this computer (GitHub reports them failed with a lost runner); `winding_down` from
  before the boot is cleared; "this task" grants are ended.
- **What is lost:** work inside apps that was not saved; agent file changes after the last snapshot that were not made
  through T3's file tools (those have pre-images). Both are stated in the failure table (§15).

## 13. Speed of the parts, and language

- **Engine: one native binary per OS, in Rust** (recommendation). Speed first: the tree cache, event handlers, batches
  and input need direct OS calls with no runtime start or bridge in between (UI Automation with cache requests,
  AXObserver, AT-SPI over D-Bus, `SendInput`/`PostMessage`/`CGEvent`/libei). Rust has maintained bindings for all three
  and one language serves every OS. Alternatives: C# (Windows-strong, weaker on macOS accessibility), Go (C interop
  for COM and AppKit is more work), TypeScript in the Electron process (slowest for UIA and blocks the helper). Cost: a
  Rust build per OS in fork CI.
- **Command broker and guard service:** small, per OS; Rust too, sharing the link-safe file operations of §3.4.
- **Helper, toolkit and core:** TypeScript inside T3 and the desktop app's main process. No new runtime to install.
- **Per call:** one round trip over the link, plus the engine's own time (§8.1). Today a link whose first address is
  silent costs 10 s per call (upstream #35519), so the computer's saved routes list the tailnet address first.

## 14. Threat model and what still leaks

| Threat                                         | What stops it                                                                                                                            | What is left                                                                                           |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Prompt injection from files, screens, bridges  | the sensitive set, the high-risk ask, untrusted marking, project grants and outside-input threads, indicator, take-over, undo, log       | in a non-sensitive app it can do what you could there (send a message, press a button)                 |
| Files the agent wrote run as you (B1)          | file-type rule, protected working folders, frozen copies for scripts, named and admin commands, bridge hashes, auto-run paths denied     | a data file that exploits a bug in an app's parser                                                     |
| Planted links redirect a privileged write (B2) | link-safe operations (§3.4): no following, link count 1, final-path check, walks stop at links                                           |                                                                                                        |
| The agent widens its own policy (B3)           | protected store owned by the guard; widening through the ask, with OS consent while a full-rights grant is active; store windows refused | during a full-rights grant the agent has your user rights (§14.1)                                      |
| A command takes over T3 on the computer (M1)   | separate host and agent accounts; admin-owned install; engine admits only the host account; no outbound link                             | delegated full agents stay off until they run through the broker                                       |
| Speculative run escapes (M2)                   | OS-enforced sandbox; no network or local IPC; layers made by the agent account                                                           |                                                                                                        |
| Agent reaches local services (M3)              | default-deny network classes, own-loopback only, probed at install                                                                       | the public internet: anything readable can leave                                                       |
| Secrets via clipboard, logs, outputs (M4)      | concealed markers, restored clipboard, redaction on every path and record                                                                | a secret in an unknown format                                                                          |
| Typing lands in the wrong window (M5)          | the input ladder (rungs 1-3 need no focus); rung 4 focus lock, chunk checks, abort                                                       | an app that changes its own focus inside a chunk of 8 events                                           |
| An untrusted thread calls host tools (M6)      | per-project grants, outside-input threads read-only, caller shown and logged                                                             | a thread in a granted project that reads outside text without the flag                                 |
| Stolen link (M7)                               | device binding, unknown-device alert, revoke all, last use shown                                                                         | an infected own machine can use its links until kill switch / revoke all                               |
| State fails open (M8)                          | fail-closed rows in §15; install verify gates host tools; heartbeat; uninstall rows                                                      |                                                                                                        |
| Agent undo wipes your work (M9)                | writer recorded; agent undo restores only agent changes with matching hash                                                               | changes whose writer the OS cannot tell count as yours and are not undone by the agent                 |
| Browser with your rights (M10)                 | headless as the agent account; hardened visible profile; typed CDP verbs only                                                            | your own browser through a bridge acts with your logins                                                |
| Reading your other files                       | reach map, sensitive set, reach check                                                                                                    | folders every user may read that you chose to keep                                                     |
| Secrets inside the reach map                   | built-in patterns, deny entries, fail-closed deny pass, watcher                                                                          | a secret in an unknown format or ordinary file name; about one second after you create a matching file |
| Damage to your folders                         | snapshots, pre-images, writer log, `host_undo`, quota                                                                                    | agent changes between snapshots made by commands; changes older than 24 hours; changes inside apps     |

**What still leaks, plainly:** files in the reach map and what the apps the agent uses show (trees, values,
screenshots) can reach the model provider and the internet. Process names and redacted window titles of all apps. Data
read goes into transcripts.

### 14.1 Owner scope vs lens

Where a lens fix would contradict the owner's scope (A: goal-driven, whole computer except sensitive things, one
batched ask), A is kept and the hole is closed another way.

| Finding | Lens default not taken                                        | Why                                                                    | What closes the hole instead                                                                                                         | Residual risk                                                                     |
| ------- | ------------------------------------------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------- |
| B3c     | full-user-rights apps never get acting from a default         | A: one batched ask, not per-app acknowledgements in the tray           | they are on the high-risk list; granted with OS consent, scope "this task" by default; widening needs OS consent while one is active | during a grant the agent has your user rights, including your profile's secrets   |
| m5      | unknown apps hidden, opt-in per app                           | A: whole computer by default                                           | sensitive set refused, relevance-scoped observation, redacted titles, untrusted marking                                              | what a used app shows (mail, chat) reaches the provider                           |
| B1a     | open files only in apps named in the policy                   | A: no per-app list                                                     | the file-type rule for agent-writable folders in every app; macros forced off; protected working folders                             | a data file that exploits an app's parser                                         |
| r2 §6.5 | acting and synthetic input off until enabled                  | A and the owner's input decision                                       | the input ladder: real input only last, `visible: true`, indicator, take-over                                                        | rung 4 still uses your mouse when nothing else works                              |
| M4      | clipboard read and write off by default                       | A: the clipboard is part of the computer                               | concealed markers refused, redaction, writes are acting and put back after the batch                                                 | a password copied from a plain app without markers is readable                    |
| M6      | host tools off for every thread until granted                 | owner: his machines and projects are trusted, no friction between them | one toggle per project; outside-input threads read-only; caller logged and shown                                                     | a thread in a granted project reading outside text without the flag               |
| M7      | refresh-token rotation, reuse detection, absolute lifetime    | owner chose the long link lifetime and easy extension                  | device binding, unknown-device alert, last use shown, revoke all, lifetime setting                                                   | an infected own machine can use its links until kill switch / revoke all          |
| B1e     | package manifests and other code files read-only to the agent | agent work is code; A                                                  | only auto-run paths (code that runs without you choosing it) are denied; named commands use frozen copies                            | code you choose to run (a test) may contain agent changes, as with any agent work |
| m4      | bridge verbs allowed one at a time                            | A: one batched ask                                                     | the ask lists every verb with its own tick box; changed bridge files go back to waiting                                              | "Allow" with all boxes ticked grants every verb                                   |

**Owner decisions changed by a blocker:** only one. B3 adds the OS's own consent prompt to ask items that grant full
user rights or elevation, and to any widening while such a grant is active. Everything else from earlier rounds stays
(speed first, invisible by default, speculative batches, tray and wind down, runner-side activity), changed only where
the owner changed it this round (links, wind-down timer, runner levels, snapshot size, speculative sizing).

## 15. Failure table (state the feature changes)

What happens when it runs again, stops half-way, or meets old or empty state:

| Step that writes                         | Crash or stop half-way                                                                                                                                                                                                                            | Run again                                                                                                                        | Old / empty / half-written state                                                                                                                                                           | Test                          |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------- |
| install: create both accounts            | one or both exist, nothing else                                                                                                                                                                                                                   | finds them, keeps them, re-checks groups (refuses if either is an admin)                                                         | an account of that name that is an admin or in a remote group: refused                                                                                                                     | fake OS layer                 |
| install: reach map grants                | some folders granted                                                                                                                                                                                                                              | re-applies all through link-safe walks (idempotent); removes grants for folders no longer listed                                 | a folder deleted since: reported, skipped; a planted link in a folder: not followed, reported                                                                                              | fake ACL layer + link fixture |
| install: services, broker, guard         | some installed, not started                                                                                                                                                                                                                       | replaced with the same definitions                                                                                               | an upstream "as me" task for the same T3 home: refused with the reason                                                                                                                     | fake OS layer                 |
| **install verify half-applied**          | host tools stay off; `host_status` names the failed check                                                                                                                                                                                         | re-runs every check; tools come on only when all pass                                                                            | a later change (account added to a group, rule deleted): next quick or daily check turns tools off                                                                                         | verify fakes per check        |
| **secret deny pass**                     | file tools stay off until a full pass completes                                                                                                                                                                                                   | a full pass is idempotent                                                                                                        | files created while the helper was down or during a watcher overflow: covered by the pass on start / after overflow; reads refused while the pass is stale                                 | fake FS + clock               |
| reach map: folder removed                | grants removed for some files                                                                                                                                                                                                                     | removal finishes; agent processes with handles under it are ended first                                                          | a process that held a handle across the change: ended, logged                                                                                                                              | handle fixture                |
| ask answer (guard)                       | answer written atomically or not at all; not written = denied                                                                                                                                                                                     | the same answer again is a no-op                                                                                                 | a "this task" grant from before a restart: ended; an unanswered ask after a restart: denied                                                                                                | fake guard                    |
| protected store edit                     | temp + atomic rename by the guard                                                                                                                                                                                                                 | new store or old one, never half                                                                                                 | edited outside the guard (signature fails): ignored, last good version used, tray asks you to confirm with OS consent; unreadable: all write and acting tools refused                      | signature test                |
| `host_write`                             | temp beside target, target unchanged; pre-image saved first                                                                                                                                                                                       | same content: OK; other content: compare-and-swap refuses                                                                        | target changed since read: refused                                                                                                                                                         | fake FS                       |
| **snapshot**                             | snapshot taken or not; log line says which; an unfinished OS snapshot is discarded on start                                                                                                                                                       | a request within 30 minutes of the last reuses it                                                                                | store full, or keeping it would push out one younger than 1 hour: agent writes and runs **refused**, tray red                                                                              | fake snapshot store           |
| `host_undo` (agent)                      | some files restored, each through link-safe writes                                                                                                                                                                                                | re-run restores the rest; already-restored files compare equal                                                                   | a file you changed since: listed, not restored; writer unknown: treated as yours; snapshot expired: refused with the oldest time                                                           | writer fixture                |
| log line                                 | line written before the action and after; a call whose log write fails is refused                                                                                                                                                                 | the guard's countersign continues from the last good head                                                                        | half line at the end: `truncated` marker, chain continues; a T3 log head that differs from the countersigned one: tray alert                                                               | log fixture                   |
| `host_do` batch                          | journal (fsynced) says which steps are done; started-but-unconfirmed is `unknown`                                                                                                                                                                 | same key: journalled results, no step re-runs; a new key is a new batch                                                          | journal from before a restart: kept 24 h for retries, then dropped; empty step list: `ok`, nothing run                                                                                     | fake engine back end          |
| **input rung step-down**                 | the rung tried and its result are journalled before the next rung is used                                                                                                                                                                         | same key: no rung is re-tried; an unsafe-to-repeat action with an unclear check returns `unknown`, never a second apply          | rung memory from an older app version: tried first, steps down once, memory updated                                                                                                        | fake app per rung             |
| **focus change mid-burst (rung 4)**      | burst aborted between chunks, held input released, `stale` with the chunks sent                                                                                                                                                                   | the agent re-plans from a fresh diff; never resumes the burst by itself                                                          | the target window gone: `stale`                                                                                                                                                            | focus-steal test per OS       |
| **you grab the mouse/keyboard mid-step** | step stops, all held keys and buttons released, queued steps dropped, `interrupted_by_user` with what ran and a diff                                                                                                                              | a new acting batch on that window waits for your idle time or "let the agent continue"                                           | a take-over during a `wait`: the wait ends as interrupted too                                                                                                                              | input hook fake               |
| **helper dies, engine lives**            | engine stops acting within 500 ms of the missed heartbeat and releases held input                                                                                                                                                                 | acting resumes only when the helper is back and the indicator is up                                                              | an engine from before a helper restart: the new helper stops it and starts a fresh one                                                                                                     | kill test per OS              |
| **engine dies mid-drag**                 | the engine writes every held key/button to a held-input file before sending it; the helper (or the next start) sends the ups, then deletes the file                                                                                               | after restart the batch is `unknown` from the journal and is not re-sent                                                         | a held-input file from before a reboot: dropped (the OS reset input)                                                                                                                       | kill test per OS              |
| **clipboard write**                      | the previous content is saved in the journal before the write                                                                                                                                                                                     | after a crash the next start puts the saved content back if the clipboard still holds the agent's value                          | the clipboard changed by you since: left alone                                                                                                                                             | fake clipboard                |
| **speculative batch**                    | the journal lists each private layer **before** it is made; on restart every listed layer is deleted and its per-slot processes ended; a winner whose apply did not finish is re-applied file by file (compare-and-swap makes done files a no-op) | same key: the journalled winner and outcome, nothing re-runs; a key whose alternatives never finished: `unknown`, layers cleaned | layers with no journal entry: left alone and reported; a real file changed during the run: `conflict`, nothing applied; zero alternatives: refused                                         | fake engine + fake FS         |
| **window pops in front** (§6.8)          | the engine hides it and gives the front back; if the engine dies first, the window stays and the next start logs it                                                                                                                               | the same program is started the other way the result told the agent; a repeat offender is stopped                                | a recipe that now shows a window: `visible_side_effect`, recipe marked `needs repair`                                                                                                      | no-pop-in test                |
| recipe save                              | temp + atomic rename into the store                                                                                                                                                                                                               | same content: no-op (content hash); new content: new version                                                                     | a recipe for an older app version: runs, and a failing step marks it `needs repair`; empty store: fine                                                                                     | fake guard                    |
| pause / kill switch / revoke all         | pause state in the store; accounts disabled; links revoked one by one, each committed                                                                                                                                                             | idempotent; a revoke-all re-run revokes what is left                                                                             | kill switch with no agent processes: OK; revoke-all with no links: OK                                                                                                                      | fake OS layer                 |
| **wind down**                            | the `winding_down` state (start time, items, choice per item) is written first; after a helper crash it is read back and continues; parked and moved items are in the ledger and not moved again                                                  | pressing it again shows progress, starts nothing new                                                                             | a `winding_down` state older than the last boot: cleared at sign-in (unless "stay paused"); nothing running: "safe to shut down" at once                                                   | fake clock + fakes            |
| **runner shrink**                        | the ledger line (job, run, attempt, choice) is written before the park or cancel; on restart, lines without "re-run asked" get it; a job already re-run (newer attempt exists) is marked done                                                     | same budget again: no-op; growth held off by the 2-minute rule; a second drop does not move the same attempt twice               | `activity.json` stale or unreadable while someone is signed in: admit nothing new, tray note; unknown format: same; no jobs: only the heartbeat changes                                    | fake dispatcher + fake meters |
| **power loss (any moment)**              | every write above is atomic or journalled; on start: store signature, log tails, deny pass, install verify, held-input drop, journal `unknown`s, layer cleanup, unfinished snapshots discarded, runner ledger and lost-runner jobs re-asked       | a second power loss during recovery: each recovery step is idempotent and runs again                                             | lost: unsaved work inside apps; command changes after the last snapshot (T3 file tool changes have pre-images)                                                                             | power-cut test in a VM per OS |
| **link extend**                          | the computer commits the new expiry in one transaction with the revocation check; the caller may miss the reply                                                                                                                                   | extending again sets the same or a later expiry (idempotent)                                                                     | a link expired more than 30 days ago: refused, pair again; a link made before device binding: bound to the first device that extends it, with a tray note                                  | fake clock                    |
| **link used from an unknown device**     | the call is refused before any tool runs                                                                                                                                                                                                          | each further try is refused and counted                                                                                          | a link whose device identity was never recorded (older link): works until it is extended or re-paired, then bound                                                                          | fake tailnet whois            |
| availability, project grants             | settings saved                                                                                                                                                                                                                                    |                                                                                                                                  | "off until" in the past: on; bad value: off; a project with no setting: off                                                                                                                | settings test                 |
| **host tools off / uninstall**           | each step is listed in a removal ledger first; a half removal finishes on the next run                                                                                                                                                            | idempotent                                                                                                                       | removes: reach map grants, deny entries, auto-run denies, firewall rules, services, broker, guard tasks, snapshots it made, links it issued, both accounts (after you confirm their files) | uninstall test per OS         |

## 16. Tests (when built)

- **Core on Linux CI with a fake OS layer:** policy checks (reach map, sensitive set, high-risk list, file-type rule),
  secret patterns (a hand-written fixture of expected secret paths per pattern, so weakening a pattern turns a test
  red), the ask (scopes, remembered answers, wear-down rules, OS-consent items), step-list checks and limits, journal
  and idempotency, input ladder logic with fake rungs (step-down, no double apply, rung memory), recipes, snapshot and
  undo with writers (agent undo never touches a file you changed), log chain and countersign, pause, wind-down choices
  by cost, availability and project grants, link extend and device binding, redaction of every output path.
- **The toolkit** through upstream's MCP test kit: a forwarded call over a test link; a read-only link refusing write
  tools; an outside-input thread getting read tools only; the caller id arriving on the computer; a token from another
  device refused.
- **Per-OS layers** (a real run on each OS before that OS is enabled, Windows first):
  - accounts: the agent account cannot read your profile root, a denied `.env`, T3's data or install, and cannot write
    an auto-run path; the host account cannot be reached by agent commands; the broker refuses the agent account;
  - links (B2): a planted junction, symlink and hard link do not widen a grant or redirect a restore or copy;
  - B1: the engine refuses to open an agent-written `.bat`, `.lnk` and `+x` file; an admin command runs the copy shown,
    not a file swapped after the ask;
  - B3: an edited store file is ignored; the store's folder is refused in file dialogs; widening during a full-rights
    grant needs OS consent;
  - network (M3): each class of §3.5 unreachable from the agent account; own loopback reachable;
  - speculative (M2): a speculative run cannot reach the engine, the broker, T3 or a denied file in its layer, cannot
    reach the network, and leaves no process after cleanup;
  - clipboard (M4): concealed content refused; content put back after a batch;
  - input (M5): a window that steals focus mid-burst gets no typed text; each rung reaches the test apps it claims and
    steps down from the ones it cannot;
  - the kill switch ends every agent process; pause ends daemonised children; the engine stops without the helper;
  - a snapshot restores a deleted file; refusal when the snapshot store is full;
  - refusals (lock screen, password field, own policy screens, inactive session);
  - power cut in a VM at random points during a batch, a speculative call, a wind-down and a runner shrink: the next
    start recovers as §15 says;
  - the §8.2 micro-benchmarks; the **no pop-in test** of §6.8 (required, blocks enabling an OS).
- **Runners:** the budget logic with fake meters (a game taking memory, a spike), finish / park / move choices by cost
  with a fake dispatcher.

## 17. Costs and open points

- **The beta switch.** Linked environments is an upstream beta, off by default, and it must be on in **both** T3
  installs (agent machine and computer).
- **MCP tool-list size.** The host tools add several KB, so they are hidden unless at least one linked computer
  reports host tools on, and then deferred behind `tool_search`.
- **Two accounts and three small services** on the computer (T3 as the host account, the broker, the guard), plus the
  desktop app's helper. Setup is one installer run with one UAC prompt.
- **Two T3 installs on the computer** if you also use the desktop app there; the host account's T3 is separate.
- **Delegated full agents on the computer** wait for the broker spawn hook (§3.2).
- **A new native component** (the engine, and the small broker and guard) with a Rust build per OS in fork CI.
- **Rung 3 on Windows and macOS is weaker** than on Linux: no real hardware-style input into a hidden desktop; apps
  that need it use rung 4. To verify per OS, including GPU apps on hidden displays.
- **Wayland** rung 4 needs the RemoteDesktop portal, which asks once per session on some desktops; rung 2 does not
  exist for native Wayland apps.

**Open for the owner** (first one first):

1. After you take over a window: resume after 5 s of idle (recommended), another time, or only when you press "let the
   agent continue"?
2. Link lifetime default: **365 days with one-click extension** (recommended: a link stolen without anyone noticing
   still dies within a year, and extending costs one click), or **never expires**?
3. The default reach map: your profile's documents, desktop, downloads, media and source folders plus the data folders
   on extra drives (recommended), or a smaller start?
4. Full-user-rights apps: keep the OS consent prompt for these grants (recommended, forced by blocker B3), or accept
   the risk that a running full-rights grant can approve further grants by itself?
5. Project grants on the agent machine: new projects **off** until you toggle them (recommended), or on?
6. `.git/config` read-only for the agent until you unlock a repo through the ask (recommended), so `git push -u` fails
   for agents there; or writable with a guard that strips code-running keys?
7. Whether the agent account's internet should be limited further (for example to package registries and git hosts).

**Follow-up research (named here, not done in this design):** which command languages and tools agents should use on
each OS for speed and reliability, general across projects: whether agents should write Python at all; how good the
Python, PowerShell and shell they write is in practice; built-in commands versus scripts; and a curated, fast toolset
per OS (startup time, output size, error behaviour) that `host_status` could advertise to agents. The result feeds the
tool descriptions and the recipe guidance.

## General vs project

Everything in this design is general. Machine names, addresses, accounts, folders, apps, games, bridges, recipes and
named commands are local policy and settings on each install; examples use placeholders only.
