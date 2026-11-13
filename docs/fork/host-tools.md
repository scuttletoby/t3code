# Host tools: agents on a person's own computer (design, no code)

Status: design for review. Fork feature, not upstream. Nothing here names a real machine, person or address; those
live in each install's local settings.

## In short

- **What it lets agents do:** an agent on one of your machines (for example a headless server) can read and change
  files in folders you allowed on another of your computers, run normal commands there (builds, scripts, git), take a
  screenshot and read the clipboard. It works the moment that computer is on; no clicks for normal work.
- **How it is kept safe:** the agents work as a separate, limited account on that computer. That account cannot open
  your personal files outside the allowed folders, cannot change system settings and is not an administrator. The
  operating system enforces this, not the agent.
- **What it never does:** read key and secret files (they stay unreadable to the agent account), install software or
  use admin rights without asking you, or act while you paused it.
- **What you click:** one pop-up for installs and anything that needs admin. Nothing else asks.
- **What you can always do:** pause from the tray or with a file, hit the kill switch (stops every agent process on
  that computer at once), switch it off or "off until …" in T3's settings, undo changed or deleted files from
  snapshots, and read the full log of what happened.
- **What you decide:** which folders agents may use (and which only read), which games count as "gaming", and the
  open points in §12.

## 1. Parts

| Part                 | Where it runs                                       | Built from                                                       | What it does                                                                                                              |
| -------------------- | --------------------------------------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Host environment     | the computer, as the **agent account** (§3)         | upstream T3 server, installed with upstream's background service | serves the host tools over the link; refuses work while paused, gaming or switched off                                    |
| Linked environment   | between the agent machine and the computer          | upstream peer links (`docs/internals/remote.md`, "Peer links")   | auth, routes (Tailscale, LAN), revocation, access ceiling, expiry                                                         |
| `host` toolkit       | inside T3's MCP server                              | **fork**, one toolkit like `toolkits/environment/`               | typed tools (§6); each takes `environmentId`, so upstream forwards it to the computer                                     |
| Session helper       | the computer, in **your** desktop session           | **fork** add-on to the T3 desktop app (main process)             | tray (pause, kill switch, status), gaming detection, screenshots, clipboard, the install/admin pop-up, editing the policy |
| Guard tasks          | the computer, as the system (admin)                 | **fork**, per OS, made once at install                           | snapshots for undo, the kill switch, re-applying folder permissions                                                       |
| Policy               | the computer, a file only you and admins can change | **fork**, JSON, shown in T3's settings UI                        | allowed folders, read-only folders, extra secret patterns, gaming list, named commands                                    |
| Availability setting | the agent machine's T3 settings                     | **fork**, one server setting                                     | per linked computer: on / off / off until a time (§4.7)                                                                   |

The **core is OS-neutral**: tool schemas, the policy format and checks, secret patterns, the log format, pause, gaming
and availability logic, the undo index. The **per-OS layer** is small and named in §3: making the account, folder
permissions, the service install, snapshots, screenshot/clipboard/pop-up/full-screen calls, and the kill switch.
Windows is built first; nothing Windows-only goes into the core.

## 2. Upstream fit

Read at upstream `main` 76d0fff5fd (2026-11-13), with open PRs and issues searched the same day.

