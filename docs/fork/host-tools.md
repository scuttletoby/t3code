# Host tools: agents on a person's own computer (design, no code)

Status: design for review, revision 5 (the third security review, the correctness review, the owner's answers of
2026-11-13 and the own-desktop spike folded in). Fork feature, not upstream. Nothing here names a real machine, person or address; those live in each install's local
settings.

## In short

- **What it lets agents do:** you give an agent on one of your machines (for example a headless server) a goal on
  another of your computers: "I want X done". The agent works out the fastest, best route itself: files, commands, the
  apps' own automation, app-side bridges, accessibility elements, and the mouse and keyboard only as the last resort.
- **Scope (owner decision 2026-11-13):** the agent may use the **whole computer except sensitive things**: keys, `.env`
  files, credentials, password fields and secret stores. It may read system areas too (not their secrets). It looks
  only at what the task needs. When it needs something more specific (the short high-risk list in §4.2), it **asks
  once with a list**: "Can I use X, Y and Z?". Your answers are remembered for the scope you choose. This replaces a
  per-app allow list.
- **Speed first:** the computer runs a whole list of steps locally, with waits like "until this button appears", and
  sends back only what changed. One network hop and one model turn do the work that takes classic computer use dozens
  of screenshot-and-click turns (§7, §8). When the agent is unsure which of two ways works, it tries both at once on
  private copies and keeps the one that worked (§7.4).
- **Your mouse stays yours:** the agent clicks and types through the least intrusive way that works: element actions,
  then events sent to one window, then its own screen, and only last the real mouse and keyboard on your desktop (§6.4).
- **Invisible by default:** commands, scripts and batches run in the background with no window shown and no focus
  taken. Only UI work you asked for touches the visible screen, and a test checks that nothing else ever does (§6.8).
- **How it is kept safe:** commands run as a separate, limited **agent account** that the OS keeps out of your
  sensitive files and away from T3's own data (T3 itself runs as a second account). The rules live in a store the
  agent cannot change; widening them needs you.
- **Said openly: in your apps, the agent acts as you** (owner decision 2026-11-13). Through an app's file dialog, a
  link or a launcher it can run any file with your rights, including one it wrote, and nothing marks or asks first.
  What holds then: **protected areas** (system folders, other users' profiles and every autostart and persistence
  point, including the system places the OS leaves writable to users) are never written, deleted or run without your
  OK for that one action, enforced by the OS and by deny entries where it can be, otherwise held by the guard (§4.4);
  and a **log of every file the agent wrote or ran** is in the tray.
- **While the agent uses your screen:** a clear on-screen sign shows it. When it drives your real mouse and keyboard,
  **any real click or key press from you takes control back at once**, and acting on your screen stays paused until
  you say continue (background commands go on). Everything is logged.
- **Network:** the agent account has **no direct internet** by default. Web search and fetch run on the server; the
  agent account reaches only the services you name on your own linked machines, and a goal that needs more asks once
  (§3.5).
- **What you click:** the one batched ask, when the agent needs something from the high-risk list (installs, admin
  and protected-area actions are on it). Nothing else asks.
- **What you can always do:** from the tray icon: pause, **wind down** (start nothing new, finish or park what runs,
  then "safe to shut down"), the kill switch, and revoke every link; undo the agent's file changes of up to the last 24
  hours (§4.3 rule 6).
- **Power can go at any time:** every part recovers on the next start (§12.3, §15).
- **Links between your machines** expire after 365 days and extend with one click from the tray or any linked
  machine, without new pairing codes; the strong controls are against outsiders (§10).
- **Off by default for new projects:** each project gets host tools only when you switch them on (§4.3 rule 9).
- **Background runners** (CI jobs, builds) use the hardware you leave free, minus a small buffer, and shrink when you
  need it back (§11). Host tools themselves never slow down because you are busy.

## 1. Parts

| Part                         | Where it runs                                                                                            | Built from                                                       | What it does                                                                                                                            |
| ---------------------------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Host environment             | the computer, as the **host account** (§3)                                                               | upstream T3 server, installed with upstream's background service | serves the host tools over the link; owns T3's data; runs no agent code                                                                 |
| Command broker               | the computer, as the **agent account** (§3)                                                              | **fork**, small, per OS                                          | starts every agent command as the agent account; accepts requests only from the host account                                            |
| Linked environment           | between the agent machine and the computer                                                               | upstream peer links (`docs/internals/remote.md`, "Peer links")   | transport, auth, routes (Tailscale, LAN), revocation, access ceiling, expiry (§10)                                                      |
| `host` toolkit               | inside T3's MCP server                                                                                   | **fork**, one toolkit like `toolkits/environment/`               | typed tools (§9); each takes `environmentId`, so upstream forwards it to the computer                                                   |
| Session helper               | the computer, in **your** desktop session, as you                                                        | **fork** add-on to the T3 desktop app (main process)             | tray (§12), the batched ask (§4.2), policy view; starts the engine; hosts the bridge endpoint                                           |
| Engine                       | the computer, as you, started by the session helper                                                      | **fork**, new, one native binary per OS (§13)                    | the seeing and acting layer (§6-§8): app state, accessibility tree cache, batches, waits, input, screenshots                            |
| Engine worker                | the computer, as the **agent account**, inside the agent's own session or display, started by the broker | **fork**, the same binary in worker mode                         | rung 3 (§6.4): seeing and acting in the agent's own session; owns the agents' headless browser (§6.5); never in your session            |
| Guard service                | the computer, as the system (admin)                                                                      | **fork**, per OS, installed once                                 | owns the protected store, snapshots, the authoritative log, the kill switch, permission re-application, the protected-area watch (§4.4) |
| Protected store              | the computer, owned by the guard service                                                                 | **fork**, JSON, shown in T3's settings UI and the tray           | reach map, secret patterns, ask answers, recipes, approved scripts, named commands, bridges                                             |
| Availability, project grants | the agent machine's T3 settings                                                                          | **fork**, server settings                                        | per linked computer: on / off / off until; per project: may this project use host tools (§4.3 rule 9)                                   |
| Activity service             | the computer, in your session (part of the helper)                                                       | **fork** (§11)                                                   | measures free hardware for background runners and for sizing speculative runs; never gates host tools                                   |

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
| A full agent working on the computer  | `delegate_task` with `environmentId`; the child is an ordinary thread there (`apps/server/src/peer/RemoteDelegation.ts`)                                                     | **off in v1** for host-tools computers, with every other forwarded tool outside the host allowlist (§3.2)                 |
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
separate trust zone, or of a batched agent-native desktop layer. Each is an add-on module. Changes to upstream files
are limited to: one registration line per toolkit and setting; the link changes of §10 (lifetime setting, extension,
device binding, the linking side taking the current expiry from each reply); the forwarded-tool allowlist on the
computer (§3.2); the setting that turns T3's terminal and provider features off in the host account's install (§3.2);
and `t3 service install --account` (§3.1).

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
accounts**, and neither account can reach the other's folders; runner accounts on a personal computer get the isolation
of §11.

## 3. The two accounts (per OS)

Two local accounts per computer. Both are standard users, never administrators, and have no password anyone types (a
long random one held by the OS's task or service store, or none where the OS allows it). The agent account's password
also signs in its own session for rung 3 (§6.4): it is random, held by the guard, rotated daily, never shown, and is
never your password or your sign-in (spike 2026-11-13: a session that depends on your sign-in fails on a PC with
"Windows Hello sign-in only").

- **Host account** (for example `t3-host`): runs the computer's T3 server. It owns T3's data (auth database, issued
  sessions, link state, its log). It runs no command, script or provider the agent supplies.
- **Agent account** (for example `t3-agent`): runs every command, build and script the agent starts, through the
  **command broker**, which runs as this account and accepts requests only from the host account. It cannot read or
  write T3's data, T3's secrets or the T3 install.
- **Agent group** (for example `t3-agents`): the agent account and every per-slot user of speculative runs (§7.4).
  Every reach-map grant, secret deny entry and auto-run deny entry names this group, never one account, so a slot user
  can never read what the agent account cannot (lens N6).
- The **T3 install** (program files) is owned by an administrator and read-only to both accounts. The computer's T3
  holds **no outbound link** to the agent machine, so nothing on the computer can reach back with T3's credentials.

Why two accounts (lens M1): if commands ran as the T3 server's own account, one hostile build script could raise a
link's ceiling, mint sessions, read link credentials, patch T3 to run its code at boot and call the engine with fake
caller details.

### 3.1 Per OS

|                                        | Windows (first)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | macOS                                                                                                         | Linux                                                              |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| Create both accounts                   | local standard users, not in Administrators; "deny log on locally" for both; "deny log on through RDP" for the host account, and for the agent account unless its own session (next row) is on                                                                                                                                                                                                                                                                                                                                    | standard users, hidden from the login window (`IsHidden`), no admin group                                     | system users with a home, no sudo, shell login disabled for people |
| The agent's own session (rung 3, §6.4) | a Remote Desktop session of the agent account, opened on the computer itself by the guard (lens N7's conditions): the RDP port answers only the computer itself (listener on loopback where Windows allows, otherwise a firewall rule that drops every other address, checked by the install verify), NLA, the guard-held password, and the agent account gets only the RDP sign-in right (not Remote Desktop Users for anything else); Pro and higher (Home: none, see §6.4); to verify in a spike with a separate local account | a second GUI login of the agent account through Screen Sharing on a virtual display (to verify); else none    | Xvfb or a headless compositor started as the agent account         |
| Run T3 as the host account             | a Task Scheduler task "run whether the user is signed in or not", at boot (upstream installs "at logon, as me"; the fork adds `--account`)                                                                                                                                                                                                                                                                                                                                                                                        | a LaunchDaemon with `UserName` (a login item would only run when that user logs in)                           | a systemd system unit with `User=`                                 |
| Run the broker as the agent account    | a second such task; the broker's named pipe ACL admits only the host account                                                                                                                                                                                                                                                                                                                                                                                                                                                      | a second LaunchDaemon; Unix socket owned by the host account's group, mode 0660                               | a second unit; same socket rule                                    |
| Reach of the agent group               | Modify on the reach map's folders (§4.3 rule 1), inherited, applied by the helper as the folders' owner                                                                                                                                                                                                                                                                                                                                                                                                                           | ACL entries (`chmod +a`) by the folders' owner                                                                | POSIX ACLs (`setfacl`) for the group                               |
| Secret and auto-run paths              | an explicit Deny ACE for the agent group on each match (§4.3 rules 2-3)                                                                                                                                                                                                                                                                                                                                                                                                                                                           | a deny ACL entry for the group                                                                                | an ACL entry with no rights for the group                          |
| The agents' own persistence points     | Deny ACEs on the agent accounts' own Startup folders and Run keys; the guard deletes and logs any scheduled task an agent account registers                                                                                                                                                                                                                                                                                                                                                                                       | no write to their own `LaunchAgents`; `cron` and `at` denied                                                  | `cron.deny`, `at.deny`; no `systemd --user` lingering              |
| Disk quota (agent account)             | NTFS per-user quota on each volume it can write                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | APFS has no per-user quota: the agent account's scratch lives on its own APFS volume with a quota (to verify) | `quota` / XFS or ext4 project quota                                |
| Network                                | §3.5                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | §3.5                                                                                                          | §3.5                                                               |
| Screen, apps, clipboard                | none: neither account is in your session. Only through the engine (§6), which admits only the host account                                                                                                                                                                                                                                                                                                                                                                                                                        | same                                                                                                          | same                                                               |

### 3.2 How commands run