| Need                                  | Upstream mechanism we use as is                                                                                                                                              | What the fork adds (thin)                                                                                            |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Run T3 on the computer, start at boot | background service: `t3 service install` (Windows Task Scheduler task, macOS login item, Linux systemd user unit; `docs/user/background-service.md`)                         | an option to install it **for another account** (§3), since upstream installs it for the signed-in user              |
| Reach the computer, auth, revoke      | linked environments: pairing code, OAuth session in the secret store, routes over Tailscale/LAN, revoke in Sharing & access (`apps/server/src/peer/PeerLinks.ts`)            | nothing                                                                                                              |
| Send a tool call to the computer      | tools whose parameters take `OrchestratorMcpEnvironmentTarget` are forwarded by `McpToolAccess.toLayer` after the caller is checked (`apps/server/src/mcp/McpToolAccess.ts`) | the `host` toolkit, declared like `t3_environment_read` (`toolkits/environment/tools.ts`)                            |
| Limit what a link may do              | link access (read-only or a runtime-mode ceiling), `T3-Mode-Limit` on every forwarded call, `linkOrigin` fence below full access                                             | host tools are declared read or write with upstream's access declarations; a read-only link gets only the read tools |
| A full agent working on the computer  | `delegate_task` with `environmentId`; the child is an ordinary thread there (`RemoteDelegation.ts`)                                                                          | nothing: it runs as the agent account, so the same OS rules hold                                                     |
| Settings and their UI                 | `ServerSettings` / `ServerSettingsPatch` (`packages/contracts/src/settings.ts`), settings pages, settings search                                                             | one availability setting (§4.7) and a read view of the policy                                                        |
| Keep the tool list small              | `McpToolExposure` and `tool_search` (deferred tools); link tools are hidden while the beta is off (merged #30371)                                                            | host tools are deferred and hidden unless a linked computer reports host tools on                                    |
| Ask the person                        | pending requests and cards in the client (`request_secret`, questions)                                                                                                       | not used for installs/admin: that pop-up must appear **on the computer itself** (§5)                                 |

**Custom, and why.** The session helper, the guard tasks, the snapshots and the policy file are custom because
upstream has no notion of a second, limited OS account or of the person's desktop session. Each is an add-on module
with no change to upstream files except one registration line per toolkit and setting.

**When upstream changes the Linked environments beta:**

- **The beta becomes default-on or the flag goes away:** our check of `linkedEnvironmentsEnabled` is dropped; nothing
  else changes.
- **Forwarding changes shape:** the `host` toolkit is built exactly like upstream's own forwarded tools, so it breaks
  where theirs break and takes the same fix. The fork's follow job (nightly) shows it the same day.
- **Linked environments are removed or redesigned without forwarding:** fallback without new transport code: the
  agent machine adds the computer's `/mcp` as an outside MCP client (`docs/internals/environment-auth.md`, "MCP clients
  are a separate audience"). Same tools, same rules on the computer; only the setting in §4.7 would move.
- **Upstream adds its own remote file or command tools:** we compare and drop ours where theirs cover a tool, keeping
  only the OS-account rules, the helper and the snapshots.

Related upstream work, not merged on 2026-11-13: #36102 (moving work carries its files), #32639 (re-linking revokes the
old session), #35519 (a silent first address costs 10 s per call), #32158 (OpenShell worker fleet), #35826 (read-only
clients get a 403 for write tools).

**Next to the fork's fleet runners (#13).** Separate features. Fleet runners run the fork's own CI jobs on fleet hosts
(root dispatcher, one Linux user per slot, one-job runners, `.github/fork-fleet.json`). Host tools give agents typed
access to a person's own computer. They share only two ideas, a limited OS account and Tailscale, and no code or
config in v1. Machines here are identified by linked environments, not by the fleet file. A computer that is both a
fleet host and a host-tools computer keeps them in **separate accounts**, and neither account is in the other's
allowed folders.

## 3. The agent account (per OS)

One local account per computer, for example `t3-agent`. It is a standard user, never an administrator, and has no
password anyone types (a long random one held by the OS's task or service store, or none where the OS allows it).

|                                     | Windows (first)                                                                                                                                                             | macOS                                                                               | Linux                                                                     |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Create                              | local standard user, not in Administrators or Remote Desktop Users; "deny log on locally" and "deny log on through RDP" so nobody signs in as it                            | standard user, hidden from the login window (`IsHidden`), no admin group            | system user with a home, no sudo, shell login disabled for people         |
| Run T3 as it                        | a Task Scheduler task "run whether the user is signed in or not", at boot, as that account (upstream installs "at logon, as me"; the fork adds `--account`)                 | a LaunchDaemon with `UserName` (a login item would only run when that user logs in) | a systemd system unit with `User=`, or its user unit with lingering       |
| Allowed folders                     | the owner grants the account Modify (read-only folders: Read) on each folder, inherited; done by the helper as the folder's owner, no admin needed                          | ACL entries (`chmod +a`) by the folder's owner                                      | POSIX ACLs (`setfacl`) or a shared group                                  |
| Your other files                    | your profile already denies other standard users; the install checks the rest (below)                                                                                       | home folders are private by default; the check still runs                           | home `0700` or `0750`; the check still runs                               |
| Secret files inside allowed folders | an explicit Deny ACE for the agent account on each match (§4.2)                                                                                                             | a deny ACL entry                                                                    | an ACL with no rights for the account (or the file left out of the group) |
| Network                             | allowed: internet; blocked for this account: your local network, except the linked machine's address (Windows Firewall rule scoped to the account; verify on first install) | `pf` rules with `user`                                                              | `nftables`/`iptables` `owner --uid-owner`                                 |
| Screen, clipboard                   | none: it is not in your session. Only through the session helper (§6)                                                                                                       | same                                                                                | same                                                                      |

**How commands run.** `host_run` starts the program as the agent account (it is the T3 server's own account), with
`shell=false` by default and a shell only when the caller asks for one, in a job (Windows job object / process group /
cgroup) that is killed on timeout, on pause and when T3 stops. A delegated agent on the computer runs its own tools
the same way. Either way, what a command can touch is what the account can touch.

**The install check ("what can the agent account reach").** Some places are readable by every user by default:
Windows gives every signed-in user read access, and often write access, to folders on extra drives (`D:\…`),
and to `C:\Users\Public`; macOS and Linux have world-readable shared folders. The install walks every fixed drive as the agent account and lists folders it
can read outside the allowed list. You then remove access there, or accept it in the policy. `host_status` repeats the
check weekly and reports new finds.

## 4. Rules instead of pop-ups

Normal work needs no click. Safety comes from rules the computer enforces. Each rule says **who enforces it**,
because a command run as the agent account can change anything that account owns (including its own T3 install and
data). So nothing that must hold is enforced only by the T3 server.

1. **Allowed folders.** Two lists in the policy: read-write and read-only. Enforced by the OS (§3). The helper applies
   permissions when you change the list; a guard task re-applies them daily and reports drift.
2. **Secret list.** The built-in patterns of the first design stay (keys, `.env`, browser and app login stores,
   password managers, wallets, OS credential stores, `*.pem`, `*.kdbx`, …), as one OS-neutral list in the core. Most
   secret stores are in your profile and so already out of reach. Inside allowed folders the helper puts a deny entry on
   every match, and watches the folders: a matching file **you** create is denied within about a second (in that window
   it is readable). The policy can add patterns, never remove a built-in one. Token shapes (`ghp_`, `sk-ant-`, `AKIA…`,
   JWTs, …) are still redacted in what host tools return, as defence in depth.
3. **Undo.** OS snapshots, taken by a guard task the agent account cannot change or delete: before each `host_run` and
   each burst of writes (at most every 5 minutes), and every 15 minutes while the agent account has a process running.
   Kept 7 days within a size cap. The helper's folder watcher records every created, changed and deleted file with its
   time, so "undo since 14:05" restores exactly those files from the snapshot before that time. The guarantee: any file
   can go back to its state at the last snapshot before the change; edits between two snapshots are not each kept.
   - Windows: Volume Shadow Copy of the volumes that hold allowed folders (verify the storage cap and the 64-copy limit
     on first install).
   - macOS: APFS local snapshots (`tmutil localsnapshot`); macOS deletes them after about 24 h, so 7 days needs Time
     Machine (to verify).
   - Linux: btrfs/ZFS/LVM-thin snapshots where the folder lives on one; otherwise a root-owned copy of the allowed
     folders every 15 minutes (`rsync --link-dest`), with a size cap that, when hit, says so in `host_status`.
4. **Log.** Two logs, both append-only for the agent account (Windows: the account gets _append data_ only on the file;
   macOS `uappnd`; Linux `chattr +a`, set by the guard task): the T3 side logs every host tool call and every delegated
   turn (who, tool, arguments without file contents, result, bytes, time); the helper logs every file change in allowed
   folders, every pop-up and answer, pauses and kill switches. Each line carries the hash of the line before, so a cut
   shows. The agent account can add lines, not change them.
5. **Pause and kill switch.** _Pause_ (tray, or a `PAUSED` file in the policy folder that only you can write): the
   server refuses every host tool and every new delegated turn with "paused by owner", and running `host_run` jobs are
   stopped. _Kill switch_ (tray, or one command): a guard task kills every process of the agent account and disables the
   account until you turn it back on. Pause relies on the T3 server; the kill switch relies only on the OS.
6. **Gaming mode.** Gaming is on when a process from the policy's list runs (the server sees process names) or when
   the helper reports a full-screen app (Windows `SHQueryUserNotificationState`; macOS/Linux: the helper's full-screen
   check). While gaming: heavy tools and `host_run` are refused, delegated turns wait, no pop-up is shown, reads stay
   cheap. A helper that stops reporting while you are signed in counts as gaming (fail closed), and `host_status` says
   why.
7. **Availability, a T3 setting.** On the agent machine, Settings → Linked environments → _computer_ → Host tools:
   **on** (default, as the owner chose), **off**, or **off until** a time. Stored in `ServerSettings`, no database
   change. The agent machine refuses to forward host tools while off. This is a convenience switch: it lives on the
   agent machine, so the computer's own pause and kill switch are what you rely on, and they win.

## 5. Pop-ups: only what no rule can make safe

1. **Installs.** `host_request_install` names a package (winget / Homebrew / the distro's package manager) and a
   reason. The helper shows what will be installed, from where and for whom; on OK it installs as **you**, and Windows
   UAC or the OS password prompt follows when it is machine-wide. Installing tools for the agent account only (into its
   own folders) needs no pop-up.
2. **Anything that needs admin.** `host_request_admin` with the exact command. Same pop-up, then the OS's own
   elevation prompt.

The pop-up shows facts first (package or command, source, size, what it changes), the agent's reason last and marked
unverified, Deny as the default, one at a time, refused after 60 s or while gaming.

**Named commands** stay for one case: things that need **your** secrets (a deploy with your SSH key). They are entries
in the policy, run by the helper as you, with output scrubbed of every secret value (first design §4a). They need no
pop-up because you wrote them.

## 6. Tools

All take `environmentId` (the computer) and are logged. Read tools work over a read-only link; the others need write
access on the link and a non-plan caller (upstream checks both).

| Tool                                                   | Kind  | What it does                                                                                                            | Limits                                                                                                   |
| ------------------------------------------------------ | ----- | ----------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `host_status`                                          | read  | online, paused, gaming (and why), availability, policy hash, allowed folders, reach-check finds, snapshot and log state |                                                                                                          |
| `host_list`, `host_stat`, `host_read`, `host_search`   | read  | files in allowed and read-only folders; a secret shows as name + `secret` only                                          | 1 MB per read, 2,000 entries, search 20 s; heavy ones refused while gaming                               |
| `host_write`, `host_mkdir`, `host_move`, `host_delete` | write | in read-write folders; compare-and-swap on the old hash for writes                                                      | snapshot before the first write of a burst                                                               |
| `host_run`                                             | write | a program with arguments, a working folder in an allowed folder, a timeout                                              | refused while paused or gaming; output 64 KB tail, redacted                                              |
| `host_undo`                                            | write | list changes since a time; restore files from the snapshot before it                                                    | only inside allowed folders                                                                              |
| `host_screenshot`, `host_clipboard`                    | read  | through the helper, in your session                                                                                     | refused while the screen is locked, a private app is in front, or the clipboard item is marked sensitive |
| `host_request_install`, `host_request_admin`           | write | the pop-up of §5                                                                                                        | one at a time                                                                                            |
| `host_named`                                           | write | a named command from the policy (§5)                                                                                    |                                                                                                          |

The file and run tools exist so agents on another machine need no full agent on the computer. A delegated agent there
uses its own tools under the same account and rules.

**Who enforces the helper's tools.** The helper serves screenshots, the clipboard and pop-ups over a local channel
(Windows: a named pipe whose ACL admits only the agent account; macOS/Linux: a Unix socket with the same rule). Any
process of the agent account can call it, not only the T3 server, so the helper checks pause, gaming, lock screen and
private apps itself and keeps its own log.

## 7. Network and auth

- All traffic goes over the linked environment: upstream's auth, routes and revocation. Tailscale is the expected route
  between machines; the computer's T3 service answers on loopback, LAN and tailnet only (upstream behaviour).
- Revoking the link on the computer (Sharing & access) ends access at once. The link also expires and must be renewed
  (§12).
- The agent account's outgoing network: internet open (builds, git, package downloads), your local network blocked
  except the linked machine (§3).

## 8. Threat model and what still leaks

| Threat                                   | What stops it                                                                                           | What is left                                                                                                             |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Prompt injection from files it reads     | the account's reach (allowed folders only), secrets denied, no admin, undo, log, pause, kill switch     | it can still change files in read-write folders and send what it read anywhere on the internet                           |
| A command changes T3's own rules         | rules that must hold are enforced by the OS, the helper or guard tasks (§4), not by the T3 server alone | pause is server-side; the kill switch is not                                                                             |
| Reading your other files                 | OS permissions; the install reach check                                                                 | folders every user may read that you chose to keep                                                                       |
| Secrets inside allowed folders           | built-in patterns + deny entries + the watcher                                                          | a secret in an unknown format, in an ordinary file name (`notes.txt`); about one second after you create a matching file |
| Damage to allowed folders                | snapshots + change log + `host_undo`                                                                    | edits between two snapshots; snapshot space running out (said in `host_status`)                                          |
| Abuse of the helper                      | narrow verbs, its own checks and log, pipe/socket ACL                                                   | a screenshot shows whatever is on screen while not paused                                                                |
| Stolen link session on the agent machine | revocation on the computer; access ceiling                                                              | until revoked, whatever the link allows                                                                                  |
| Network misuse from the agent account    | LAN blocked for the account                                                                             | internet is open: anything readable can leave                                                                            |
| Gaming disruption                        | gaming mode (§4.6)                                                                                      | cheap reads still cost a little I/O                                                                                      |

**What still leaks, plainly:** files in allowed and read-only folders, and anything they contain, can reach the
model provider and the internet. Everything every user may read on the computer (after the reach check, only what you
accepted). Process names of other users. A screenshot shows what is on screen. Data read goes into transcripts.

## 9. Speed

- **Per call:** one round trip over the link (Tailscale on a home network: a few ms; through a relay: tens of ms),
  plus the file I/O or the command's own time. T3 server start is paid once: the service is long-running. Today a
  link whose first address is silent costs 10 s per call (upstream #35519), so the computer's saved routes list the
  tailnet address first.
- **Model turns** cost seconds; tool overhead is small next to them.
- **Language:** TypeScript inside T3 for the toolkit and checks, and the T3 desktop app's main process for the helper.
  No new runtime to install. The alternatives: a Go or Rust single binary helper would also work while the desktop app
  is closed and is a bit faster to start, but adds a second build and release chain and a language the fork does not
  test. PowerShell works only on Windows and is slow to start. **Recommendation: no new language.** If the helper must
  run without the desktop app later, a Go binary is the fallback.

## 10. Failure table (state the feature changes)

What happens when it runs again, stops half-way, or meets old or empty state:

| Step that writes            | Crash right after                                                         | Run again                                                                  | Old / empty / half-written state                                        | Test           |
| --------------------------- | ------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------------- | -------------- |
| install: create account     | account exists, nothing else                                              | finds it, keeps it, re-checks group membership (refuses if it is an admin) | an account of that name that is an admin: refused                       | fake OS layer  |
| install: folder permissions | some folders granted                                                      | re-applies all (idempotent); removes grants for folders no longer listed   | a folder deleted since: reported, skipped                               | fake ACL layer |
| install: service task       | task exists, not started                                                  | replaced with the same definition                                          | an upstream "as me" task for the same T3 home: refused with the reason  |                |
| policy edit (helper)        | write to temp + atomic rename                                             | new policy or old one, never half                                          | unreadable policy: server refuses all write tools and says why          |                |
| `host_write`                | temp beside target, target unchanged                                      | same content: OK; other content: compare-and-swap refuses                  | target changed since read: refused                                      |                |
| snapshot request            | snapshot taken or not; log line says which                                | next request within 5 min reuses it                                        | snapshot store full: writes and runs go on, `host_status` red           |                |
| `host_undo`                 | some files restored                                                       | re-run restores the rest; already-restored files compare equal             | snapshot expired: refused with the oldest available time                |                |
| log line                    | line before the action and after; a call whose log write fails is refused |                                                                            | half line at the end: closed with a `truncated` marker, chain continues |                |
| pause / kill switch         | file present or account disabled                                          | idempotent                                                                 | kill switch with no agent processes: OK                                 |                |
| availability "off until"    | setting saved                                                             |                                                                            | time in the past: treated as on; bad value: off                         |                |

## 11. Tests (when built)

- Core on Linux CI with a fake OS layer: policy checks, secret patterns (a hand-written fixture of expected secret
  paths per pattern, so weakening a pattern turns a test red), log chain, pause, gaming, availability, undo index.
- The toolkit through upstream's MCP test kit, including a forwarded call over a test link and a read-only link
  refusing write tools.
- Per-OS layers: a real run on each OS before that OS is enabled (Windows first): account cannot read a file in your
  profile, cannot write outside allowed folders, cannot read a denied `.env`, kill switch ends its processes, a snapshot
  restores a deleted file, LAN blocked.

## 12. Costs and open points

- **The beta switch.** Linked environments is an upstream beta, off by default, and it must be on in **both** T3
  installs (agent machine and computer). Off on either side refuses link calls.
- **Link renewal.** A link expires with its session (about 30 days today); renewing needs a new pairing code from the
  computer, asked for through upstream's link card. `host_status` warns 3 days before.
- **MCP tool-list size.** Upstream measures the tool list in CI (closed PR #16049 tripped it). About 16 tools would add
  several KB to every request, so host tools are **hidden unless at least one linked computer reports host tools on**,
  and then deferred behind `tool_search`.
- **Two T3 installs on the computer.** If you also use the desktop app there, it keeps its own local environment (or
  none). The agent account's T3 is a separate install with its own data.
- **Provider logins for delegated agents.** Only needed if full agents run on the computer; host tools need none.
- **Open for the owner:** the default read-write and read-only folders; the gaming process list; the snapshot size cap
  and days; whether the agent account's internet should be limited further.

## General vs project

Everything in this design is general. Machine names, addresses, accounts, folders, games and named commands are local
policy and settings on each install; examples use placeholders only.