`host_run` asks the broker to start the program as the agent account, with `shell=false` by default and a shell only
when the caller asks for one, in a job (Windows job object / process group / cgroup) that is killed on timeout, on
pause and when T3 stops. The working folder must be inside the reach map. Either way, what a command can touch is what
the agent account can touch. The broker starts nothing (no `host_run`, no `run` step, no speculative run, no engine
worker or agents' browser) while the secret deny pass is not current (§4.3 rule 2).

**Delegated full agents on the computer are off in v1.** Upstream starts provider processes from the T3 server, so a
delegated agent would run as the host account and could reach T3's data. They are enabled once the fork starts provider
and terminal processes through the broker (one spawn point, to locate in upstream code when built). Host tools need no
provider on the computer. This changes the r2 statement that delegated agents need nothing added.

**Forwarded tools: an allowlist on the computer (lens N2).** `delegate_task` is not the only upstream tool that starts
work where it is forwarded: thread launch and thread creation start a provider, project create and clone, worktree,
terminal and git tools make the server run processes. On a host-tools computer, the host account's T3 refuses **every**
forwarded tool call outside an exact allowlist: the host tools of §9 (`host_status`, `host_list`, `host_stat`,
`host_read`, `host_search`, `host_write`, `host_mkdir`, `host_move`, `host_delete`, `host_run`, `host_undo`,
`host_observe`, `host_do`, `host_recipe`, `host_ask`, `host_named`) and `t3_environment_read`. A tool upstream adds
later is denied until the list names it. The check runs on the computer, where the call arrives, not on the agent
machine, and on **every** MCP session of that install: peer links and outside MCP clients (the §2 fallback) alike. T3's own terminal and provider features are switched off in that install (a fork setting the install writes;
the host account's T3 starts no provider and no terminal). The install verify (§3.3) checks both.

### 3.3 Install verify (host tools stay off until it passes)

The install, and every later start, checks:

- both accounts exist and are in no admin or remote group (the agent account holds only the RDP sign-in right, and
  only when its own session is on; the RDP port answers no address but the computer's own); the agent group holds
  exactly the agent account and the slot users; the install is admin-owned and read-only to both; the broker's, the
  engine's and the engine worker's channel ACLs are as stated;
- the forwarded-tool allowlist is on and T3's terminal and provider features are off in the host account's T3 (§3.2);
- the reach map's grants, the secret deny pass and the auto-run denies are applied and current; the agents' own
  persistence points are denied (§3.1); the protected-area watch runs (§4.4);
- **every writable spot in a protected area has its deny entry** (lens P1, owner answer 7 of 2026-11-13): the reach
  check below lists them, and the install puts Deny write, delete and create-child entries for the agent group on each
  (link-safe, §3.4), with no "accept". A missing entry fails the verify;
- the agent and runner groups cannot start WSL, Hyper-V or other VM tools (Windows: Deny execute on `wsl.exe`,
  `wslg.exe`, `wslhost.exe` and `hvc.exe` for those groups, plus an App Control rule where the edition has it; macOS
  and Linux: no access to the virtualization frameworks, `/dev/kvm` or the container daemons' sockets);
- the firewall rules exist and each network class of §3.5 is probed from the agent account, including the computer's
  own global addresses, an on-link neighbour, a name lookup outside the tailnet, a BITS download and a WSL start;
- the disk quota is set; on Linux `fs.protected_hardlinks=1` and `fs.protected_symlinks=1`;
- elevation stays behind a prompt the engine cannot click: Windows UAC on, with the secure desktop; Linux and macOS:
  no `NOPASSWD` sudo rule for your account (§4.4);
- runner accounts, if any, have the isolation of §11.

**UAC "Always notify" by default (owner answer 4, 2026-11-13).** Setup sets UAC to "Always notify". You can lower it in
Windows settings; the verify then shows a warning (below), it does not turn host tools off.

**Host tools stay off until every check passes**, and `host_status` reports which one failed. `host_status` re-runs
the quick checks on every call and the full set daily; a failure turns host tools off again with the reason.

**Warnings that do not block** (shown in the tray and `host_status`, and named in §14): your Windows account is an
administrator with UAC below "Always notify" (known auto-elevation tricks skip the prompt at the default level); on
Linux, your account in the `docker`, `lxd`, `incus` or `libvirt` group, or a polkit rule that keeps admin
authorisation (`auth_admin_keep`) or needs none, since each gives root with no prompt; git
`safe.directory = *` in your git config (git then trusts a `.git` the agent account made); IDE settings that run code
when build files change (IntelliJ and Android Studio auto-reload of Gradle or Maven builds, rust-analyzer build scripts
and proc macros, Visual Studio design-time builds, VS Code `task.allowAutomaticTasks` on or Workspace Trust off), §4.3
rule 3.

**Reach check (lens P1, correctness).** Some places are readable or writable by every user by default (Windows:
folders on extra drives, `C:\Users\Public`, `C:\ProgramData`, where Users may create files and folders, the root of
the system drive, where Authenticated Users may create folders, Program Files folders that installers left writable,
HKLM keys that grant Users write; macOS: world-readable shared folders, `/Applications` for admins, a Homebrew prefix;
Linux: world-readable shared folders, `/opt` and `/usr/local` subtrees owned by your account). The install walks every
fixed drive and the registry:

- **as the agent account**, listing every place it can **read or write** outside the reach map. A writable spot in a
  protected area gets the agent-group deny entries above, with no choice to accept it. A readable spot outside the
  protected areas you remove or accept; a readable secret-pattern match gets a deny entry (§4.3 rule 2);
- **as your own standard (filtered) token**, listing every protected-area spot your account writes without elevation.
  These join the guard's watch and lock (§4.4 layers 1-2), so a change there by an agent-caused process or while acting
  is held.

The daily check repeats both walks, applies new deny entries, and reports new finds.

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

### 3.5 Network for the agent account (lens M3, N5; owner decision 2026-11-13)

**Owner decision: no direct internet by default.** Work that needs no computer of yours runs on the server: web search
and fetch already do. The agent account (and the agent group) gets:

- **Allowed by default:** only the **services you name on your own linked machines** (§10), by their tailnet
  addresses and ports: for example the git server and the package cache you run there (correctness review). The list
  is config per linked machine, default empty; the install offers the services it finds listening there, one tick
  each. A whole machine is never opened, so a hostile build script cannot reach the agent machine's T3, SSH,
  databases or dev servers.
- **Opened per goal:** a goal that needs more (a package registry, a git host, "the public internet") asks once through
  the batched ask (§4.2), default scope **this task**. The ask names the hosts; "the public internet" is one item of
  its own. Firewalls match addresses, not names, so for a named host the guard resolves the name itself and pins the
  answers into the goal's address set, re-resolving when their time to live ends, for as long as the goal lasts.
- **Name lookups (lens P3, correctness).** The agent group gets a resolver run by the guard. It answers only the names
  of your linked machines and the hosts a goal opened, and refuses every other name, so a lookup of
  `<data>.attacker.example` never leaves the computer through it (the tailnet's own resolver forwards outside names to
  public resolvers, so it is not used). Linux: the command's namespace sees only that resolver, and the system
  resolver's sockets (`systemd-resolved`'s varlink socket, `nscd`) are hidden in its mount namespace. **Windows and
  macOS:** programs resolve through the DNS Client service or `mDNSResponder`, which do not run as the agent account,
  so firewall rules by account do not see those lookups; the guard's resolver serves only programs that ask it
  directly. There, a lookup can still carry a little data out (named in §14).
- **Browsing as you** (your logins, your sites) happens in your own browser session through the UI, which is acting as
  you (§4.4), not through the agent account.

**Always denied, even when the internet is opened** (the floor no answer removes, except an explicit entry for one
address): loopback (`127.0.0.0/8`, `::1`), RFC 1918, CGNAT and tailnet (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) other
than your linked machines, link-local (`169.254.0.0/16`, `fe80::/10`, which includes cloud metadata
`169.254.169.254`), ULA (`fc00::/7`), multicast and broadcast, **every address of every local interface** in both
families (so your own services listening on all addresses are not reachable through the computer's global IPv6
address), and **every on-link prefix** (so a NAS or router with a global IPv6 address on your LAN is not "the
internet"). The guard re-reads the interface addresses and prefixes on every address change (Windows
`NotifyIpInterfaceChange`, macOS `SCDynamicStore`, Linux rtnetlink) and updates the rules before the next agent
connection; while an update is pending, the agent account's network is closed.

- **Loopback:** the agent account may reach only listeners of its own (tests that start a local server). Linux: each
  command runs in its own network namespace with its own loopback and a user-mode route out (`pasta` with the
  host-loopback mapping off, `--no-map-gw`, and DNS forwarded only to the guard's resolver); macOS: `pf` `user` rules
  on `lo0` (to verify); Windows: a WFP filter on loopback that matches the connecting and the listening user (to
  verify). It never reaches your dev servers, a browser's debug port, a desktop T3, container APIs or sync UIs.
- **Namespaces set up by the guard (correctness).** On Linux the guard (root) makes each command's mount and network
  namespaces and then starts the command as the agent account's own uid, with no capabilities and no user namespace of
  its own, so it cannot unmount the read-only binds of §4.3 rule 3 or change its network. `pasta` runs as the agent
  account on the host side, so the `nftables` `skuid` floor applies to its sockets too.
- **Per OS:** Windows: raw WFP filters in the fork's own sublayer, with weights, scoped to the agent group's SIDs
  (ordinary Windows Firewall rules cannot express "block all except" because a block always beats an allow there);
  macOS `pf` with `user`; Linux `nftables` `meta skuid` / `meta skgid`.
- **Openings never outlive their grant (correctness).** A per-goal opening and a one-address exception are firewall
  state. The guard writes a ledger line before it adds the rule, and removes the rule when the grant ends, on pause and
  on the kill switch. Windows uses WFP dynamic-session filters, which vanish with the guard's session; on every start
  the guard removes every opening in its ledger **before** the agent group's network comes up, so a crash or power
  cut cannot leave the network open.
- **No way around the floor through system services or VMs (lens P3).** WSL and Hyper-V traffic leaves through their
  own NAT, not as the agent account, so the agent and runner groups may not start them (§3.3). BITS transfers run in a
  service: the verify probes a BITS download from the agent account, and if it gets through, host tools stay off until
  an App Control rule denies the agent group BITS (to verify).
- The computer's T3 gives loopback callers **no** trust or bootstrap: other local accounts are remote clients that must
  sign in.
- The install verify (§3.3) probes one address of each class from the agent account, plus the computer's own global
  IPv6 address, an on-link neighbour, a port on a linked machine that config does not name, a lookup of a name outside
  the tailnet, a BITS download and a WSL start, and refuses to enable host tools if any gets through (on Windows and
  macOS the lookup probe checks the guard's resolver only, as above).

## 4. Scope and rules

### 4.1 Goal-driven scope (owner decision 2026-11-13)

You say what you want done. The agent picks the route (§5). It may use the **whole computer except sensitive things**,
and it looks only at what is relevant to the task.

**Open by default:**

- Your data folders (the reach map, §4.3 rule 1): read and write, with undo.
- System areas: **read** as far as the OS lets any standard user, except secrets. Writing, deleting and running there
  is a protected-area action (§4.4).
- Every app and window, for seeing and acting, through the routes of §5 and the input ladder of §6.4. Acting there is
  acting **as you** (§4.4).
- The clipboard (with the limits of §6.7).

**The sensitive set: never usable, no answer can open it** (hard refusals):

- Secret files by built-in pattern (keys, `.env`, `*.pem`, `*.kdbx`, cloud and CLI credential files, …) and every
  secret store: OS keychains and credential managers, browser login and cookie stores, password managers, wallets.
  System secrets too (the SAM and SECURITY hives, `/etc/shadow`, SSH host keys), which the OS already denies.
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

| On the high-risk list                                                                                                                                                                                                                                                                                             | Why                                                                                                    | Default scope                        |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------ |
| **Full-user-rights apps** for acting: shells and terminals (also those built into IDEs, and IDE task runners), the Run dialog, the Start/Search box, Spotlight and other launchers, the file manager, system settings, the task scheduler, the registry editor, script hosts and macro consoles, browser devtools | acting there runs any code as you directly (other apps can too, through a file dialog or a link; §4.4) | this task                            |
| **Privilege elevation:** a command run as admin, a machine-wide install                                                                                                                                                                                                                                           | changes the OS for everyone                                                                            | once (one command), never remembered |
| **A protected-area action** (§4.4): one exact write, delete or run in a system area, another user's profile or an autostart or persistence point                                                                                                                                                                  | system-wide or lasting effects                                                                         | once, never remembered               |
| Installs for you (winget / Homebrew / the distro's package manager)                                                                                                                                                                                                                                               | new code that runs as you                                                                              | this task                            |
| Your profile's config and app-data areas (`AppData`, `~/Library`, `~/.config`, dot-folders) beyond the reach map                                                                                                                                                                                                  | they hold tokens and sessions outside the built-in patterns                                            | this project                         |
| Auto-run paths in a folder (git hooks and config, IDE task files, mods and plug-in folders; §4.3 rule 3)                                                                                                                                                                                                          | code there runs as you without you choosing to run it                                                  | this project, per folder             |
| Other users' data on the computer                                                                                                                                                                                                                                                                                 | not yours to give                                                                                      | this task                            |
| A new app-side bridge, with its verbs listed (§6.6)                                                                                                                                                                                                                                                               | a bridge acts inside an app with your rights                                                           | always                               |
| Network for the agent account beyond the named services on your linked machines (§3.5): named hosts, or the public internet                                                                                                                                                                                       | data can leave; downloads can bring code in                                                            | this task (per goal)                 |
| An address in an always-denied class (§3.5), one address at a time                                                                                                                                                                                                                                                | your LAN and tailnet services                                                                          | this project                         |

**Remembered.** Answers live in the protected store, per scope: the agent never asks twice for what you already
answered in that scope. A "this task" grant ends with the task (the T3 turn chain that asked), on pause, and on the
kill switch.

**Not worn down (lens m4).** After a Deny, the same item is not shown again in that task; a new ask with only items
denied in the last 10 minutes is dropped without a dialog; the dialog shows how many asks this task already made. An
ask that times out unanswered is denied for that call only: it does not count as a Deny for these rules, so one missed
dialog does not block the item for the rest of the task.

**Confirmed where the agent cannot click (owner decision 2026-11-13: keep the OS consent).** The engine refuses to see
or act on the ask dialog. Items that grant **full user rights, elevation or a protected-area action** are also
confirmed through the OS's own consent, because code with your rights may already run (a full-rights grant, or a file
run through an app's UI, §4.4) and could click a dialog in your session. For the same reason, **every widening of the
policy needs that OS consent whenever an acting batch, a full-rights grant or a persistence lock window (§4.4) ran in
the last 30 minutes (setting)**, not only while a marked process is still alive, since a process can escape the mark
(lens P5). Narrowing never does.

**Every tray and T3 action is classed (lens P5).** Widening actions are written only by the guard, never by the helper
or T3 alone:

| Action                                                                                                                                                                                                                                                                                                         | Class     | OS consent the guard checks                                                           |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------- |
| Deny, pause, wind down, kill switch, revoke, revoke all, Remove of a held or quarantined item, End of a process, removing a reach-map folder, runners off                                                                                                                                                      | narrowing | never                                                                                 |
| Allow on an ask item of full user rights, elevation or a protected-area action                                                                                                                                                                                                                                 | widening  | always (above)                                                                        |
| Keep of a held protected-area change, Keep of a quarantined auto-run path, "make it mine", **Continue** after a take-over (tray, T3, or Allow on the agent's ask item "continue on your screen"), turning host tools back on after a failed verify or the kill switch, confirming an outside edit of the store | widening  | always                                                                                |
| Resume after pause or wind-down, Allow on other ask items, accepting a reach-check find, adding a reach-map folder, extending links, every other setting that widens                                                                                                                                           | widening  | when an acting batch, a full-rights grant or a lock window ran in the last 30 minutes |

**Continue and widening come only from a person.** The computer accepts Continue and every other widening from T3 only
through a person's T3 UI session on that computer, never through an MCP or agent session, and the guard still applies
the consent rule above. The engine refuses to see or act on any window that shows a T3 page with host-tools controls,
including Continue banners and the ask (§6.7).

**The guard checks the consent itself (lens N4).** The helper runs as you, so code running as you could inject into
it or call the guard's channel and claim "the owner agreed". The guard therefore never takes the helper's word for an
item that needs OS consent; it writes the answer only after it verified the consent itself:

- Windows: the answer must come from a process the guard can check: the fork's signed confirm binary, started through
  UAC on the secure desktop, connecting with a full (elevated) high-integrity token. The guard checks the token's
  elevation type and integrity, the image path and its signature.
- macOS: a root LaunchDaemon cannot show `SecurityAgent` itself, so the helper obtains the guard's own right with
  `AuthorizationCopyRights` (the right is defined with `authenticate-session-owner`, `shared = false` and
  `timeout = 0`, so `SecurityAgent` asks you every time) and passes it as `AuthorizationExternalForm`; the guard
  verifies that right itself before it writes the answer (lens p4).
- Linux: the guard itself makes the polkit check for its own action (`auth_self`, not kept), with the helper's
  process as subject, so your polkit agent asks you. On X11, code running as you can read what you type into the
  polkit agent, including your password; on Wayland it cannot. Named in §14; Wayland is recommended for host tools.

Other answers (a dot-folder, a network host) come from the helper as before. Code running as you could forge those;
they grant the agent account only things your own account already has, but a forged "always" answer would outlive the
code. That is why widening needs OS consent after recent acting, and why every remembered answer is listed in the tray
with its time and the consent it carried.

### 4.3 Rules the computer enforces

Normal work needs no click. Each rule says **who enforces it**, because a command run as the agent account can change
anything that account owns. Nothing that must hold is enforced only by the T3 server.

1. **Reach map** (OS). The agent group gets read-write on your data folders. **Default (owner decision 2026-11-13):**
   documents, desktop, downloads, media (pictures, music, videos), code folders and the data drives. The list is
   config: it grows over time (the tray and settings add folders; the install suggests code folders it finds), and
   the core holds only the default kinds, never a person's paths. Your profile root, its config and app-data areas and
   other profiles are not granted (they are on the ask list); system folders are readable as the OS allows and are
   protected areas (§4.4). The helper applies grants as the folders' owner; the guard service re-applies them daily
   (§3.4 rules) and reports drift. Removing a folder ends every agent-account process with an open handle under it
   (Windows Restart Manager, `lsof`/`/proc`), because an open handle keeps its access after an ACL change.
2. **Secret deny list** (OS, applied by the helper and the guard). Built-in patterns, one OS-neutral list in the core;
   the policy can add patterns, never remove a built-in one. Every match inside the reach map gets a deny entry, and so
   does every match the reach check (§3.3) finds readable outside it, including app tokens in `ProgramData` and configs
   in `/etc` (the guard adds a deny entry for the agent group only, which changes nobody else's access; where it cannot,
   the match is named in `host_status` and §14, lens p9). A watcher denies a matching file you create within about a
   second. **Fail closed:** on helper start, after a watcher overflow and after the daily re-apply, a full deny pass
   runs **before** host tools are enabled. While the pass is not current, the server refuses file reads **and** the
   broker starts no agent-account process: no `host_run`, no `run` step, no speculative run, no engine worker and no
   agents' browser (lens M8b, correctness review), since those read through the OS, not through the file tools. Token shapes (`ghp_`, `sk-ant-`, `AKIA…`, JWTs, …) are
   redacted in every output path and every stored record (§6.7).
3. **Auto-run paths are read-only to the agent** (OS deny entries and the guard, lens B1e, N3). Inside writable
   folders: `.git/config`, `.git/hooks/`, `.git/info/` (`.gitattributes` filter lines are left alone, since the
   filters themselves live in `.git/config`); IDE task and settings files (`.vscode/`, `.idea/`); `.envrc`; and the
   mod and plug-in folders of apps you run. Code there runs as you when you merely open or `git status` the folder.
   **`.git/config` stays read-only until you unlock that repo** through the ask (owner decision 2026-11-13); git
   commands that write repo config (`push -u`, `remote add`) fail for the agent until then, and the tool descriptions
   say so. Autostart places outside a project (shell profiles, Startup folders, login items) are protected areas
   (§4.4).
   - **No rename or delete around the deny** (N3a). Windows: Deny `DELETE` on each auto-run file and folder, and Deny
     `FILE_DELETE_CHILD` on the folder that holds it, for the agent group (Windows allows a rename or delete if either
     right is there, so both are denied; other entries in that folder stay deletable through their own `DELETE`).
     macOS: Endpoint Security `AUTH_RENAME`, `AUTH_UNLINK` and `AUTH_CREATE` deny the agent group on auto-run names.
     Linux: each command runs in its own mount namespace (next to the network namespace of §3.5), and every existing
     auto-run path is bind-mounted read-only into it; a mount point cannot be renamed or removed.
   - **New auto-run paths are quarantined** (N3b). Where the agent group **creates** an auto-run path (a new
     `.vscode/tasks.json` in a folder that had none, an `.envrc`, a new plug-in in a mod folder), it is blocked where the
     OS can block (macOS Endpoint Security; Linux a BPF LSM where the kernel has one), otherwise the guard moves it into
     the undo store within about a second (writer recorded, §3.4 link-safe) and lists it in the tray and the next ask,
     with its content, as "keep this?". A live watcher alone is not relied on: the quarantine happens whether or not you
     are looking.
   - **New repositories** (`git init`, `git clone` by the agent): **only `.git` is exempt** (lens P6). It is owned by the
     agent account, and your git refuses to use it (git's `safe.directory` owner check) until you take it over with
     "make it mine" in the tray, which hands `.git` to you and applies the denies above. Every other auto-run path in
     the new repository (a cloned `.vscode/tasks.json` with `runOn: folderOpen`, `.idea` run configurations, `.envrc`,
     plug-in files) is quarantined like any other new auto-run path, and "make it mine" lists them in the same ask, with
     their content. With `safe.directory = *` in your git config (warned in §3.3), new `.git` folders are quarantined
     too.
   - **Package manifests and build files stay writable** (`package.json`, `build.gradle(.kts)`, `build.rs`, `.csproj`,
     …): agent work is code. They run when you choose to build, **and also when an IDE syncs them on its own**
     (IntelliJ and Android Studio Gradle or Maven reload, rust-analyzer build scripts and proc macros, Visual Studio
     design-time builds). The install verify warns about these settings (§3.3); every agent write to a build file is
     flagged in the activity log (rule 7); the residual is in §14.1 (N3d).
4. **Approved runs use frozen copies** (engine and helper, lens B1). This rule covers what T3 runs as you on your
   behalf; running files through the UI is open (§4.4).
   - Every launch, approved script, named command and admin command runs in a working folder the agent account cannot
     write (so no planted DLL is picked up).
   - Approved scripts, named commands and admin commands may not run files from agent-writable folders. A command that
     needs one (a deploy from a repo) runs from a **frozen copy**: the helper copies the referenced files, or makes a
     verified git copy at a commit, into a protected folder; the ask shows the hash or commit **and the diff since the
     last commit you approved** (lens n6); the copy runs.
   - **A frozen git copy is built only from verified objects (lens P2).** The agent can write `.git/objects`, packs and
     `objects/info/alternates`, and git does not re-hash objects on normal reads, so a commit id alone proves nothing.
     The helper clones into the protected folder with `git -c transfer.fsckObjects=true clone --no-local --no-hardlinks`
     and no alternates, then checks out the commit, so every object is re-hashed on the way in. Git runs there with the
     source repo's config, hooks, attribute filters and alternates ignored (a fixed system and global config of its
     own, `core.hooksPath` empty). "The same hash later needs no new ask" reuses the **kept, verified copy**; a missing
     or changed copy is built and verified again.
   - A bridge whose file is in an agent-writable folder must be listed with its hash (§6.6).
   - The engine's own COM automation (Office and similar) creates servers with macros forced off
     (`AutomationSecurity = msoAutomationSecurityForceDisable`).
   - **No mark and no ask before opening** (owner decision 2026-11-13). Files the agent writes get no Mark-of-the-Web or
     quarantine flag, and the engine's `app open` and `window launch` open any file you could open. The r3 file-type
     rule is dropped: it held only for the engine's own verbs, and any app's file dialog went around it (lens N1).
     Every open of an executable or script by an agent-caused process is logged as a run (rule 7).
5. **Protected store** (guard service, lens B3). The policy, ask answers, recipes, approved scripts, named commands,
   bridges and pause state live in a store owned by the guard service. Your account and the host account can only read
   it; the agent group cannot read it at all (lens n4). Changes go through the guard: the helper asks, the guard writes,
   and widening follows §4.2, with the consent the guard checks itself. A store file edited outside the guard (by an
   admin) fails the guard's signature check on load and is ignored until you confirm it in the tray with OS consent
   (recovery after a power cut: §12.3). The engine refuses acting on any window whose document is in the store's folder
   and refuses file-dialog navigation into it.
6. **Undo** (guard service).
   - **Snapshots:** OS snapshots of the volumes in the reach map, taken by the guard before an agent write or run when
     the last one is older than 30 minutes. The wind-down snapshot (§12.2) and the one before a speculative winner is
     applied (§7.4) reuse the last snapshot only if no agent write came after it; otherwise they take a fresh one. At
     most 48 a day, and the guard counts System Restore and other copies on the volume against the Windows Volume Shadow
     Copy limit of 64. Windows: Volume Shadow Copy; macOS: APFS local snapshots (`tmutil localsnapshot`); Linux:
     btrfs/ZFS/LVM-thin snapshots where the folder lives on one.
   - **Where there are no usable snapshots (correctness review).** Linux on other filesystems: `fanotify` reports a
     change only after it happened, so nothing can copy a file before a command changes it. There, **command changes
     have no undo**; only T3's file tools undo (pre-images). `host_status` reports `undo: file tools only`, the tray
     shows it, and the install suggests a btrfs or ZFS volume for the reach map. macOS deletes local snapshots by itself
     under disk pressure, so neither the budget nor the undo window below can be promised there; the tray warns when
     one disappears early.
   - **Pre-images:** T3's own file tools (`host_write`, `host_move`, `host_delete`) keep the previous version of each
     file they change in the undo store, so their changes undo exactly, not only to the last snapshot.
   - **The undo store does not fill in normal use (owner answer 6, 2026-11-13).** The undo store and the snapshot
     storage share one disk budget:
     - **sized from measured need:** the guard records how much undo space agent work used per day on this computer and
       sizes the store for the undo window (24 hours, setting) of the busiest day seen so far, plus a margin; the first
       time it uses a default estimate;
     - **on a drive with room:** the store, and on Windows the shadow-copy storage area (which can live on another
       volume), go on the fixed drive with the most free space that the agent group cannot write;
     - **grows within a budget you set** (setting; the install suggests one from the free space);
     - **prunes only what is safe:** copies that belong to goals you accepted or that have finished, and only once they
       are older than the undo window. Never a held or quarantined item, never anything inside the undo window;
     - **warns early:** at 80 % of the budget the tray says so, with what fills it and "raise the budget";
     - **refusing is the last resort, on by default:** only if the budget is still full, an agent write whose
       pre-images would not fit, or that would push out a snapshot inside the undo window, is refused, with a clear tray
       message (what is full, by what, and the choices: raise the budget, accept finished goals, or turn this guard off).
       A `host_delete` of a large tree that would not fit is refused with the size needed, never run without undo
       (lens n7). With the guard turned off, snapshots older than 1 hour (the floor) may be pushed out, and the tray
       names the agent changes that lost their undo.
   - **Held and quarantined items never fail open (lens P7).** Quarantined auto-run paths (rule 3) and held
     protected-area changes (§4.4) sit in a **reserved area** of the undo store, outside the budget's pruning and outside
     the undo window: an item waiting for Keep or Remove never expires and is never pruned, however long it waits. The
     reserved area has its own size (setting, default 1 GB). If a quarantine or a revert does not fit even there, the
     guard still removes the path (link-safe), logs its content hash, size and first bytes, refuses every agent write
     and run until there is room, and shows it in red in the tray.
   - **Writer recorded:** the guard records which account changed each file (Windows ETW kernel file events, macOS
     Endpoint Security, Linux `fanotify`). Where the writer cannot be told, the change counts as yours.
   - **Fail closed (lens M8a):** if a snapshot cannot be taken, agent writes and runs are refused and the tray says
     why. The agent account's disk quota stops it filling the disk to push snapshots out.
   - **Agent undo restores only agent changes (lens M9):** an agent-called `host_undo` restores only files whose
     changes in the window came from the agent account, and only if the file still has the hash the agent left it
     with. Files you changed are listed, not touched; restoring them is offered only in the tray.
   - **The real guarantee (lens N9).** T3 file tool changes undo exactly for the undo window (pre-images). Other changes
     go back to the last snapshot before them for the whole undo window while the last-resort guard is on (the
     default), and for at least 1 hour with it off. Not covered: command changes on Linux without btrfs, ZFS or
     LVM-thin; snapshots macOS deletes under disk pressure; changes **inside apps** unless the app saves into the reach
     map.
7. **Log** (guard service is the authority, lens m2). The guard's log records every file change with its writer,
   every batch and step the engine ran (app, target, action, rung, result, timing), every ask and answer, take-over,
   pause, wind-down and kill switch. The T3 log (host account) records every host tool call and its caller (§9): who,
   tool, arguments without file contents, result, bytes, time. Each line carries the hash of the line before; every 5
   minutes the guard countersigns the T3 log's head into its own log, so a forged or cut T3 log shows. A call whose
   log write fails is refused.
   - **What the agent wrote and ran** (owner decision 2026-11-13). A tray view, also in `host_status`, lists every file
     written by the agent group or by an agent-caused process as you (§4.4), and every program and script an
     agent-caused process started, with time, task and thread, writer, path, hash, and the file's last writer (so a run
     of a file the agent wrote stands out). Writes to build files that IDEs sync (rule 3) and every held protected-area
     change are flagged. Each line offers Undo or Remove where one exists, and each goal offers **Accept** (its undo
     copies may then be pruned once they are older than the undo window, rule 6).
8. **Pause, wind down, kill switch, revoke all** (tray, §12). _Pause_: every host tool and every new turn is refused
   with "paused by owner"; **every agent-account process** is ended (the T3 server is the host account, so nothing it
   needs survives; daemonised children and scheduled runs go too, lens m1); batches stop; held keys and buttons are
   released; "this task" grants end; agent-caused processes running as you (§4.4) are listed in the tray with "end"
   (they may be apps you now use, so pause does not end them). _Wind down_: §12.2. _Kill switch_: the guard ends every
   agent-group process **and every agent-caused process running as you**, stops the engine and the host account's T3,
   and disables both accounts until you turn them back on (with OS consent the guard checks). _Revoke all_: every link
   to and from this computer is revoked (§10). Pause and wind-down rely on the guard and the helper; the kill switch
   relies only on the guard and the OS, not on T3 or the helper. **Narrowing is durable before it is confirmed (lens N8):** pause, kill switch, revoke, Deny
   and narrowing the reach map are written and fsynced by the guard, in the store and as a narrowing record in its log,
   before the tray shows them as done.
9. **Availability and who may call** (agent machine, then the computer; lens M6).
   - Per computer, on the agent machine: Settings → Linked environments → _computer_ → Host tools: **on** (default),
     **off**, or **off until** a time. Stored in `ServerSettings`.
   - Per project, on the agent machine: **host tools for this project** on or off (a project setting, **default off
     for new projects**, owner decision 2026-11-13; one toggle each). Your own machines and projects are trusted; this keeps them away from projects
     that mostly read outside text.
   - Per thread: a thread marked **outside input** (set by you, or by a launcher for work on outside contributions,
     public issues or web research) gets the read tools at most.
   - The agent machine forwards the calling project and thread id with each call; the computer logs it and the tray
     shows it while acting.
   - These are convenience switches on the agent machine; the computer's own pause, wind-down and kill switch win.

**No activity gate.** Host tools do not check whether you are gaming or busy. Work you asked an agent to do runs while
you do anything else. Only §11's background runners adapt to the free hardware.

### 4.4 Acting as you, and protected areas (owner decision 2026-11-13, lens N1)

**Said openly: when the agent acts in your apps' UI, it acts as you.** Any app with a file dialog, a launcher, a link,
a built-in terminal or a macro console can run a file with your rights, including a file the agent wrote, and the OS
shows no warning: it sees you opening a file. Files the agent writes are not marked as downloaded, and nothing asks
before opening (§4.3 rule 4). The full-user-rights list (§4.2) closes the direct routes (terminals, launchers); it is
not a wall around the rest. Once agent-caused code runs as you, it can do whatever your account can. What holds
instead:

1. **Protected areas** need your OK for each single action, enforced by the OS and deny entries where they can be,
   otherwise held by the guard (below).
2. **Every file the agent wrote or ran is logged and shown** (§4.3 rule 7).
3. The sensitive set (§4.1), take-over (§6.7), undo (§4.3 rule 6), the guard-checked consent (§4.2) and the kill
   switch, which also ends agent-caused processes running as you.

**Protected areas** (one OS-neutral list of kinds in the core, with per-OS entries; the policy can add entries, never
remove a built-in one):

| Kind                      | Windows                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | macOS                                                                                                                                                                                                                                          | Linux                                                                                                                                                                                                                                                                                          |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| System                    | `C:\Windows` (System32 and the rest), `Program Files*`, `ProgramData`, the root of each fixed drive, boot and EFI partitions, the HKLM registry                                                                                                                                                                                                                                                                                                                                                                                                                                    | `/System`, `/Library`, `/usr`, `/bin`, `/sbin`, `/etc`, `/private`, `/Applications`, the Homebrew prefix (`/opt/homebrew`, `/usr/local`), boot volumes                                                                                         | `/etc`, `/boot`, the EFI partition, `/usr` (with `/usr/local`), `/bin`, `/sbin`, `/lib*`, `/opt`, `/var/lib`                                                                                                                                                                                   |
| Other users               | other profiles under `C:\Users`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | other folders under `/Users`                                                                                                                                                                                                                   | other homes, `/root`                                                                                                                                                                                                                                                                           |
| Autostart and persistence | Startup folders (yours and all users'), Run and RunOnce keys (HKCU and HKLM, both views), scheduled tasks, services and drivers, WMI event subscriptions, Winlogon, Image File Execution Options and AppInit keys, per-user COM and file-association overrides (`HKCU\Software\Classes`), PowerShell profiles, Office add-in and template folders, browser extension folders, **WSL boot config** (`.wslconfig`, each distro's `/etc/wsl.conf` `[boot]`), the folder redirect keys (`HKCU\...\Explorer\User Shell Folders` and `Shell Folders`, which can point Startup elsewhere) | LaunchAgents and LaunchDaemons (yours and the system's), login items and Background Task Management items, `cron`, `periodic`, shell profiles, `/etc/paths.d`, `launchctl setenv` and `~/.MacOSX/environment.plist`, browser extension folders | systemd system and user units, timers and generators, `cron` and `anacron`, `/etc/profile.d`, shell profiles, XDG autostart (`~/.config/autostart`), `environment.d` and `~/.pam_environment` (which can move `XDG_CONFIG_HOME` to an unwatched folder), udev rules, browser extension folders |

**What "your OK per action" means.** A write, delete or run in a protected area is one `host_ask` item (§4.2) that
names the exact path, key or command, shows the content or the diff, has the fixed scope **once** and is never
remembered, and is confirmed through OS consent that the guard checks itself. **Reading** protected areas stays allowed,
except secrets (§4.1).

**"Run" (owner answer 1, 2026-11-13)** is any of: running a file **the agent itself placed** in a protected area (a
file whose recorded writer is the agent group or an agent-caused process, including one you kept after a hold);
running **anything elevated** (admin or root); and anything that **registers autostart or persistence** (a service,
driver, scheduled task, boot or login entry). Running an ordinary installed program (a shell, git, a compiler) with the
agent account's or your rights is not a protected-area action. A run of an agent-placed file without your OK is blocked
where the OS can block (macOS Endpoint Security `AUTH_EXEC`; Linux `fanotify` `FAN_OPEN_EXEC_PERM`); on Windows in v1
the guard ends it at its process-start event and holds it like a persistence change (blocking before it starts needs
the driver of §17, owner answer 5: only if measurements show it is needed). `host_run`, the engine and the helper
refuse such a run before starting it.

**Who enforces it:**

| Route                                                             | Enforced by                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Commands (agent group)                                            | **the OS and deny entries:** standard accounts with no admin rights and no way to elevate (they have no interactive session of yours to show a prompt in); every spot of a protected area that the OS leaves writable to users (`ProgramData`, a drive root, Program Files folders installers left writable, HKLM keys that grant Users write, `/opt` and `/usr/local` subtrees) carries Deny write, delete and create-child entries for the agent group, found and checked by the reach check (§3.3, lens P1); other profiles are not readable or writable to them; their own persistence points are denied (§3.1)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| Acting as you: system areas that need elevation                   | **the OS:** the elevation prompt is on the secure desktop or in `SecurityAgent` / the polkit agent, which the engine refuses (§6.7), so only you can approve, once. Setup sets UAC to "Always notify" (owner answer 4) with the secure desktop, and the verify requires no `NOPASSWD` sudo rule for your account (§3.3); on Linux and macOS the guard clears your sudo timestamps when an acting batch or a full-rights grant starts, so a recent `sudo` of yours in an open terminal does not carry over                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Acting as you: system spots your account writes without elevation | **the guard**, the same three layers as your persistence points (next row). The reach check walks the protected areas as your standard (filtered) token and lists every such spot (`ProgramData` folders, user-writable Program Files folders, writable HKLM keys, `/Applications` for an admin, the Homebrew prefix, `/opt` or `/usr/local` subtrees you own); each joins the watch, and the lock while acting where it takes ACLs                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| Acting as you: your own persistence points                        | **the guard**, three layers. (1) **Lock while acting:** while an acting batch or a full-rights grant runs, and for 2 minutes after (setting), the guard puts deny-write entries for your account on the persistence points that take ACLs (Startup folder, Run keys, profile files, `LaunchAgents`, systemd user unit and autostart folders), and takes ownership of them for that time (SYSTEM / root), since an owner can always rewrite its own ACL; afterwards it gives ownership back and removes the entries, and a ledger undoes leftovers on the next start. Each locked object has a lease count in the ledger, so when two windows overlap, the first one ending does not unlock it (lens p6). Layer (1) holds only against **edits in place**: you own your profile and home, so a locked file can still be deleted and recreated through its parent folder, or a parent renamed; (2) catches both. (2) **Watch always:** Windows ETW registry and file events plus Task Scheduler and service-control events; macOS Endpoint Security, which can block; Linux `fanotify`, plus a BPF LSM where the kernel has one, which can block. A change by an agent-caused process, or any change during the window of (1), is **held**: blocked where the OS can block, otherwise reverted at once into the reserved area of the undo store (§4.3 rule 6), and offered in the tray and the next ask with Keep (OS consent, once) or Remove. When the guard holds or reverts a scheduled task, unit, LaunchAgent, service or WMI entry, it also **ends every process that entry started** (Task Scheduler process-creation events carry the process id; the unit's cgroup; the launchd job), since such a process may have run in the second before the revert (lens P4). Other new entries (your own installs) are shown in the tray with Remove and logged, not held. (3) **Baseline diff (lens N4):** each full-rights grant and each visible batch records the persistence points at its start and diffs them at its end; every addition is shown with Remove and logged |

**Agent-caused processes.** The guard marks as agent-caused: every agent-group process; every process the engine
starts; every process started as you by an app the engine acted in, during the batch or within 10 s after (setting);
**every process started as you by the Task Scheduler, WMI, COM/DCOM activation, `systemd --user` or `launchd` during an
acting batch or its lock window** (lens P4: these break the parent chain); every process started by an entry the guard
held; and the descendants of all of these. It tracks them from the OS's process events (Windows ETW kernel process
events, macOS Endpoint Security, Linux the proc connector). The mark is still best effort: a process started through
such a service after the lock window is not marked. Layers (1) and (3) above do not depend on it.

**Marks and baselines survive a restart (correctness review).** The guard journals each mark (process id **and** start
time, so a reused id is never hit) and each grant's persistence baseline in its ledger. On start it re-attaches to the
marked processes that still run (the kill switch can still end them). For a grant that never reached its end it diffs
against the journalled baseline; if the baseline is missing, it shows every persistence entry newer than the grant's
start.

**What is left** (also in §14): code that runs as you can read and change everything your account can outside the
protected areas, including your profile's secrets, for as long as it runs. A process that escaped the agent-caused mark
is **not** ended by the kill switch and survives a restart if it set up persistence the guard missed (lens p1). It can wait until the lock window has passed
and use a persistence point not on the built-in list, or change a program you run that lives in a writable folder (a
tool in `~/bin`, a global package), which only the activity log shows. On Windows, `wsl -u root` needs no password, so
such code is root inside your WSL distros; only their boot config is watched. Setup sets UAC to "Always notify"
(owner answer 4); if you turn that off and use an administrator account, known auto-elevation tricks get admin rights
with no prompt, and that defeats the guard-checked consent, the protected store and **every** protected area, not only
system writes (warned in §3.3 and the tray; lens p1). On Windows the guard blocks only by ACL in v1 and ends a run or
change after the fact; a signed minifilter and registry callback would let it block per process before the fact. That
driver comes only if measurements show it is needed (owner answer 5, §17).

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
  take-over pause (§6.7), limits, and its own log line to the guard. The T3 server's checks come first but are
  not relied on.
- The engine runs **no free-form script as you**. Automation adapters are typed verbs shipped with the fork. A raw
  script (PowerShell, AppleScript, a shell line) runs as you only if it is an **approved script** in the protected
  store, from a frozen copy (§4.3 rule 4). Agent-written scripts run as the agent account through `host_run`.
- The engine never runs elevated. Windows: no `uiAccess`, medium integrity, so admin windows are out of reach by UIPI.
  macOS: no root helper. Linux: no root.
- **The engine worker** (§1) is the same binary started by the broker **as the agent account** inside the agent's own
  session or display (§3.1). It does rung 3 there and owns the agents' headless browser (§6.5). It has no channel into
  your session and none of the engine's power; the engine sends it typed verbs over a channel only the engine and the
  broker can open, and applies the same policy checks before it does (correctness review: rung 3 and the headless
  browser are not the engine's, which runs only as you).
- **Heartbeat (lens M8d):** the helper draws the indicator; the engine stops acting within 500 ms when the helper's
  heartbeat stops, releases held input, and refuses acting until the helper is back. It never acts without an
  indicator.

**What this means plainly:** with A's open scope, the agent can do in a non-sensitive app what you could do there by
hand, with your rights, and that includes running files through a file dialog, a link or a launcher (§4.4). Terminals
and launchers need the batched ask (§4.2); protected areas, the activity log, undo, take-over and the kill switch are
what limit the rest.

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

`app open` and `window launch` open any file you could (§4.3 rule 4); opening an executable or a script is logged as a
run. `input` steps aim at an element (its centre, or an offset in it) or at a point relative to a window, never at a
bare screen point.

### 6.4 The input ladder (owner decision 2026-11-13)

The agent **may** simulate mouse and keyboard when it needs to, but must **never take over your real cursor or
keyboard when a lower-level way exists**. Each `input` step (and each `el` action) runs on the fastest, least intrusive
rung that works for that app; the engine steps down only when a rung did not work.

| Rung | How                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Reaches                                                                                                                                                                                            | Cannot reach                                                                                                                                                                                                                                                                                  | Speed (p50)                                                                   | Your cursor and keys                                  |
| ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------- |
| 1    | **Actions with no coordinates:** UIA Invoke / Value / Toggle patterns; macOS AX actions and `AXValue`; AT-SPI Action and EditableText; app bridges; CDP `Input.dispatchMouseEvent`, `Input.dispatchKeyEvent`, `Input.insertText` for the agents' browser                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | standard controls in most apps, browser pages, any app with a bridge, background windows                                                                                                           | custom-drawn UIs, canvases and games without a bridge; controls that expose no pattern                                                                                                                                                                                                        | dispatch ≤ 10 ms; 1,000 chars of text in one `set_value`/`insertText` ≤ 20 ms | untouched                                             |
| 2    | **Events sent to one window** at window coordinates, cursor not moved: Windows `PostMessage`/`SendMessage` of `WM_*BUTTON*`, `WM_MOUSEWHEEL`, `WM_KEYDOWN`/`WM_CHAR` to the window handle; macOS `CGEventPostToPid`; X11 `XSendEvent` (xdotool `--window`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | classic Win32 and many toolkit apps, many macOS apps for keys, X11 apps that accept sent events                                                                                                    | games using Raw Input, DirectInput or `GetAsyncKeyState`; some Chromium content areas, UWP/WinUI windows and apps that check real key state; windows of higher integrity (UIPI blocks them); X11 apps that drop `send_event` events (xterm by default); native Wayland apps (no such channel) | ≤ 1 ms per event; 1,000 chars ≤ 60 ms                                         | untouched                                             |
| 3    | **The agent's own screen and input.** Main route, run by the engine worker as the agent account: Windows the agent account's loopback-only Remote Desktop session (§3.1; Pro and higher); macOS a second GUI login of the agent account on a virtual display (to verify; weakest OS here); Linux Xvfb or a headless compositor (weston or wlroots headless, gamescope headless). It never depends on your sign-in or stored password (spike 2026-11-13). For apps that need **your** logins: a hidden desktop in your session (`CreateDesktop`, its apps run as you, so it is acting as you); this is also the only rung 3 on Windows Home. A child session of your own account (picture-in-picture) is an option only where password sign-in is allowed (refused with "Windows Hello sign-in only", spike 2026-11-13) | apps the agent opens for its own use; on Linux real XTest/libei input into its own display (games included); on Windows and macOS rungs 1-2 inside it (`SendInput` reaches only the input desktop) | apps already open on your screen (they would have to be reopened there); on Windows and macOS, apps that need real hardware input; GPU apps where the hidden display has no GPU (to verify per OS)                                                                                            | as the rung used inside it, plus the app's start                              | untouched                                             |
| 4    | **Real synthetic input on your desktop:** `SendInput` / `CGEventPost` / XTest on X11, libei through the RemoteDesktop portal on Wayland (no `uinput` fallback: kernel-level input would reach the lock screen and polkit agents past every check, lens N7)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | everything you can reach by hand                                                                                                                                                                   | elevated and secure-desktop windows (UIPI, refused anyway)                                                                                                                                                                                                                                    | ≤ 2 ms per event; 1,000 chars ≤ 300 ms with the checks below                  | **used**: needs `visible: true`, indicator, take-over |

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
| 3    | none with your desktop; inside the agent's own session the same checks as the rung used there                                                                                                                                                                                                                   | as the rung used there                                                                                      |
| 4    | Windows `LockSetForegroundWindow(LSFW_LOCK)` for the burst; events in chunks of at most 8 with a foreground, target and pid check between chunks; abort on a foreground change (`EVENT_SYSTEM_FOREGROUND`, the `NSWorkspace` activation notification, `_NET_ACTIVE_WINDOW`), release held input, return `stale` | chunks of at most 8 key events with the same checks; text goes through rung 1 whenever the target allows it |

**Take-over by rung.** Rungs 1-3 never move your pointer or type into the window you use, so the agent keeps working
while you use other windows. Take-over (§6.7) stops a batch on rungs 1-2 only when your real input goes to the same
window the agent is acting in. On rung 4, **any** real click or key press stops it (owner decision 2026-11-13). Either
way the agent then stays paused until you say continue (§6.7).

**Rules that hold on every rung:** the sensitive set, the high-risk list (keys sent to a terminal by `PostMessage` are
still acting in a full-user-rights app), protected areas (§4.4), the indicator (for rungs 1-3 a quiet banner "agent working
in _app_"; rung 4 the full border), the log.

### 6.5 Browsers (lens M10)

- **The agents' browser, headless:** run by the **engine worker** as the **agent account** (rung 3), so a page it visits has
  the agent account's file rights, not yours, and its network (§3.5: the named services on your linked machines
  only, unless a goal opened more). Used for local pages and the agent's own test servers. CDP over a pipe only the
  engine worker holds
  (`--remote-debugging-pipe`), no local port.
- **No visible agents' browser** (r3 had one running as you with browser policy). Dropped: per-user browser policy also
  bound your own browser of the same brand (lens n3), and browsing on your screen is now done in your own browser.
- **CDP is never passed through:** the engine exposes typed verbs (navigate, read the page tree, element actions, the
  `Input.*` calls of rung 1, screenshot), not raw protocol.
- **Your own browser** (your logins, owner decision 2026-11-13): browsing as you happens here, through its
  accessibility tree or a bridge extension you allow (§6.6), which also offers typed verbs only. It is acting as you
  (§4.4). Its password manager pages, autofill and saved-login screens are in the sensitive set.

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
- **Inactive session (lens m3, n5):** acting in your session is refused while it is locked, disconnected, switched away
  or remote: rungs 1, 2 and 4, and rung 3 on a hidden desktop in your session (its apps run as you). Rung 3 in the
  agent account's own session or display (engine worker) goes on.
- **On-screen indicator.** While a batch acts, the tray icon changes and a small banner names the app, the step and the
  rung. On rung 4 a coloured border runs around the screen and the agent's pointer is drawn. Drawn by the helper; the
  engine stops without the helper's heartbeat (§6.1).
- **You take over at once.** The engine watches real input and tells it apart from its own (Windows: low-level hooks,
  `LLMHF_INJECTED` / `LLKHF_INJECTED`; macOS: an event tap, our events carry a source tag; X11: XInput2 raw events by
  device; Wayland: the compositor's idle and input signals, to verify per compositor). On take-over (rules per rung in
  §6.4): the running step stops, every key and button the engine holds is released, queued steps are dropped, and the
  batch returns `interrupted_by_user` with what ran and a fresh diff. When the engine cannot tell your input from its
  own on a desktop (a compositor without the signals), rung 4 is not offered there.
- **Paused after a take-over (owner decisions 2026-11-13: no idle timer; owner answers 2 and 3).** After a take-over,
  acting in your session (rungs 1, 2 and 4, and rung 3 on a hidden desktop in your session) **stays paused for the
  whole computer**, not only for the task that was interrupted (lens: another task or agent must not carry on clicking
  where you just took over). The pause stops **only on-screen acting**: commands, file tools and rung 3 in the agent
  account's own session go on (owner answer 2). Acting continues only when you press **Continue** in the tray or in T3,
  or when the agent asks again through `host_ask` (item "continue on your screen") and you press **Allow**; there is no
  automatic continue (owner answer 3). Both always need **OS consent** that the guard checks itself (§4.2), and are
  accepted only from a person's T3 UI session, never from an MCP or agent session (lens P5). The pause is written to
  the protected store, so a restart keeps it.
- **Clipboard (lens M4, n2).** Read is refused when the content carries a concealed or excluded marker (Windows
  `ExcludeClipboardContentFromMonitorProcessing` or `CanIncludeInClipboardHistory = 0`; macOS
  `org.nspasteboard.ConcealedType`; KDE `x-kde-passwordManagerHint`), and token shapes are redacted. A write counts as
  acting: logged, indicator on, and the previous content is **put back when the batch ends**. A step may mark its
  result "for you" (left on the clipboard); then the tray shows the kept content in a notification, so you see what you
  would paste. Your previous clipboard is journalled for the restore (§15); content with a concealed marker is
  journalled only encrypted for you (DPAPI, the keychain, the Secret Service) and deleted after the restore.
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
| browser work                     | the agents' browser always runs headless as the agent account; visible browsing uses your own browser (§6.5)                                                                                                                                                                | same                                                                                                                                                                  | same                                                                                                                                       |
| rungs 1-3                        | UIA patterns and window messages work on windows that are not in front; the engine does not call `SetForegroundWindow`; rung 3 windows are in the agent's own session or on a hidden desktop you do not see                                                                 | AX actions without `AXRaise`; `CGEventPostToPid` does not activate                                                                                                    | AT-SPI actions without focus; rung 3 on its own display                                                                                    |

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

**Key tombstones (correctness review).** Journal entries are kept 24 hours (§15), but each key leaves a **tombstone**
(the key and a hash of its step list) for 30 days (setting, always longer than any retry window). A call with a known
key and a **different** step list is refused (`key_mismatch`), never answered with the old result. A call with a key
whose entry is gone but whose tombstone remains is refused (`key_expired`) instead of running the batch again.

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
- macOS: a sandbox profile with file-write limited to the private layer, network denied, file-read denied on every
  secret-pattern path, and no Mach or Unix-socket IPC to the engine, the broker or T3; each run under a per-slot hidden
  user from a small pool, so cleanup ends every process of that user (process groups do not hold `setsid` daemons).
  The slot users are in the agent group (§3), so every deny entry of the agent account holds for them too (lens N6).
- Windows: one AppContainer **per alternative**, with no capabilities. Read access to the real folders is granted to
  one fixed **fork-owned capability SID** that every speculative AppContainer carries (never `ALL APPLICATION
  PACKAGES`, which would open your folders to every packaged app; lens p10); write access to a private layer is
  granted only to that alternative's own AppContainer SID, so one alternative cannot write another's layer
  (correctness review). The engine's and broker's pipes carry no AppContainer ACE, so they refuse it. The read ACEs
  are applied with the §3.4 walk and are on the uninstall removal list (§15; lens n8).
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
  agent applies one (`host_do { apply: <alternative> }`) or 10 minutes pass. An `apply` on a layer that is gone
  (expired, or deleted by a restart, §12.3) returns `layer_gone` and changes nothing.
- The result says which alternatives ran, which won, why each loser lost (`failed`, `check_false`, `cancelled`,
  `timeout`), and the time each took.

**Cancelling and cleaning up losers.** As soon as a winner is known (`first`) or the agent applies one (`all`): each
loser's processes are ended (job object / cgroup / per-slot user: a polite stop, then a hard kill after 2 s); its
private layer is deleted; its waits, watchers and cached trees are dropped; it is logged as `cancelled_speculative`.

**Applying the winner (lens N6).** Read-only winners just return their results. A winner's changes are applied to the
real folders as one write burst **by the agent account**, through the broker, so every OS deny entry holds: a layer
that contains a `.git/hooks/post-checkout`, a new `.vscode/tasks.json` or a file named `.env` fails to apply there just
as a direct write would. The applier also refuses, before it writes anything, every path that matches a secret pattern
or an auto-run path, so a refused apply never leaves half a layer. The burst: snapshot first (§4.3 rule 6), then each
file compare-and-swapped against the hash it had when the layer was made, through the link-safe operations of §3.4.
If any real file changed meanwhile, nothing is applied and the result is `conflict` with the list. Every file is
written from a pre-image journalled first; if the burst stops part-way (a crash, or a re-apply after a crash that
meets a file you changed in the meantime), the files already written are **rolled back from their pre-images** and
the result is `conflict` with undo offered, so a winner is never left half applied (lens p7).

**Overlay caveat (Linux).** overlayfs does not define what a reader sees when the lower (real) folders change while
the overlay is mounted. Where the filesystem has snapshots (btrfs, ZFS, LVM-thin) the lower layer is a read-only
snapshot taken at the start; elsewhere a run that reads a file you are editing may see torn data. The apply's
compare-and-swap still catches the final conflict (§14).

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

Eight tools (plus the file group). The file and run tools exist so agents on another machine need no full agent on the
computer.

### 9.1 Pop-ups: only the batched ask

The batched ask (§4.2) is the only dialog. Two of its item kinds carry a little more:

1. **Installs.** The item names a package (winget / Homebrew / the distro's package manager), its source and size, and
   for whom. On Allow the helper installs as **you**; Windows UAC or the OS password prompt follows when it is
   machine-wide. Installing tools for the agent account only (into its own folders) needs no ask.
2. **Admin commands and other protected-area actions** (§4.4). The item shows the exact command, path or key and the
   content or diff. The helper copies every file the command references into a protected folder and shows its hash or
   content; on Allow the OS's own elevation prompt follows, the guard checks it (§4.2) and the **copy** runs, in a
   protected working folder, so nothing can be swapped between the ask and the run (lens B1). Each Allow covers that
   one action once.

**Named commands and approved scripts** are entries in the protected store, run by the helper as you, with output
scrubbed of every secret value: things that need **your** secrets (a deploy with your SSH key) or your session. They
need no ask because you wrote or approved them, and they run from a frozen copy (§4.3 rule 4): a deploy from a repo
runs at the commit you approved; a new commit is one ask showing the commit and the diff since the last approved one.

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

**Owner decision (2026-11-13): links expire after 365 days and extend with one click; no refresh tokens.** You trust
your own computers; the goal is to keep **outside people** out, not to add friction between your machines.

1. **Lifetime setting.** `peerLinkSessionDays` on the computer, for the MCP-client sessions it issues to peer links:
   default **365 days**; a shorter or longer number of days is allowed, "never expires" is not. It replaces the fixed
   constant at `EnvironmentAuth.ts:79` for peer-link sessions only, and the issued `expires_in` follows it, so the
   linking side's own expiry check keeps working. **Lowering** the setting also cuts existing links to the new
   lifetime, counted from their last extension (lens p8). Written as a small change upstream could take for #33183.
2. **Extending is one click, no pairing code**, from the tray or from any linked machine. On the computer, the tray's
   **Extend links** sets every link to now + the lifetime, including links that expired in the last 30 days (older
   ones need pairing again). From any of your already-linked machines, one click in Sharing & access (or `t3` link
   extend for a named computer) asks over its live link; the computer extends that link, and, when the caller's link
   has write access, any other link you name. The extension happens on the computer, in the same transaction that
   checks revocation, so a revoke always wins over an extension in flight. **Only device-bound links extend:** a link
   with no recorded device (made before device binding) is not extended and is not given host tools; the tray asks you
   to pair it again (a stolen unbound token can therefore not bind itself to a new device). The tray and Sharing &
   access show, for each link, when and **from which device** it was last extended (lens p2).
   **The linking side learns of the extension (correctness review).** Upstream stores the expiry once at sign-in and
   refuses its own calls after it. So every reply from the computer carries the link's current expiry, and the linking
   side updates its stored `expiresAt` from it; and when a link looks expired locally, `usableLink` asks the computer
   once (a small status call on that link) before dropping it, so a link extended in the tray works again without
   pairing. Both are fork changes to `PeerLinks.ts` and `PeerLinkRequests.ts` (§2).
3. **Heads-up** 7 days before any link expires: tray on both machines, `host_status`, and a T3 notification on the
   agent machine, each with the extend action.

**Strong against outsiders:**

- **Device binding.** At pairing the linking side makes a key pair, kept in its secret store, and **every** request is
  signed with it (proof of possession, RFC 9449 DPoP style), on every route. When a call arrives over the tailnet, the
  computer also checks the caller's Tailscale node identity recorded at pairing (it asks its local Tailscale who the
  connection comes from, the idea of #26757; asking `whois` from the host account, which has no interactive session,
  is to verify on Windows). The node check is only an extra check, so the same link used over the LAN route is not a
  false alarm (correctness review). A token without a valid signature, or over the tailnet from another node, is
  refused, the link is marked suspect, and the tray alerts "a link was used from an unknown device", with Revoke.
- **No new devices without you.** Pairing codes stay upstream's short-lived single-use codes; a new device is always a
  pairing you start, and the tray shows each new link.
- **Visible use.** The computer tracks each link's last use and shows it in the tray and Sharing & access; each
  extension and each refused device is logged.
- **Revoke all** in the tray ends every link to and from the computer at once.

**Light between your own machines:** no per-call confirmation, no token rotation, no re-pairing to extend.

**Residual risk:** an infected machine of yours can use its links to your other machines until you press the kill
switch or **revoke all** on them. A **lost or stolen** device can extend its own links from itself, so expiry never
ends its access; only revoking it does (lens p2).

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

**Isolation of runner accounts on a personal PC (lens N10).** A CI job runs repository code, so runner slot accounts get
at least the agent account's walls, checked by the install verify (§3.3) and daily:

- **Accounts:** one standard account per slot, in a runner group that is separate from the agent group; no admin, no
  remote or local logon, no session of yours (no window station, display or session bus; §6.8's `host_run` row).
- **Files:** no reach map at all; each slot works in its own folder. The reach check (§3.3) runs as each slot account
  and must find nothing of yours readable; the secret deny pass applies to the runner group too.
- **Network:** the always-denied floor of §3.5 (loopback except its own, LAN, tailnet, link-local, the computer's own
  addresses, on-link prefixes). Unlike the agent account, a runner has the public internet, since a CI job needs it.
- **Disk:** a quota per slot account, and the cgroup or job-object caps of this section.
- **Persistence:** the agents' own-persistence denies of §3.1 apply to runner accounts too; slots are wiped after each
  job (the fleet runners' one-job rule, #13).
- **Who may trigger:** on a personal PC, only jobs started by you (your pushes, your PRs, schedules of your repos) run;
  jobs from other people's pull requests go to the server pool or GitHub-hosted runners. The **dispatcher on the PC
  checks each job's event and actor itself** (from the job's signed context, against the repos and accounts in your
  policy) before it runs it; routing on the server is not trusted for this (lens p11).

## 12. Tray icon, wind down, kill switch and power loss

One tray icon per computer, from the session helper, covering host tools and runner work: in the Windows notification
area (first run tells you how to pin it), the macOS menu bar, and the Linux status area (StatusNotifierItem /
AppIndicator; GNOME needs the AppIndicator extension, to verify). Built on Electron's `Tray`; upstream's desktop app has
no tray today.

### 12.1 The menu

- **Status:** host tools (idle, working, acting in _app_ on rung _n_, paused, winding down, safe to shut down), the
  calling project and thread while acting, link expiry and last use, runners (jobs running, budget and why).
- **Pause / Resume**, and **Continue** after a take-over (§6.7, needs OS consent; the take-over pause covers the whole
  computer).
- **Wind down** and **Shut down when safe** (wind down, then the OS shutdown).
- **Kill switch** (asks once: "stop every agent process on this computer now?"), and **Revoke all links**.
- **What the agent wrote and ran** (§4.3 rule 7), **held changes** to protected areas and quarantined auto-run paths
  (Keep / Remove), agent-caused processes running as you (with End).
- **Extend links** (§10), with when and from which device each link was last extended; grants in force and remembered
  answers (with Revoke), runner override (§11), open log, open policy, recipes, bridges waiting, install-verify warnings
  (§3.3, including a UAC level below "Always notify"), network openings in force (§3.5).
- **Undo store** (§4.3 rule 6): space used against your disk budget, the 80 % warning, held or quarantined items older
  than 24 hours, copies pruned early, and the last-resort refusal when it is on and the store is full.

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
   "stay paused after restart" is ticked). At that sign-in the guard clears `winding_down` in the protected store
   **and** in the T3 server's and runners' state in one step, so no part keeps refusing work (correctness review).

The **kill switch** stays the immediate stop (§4.3 rule 8).

### 12.3 Power loss and hard reset (owner decision 2026-11-13)

The computer may be switched off at any moment. Every part writes its state so that the next start recovers:

- **Atomic and durable writes:** the protected store, recipes, journals, ledgers and state files are written to a temp
  file, fsynced, then renamed; logs are fsynced per line. **Narrowing first (lens N8):** revoke, Deny, pause, the kill
  switch and narrowing the reach map are durable (store and a narrowing record in the guard's log, both fsynced)
  before the tray confirms them; a power cut can lose a narrowing only if the tray never said it was done.
- **No fallback that widens (lens N8):** when the store's signature fails, the guard does not simply load the last
  good version, which could hold grants you revoked since, an auto-run unlock you withdrew or no pause. It loads that
  version and **replays every narrowing record in its log since that version**. If it cannot (the log is damaged too),
  it starts with **no grants beyond the built-in defaults and with pause on**, and the tray says so.
- **On the next start, in order:** the guard checks the protected store's signature (with the fallback above) and the
  log tails (a half line gets a `truncated` marker); it removes leftover persistence locks from its ledger (§4.4); the
  helper runs the full deny pass and the
  install verify before host tools come back (§3.3); the engine drops held-input files from before the boot (the OS
  reset input) and marks journalled started-but-unconfirmed steps `unknown`; speculative layers listed in the journal
  are deleted; snapshots the OS did not finish are discarded; the runner dispatcher re-asks for every job in its ledger
  and for jobs that were running on this computer (GitHub reports them failed with a lost runner); `winding_down` from
  before the boot is cleared; "this task" grants are ended; a take-over pause stays (§6.7).
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

| Threat                                                 | What stops it                                                                                                                                                                                                                                                                                                                                                                | What is left                                                                                                                                                                                                                                                                                                                          |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Prompt injection from files, screens, bridges          | the sensitive set, the high-risk ask, protected areas, untrusted marking, project grants and outside-input threads, indicator, take-over, the activity log, undo, the kill switch                                                                                                                                                                                            | **in any app it acts as you** (§4.4): it can send a message, and through a file dialog, a link or a launcher run any file as you, including one it wrote; that code can do what your account can outside the protected areas                                                                                                          |
| Agent-caused code persists after the task (N1, N4)     | protected areas held per action (OS and deny entries for system areas, including user-writable spots; locked while acting with leases, watched always and diffed per grant for yours); a held entry's processes ended; marks from Task Scheduler, WMI, COM, `systemd --user` and `launchd` starts during a window; marks journalled; kill switch ends agent-caused processes | a persistence place not on the built-in list, a changed program in a writable folder, or a change made after the lock window through a process that escaped the agent-caused mark: shown only in the activity log, not ended by the kill switch                                                                                       |
| Files the agent wrote run as you (B1, N1)              | approved runs use frozen copies and protected working folders; bridge hashes; auto-run paths denied, renames blocked, new ones quarantined; the activity log flags every run of an agent-written file                                                                                                                                                                        | **open by owner decision:** you or the agent opening such a file through an app's UI runs it, with no OS warning; IDE build sync can run an agent-edited build file (N3d)                                                                                                                                                             |
| Planted links redirect a privileged write (B2)         | link-safe operations (§3.4): no following, link count 1, final-path check, walks stop at links; the guard's own copy walker on Linux                                                                                                                                                                                                                                         |                                                                                                                                                                                                                                                                                                                                       |
| The agent widens its own policy (B3, N4)               | protected store owned by the guard, unreadable to the agent group; consent for full rights, elevation and protected areas checked by the guard itself; widening needs that consent while agent-caused code runs as you                                                                                                                                                       | a forged ordinary answer while agent-caused code escaped the mark; it grants only what your account already has, and is listed in the tray                                                                                                                                                                                            |
| A command takes over T3 on the computer (M1, N2)       | separate host and agent accounts; admin-owned install; engine admits only the host account; no outbound link; forwarded-tool allowlist on the computer; T3 terminal and provider features off there                                                                                                                                                                          | delegated full agents stay off until they run through the broker                                                                                                                                                                                                                                                                      |
| Speculative run escapes (M2, N6)                       | OS-enforced sandbox; no network or local IPC; layers made by the agent account; slot users in the agent group; one AppContainer per alternative with a fork capability SID for reads; winner applied by the agent account, secret and auto-run paths refused, rolled back from pre-images if it stops part-way                                                               | Linux overlayfs: a run may read torn data while you edit a file it reads, where no snapshot mounts the lower layer (the apply still catches the conflict)                                                                                                                                                                             |
| Agent reaches the network (M3, N5)                     | no direct internet by default; only the services named in config on your linked machines (default none); per-goal ask, named hosts pinned by the guard; an always-denied floor including the computer's own addresses and on-link prefixes; lookups only through the guard's resolver; openings ledger cleared at start, pause and kill; probed at install                   | after you open the internet for a goal: anything the agent account can read can leave during that goal; on Windows and macOS, names may still leak through the system resolver (§3.5)                                                                                                                                                 |
| Secrets via clipboard, logs, outputs (M4)              | concealed markers, restored clipboard, "for you" content shown, concealed journal copies encrypted, redaction on every path and record                                                                                                                                                                                                                                       | a secret in an unknown format                                                                                                                                                                                                                                                                                                         |
| Typing lands in the wrong window (M5)                  | the input ladder (rungs 1-3 need no focus); rung 4 focus lock, chunk checks, abort; any real input stops rung 4 and pauses                                                                                                                                                                                                                                                   | an app that changes its own focus inside a chunk of 8 events                                                                                                                                                                                                                                                                          |
| An untrusted thread calls host tools (M6)              | per-project grants (new projects off), outside-input threads read-only, caller shown and logged                                                                                                                                                                                                                                                                              | a thread in a granted project that reads outside text without the flag                                                                                                                                                                                                                                                                |
| Stolen link (M7)                                       | device binding (DPoP key on every call, tailnet node as an extra check), only bound links extend or get host tools, unknown-device alert, revoke all, last use and "extended by" shown, 365-day expiry, lowering the lifetime cuts existing links                                                                                                                            | an infected own machine can use its links until kill switch / revoke all; a lost device can extend its own links, so only revoke ends it                                                                                                                                                                                              |
| State fails open (M8, N8)                              | fail-closed rows in §15; install verify gates host tools; heartbeat; narrowing durable before confirmed; fallback replays narrowing or starts paused; uninstall rows                                                                                                                                                                                                         |                                                                                                                                                                                                                                                                                                                                       |
| Agent undo wipes your work (M9)                        | writer recorded; agent undo restores only agent changes with matching hash                                                                                                                                                                                                                                                                                                   | changes whose writer the OS cannot tell count as yours and are not undone by the agent                                                                                                                                                                                                                                                |
| Browser with your rights (M10)                         | the agents' browser headless as the agent account, typed CDP verbs only; no visible agents' browser                                                                                                                                                                                                                                                                          | browsing in your own browser acts with your logins (§4.4)                                                                                                                                                                                                                                                                             |
| Input below the OS's checks (N7)                       | no RDP session for the agent account; no `uinput`                                                                                                                                                                                                                                                                                                                            |                                                                                                                                                                                                                                                                                                                                       |
| Hostile CI job on your PC (N10)                        | runner accounts: own group, no reach map, network floor, quota, no session, only your own jobs                                                                                                                                                                                                                                                                               | what the public internet gives a job you started                                                                                                                                                                                                                                                                                      |
| Elevation without a prompt                             | setup sets UAC to "Always notify" with the secure desktop (owner answer 4), sudo timestamps cleared, no `NOPASSWD`; on Linux and macOS consent through `AuthorizationExternalForm` / polkit checked by the guard                                                                                                                                                             | if you lower UAC and use an administrator account, known auto-elevation tricks defeat the guard-checked consent, the protected store and every protected area (warned); root inside your WSL distros (`wsl -u root`); on Linux with X11, code running as you can read the password you type into the polkit agent (Wayland closes it) |
| Runs in protected areas (owner answer 1)               | running an agent-placed file, anything elevated, or anything that registers persistence needs your OK per action; blocked before start by Endpoint Security and fanotify                                                                                                                                                                                                     | on Windows in v1 the run is ended at its process-start event, not blocked before it (driver only if measurements show the need)                                                                                                                                                                                                       |
| Take-over is ignored                                   | the pause covers on-screen acting on the whole computer; Continue and a renewed ask need OS consent from a person's T3 UI session                                                                                                                                                                                                                                            | none known beyond the consent prompt itself                                                                                                                                                                                                                                                                                           |
| Reading your other files                               | reach map, sensitive set, reach check                                                                                                                                                                                                                                                                                                                                        | folders every user may read that you chose to keep; system areas (readable by design)                                                                                                                                                                                                                                                 |
| Secrets inside the reach map and readable system areas | built-in patterns, deny entries, fail-closed deny pass over the reach map and the readable system areas the reach check finds, watcher                                                                                                                                                                                                                                       | a secret in an unknown format or ordinary file name; about one second after you create a matching file                                                                                                                                                                                                                                |
| Damage to your folders                                 | snapshots and pre-images in an undo store sized from measured need within your disk budget, pruning only accepted goals, 80 % warning, last-resort refusal; writer log; `host_undo`                                                                                                                                                                                          | changes made by commands between snapshots; on Linux without btrfs, ZFS or LVM-thin, no undo of command changes (only T3's own file tools keep pre-images); macOS snapshot limits (§4.3 rule 6); changes inside apps                                                                                                                  |

**What still leaks, plainly:** files in the reach map, readable system areas, and what the apps the agent uses show
(trees, values, screenshots) reach the model provider through the agent machine, and the internet when you open it for
a goal. Process names and redacted window titles of all apps. Data read goes into transcripts. **Anything your account
can read can leave once agent-caused code runs as you**, because that code has your network, not the agent account's.

### 14.1 Owner scope vs lens

Where a lens fix would contradict the owner's scope (A: goal-driven, whole computer except sensitive things, one
batched ask), A is kept and the hole is closed another way, or the owner accepts it and it is said openly.

| Finding  | Lens default not taken                                                                                                                              | Why                                                                                                        | What closes the hole instead                                                                                                                                                                                                                                                                                                                                                                                              | Residual risk                                                                                                                                                             |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| N1       | file dialogs limited to typing a path; full-rights class by ability with an exec backstop; Mark-of-the-Web / quarantine flag on agent-written files | owner decision 2026-11-13: option A only, acting in the UI is acting as you, no marking or ask-before-open | said openly (§4.4); protected areas per action, OS-enforced where possible; the activity log of every file written and run; launchers and IDE terminals on the full-rights list; kill switch ends agent-caused processes; held entries' processes ended; Task Scheduler, WMI, COM, `systemd --user` and `launchd` starts during a window marked; user-writable system spots denied to the agent group and watched for you | agent-caused code runs as you and can do what your account can outside the protected areas, until the kill switch, and a process that escaped the mark is not ended by it |
| B3c      | full-user-rights apps never get acting from a default                                                                                               | A: one batched ask, not per-app acknowledgements in the tray                                               | high-risk list; OS consent the guard checks itself (owner kept it); scope "this task"; widening needs that consent while agent-caused code runs as you; persistence baseline diff per grant                                                                                                                                                                                                                               | during a grant the agent has your user rights, including your profile's secrets; after it, whatever escaped the persistence watch (§4.4 "What is left")                   |
| m5       | unknown apps hidden, opt-in per app                                                                                                                 | A: whole computer by default                                                                               | sensitive set refused, relevance-scoped observation, redacted titles, untrusted marking                                                                                                                                                                                                                                                                                                                                   | what a used app shows (mail, chat) reaches the provider                                                                                                                   |
| B1a      | open files only in apps named in the policy                                                                                                         | A: no per-app list                                                                                         | macros forced off in the engine's own automation; protected working folders; the activity log                                                                                                                                                                                                                                                                                                                             | a data file that exploits an app's parser; any file run through the UI (N1)                                                                                               |
| r2 §6.5  | acting and synthetic input off until enabled                                                                                                        | A and the owner's input decision                                                                           | the input ladder: real input only last, `visible: true`, indicator, take-over that pauses until you continue                                                                                                                                                                                                                                                                                                              | rung 4 still uses your mouse when nothing else works                                                                                                                      |
| M4       | clipboard read and write off by default                                                                                                             | A: the clipboard is part of the computer                                                                   | concealed markers refused, redaction, writes are acting and put back after the batch                                                                                                                                                                                                                                                                                                                                      | a password copied from a plain app without markers is readable                                                                                                            |
| M6       | host tools off for every thread until granted                                                                                                       | owner: your own machines and projects are trusted, no friction between them                                | one toggle per project, off for new projects; outside-input threads read-only; caller logged and shown                                                                                                                                                                                                                                                                                                                    | a thread in a granted project reading outside text without the flag                                                                                                       |
| M7       | refresh-token rotation, reuse detection, absolute lifetime                                                                                          | owner chose 365 days with one-click extension                                                              | device binding, only bound links extend, unknown-device alert, last use shown, revoke all                                                                                                                                                                                                                                                                                                                                 | an infected own machine can use its links until kill switch / revoke all                                                                                                  |
| B1e, N3d | package manifests, build files and other code read-only to the agent; or IDE auto-sync required off                                                 | agent work is code; A                                                                                      | only auto-run paths are denied, with rename blocks and quarantine; IDE sync settings warned at install; agent writes to build files flagged in the activity log                                                                                                                                                                                                                                                           | an IDE that syncs build files on change runs an agent edit as you with no click; code you choose to run (a test) may contain agent changes                                |
| m4       | bridge verbs allowed one at a time                                                                                                                  | A: one batched ask                                                                                         | the ask lists every verb with its own tick box; changed bridge files go back to waiting                                                                                                                                                                                                                                                                                                                                   | "Allow" with all boxes ticked grants every verb                                                                                                                           |

**Owner decisions changed by a blocker:** B3 (round 2) added the OS's own consent to ask items that grant full user
rights or elevation; the owner kept it in round 3 and it now also covers protected-area actions, checked by the guard
itself (N4). Blocker N1 (round 3) did not change an owner decision: the owner chose to keep A and say it openly, and
added the protected areas and the activity log. Everything else from earlier rounds stays (speed first, invisible by
default, speculative batches, tray and wind down, runner-side activity, goal-driven scope, the batched ask, the input
ladder), changed only where the owner changed it (no direct internet for the agent account, take-over pause with no
idle timer, 365-day links, default folders, no visible agents' browser).

## 15. Failure table (state the feature changes)

What happens when it runs again, stops half-way, or meets old or empty state:

| Step that writes                         | Crash or stop half-way                                                                                                                                                                                                                                                                                                                                                                | Run again                                                                                                                                                                       | Old / empty / half-written state                                                                                                                                                                                                                                                                                        | Test                                                 |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| install: create both accounts            | one or both exist, nothing else                                                                                                                                                                                                                                                                                                                                                       | finds them, keeps them, re-checks groups (refuses if either is an admin)                                                                                                        | an account of that name that is an admin or in a remote group: refused                                                                                                                                                                                                                                                  | fake OS layer                                        |
| install: reach map grants                | some folders granted                                                                                                                                                                                                                                                                                                                                                                  | re-applies all through link-safe walks (idempotent); removes grants for folders no longer listed                                                                                | a folder deleted since: reported, skipped; a planted link in a folder: not followed, reported                                                                                                                                                                                                                           | fake ACL layer + link fixture                        |
| install: services, broker, guard         | some installed, not started                                                                                                                                                                                                                                                                                                                                                           | replaced with the same definitions                                                                                                                                              | an upstream "as me" task for the same T3 home: refused with the reason                                                                                                                                                                                                                                                  | fake OS layer                                        |
| **install verify half-applied**          | host tools stay off; `host_status` names the failed check                                                                                                                                                                                                                                                                                                                             | re-runs every check; tools come on only when all pass                                                                                                                           | a later change (account added to a group, rule deleted): next quick or daily check turns tools off                                                                                                                                                                                                                      | verify fakes per check                               |
| **secret deny pass**                     | file tools stay off until a full pass completes                                                                                                                                                                                                                                                                                                                                       | a full pass is idempotent                                                                                                                                                       | files created while the helper was down or during a watcher overflow: covered by the pass on start / after overflow; reads refused while the pass is stale                                                                                                                                                              | fake FS + clock                                      |
| reach map: folder removed                | grants removed for some files                                                                                                                                                                                                                                                                                                                                                         | removal finishes; agent processes with handles under it are ended first                                                                                                         | a process that held a handle across the change: ended, logged                                                                                                                                                                                                                                                           | handle fixture                                       |
| ask answer (guard)                       | answer written atomically or not at all; not written = denied                                                                                                                                                                                                                                                                                                                         | the same answer again is a no-op                                                                                                                                                | a "this task" grant from before a restart: ended; an unanswered ask after a restart: denied                                                                                                                                                                                                                             | fake guard                                           |
| **protected-area change held** (§4.4)    | blocked, or reverted into the undo store with a ledger line written first; the hold is in the store                                                                                                                                                                                                                                                                                   | Keep or Remove twice: the second is a no-op; a revert interrupted by a crash is finished on start from the ledger                                                               | a held change whose entry you since changed by hand: shown with both versions, nothing applied without you; nothing held: no tray item                                                                                                                                                                                  | fake watcher per OS                                  |
| **persistence lock window** (§4.4)       | the guard writes each lock (object, old owner, old ACL, lease count) to its ledger before applying it                                                                                                                                                                                                                                                                                 | a second window on a locked object adds a lease; ending a window drops its lease and restores only when the count reaches zero (lens p6)                                        | locks left by a crash or power cut: all leases dropped, restored to the recorded owner and ACL on the next start; an object you deleted meanwhile: ledger line dropped                                                                                                                                                  | fake ACL layer + power cut + two overlapping windows |
| **auto-run path quarantined**            | the move into the undo store is link-safe and journalled; the tray item appears after the move                                                                                                                                                                                                                                                                                        | Keep restores it with your denies applied; repeated Keep or Remove: no-op                                                                                                       | a quarantined path whose folder is gone: Keep refused with the reason; "make it mine" on a repo whose `.git` you already own: no-op                                                                                                                                                                                     | fake FS + writer fixture                             |
| **writable-spot deny entries** (§4.4)    | each deny entry is journalled before it is set; the verify re-checks all                                                                                                                                                                                                                                                                                                              | setting an entry that is already there: no-op                                                                                                                                   | a new user-writable spot (an installer since): found by the next reach check, denied, logged; host tools stay off until it is denied                                                                                                                                                                                    | fake ACL layer + reach fixture                       |
| **held item older than 24 h**            | the item stays in the reserved area of the undo store; never pruned                                                                                                                                                                                                                                                                                                                   | reminders in the tray and the next ask, not repeated more than daily                                                                                                            | store full: the hold stays, the last-resort refusal applies to new agent writes; you remove or keep it                                                                                                                                                                                                                  | fake clock                                           |
| **marks and baselines**                  | each mark (process id + start time) and each grant baseline is journalled before it counts                                                                                                                                                                                                                                                                                            | re-attaching to a marked process twice: no-op                                                                                                                                   | after a guard restart: still-running marked processes re-attached; a reused process id with another start time is not marked; a grant with no end: diffed against its baseline, or against everything newer than its start if the baseline is missing                                                                   | fake process events + restart                        |
| **forwarded tool outside the allowlist** | refused on the computer before anything runs; logged                                                                                                                                                                                                                                                                                                                                  | refused again                                                                                                                                                                   | an older agent machine without the allowlist: still refused on the computer                                                                                                                                                                                                                                             | toolkit test                                         |
| **network floor on address change**      | the agent group's network is closed while the rule update is pending                                                                                                                                                                                                                                                                                                                  | the update is idempotent                                                                                                                                                        | a new global IPv6 address or on-link prefix: in the floor before the next agent connection                                                                                                                                                                                                                              | fake interface events                                |
| **network openings**                     | each opening (goal, service or host, address set, WFP dynamic-session filter or nft set) is written to the ledger before it is applied                                                                                                                                                                                                                                                | opening the same thing again: no-op                                                                                                                                             | after a crash or power cut: every opening in the ledger and every leftover filter is removed at start (dynamic WFP filters vanish with the guard's session anyway); pause and kill remove them too                                                                                                                      | fake WFP / nft layer + crash                         |
| protected store edit                     | temp + atomic rename by the guard                                                                                                                                                                                                                                                                                                                                                     | new store or old one, never half                                                                                                                                                | edited outside the guard or damaged (signature fails): the last good version plus every narrowing record from the guard's log since it; if those cannot be replayed, built-in defaults with pause on and a tray note; you confirm an outside edit with OS consent; unreadable: all write and acting tools refused       | signature test + narrowing replay                    |
| `host_write`, `host_delete`              | temp beside target, target unchanged; pre-image saved first                                                                                                                                                                                                                                                                                                                           | same content: OK; other content: compare-and-swap refuses                                                                                                                       | target changed since read: refused; pre-images that would not fit the undo store's cap: refused with the size                                                                                                                                                                                                           | fake FS                                              |
| **snapshot**                             | snapshot taken or not; log line says which; an unfinished OS snapshot is discarded on start                                                                                                                                                                                                                                                                                           | a request within 30 minutes of the last reuses it; wind-down and winner-apply snapshots reuse one younger than 30 minutes the same way                                          | store at 80 % of the disk budget: tray warning; full: first prune copies of accepted or finished goals outside the undo window, then grow within the budget; still full: agent writes and runs **refused** (last resort, on by default), tray red with the reason; held and quarantined items are never pruned          | fake snapshot store + fake budget                    |
| `host_undo` (agent)                      | some files restored, each through link-safe writes                                                                                                                                                                                                                                                                                                                                    | re-run restores the rest; already-restored files compare equal                                                                                                                  | a file you changed since: listed, not restored; writer unknown: treated as yours; snapshot expired: refused with the oldest time                                                                                                                                                                                        | writer fixture                                       |
| log line                                 | line written before the action and after; a call whose log write fails is refused                                                                                                                                                                                                                                                                                                     | the guard's countersign continues from the last good head                                                                                                                       | half line at the end: `truncated` marker, chain continues; a T3 log head that differs from the countersigned one: tray alert                                                                                                                                                                                            | log fixture                                          |
| `host_do` batch                          | journal (fsynced) says which steps are done; started-but-unconfirmed is `unknown`                                                                                                                                                                                                                                                                                                     | same key: journalled results, no step re-runs; a new key is a new batch                                                                                                         | journal from before a restart: kept 24 h for retries, then dropped; empty step list: `ok`, nothing run                                                                                                                                                                                                                  | fake engine back end                                 |
| **journal tombstones** (§7.2)            | the tombstone (key + step-list hash) is written with the first journal entry                                                                                                                                                                                                                                                                                                          | same key, same steps: journalled result; different steps: `key_mismatch`; entry gone, tombstone kept: `key_expired`                                                             | tombstones older than 30 days: dropped; a damaged tombstone file: every unknown key refused until rebuilt from the log                                                                                                                                                                                                  | fake clock + journal                                 |
| **input rung step-down**                 | the rung tried and its result are journalled before the next rung is used                                                                                                                                                                                                                                                                                                             | same key: no rung is re-tried; an unsafe-to-repeat action with an unclear check returns `unknown`, never a second apply                                                         | rung memory from an older app version: tried first, steps down once, memory updated                                                                                                                                                                                                                                     | fake app per rung                                    |
| **focus change mid-burst (rung 4)**      | burst aborted between chunks, held input released, `stale` with the chunks sent                                                                                                                                                                                                                                                                                                       | the agent re-plans from a fresh diff; never resumes the burst by itself                                                                                                         | the target window gone: `stale`                                                                                                                                                                                                                                                                                         | focus-steal test per OS                              |
| **you grab the mouse/keyboard mid-step** | step stops, all held keys and buttons released, queued steps dropped, `interrupted_by_user` with what ran and a diff; the take-over pause is written to the store                                                                                                                                                                                                                     | acting in your session stays paused until you press Continue or allow the agent's ask; no idle timer                                                                            | a take-over during a `wait`: the wait ends as interrupted too; a pause from before a restart: kept                                                                                                                                                                                                                      | input hook fake                                      |
| **helper dies, engine lives**            | engine stops acting within 500 ms of the missed heartbeat and releases held input                                                                                                                                                                                                                                                                                                     | acting resumes only when the helper is back and the indicator is up                                                                                                             | an engine from before a helper restart: the new helper stops it and starts a fresh one                                                                                                                                                                                                                                  | kill test per OS                                     |
| **engine dies mid-drag**                 | the engine writes every held key/button to a held-input file before sending it; the helper (or the next start) sends the ups, then deletes the file                                                                                                                                                                                                                                   | after restart the batch is `unknown` from the journal and is not re-sent                                                                                                        | a held-input file from before a reboot: dropped (the OS reset input)                                                                                                                                                                                                                                                    | kill test per OS                                     |
| **clipboard write**                      | the previous content is saved in the journal before the write (encrypted for you when it carries a concealed marker)                                                                                                                                                                                                                                                                  | after a crash the next start puts the saved content back if the clipboard still holds the agent's value, then deletes the saved copy                                            | the clipboard changed by you since: left alone, saved copy deleted                                                                                                                                                                                                                                                      | fake clipboard                                       |
| **speculative batch**                    | the journal lists each private layer **before** it is made; on restart every listed layer is deleted and its per-slot processes ended; a winner whose apply did not finish is re-applied file by file by the agent account (compare-and-swap makes done files a no-op); if that meets a file you changed meanwhile, the written files are rolled back from their pre-images (lens p7) | same key: the journalled winner and outcome, nothing re-runs; a key whose alternatives never finished: `unknown`, layers cleaned; `apply` on a layer that is gone: `layer_gone` | layers with no journal entry: left alone and reported; a real file changed during the run: `conflict`, nothing applied; a layer with a secret or auto-run path: refused before any write; zero alternatives: refused                                                                                                    | fake engine + fake FS                                |
| **window pops in front** (§6.8)          | the engine hides it and gives the front back; if the engine dies first, the window stays and the next start logs it                                                                                                                                                                                                                                                                   | the same program is started the other way the result told the agent; a repeat offender is stopped                                                                               | a recipe that now shows a window: `visible_side_effect`, recipe marked `needs repair`                                                                                                                                                                                                                                   | no-pop-in test                                       |
| recipe save                              | temp + atomic rename into the store                                                                                                                                                                                                                                                                                                                                                   | same content: no-op (content hash); new content: new version                                                                                                                    | a recipe for an older app version: runs, and a failing step marks it `needs repair`; empty store: fine                                                                                                                                                                                                                  | fake guard                                           |
| pause / kill switch / revoke all         | each is fsynced in the store and as a narrowing record in the guard's log **before** the tray confirms it; accounts disabled; links revoked one by one, each committed                                                                                                                                                                                                                | idempotent; a revoke-all re-run revokes what is left                                                                                                                            | kill switch with no agent processes: OK; revoke-all with no links: OK; a narrowing the tray confirmed survives any power cut                                                                                                                                                                                            | fake OS layer + power cut                            |
| **wind down**                            | the `winding_down` state (start time, items, choice per item) is written first; after a helper crash it is read back and continues; parked and moved items are in the ledger and not moved again                                                                                                                                                                                      | pressing it again shows progress, starts nothing new                                                                                                                            | a `winding_down` state older than the last boot: cleared at sign-in in the protected store and in the T3 server's and runners' state together (unless "stay paused"); nothing running: "safe to shut down" at once                                                                                                      | fake clock + fakes                                   |
| **runner shrink**                        | the ledger line (job, run, attempt, choice) is written before the park or cancel; on restart, lines without "re-run asked" get it; a job already re-run (newer attempt exists) is marked done                                                                                                                                                                                         | same budget again: no-op; growth held off by the 2-minute rule; a second drop does not move the same attempt twice                                                              | `activity.json` stale or unreadable while someone is signed in: admit nothing new, tray note; unknown format: same; no jobs: only the heartbeat changes                                                                                                                                                                 | fake dispatcher + fake meters                        |
| **power loss (any moment)**              | every write above is atomic or journalled; narrowing is durable before it is confirmed; on start: store signature with narrowing replay, log tails, leftover persistence locks removed, deny pass, install verify, held-input drop, journal `unknown`s, layer cleanup, unfinished snapshots discarded, runner ledger and lost-runner jobs re-asked                                    | a second power loss during recovery: each recovery step is idempotent and runs again                                                                                            | lost: unsaved work inside apps; command changes after the last snapshot (T3 file tool changes have pre-images); never lost: a narrowing the tray confirmed                                                                                                                                                              | power-cut test in a VM per OS                        |
| **link extend**                          | the computer commits the new expiry in one transaction with the revocation check; the caller may miss the reply                                                                                                                                                                                                                                                                       | extending again sets the same or a later expiry (idempotent); a revoke in flight always wins                                                                                    | a link expired more than 30 days ago: refused, pair again; a link with no recorded device: refused, pair again (tray note); the linking side still holds the old expiry: it updates from the next reply, or `usableLink` asks once before dropping the link; a lowered lifetime: existing links cut to it               | fake clock + two fake servers                        |
| **link used from an unknown device**     | the call is refused before any tool runs                                                                                                                                                                                                                                                                                                                                              | each further try is refused and counted                                                                                                                                         | a link whose device identity was never recorded (older link): ordinary upstream calls work until its expiry; host tools and extension refused                                                                                                                                                                           | fake tailnet whois                                   |
| availability, project grants             | settings saved                                                                                                                                                                                                                                                                                                                                                                        |                                                                                                                                                                                 | "off until" in the past: on; bad value: off; a project with no setting: off                                                                                                                                                                                                                                             | settings test                                        |
| **host tools off / uninstall**           | each step is listed in a removal ledger first; a half removal finishes on the next run                                                                                                                                                                                                                                                                                                | idempotent                                                                                                                                                                      | removes: reach map grants, deny entries, auto-run denies and rename blocks, AppContainer read ACEs, persistence locks, firewall and network-floor rules, the protected-area watch, services, broker, guard tasks, snapshots it made, links it issued, both accounts and the agent group (after you confirm their files) | uninstall test per OS                                |

## 16. Tests (when built)

- **Core on Linux CI with a fake OS layer:** policy checks (reach map, sensitive set, high-risk list, protected areas),
  secret patterns (a hand-written fixture of expected secret paths per pattern, so weakening a pattern turns a test
  red), the ask (scopes, remembered answers, wear-down rules, OS-consent items), step-list checks and limits, journal
  and idempotency, input ladder logic with fake rungs (step-down, no double apply, rung memory), recipes, snapshot and
  undo with writers (agent undo never touches a file you changed), log chain and countersign, pause, wind-down choices
  by cost, availability and project grants, link extend and device binding, redaction of every output path, the take-over
  pause (kept across a restart; only Continue or an allowed ask ends it; no timer ends it), narrowing replay after a
  store fallback, and the persistence-lock ledger.
- **The toolkit** through upstream's MCP test kit: a forwarded call over a test link; a read-only link refusing write
  tools; an outside-input thread getting read tools only; the caller id arriving on the computer; a token from another
  device refused; **N2:** a forwarded thread launch, `delegate_task` and every other forwarded tool outside the
  allowlist are refused on the computer, also when the agent machine runs an older build without the allowlist.
- **Per-OS layers** (a real run on each OS before that OS is enabled, Windows first):
  - accounts: the agent account cannot read your profile root, a denied `.env`, T3's data or install, and cannot write
    an auto-run path; the host account cannot be reached by agent commands; the broker refuses the agent account;
  - links (B2): a planted junction, symlink and hard link do not widen a grant or redirect a restore or copy;
  - B1: an admin command runs the frozen copy shown in the ask, not a file swapped after the ask;
  - **protected areas (N1):** in a UI-acting batch the agent writes a `.bat` that adds a Run key, a Startup shortcut and
    a scheduled task, then opens it through a file dialog; each change is blocked or held and reverted, the tray offers
    Keep or Remove, and the activity log shows the write and the run; the same on macOS (a LaunchAgent) and Linux (a
    systemd user unit, an XDG autostart entry); a write to a system area needs the elevation prompt and is refused by
    the engine; a change made after the lock window by a process with a broken parent chain is shown by the baseline
    diff; locks are gone after a power cut in the window;
  - **N3:** renaming or deleting `.vscode`, `.idea` or `.git/hooks` as the agent account is denied; a new
    `tasks.json`, `.envrc` or git hook from the agent account is quarantined; "make it mine" applies your denies;
  - **N6:** a speculative winner that writes an auto-run path or a secret path is refused before any file is applied;
    on macOS a slot user cannot read a denied `.env`;
  - B3: an edited store file is ignored; the store's folder is refused in file dialogs; widening during a full-rights
    grant needs OS consent;
  - network (M3, N5): no direct internet from the agent group; each class of §3.5 unreachable, including the
    computer's own global IPv6 and LAN addresses and an on-link neighbour; a new address added mid-run is in the floor
    before the next connection; only the named services on linked machines are reachable (none by default); a
    per-goal ask opens exactly what it names, pinned to the addresses the guard resolved; **DNS:** a lookup of a name
    outside the linked machines and the goal's hosts is refused by the guard's resolver, and a direct query to any
    other resolver (including MagicDNS) is dropped; an opening left by a crash is gone after the next start;
  - speculative (M2): a speculative run cannot reach the engine, the broker, T3 or a denied file in its layer, cannot
    reach the network, and leaves no process after cleanup;
  - clipboard (M4): concealed content refused; content put back after a batch;
  - input (M5): a window that steals focus mid-burst gets no typed text; each rung reaches the test apps it claims and
    steps down from the ones it cannot;
  - **take-over:** a real click or key press during a rung-4 step stops it and releases all held input; on-screen
    acting stays paused for every task, with no timer, while commands and the agent's own session go on; Continue in
    the tray or T3, or your Allow on the agent's ask, ends the pause only with OS consent and only from a person's T3
    UI session (an MCP session's Continue is refused); the pause survives a restart;
  - **protected areas (r5):** the agent group cannot write any user-writable spot the reach check found
    (`ProgramData`, a drive root, a writable Program Files folder or HKLM key, `/usr/local`); a scheduled task, WMI
    subscription or user unit created during an acting window is held **and** the process it started is ended; a
    process started by Task Scheduler, WMI, COM, `systemd --user` or `launchd` during a window is marked; a locked
    persistence file deleted and recreated through its folder is caught by the watch; two overlapping lock windows
    keep the object locked until the second ends; after a guard restart the marks (with start time) and a grant's
    baseline are back; running an agent-placed file in a protected area is blocked (macOS, Linux) or ended at start
    (Windows), and elevated or persistence-registering runs need your OK;
  - **correctness (r5):** your sudo timestamps are cleared at the start of an acting batch or grant; a process started
    by an app the engine acted in is marked within the 10 s window and not after, and its descendants too; unlocking
    `.git/config` for one repo through the ask works and leaves other repos locked; the reach-check walk finds a
    planted writable spot as the agent account and as your filtered token; the non-blocking install warnings appear
    (UAC below "Always notify", docker or libvirt groups, polkit `auth_admin_keep`) without blocking; a revoke wins
    over an extension in flight; the linking side picks up an extension from the next reply and `usableLink` asks
    before dropping a link; a reused `host_do` key with other steps is refused; `apply` after a restart returns
    `layer_gone`; a half-applied winner is rolled back from its pre-images; two speculative AppContainers cannot write
    each other's layers;
  - **undo store:** the 80 % warning, pruning of accepted goals only, growth within the budget, held items never
    pruned, and the last-resort refusal with its message;
  - the kill switch ends every agent process; pause ends daemonised children; the engine stops without the helper;
  - a snapshot restores a deleted file; on Linux without btrfs, ZFS or LVM-thin, `host_undo` of a command change says
    "no undo for command changes here" instead of guessing;
  - refusals (lock screen, password field, own policy screens, inactive session);
  - power cut in a VM at random points during a batch, a speculative call, a wind-down and a runner shrink: the next
    start recovers as §15 says; **N8:** a power cut just after the tray confirms a pause, a revoke or a narrowing, also
    with the store file damaged: the narrowing holds after the restart;
  - the §8.2 micro-benchmarks; the **no pop-in test** of §6.8 (required, blocks enabling an OS).
- **Runners:** the budget logic with fake meters (a game taking memory, a spike), finish / park / move choices by cost
  with a fake dispatcher; the PC's dispatcher refuses a job whose event or actor is not in your policy even when the
  server routed it there (lens p11); **N10:** a runner job cannot read the agent accounts' or your files, cannot reach the engine,
  broker or T3, and leaves no process after it ends.

## 17. Costs and open points

- **The beta switch.** Linked environments is an upstream beta, off by default, and it must be on in **both** T3
  installs (agent machine and computer).
- **MCP tool-list size.** The host tools add several KB, so they are hidden unless at least one linked computer
  reports host tools on, and then deferred behind `tool_search`.
- **Two accounts, an agent group and three small services** on the computer (T3 as the host account, the agent
  account in the `t3-agents` group, the broker, the guard), plus the desktop app's helper and, for speculative runs and
  runners, extra accounts in that group. Setup is one installer run with one elevation prompt (one UAC prompt on
  Windows; the admin password on macOS and Linux).
- **macOS prerequisites (lens p5).** Endpoint Security needs Apple's ES client entitlement (applied for per developer
  account), a system extension the owner approves, and Full Disk Access. Without them: no blocking of protected-area
  writes or runs on macOS; the guard falls back to FSEvents watching and revert, and the install verify says so.
  `sandbox-exec` (used for speculative runs, §7.4) is deprecated by Apple though still shipped; a replacement is to
  find before it goes.
- **Two T3 installs on the computer** if you also use the desktop app there; the host account's T3 is separate.
- **Delegated full agents on the computer** wait for the broker spawn hook (§3.2) and stay outside the forwarded-tool
  allowlist until then.
- **A new native component** (the engine, and the small broker and guard) with a Rust build per OS in fork CI.
- **Rung 3 on Windows and macOS is weaker** than on Linux: no real hardware-style input into a hidden desktop; apps
  that need it use rung 4. The agent's own session needs Windows Pro or higher (Home: a hidden desktop in your session
  only) and a second GUI login on macOS; both to verify in a spike with a separate local account, including real input
  inside that session and GPU apps on hidden displays.
- **Wayland** rung 4 needs the RemoteDesktop portal, which asks once per session on some desktops; rung 2 does not
  exist for native Wayland apps.

**Decided by the owner (2026-11-13)**, kept here so the reasons stay with the design: acting in the UI acts as you,
with protected areas and the activity log (§4.4); take-over pauses until you continue, no idle timer (§6.7); links
expire after 365 days with one-click extension (§10); the default reach map is documents, desktop, downloads, media,
code and data drives, a list in config (§4.3); the OS consent prompt stays for full-rights grants (§4.2); host tools are
off in new projects (§4.3); `.git/config` is read-only until you unlock a repo (§4.3); the agent group has no direct
internet, only your linked machines, with a per-goal ask for more (§3.5).

**Owner answers to the r4 questions (2026-11-13):**

1. **"Run" in protected areas** = running anything the agent itself placed there, anything elevated, and anything that
   registers autostart or persistence; each needs your OK per action (§4.4).
2. **Take-over pause** stops only on-screen acting; background commands go on (§6.7).
3. **After a take-over** the agent's renewed ask needs your Allow; no automatic continue (§6.7).
4. **UAC:** setup sets "Always notify" by default; you can turn it off (§3.3, §4.4).
5. **Windows driver:** not now; only if measurements show it is needed (§4.4).
6. **Undo store:** sized so it does not fill in normal use: from measured need, on a drive with room, growing within a
   disk budget you set, pruning only copies of accepted or finished goals (never held or quarantined items, never inside
   the undo window), warning at 80 %; refusing writes stays only as the last-resort guard, on by default (§4.3 rule 6).
7. **User-writable system places** are protected areas by default: deny entries for the agent group, watched for you,
   with the residual said openly (§4.4).

**Open for the owner** (first one first; each narrows or adds to an earlier decision):

1. **Continue always needs the OS consent prompt.** A security review showed that an agent could press Continue for
   you through its own session. So Continue after a take-over, and Allow on the agent's renewed ask, now always need
   the OS consent prompt (PIN, fingerprint or password) and only work from your T3 window (§4.2, §6.7). Is one prompt
   per Continue acceptable?
2. **The agent's own screen is a loopback Remote Desktop session of the agent account** on Windows Pro and higher, with
   a random password the guard keeps and rotates daily (§3, §3.1, §6.4). It never uses your sign-in (the 2026-11-13
   spike showed your own child session cannot sign in with Windows Hello only). It needs a spike with a separate local
   account before it is final. Windows Home gets only a hidden desktop in your session. Agree?
3. **Linked machines: only named services.** Earlier: the agent group may reach your linked machines. Now: only the
   services you name per linked machine in config (for example "VM111 port 25565"), default none, because "all ports
   of all linked machines" let an agent reach admin panels and SSH there (§3.5). Agree, or should a linked machine be
   fully open by default?

**Follow-up research (named here, not done in this design):** which command languages and tools agents should use on
each OS for speed and reliability, general across projects: whether agents should write Python at all; how good the
Python, PowerShell and shell they write is in practice; built-in commands versus scripts; and a curated, fast toolset
per OS (startup time, output size, error behaviour) that `host_status` could advertise to agents. The result feeds the
tool descriptions and the recipe guidance.

## General vs project

Everything in this design is general. Machine names, addresses, accounts, folders, apps, games, bridges, recipes and
named commands are local policy and settings on each install; examples use placeholders only.
