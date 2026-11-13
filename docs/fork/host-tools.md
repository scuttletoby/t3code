# Host tools: agents on a person's own computer (design, no code)

Status: design for review. Fork feature, not upstream. Nothing here names a real machine, person or address; those
live in each install's local settings.

## In short

- **What it lets agents do:** an agent on one of your machines (for example a headless server) can work on another of
  your computers: read and change files in folders you allowed, run commands there, and **see and use the apps on your
  screen** much faster than classic "computer use", because it uses the most direct way into each app (command line,
  the app's own automation, an app-side bridge, accessibility elements) and moves the mouse only when nothing else works.
- **Speed first:** the computer runs a whole list of steps locally, with waits like "until this button appears", and
  sends back only what changed. One network hop and one model turn do the work that takes classic computer use dozens
  of screenshot-and-click turns (§7, §8). When the agent is unsure which of two ways works, it can try both at once
  where that is safe, and keep the one that worked (§7.4).
- **Invisible by default:** commands, scripts and batches run in the background with no window shown and no focus
  taken. Only UI work you asked for touches the visible screen, and a test checks that nothing else ever does (§6.6).
- **How it is kept safe:** files and commands run as a separate, limited account that the operating system keeps out
  of your personal files. Seeing and acting in your apps goes through a helper in your session that checks a policy
  you control: which apps the agent may see or use, never password fields, the lock screen or admin prompts.
- **While the agent uses your apps:** a clear on-screen sign shows it; any real mouse or keyboard input from you takes
  control back at once; everything is logged.
- **What you click:** one pop-up for installs and anything that needs admin. Nothing else asks.
- **What you can always do:** from the tray icon: pause, **wind down** (finish what is running, then "safe to shut
  down"), or the kill switch; switch host tools off in T3's settings; undo file changes from snapshots; read the log.
- **What it does not do:** watch what you are doing to slow itself down. Work you ask for runs when you ask. Only the
  background runners on the computer (CI jobs, builds) scale themselves down while you game or work hard (§11).

## 1. Parts

| Part                 | Where it runs                                       | Built from                                                       | What it does                                                                                                 |
| -------------------- | --------------------------------------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Host environment     | the computer, as the **agent account** (§3)         | upstream T3 server, installed with upstream's background service | serves the host tools over the link; runs files and commands as the agent account                            |
| Linked environment   | between the agent machine and the computer          | upstream peer links (`docs/internals/remote.md`, "Peer links")   | transport, auth, routes (Tailscale, LAN), revocation, access ceiling, expiry (§10)                           |
| `host` toolkit       | inside T3's MCP server                              | **fork**, one toolkit like `toolkits/environment/`               | typed tools (§9); each takes `environmentId`, so upstream forwards it to the computer                        |
| Session helper       | the computer, in **your** desktop session, as you   | **fork** add-on to the T3 desktop app (main process)             | tray (§12), policy editing, the install/admin pop-up; starts the engine; hosts the bridge endpoint           |
| Engine               | the computer, as you, started by the session helper | **fork**, new, one native binary per OS (§13)                    | the seeing and acting layer (§6-§8): app state, accessibility tree cache, batches, waits, input, screenshots |
| Guard tasks          | the computer, as the system (admin)                 | **fork**, per OS, made once at install                           | snapshots for undo, the kill switch, re-applying folder permissions                                          |
| Policy               | the computer, a file only you and admins can change | **fork**, JSON, shown in T3's settings UI                        | allowed folders, secret patterns, per-app see/act rules, approved scripts and recipes, named commands        |
| Availability setting | the agent machine's T3 settings                     | **fork**, one server setting                                     | per linked computer: on / off / off until a time (§4.6)                                                      |
| Activity service     | the computer, in your session (part of the helper)  | **fork**, runner side only (§11)                                 | tells background runners how much they may use; host tools never read it                                     |

The **core is OS-neutral**: tool schemas, the policy format and checks, the step language, recipes, snapshot and diff
format, secret patterns, the log format, pause and wind-down logic, the undo index. The **per-OS layer** is named in
§3, §6 and §13: making the account, folder permissions, the service install, snapshots, the accessibility and input
back ends, screenshots, refusal checks, and the kill switch. Windows is built first; nothing Windows-only goes into the
core.

## 2. Upstream fit

Read at upstream `main` 8cd8a3ca5a (2026-11-13), with open PRs and issues searched the same day.

**Owner decision (2026-11-13): upstream is used for transport and auth only.** The seeing and acting layer is the
fork's own, written from scratch and speed first (§5). Upstream's computer-use work is listed below so it is clear what
was considered and why it is not the base.

| Need                                  | Upstream mechanism we use as is                                                                                                                                              | What the fork adds (thin)                                                                                            |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Run T3 on the computer, start at boot | background service: `t3 service install` (Windows Task Scheduler task, macOS login item, Linux systemd user unit; `docs/user/background-service.md`)                         | an option to install it **for another account** (§3), since upstream installs it for the signed-in user              |
| Reach the computer, auth, revoke      | linked environments: pairing code, OAuth session in the secret store, routes over Tailscale/LAN, revoke in Sharing & access (`apps/server/src/peer/PeerLinks.ts`)            | link renewal while in use (§10), offered in a form upstream could take                                               |
| Send a tool call to the computer      | tools whose parameters take `OrchestratorMcpEnvironmentTarget` are forwarded by `McpToolAccess.toLayer` after the caller is checked (`apps/server/src/mcp/McpToolAccess.ts`) | the `host` toolkit, declared like `t3_environment_read` (`apps/server/src/mcp/toolkits/environment/tools.ts`)        |
| Limit what a link may do              | link access (read-only or a runtime-mode ceiling), `T3-Mode-Limit` on every forwarded call, `linkOrigin` fence below full access                                             | host tools are declared read or write with upstream's access declarations; a read-only link gets only the read tools |
| A full agent working on the computer  | `delegate_task` with `environmentId`; the child is an ordinary thread there (`apps/server/src/peer/RemoteDelegation.ts`)                                                     | nothing: it runs as the agent account, so the same OS rules hold                                                     |
| Settings and their UI                 | `ServerSettings` / `ServerSettingsPatch` (`packages/contracts/src/settings.ts`), settings pages, settings search                                                             | the availability setting (§4.6), link renewal settings (§10), a read view of the policy                              |
| Keep the tool list small              | `McpToolExposure` and `tool_search` (deferred tools); link tools are hidden while the beta is off (merged #30371)                                                            | host tools are deferred and hidden unless a linked computer reports host tools on                                    |

**Upstream computer-use work, considered and not used as the base:**

- Codex's built-in Computer Use is only _shown_ by T3 (`apps/server/src/provider/CodexToolPresentation.ts:195`,
  `packages/shared/src/toolActivity.ts:31`); it runs where Codex runs, not on another computer.
- The Cua Driver stack, open PRs #16527 to #16532 (bundle Cua Driver 0.38 on all three OSes, attach `cua-driver` as an
  MCP server to every provider session, settings, a floating preview of the driven window). It drives the host the
  provider runs on, one action per tool call, screenshot-led.
- agentcast, open draft #28050: remote desktops over SSH, reads UI trees, background input, refuses on a locked PC
  or an elevation prompt, journals every action so a lost reply never runs twice. Its code is in a private repository;
  we take **ideas only** (the refusals and the action journal, §6.5 and §14), per the fork rule of own implementations.
- The device toolkit (`apps/server/src/mcp/toolkits/device/tools.ts`) drives phone simulators and emulators, not a
  desktop.

None of these batches steps on the far side, keeps a diffed tree cache, or puts a person's policy between the agent
and the desktop, which is what §5 needs. If upstream later ships a fast, policy-gated desktop layer, we compare and
drop ours where theirs covers it (fork rule: build on upstream where it fits).

**Custom, and why.** The session helper, the engine, the guard tasks, the snapshots and the policy file are custom
because upstream has no notion of a second, limited OS account, of the person's desktop session as a separate trust
zone, or of a batched agent-native desktop layer. Each is an add-on module with no change to upstream files except one
registration line per toolkit and setting.

**When upstream changes the Linked environments beta:**

- **The beta becomes default-on or the flag goes away:** our check of `linkedEnvironmentsEnabled` is dropped; nothing
  else changes.
- **Forwarding changes shape:** the `host` toolkit is built exactly like upstream's own forwarded tools, so it breaks
  where theirs break and takes the same fix. The fork's follow job (nightly) shows it the same day.
- **Linked environments are removed or redesigned without forwarding:** fallback without new transport code: the
  agent machine adds the computer's `/mcp` as an outside MCP client (`docs/internals/environment-auth.md`, "MCP clients
  are a separate audience"). Same tools, same rules on the computer; only the setting in §4.6 would move.
- **Upstream adds its own remote file or command tools:** we compare and drop ours where theirs cover a tool, keeping
  only the OS-account rules, the helper and the snapshots.

Related upstream work, not merged on 2026-11-13: #36102 (moving work carries its files), #32639 (re-linking revokes the
old session), #35519 (a silent first address costs 10 s per call), #32158 (OpenShell worker fleet), #35826 (read-only
clients get a 403 for write tools), #34293 (host CPU and memory load in `t3_environment_read`; §11 can read it).

**Next to the fork's fleet runners (#13).** Separate features. Fleet runners run the fork's own CI jobs on fleet hosts
(root dispatcher, one Linux user per slot, one-job runners, `.github/fork-fleet.json`). Host tools give agents typed
access to a person's own computer. They share a limited OS account, Tailscale, the tray (§12) and the activity service
(§11, runner side), and no other code or config in v1. A computer that is both a fleet host and a host-tools computer
keeps them in **separate accounts**, and neither account is in the other's allowed folders.

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
| Screen, apps, clipboard             | none: it is not in your session. Only through the engine, behind the helper's policy (§6)                                                                                   | same                                                                                | same                                                                      |

**How commands run.** `host_run` starts the program as the agent account (it is the T3 server's own account), with
`shell=false` by default and a shell only when the caller asks for one, in a job (Windows job object / process group /
cgroup) that is killed on timeout, on pause and when T3 stops. A delegated agent on the computer runs its own tools
the same way. Either way, what a command can touch is what the account can touch.

**The install check ("what can the agent account reach").** Some places are readable by every user by default:
Windows gives every signed-in user read access, and often write access, to folders on extra drives (`D:\…`), and to
`C:\Users\Public`; macOS and Linux have world-readable shared folders. The install walks every fixed drive as the agent
account and lists folders it can read outside the allowed list. You then remove access there, or accept it in the
policy. `host_status` repeats the check weekly and reports new finds.

## 4. Rules instead of pop-ups

Normal work needs no click. Safety comes from rules the computer enforces. Each rule says **who enforces it**,
because a command run as the agent account can change anything that account owns (including its own T3 install and
data). So nothing that must hold is enforced only by the T3 server.

1. **Allowed folders.** Two lists in the policy: read-write and read-only. Enforced by the OS (§3). The helper applies
   permissions when you change the list; a guard task re-applies them daily and reports drift.
2. **Secret list.** Built-in patterns (keys, `.env`, browser and app login stores, password managers, wallets, OS
   credential stores, `*.pem`, `*.kdbx`, …), one OS-neutral list in the core. Most secret stores are in your profile and
   so already out of reach. Inside allowed folders the helper puts a deny entry on every match, and watches the folders:
   a matching file **you** create is denied within about a second (in that window it is readable). The policy can add
   patterns, never remove a built-in one. Token shapes (`ghp_`, `sk-ant-`, `AKIA…`, JWTs, …) are still redacted in what
   host tools return, as defence in depth.
3. **Undo.** OS snapshots, taken by a guard task the agent account cannot change or delete: before each `host_run` and
   each burst of writes (at most every 5 minutes), and every 15 minutes while the agent account has a process running.
   Kept 7 days within a size cap. The helper's folder watcher records every created, changed and deleted file with its
   time, so "undo since 14:05" restores exactly those files from the snapshot before that time. The guarantee: any file
   can go back to its state at the last snapshot before the change; edits between two snapshots are not each kept.
   Changes made **inside apps** (§6) are not covered by file snapshots unless the app saves into an allowed folder.
   - Windows: Volume Shadow Copy of the volumes that hold allowed folders (verify the storage cap and the 64-copy limit
     on first install).
   - macOS: APFS local snapshots (`tmutil localsnapshot`); macOS deletes them after about 24 h, so 7 days needs Time
     Machine (to verify).
   - Linux: btrfs/ZFS/LVM-thin snapshots where the folder lives on one; otherwise a root-owned copy of the allowed
     folders every 15 minutes (`rsync --link-dest`), with a size cap that, when hit, says so in `host_status`.
4. **Log.** Two logs, both append-only for the agent account (Windows: the account gets _append data_ only on the file;
   macOS `uappnd`; Linux `chattr +a`, set by the guard task): the T3 side logs every host tool call and every delegated
   turn (who, tool, arguments without file contents, result, bytes, time); the helper logs every file change in allowed
   folders, every batch and step it ran in your session (app, target, action, result, timing), every take-over, pop-up
   and answer, pauses, wind-downs and kill switches. Each line carries the hash of the line before, so a cut shows.
5. **Pause, wind down, kill switch** (all from the tray, §12; pause also with a `PAUSED` file in the policy folder that
   only you can write). _Pause_: every host tool and every new delegated turn is refused with "paused by owner", running
   `host_run` jobs and batches are stopped, held keys and buttons are released. _Wind down_: nothing new starts,
   running work finishes or reaches a safe point, then the tray says "safe to shut down" (§12). _Kill switch_: a guard
   task kills every process of the agent account, the helper stops the engine, and the account is disabled until you
   turn it back on. Pause and wind-down rely on the T3 server and the helper; the kill switch relies only on the OS.
6. **Availability, a T3 setting.** On the agent machine, Settings → Linked environments → _computer_ → Host tools:
   **on** (default, as the owner chose), **off**, or **off until** a time. Stored in `ServerSettings`, no database
   change. The agent machine refuses to forward host tools while off. This is a convenience switch: it lives on the
   agent machine, so the computer's own pause, wind-down and kill switch are what you rely on, and they win.

**No activity gate.** Host tools do not check whether you are gaming or busy. Work you asked an agent to do runs while
you do anything else. Only §11's background runners adapt to what you are doing.

## 5. Seeing and acting: principles (owner decision, speed first)

The agent works in your apps through our own layer, built from scratch for agents, not by imitating a person with a
mouse and screenshots. Five rules:

1. **Most suitable tool first, pixels last.** For each job the agent uses the first route that works:
   1. **shell, CLI and files** as the agent account (`host_run`, file tools): a build, a git command, a file export;
   2. **app and OS automation APIs**, through typed adapters in the engine: Windows COM / WinRT / Shell, macOS Apple
      Events (AppleScript, JXA) and app scripting dictionaries, Linux D-Bus (desktop portals, MPRIS, app interfaces);
   3. **app-side bridges** (§6.4): a game mod, a browser over the DevTools protocol (CDP), an editor extension;
   4. **accessibility-tree actions on elements by id**: invoke, toggle, set value, select, expand, scroll into view,
      with no mouse movement (Windows UI Automation patterns, macOS AX actions, Linux AT-SPI Action and EditableText);
   5. **synthetic mouse and keyboard** only when none of the above works (a game with no bridge, a canvas app).
2. **Few round trips.** One `host_do` call carries a list of steps (§7), with waits on conditions ("until element X
   appears", "until the window title is Y") that the engine checks **on the computer**, and returns only the results and
   what changed. No screenshot polling over the network.
3. **Compact observations.** App state and the accessibility tree come back as a filtered text snapshot, and after the
   first read **only what changed** since the last one (§6.2). Screenshots only when asked for or when an app has no
   structure, cropped to the window or region and scaled down.
4. **A latency budget with numbers** (§8), measured by a benchmark against a mouse-and-screenshot baseline.
5. **Reusable named recipes** (§7.3): once an agent has found how to do a task in an app, it saves the steps as a named,
   policy-checked recipe that replays at engine speed.
6. **Invisible by default** (owner decision 2026-11-13, §6.6): nothing the agent runs shows a window or takes focus,
   unless it is UI work you asked for.
7. **Try alternatives at once when unsure** (owner decision 2026-11-13, §7.4): side-effect-free alternatives run in
   parallel and the first one that works is kept, instead of one model turn per attempt.

## 6. Seeing and acting: the engine

### 6.1 Who runs it, and how that fits the agent account

Files and commands run as the **agent account**, which the OS keeps out of your files (§3). Seeing and using your apps
cannot work that way: only a process in **your** desktop session, as you, can read your windows' accessibility trees,
capture your screen and send input to your apps (Windows session isolation and UIPI; macOS grants Accessibility and
Screen Recording to an app in your GUI session; Wayland gives these only to the session's own clients). So:

- The **engine runs as you**, started by the session helper, and is the only part with that power.
- The agent account reaches it only through a **local channel** with a fixed set of verbs: on Windows a named pipe
  whose ACL admits only the agent account and you; on macOS and Linux a Unix socket with the same rule. Any agent-account
  process can call it (not only the T3 server), so the **engine enforces the policy itself**: pause, wind-down, per-app
  rules, refusals (§6.5), the step limits, and its own log. The T3 server's checks come first but are not relied on.
- The engine runs **no free-form script as you**. Automation adapters are typed verbs shipped with the fork. A raw
  script (PowerShell, AppleScript, a shell line) runs as you only if it is in the policy as an **approved script**
  (like the named commands of §9.1: you wrote or approved it). Agent-written scripts run as the
  agent account through `host_run`, where the OS limits them.
- The engine never runs elevated. Windows: no `uiAccess`, medium integrity, so admin windows are out of reach by UIPI.
  macOS: no root helper. Linux: no root, no `uinput` group.

**What this means plainly:** with acting on, the agent can do in an allowed app what you could do there by hand, with
your rights. An app that can open any file (an editor, a file manager, a browser) is a way around the folder rules of
§3. That is why acting is per app, off for unknown apps by default, refused in a built-in list (§6.5), visible while
it happens, logged, and stops the moment you touch the mouse or keyboard.

### 6.2 Seeing: sources, which wins, and how the agent finds them

`host_observe` returns, in one call:

- **Windows and focus:** every visible top-level window (id, app, process, title, bounds, monitor, minimised, focused),
  and which element has keyboard focus. Cheap; always included.
- **One structured snapshot per requested window**, from the best source that has data for it:

| Order | Source                     | Used when                                                                                    | What it gives                                                                 |
| ----- | -------------------------- | -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| 1     | **app-side bridge** (§6.4) | a bridge is registered for that app and answers                                              | the app's own state, exact (what the player looks at, inventory, tooltips, …) |
| 2     | **automation adapter**     | the app has a typed adapter (§5) whose read verbs cover the request                          | documents, selections, open items, as data                                    |
| 3     | **accessibility tree**     | the app exposes one (most normal apps, Electron with accessibility on, browsers' chrome)     | elements with id, role, name, value, state, bounds, available actions         |
| 4     | **screenshot**             | asked for, or sources 1-3 give a **sparse** result (games, canvases, remote-desktop windows) | a cropped, scaled image of the window or a region                             |

Sources 1 and 3 are combined when both have data (a browser: CDP for the page, the tree for the browser's own UI).
A tree is **sparse** when a visible window of more than a small area has fewer than a few named elements; the
snapshot then says `sparse: true` and suggests a bridge or a screenshot.

**Snapshot format.** Indented text, one element per line, `[id] role "name" = value {states} @x,y,w,h`. Filtered by
default: only visible, named or actionable elements; containers with one child are folded; long lists are cut to the
visible part with a count. A size cap (default 6,000 tokens per window) cuts deepest-first and says so. Element ids
are stable for as long as the element lives (the engine keeps them across reads), so a step can name `[e412]` minutes
later. **Selectors** (`role=button name="Save" in window title~"Report"`) are the stable form for recipes.

**Diffs.** Every snapshot has an id. `host_observe {since: <id>}` returns only added, removed and changed lines. The
engine keeps a live cache of each watched window's tree, updated from the OS's change events (UI Automation event
handlers with a cache request; macOS `AXObserver` notifications; AT-SPI events over D-Bus), so a diff is a read from
memory, not a new walk of the app.

**Screenshots.** Window or region crop, scaled to a maximum edge (default 1280 px), WebP or JPEG. A `zoom` region
returns a sharper crop. Windows denied for seeing (§6.5) are blacked out of any screenshot. The on-screen indicator is
excluded from captures (Windows `WDA_EXCLUDEFROMCAPTURE`; macOS window `sharingType = none`; Linux: left out of the
crop where the compositor allows, to verify).

**Discovery.** `host_status` lists what the computer offers: the OS and its accessibility back end and permission state
(macOS Accessibility and Screen Recording granted or not), the registered bridges and their verbs, the automation
adapters available, the per-app policy summary, whether acting is on, and the saved recipes. `host_observe` names the
source used per window. The tool descriptions tell agents the order of §5.1.

### 6.3 Acting

All acting goes through `host_do` (§7). The step kinds, in the order agents should prefer them:

| Step kind   | What it does                                                                                            | Moves the mouse? | Per-OS back end (Windows / macOS / Linux)                                                                                                            |
| ----------- | ------------------------------------------------------------------------------------------------------- | ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run`       | a command as the agent account (same as `host_run`)                                                     | no               | job object / process group / cgroup                                                                                                                  |
| `app`       | a typed automation verb (open a file in an app, read a document, menu command)                          | no               | COM, WinRT, Shell / Apple Events, scripting dictionaries / D-Bus, portals                                                                            |
| `bridge`    | a verb of an app-side bridge (§6.4)                                                                     | no               | the bridge's own channel                                                                                                                             |
| `el`        | an element action by id or selector: invoke, toggle, set value, select, expand, focus, scroll into view | no               | UIA patterns (Invoke, Toggle, Value, SelectionItem, ExpandCollapse, ScrollItem) / AX actions and attributes / AT-SPI Action, EditableText, Selection |
| `window`    | focus, move, resize, minimise, close a window; launch an app                                            | no               | Win32 / AppKit / X11 EWMH or the compositor's portal                                                                                                 |
| `clipboard` | read or set the clipboard                                                                               | no               | Win32 / NSPasteboard / X11 selections, Wayland data-control                                                                                          |
| `input`     | synthetic keys, text, clicks, drags, scrolls at a window-relative point or element                      | **yes**          | `SendInput` / `CGEvent` / XTest on X11, the RemoteDesktop portal with libei on Wayland                                                               |
| `wait`      | until a condition holds or a timeout (§7.1)                                                             | no               | event-driven where the OS sends events, else a 25 ms poll                                                                                            |
| `observe`   | add a snapshot or diff (or a crop) to the result at this point                                          | no               | §6.2                                                                                                                                                 |

`input` steps aim at an element (its centre, or an offset in it) or at a point relative to a window, never at a bare
screen point, so a moved window does not turn a click into a click elsewhere. Before each `input` step the engine
checks that the target window is still where the step expects and in front; if not, the step fails as `stale`.

**Browser.** For web work the engine starts a **separate browser profile for agents** with the DevTools protocol over a
pipe only the engine holds (`--remote-debugging-pipe`), so no local port is open to other processes. Your own browser
is reached only through its accessibility tree, or through a bridge extension you install, if the policy allows.

### 6.4 App-side bridges

For apps with no useful accessibility data (games, canvases, custom-drawn editors), the app itself can expose its state
and verbs. A **bridge** is a plug-in inside the app (for example a game mod or an editor extension) that connects to
the engine's **bridge endpoint** (a local pipe/socket for processes running as you) and speaks **MCP**: its tools are
its verbs, each marked read or act, with a JSON schema.

- **Registration.** A bridge names itself and the app it serves. Only bridges listed in the policy (name, and
  optionally the hash of its file) are used; a new one shows in the tray as "waiting to be allowed" and is not usable
  until you add it. No pop-up.
- **Use.** Bridge read verbs feed `host_observe` (source 1 in §6.2) and `bridge` steps; act verbs follow the same rules as
  other acting (§6.5), including take-over: while you are using that app, its act verbs wait.
- **Why preferred:** a bridge is exact (it knows what the player is looking at, not a guess from pixels), fast (one
  local call) and does not need the app in front.
- **Files** the app uses (worlds, saves, configs) are reached as usual: through allowed folders and the file tools.

### 6.5 Safety rules for seeing and acting

- **Per-app policy.** Each app (by executable path, bundle id or desktop file id) is `hidden`, `see` or `act`. Unknown
  apps default to `see` (setting; open point 2). `hidden` apps are left out of window lists' details, trees and
  screenshots (blacked out).
- **Built-in refusals, not changeable in the policy:** the lock screen and the secure desktop (Windows: input desktop
  is not `Default`, UAC, Ctrl+Alt+Del; macOS: `loginwindow`, `SecurityAgent`; Linux: the screen locker and polkit
  agents); password fields (UIA `IsPassword`, AX secure text fields and `IsSecureEventInputEnabled`, AT-SPI
  `password text`): their values are never read and no text is typed into them; elevated windows; password managers
  in the built-in list; and **the helper's, the tray's and T3's own host-tools and policy screens**, so an agent cannot
  click its own permissions wider. Seeing and acting on these return `refused` with the reason.
- **Acting is opt-in.** The policy has one switch for acting (`off`, `elements` = no synthetic input, `full`). Default
  `off` until you choose (open point 1).
- **On-screen indicator.** While a batch with acting steps runs, the tray icon changes and a small banner names the app
  and the step. While **synthetic input** is being sent, a coloured border runs around the screen and the agent's
  pointer is drawn, so it is plain that the mouse is not yours. The indicator is drawn by the helper, not the engine,
  and stays up until the engine reports the input released.
- **You take over at once.** The engine watches real input and tells it apart from its own (Windows: low-level hooks,
  `LLMHF_INJECTED` / `LLKHF_INJECTED` flags; macOS: an event tap, our events carry a source tag; X11: XInput2 raw events
  by device; Wayland: the compositor's idle and input signals, to verify per compositor). Any real key or mouse movement
  beyond a small threshold: the running step stops, every key and button the engine holds is released, queued steps are
  dropped, and the batch returns `interrupted_by_user` with what ran and a fresh diff. A new acting batch starts only
  when you have been idle for a set time (default 5 s; open point 3), or when you press "let the agent continue" on the
  banner.
- **Pause, wind-down and the kill switch** stop acting as in §4.5. Wind-down lets the current step end and releases
  input.
- **Limits.** Per batch: steps (default 200), wall time (default 2 min), typed characters, and waits (each needs a
  timeout). A batch that would act in an app whose rule is not `act` is refused **before** it starts (the engine checks
  the whole list first), and each step is checked again when it runs.
- **Logged:** every batch and step (app, target, action, values typed except into fields the policy marks sensitive,
  result, timing), every take-over and refusal.

### 6.6 Invisible by default (a tested requirement)

**Owner decision (2026-11-13):** everything the agent runs on the computer (commands, scripts, batches, adapters,
helpers) runs in the background: no window is shown, none comes to the front, focus is never taken, and no console
flashes. Only UI work you asked for touches the visible screen. **Requirement: no window ever pops in front of you
unless a step you asked for put it there.**

**Visible steps are declared.** A `host_do` batch may contain visible steps (`input`; `window` focus, move, launch
shown; `el` actions that need the window in front; `app` verbs marked `visible`) only if the batch says
`visible: true`. That flag is the agent saying "the person asked for UI work"; the computer cannot check the asking
itself, so the flag also needs the app's `act` rule, turns on the indicator (§6.5) and is logged. Without it, visible
steps are refused before the batch starts, and every other step runs off-screen.

**How each part stays off-screen:**

| Part                                         | Windows                                                                                                                                                                                                                                                                                                                                                               | macOS                                                                                                                                                                                      | Linux                                                                                                                                      |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `host_run`, delegated agents (agent account) | the account's T3 runs from a "whether signed in or not" task, so its processes live in a non-interactive window station and **cannot** draw on your desktop; children are started with `CREATE_NO_WINDOW` anyway (so the default-terminal setting never opens Windows Terminal), a pseudo-console (ConPTY, `conhost --headless`) only when a program needs a terminal | a LaunchDaemon has no GUI session; processes cannot reach the window server                                                                                                                | no `DISPLAY`, `WAYLAND_DISPLAY` or session bus is passed; the unit has no access to your session's sockets                                 |
| engine and helper (as you)                   | engine is a GUI-subsystem binary with no window; every child: `CREATE_NO_WINDOW` for console programs, `STARTF_USESHOWWINDOW` + `SW_HIDE` for GUI ones; PowerShell `-NoProfile -NonInteractive -WindowStyle Hidden` started without a console, never through `cmd /c start`                                                                                           | engine and helper are `LSUIElement` / background-only; apps opened with `open -g -j` (background, hidden) or Launch Services with "don't activate"; scripts use `launch`, never `activate` | apps started without an activation token (`xdg-activation` on Wayland; no `_NET_WM_USER_TIME` on X11), so compositors refuse to focus them |
| automation adapters (§5 route 2)             | COM servers created with `Visible = false` / `DisplayAlerts = false` where the app has them (Office and similar); WinRT and Shell verbs that need no UI                                                                                                                                                                                                               | Apple Events to a running app or a hidden launch; scripting dictionaries without `activate`                                                                                                | D-Bus calls, no window                                                                                                                     |
| browser work                                 | the agents' browser profile runs headless unless the batch is `visible`                                                                                                                                                                                                                                                                                               | same                                                                                                                                                                                       | same                                                                                                                                       |
| element actions in a background window       | UIA patterns (Invoke, Value, …) work on windows that are not in front; the engine does not call `SetForegroundWindow`                                                                                                                                                                                                                                                 | AX actions work without raising the window (`AXRaise` is never sent unless visible)                                                                                                        | AT-SPI actions work without focus                                                                                                          |

**Watched while it runs.** While a non-visible batch or `host_run` from the agent runs, the engine watches for new
shown top-level windows and for foreground changes (Windows `SetWinEventHook` for `EVENT_OBJECT_SHOW` and
`EVENT_SYSTEM_FOREGROUND`; macOS `NSWorkspace` activation notifications plus the window list; X11 `MapNotify` and
`_NET_ACTIVE_WINDOW`; Wayland: per compositor, to verify). A window shown by a process the agent started: the engine
hides it, gives the front back to the window you had, logs it, and the step's result carries `visible_side_effect`
with the program name, so the agent (and the next recipe version) learns to start it another way. A program that keeps
showing windows when hidden is stopped and the step fails with `visible_side_effect`. Windows from your own programs are
never touched.

**Tested.** A "no pop-in" test runs on each OS in fork CI (where a runner with a desktop session exists) and before an
OS is enabled: with a test window in front and focused, it runs every host tool, every adapter verb, a set of console
and GUI programs (including ones that try to show a window, a console program under Windows Terminal as the default
terminal, PowerShell, a script interpreter) and every saved test recipe, while the same watcher records events. Any
shown window or focus change not caused by a step marked visible fails the test. Each new adapter verb or bridge
must pass it before it ships.

## 7. Batches, waits and recipes

### 7.1 The step list

`host_do` takes a JSON list of steps (§6.3), not a programming language, so the engine can check the whole list against
the policy before it starts and the log shows exactly what ran. Each step can name an `id`, a `timeout`, an
`onFail` (`stop` by default, `continue`, or `goto` a later step), and can bind a result to a name that later steps use
(`${found.id}`). There are no loops beyond `wait` and a bounded `repeat` (count cap).

**Conditions** for `wait`, `assert` and `goto`: an element exists / is gone / has a value or state (by id or selector);
a window with a title or app is open / in front / closed; a process runs / exits; a file exists / changed; the clipboard
matches; a bridge read verb's result matches a JSON path; any of / all of these.

**Result:** per step `ok` / `failed` / `refused` / `stale` / `interrupted_by_user` / `skipped`, its outputs, its time;
then a diff of the touched windows since the batch started (or since the snapshot id given), and crops only if asked.

Example (placeholders): export a report from an app and wait for the file, in one call:

```json
[
  { "app": "open", "app_id": "<app>", "path": "<allowed folder>/report.src" },
  { "wait": { "window": { "title~": "report" } }, "timeout": 5000 },
  { "el": "invoke", "selector": "role=menuitem name=\"Export…\"" },
  { "wait": { "element": "role=dialog name~\"Export\"" }, "timeout": 3000 },
  {
    "el": "set_value",
    "selector": "role=edit name=\"File name\"",
    "value": "<allowed folder>/report.pdf"
  },
  { "el": "invoke", "selector": "role=button name=\"Save\"" },
  { "wait": { "file": "<allowed folder>/report.pdf" }, "timeout": 10000 }
]
```

### 7.2 Idempotency

Every `host_do` carries an idempotency key. The engine journals each step (planned, started, done, with its result)
before and after it runs. A retry with the same key returns the journalled result and never runs a done step again; a
step that was started but not confirmed is reported as `unknown` and is not re-sent (idea from agentcast, #28050). The
agent decides from a fresh diff.

### 7.3 Recipes

A recipe is a **named, parameterised step list** saved on the computer, for example `<app>.export-pdf(path)`.

- **Save.** An agent that found a working way saves it with `host_recipe save` (name, description, parameters, steps,
  the app and its version). The helper stores it (the agent account cannot write the store directly), versioned, with a
  content hash; saving the same content again is a no-op.
- **Run.** `host_recipe run` or a `recipe` step: the same policy checks as a fresh batch, at engine speed, one network
  hop. Recipes use selectors, not element ids.
- **Repair.** When a step fails because the app changed, the run stops there and returns the diff; the recipe is marked
  `needs repair` until a new version passes.
- **Review.** Recipes show in the tray's policy view with their steps; you can delete or pin them. A recipe never widens
  the policy: it can do only what a batch could.
- Recipes are per computer in v1; sharing them between computers is later.

### 7.4 Speculative parallel batches

**Owner decision (2026-11-13):** when the agent is unsure which of two or more ways works (which command finds the
file, which selector the app uses, which build flag is right), it may run the alternatives **at the same time** and
keep the one that works, instead of trying them one model turn at a time.

**Shape.** `host_do { alternatives: [steps, steps, …], pick, check }`, at most 4 alternatives (setting), one
idempotency key for the whole call.

**Which batches may run speculatively.** The engine classes every step before anything starts and refuses the call if
any alternative has a step outside these classes:

1. **read-only:** `observe`, `wait`, `assert`, element and bridge **read** verbs, `app` read verbs, file reads, and
   `run` steps marked read-only (they get a read-only view, below);
2. **writes into the alternative's own sandbox:** each alternative gets a private copy of the folders it names
   (copy-on-write clone where the file system has one: ReFS block clone, APFS `clonefile`, btrfs/XFS reflink; otherwise
   a hard-link copy that breaks links on write), and its `run` and file steps see only that copy;
3. **snapshot-covered writes to real allowed folders:** only when the alternatives declare **disjoint** write paths; a
   snapshot is taken first (§4.3) and the losers' paths are restored from it. Option 2 is preferred; option 3 is for
   folders too large to copy.

**Never speculative:** `input`, element and bridge **act** verbs, `window` and `clipboard` changes, `app` act verbs,
`host_request_install`, `host_request_admin`, `host_named`, approved scripts, and anything that sends over the
network. Speculative `run` steps get **no network** (Linux: a fresh network namespace; macOS: a sandbox profile that
denies network; Windows: an AppContainer with no network capability, to verify), so a command that needs the network
cannot be run speculatively and the agent must run it as a normal batch.

**Picking the winner and comparing results.**

- `pick: "first"` (default): the first alternative whose steps all end `ok` **and** whose `check` holds wins. `check`
  is a §7.1 condition evaluated against that alternative's own results and sandbox (for example "file X exists and
  contains Y", "exit code 0 and output matches Z"). With no `check`, "all steps ok" is the test. Ties (same
  millisecond) go to the earlier one in the list.
- `pick: "all"`: every alternative runs to its end or the batch timeout, and the result lists each with its outcome, a
  short output digest and the diff of its sandbox against the start, so the agent compares them in one look. Sandboxes
  are kept until the agent applies one (`host_do { apply: <alternative> }`) or 10 minutes pass.
- The result always says which alternatives ran, which won, why each loser lost (`failed`, `check_false`,
  `cancelled`, `timeout`), and the time each took.

**Cancelling and cleaning up losers.** As soon as a winner is known (`first`) or the agent applies one (`all`):

1. each loser's whole process tree is ended through its job object / process group / cgroup: a polite stop, then a
   hard kill after 2 s;
2. its sandbox copy is deleted; for snapshot-covered writes its declared paths are restored from the pre-call snapshot;
3. its waits, watchers and cached trees are dropped; each loser is logged as `cancelled_speculative`.

**Applying the winner.** Read-only winners just return their results. A sandboxed winner's changes are applied to the
real folders as one write burst: snapshot first, then each file compare-and-swapped against the hash it had when the
sandbox was made. If any real file changed meanwhile, nothing is applied and the result is `conflict` with the list.

**Cost guard.** Alternatives run at below-normal priority and share one set of batch limits (steps, wall time). The
runner levels of §11 do not apply to them, since they are work you asked for, but the number of speculative processes
on the computer at once is capped (default 8).

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
| element action dispatch (invoke, set value)               | ≤ 10 ms                                                   | ≤ 40 ms    | plus the app's own reaction        |
| bridge verb, local                                        | ≤ 5 ms                                                    | ≤ 30 ms    | plus the bridge's own work         |
| synthetic input event                                     | ≤ 2 ms                                                    | ≤ 10 ms    | typing 1,000 characters ≤ 150 ms   |
| wait reacting to its condition                            | ≤ 30 ms                                                   | ≤ 60 ms    | event-driven; 25 ms poll otherwise |
| crop + scale + encode screenshot (1280 px edge)           | ≤ 40 ms                                                   | ≤ 100 ms   | only when asked                    |
| observation size                                          | diff ≤ 1.5k tokens typical; window snapshot cap 6k tokens |            | screenshot about 1-1.5k tokens     |

**Task level:** the model turn (seconds) is the cost that matters. Target: a typical UI task in **1-3 model turns**
(observe, one batch, sometimes one fix), against 10-40 turns for classic computer use.

### 8.2 How it is measured

- **Engine micro-benchmarks** (no model): every item in §8.1 against stock OS apps per OS and test apps the fork ships
  (a small accessibility-rich app, a canvas app with a test bridge, a test web page). They run in fork CI on each OS
  where a runner with a desktop session exists, nightly; a p95 more than 25 % worse than the last baseline turns the
  job red.
- **Task benchmark** (with a model, run by hand or weekly, since it costs tokens): about 20 general tasks (change a
  setting, fill and save a form, export a file from an app, find a value in a long list, a multi-page web flow, read and
  act on a canvas app through its test bridge), each in three modes on the same computer, model and link:
  1. **native**: everything in §5;
  2. **tree only**: no adapters, bridges or recipes;
  3. **pixels**: screenshots and synthetic input only, one action per model turn: the classic computer-use baseline.
     Recorded per task: success, wall time, model turns, input and output tokens, and the engine's own times. Results go
     in a table in the PR that ships the engine, and the targets above are adjusted from them.

### 8.3 Why not classic computer use

Classic computer use (Anthropic's computer-use toolset, Codex Computer Use, Cua-style drivers attached as one tool per
action) has the model act like a person: take a screenshot, pick a pixel, click, take another screenshot. Each action
costs a full model turn plus an image. In practice that is about **one action every 3-6 seconds** (owner's measure:
about 5 s), and a 20-action task is 20 turns, 1.5-2 minutes and roughly 20-30k image tokens, before any mistake.

The same task in this design: one observe (a text snapshot, a few thousand tokens), one `host_do` batch that the
computer runs in well under a second plus the app's own time, one check of the diff: **2-3 model turns, about 10-20
seconds**, a fraction of the tokens, and exact element targets instead of pixel guesses. Pixels stay available as the
last route (§5.1), with the same safety rules, for the apps that have nothing better.

The §8.2 benchmark checks this estimate before the owner relies on it.

## 9. Tools

All take `environmentId` (the computer) and are logged. Read tools work over a read-only link; the others need write
access on the link and a non-plan caller (upstream checks both).

| Tool                                                   | Kind  | What it does                                                                                                                                         | Limits                                                                       |
| ------------------------------------------------------ | ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| `host_status`                                          | read  | online, paused or winding down, availability, policy hash, allowed folders, reach-check finds, snapshot and log state, link expiry, discovery (§6.2) |                                                                              |
| `host_list`, `host_stat`, `host_read`, `host_search`   | read  | files in allowed and read-only folders; a secret shows as name + `secret` only                                                                       | 1 MB per read, 2,000 entries, search 20 s                                    |
| `host_write`, `host_mkdir`, `host_move`, `host_delete` | write | in read-write folders; compare-and-swap on the old hash for writes                                                                                   | snapshot before the first write of a burst                                   |
| `host_run`                                             | write | a program with arguments, a working folder in an allowed folder, a timeout, as the agent account                                                     | output 64 KB tail, redacted                                                  |
| `host_undo`                                            | write | list changes since a time; restore files from the snapshot before it                                                                                 | only inside allowed folders                                                  |
| `host_observe`                                         | read  | windows and focus; snapshots or diffs per window from the best source; optional crops; clipboard if asked (§6.2)                                     | per-app `see`; refusals of §6.5                                              |
| `host_do`                                              | write | a step list run by the engine (§7), or speculative alternatives (§7.4)                                                                               | per-app `act`; acting switch; batch limits; take-over; `visible` flag (§6.6) |
| `host_recipe`                                          | write | list, show, save, run, delete recipes (§7.3); list and show work over a read-only link                                                               | run = same checks as `host_do`                                               |
| `host_request_install`, `host_request_admin`           | write | the pop-up of §9.1                                                                                                                                   | one at a time                                                                |
| `host_named`                                           | write | a named command from the policy, run as you (§9.1)                                                                                                   |                                                                              |

Nine tools (plus the file group). The file and run tools exist so agents on another machine need no full agent on the
computer. A delegated agent there uses its own tools under the same account and rules, and the same `host_observe`,
`host_do` and `host_recipe` through the local channel.

### 9.1 Pop-ups: only what no rule can make safe

1. **Installs.** `host_request_install` names a package (winget / Homebrew / the distro's package manager) and a
   reason. The helper shows what will be installed, from where and for whom; on OK it installs as **you**, and Windows
   UAC or the OS password prompt follows when it is machine-wide. Installing tools for the agent account only (into its
   own folders) needs no pop-up.
2. **Anything that needs admin.** `host_request_admin` with the exact command. Same pop-up, then the OS's own
   elevation prompt.

The pop-up shows facts first (package or command, source, size, what it changes), the agent's reason last and marked
unverified, Deny as the default, one at a time, refused after 60 s.

**Named commands and approved scripts** are entries in the policy, run by the helper as you, with output scrubbed of
every secret value: things that need **your** secrets (a deploy with your SSH key) or your session (an approved
PowerShell or AppleScript snippet). They need no pop-up because you wrote or approved them.

## 10. Network, auth and link renewal

- All traffic goes over the linked environment: upstream's auth, routes and revocation. Tailscale is the expected route
  between machines; the computer's T3 service answers on loopback, LAN and tailnet only (upstream behaviour).
- Revoking the link on the computer (Sharing & access) ends access at once.
- The agent account's outgoing network: internet open (builds, git, package downloads), your local network blocked
  except the linked machine (§3).

**Link expiry, what upstream does today** (upstream `main` 8cd8a3ca5a):

- A peer link signs in with OAuth `authorization_code` against the other side's MCP OAuth
  (`apps/server/src/peer/PeerLinks.ts:308`, token request at `:379`) and stores `expiresAt = now + expires_in`
  (`PeerLinks.ts:536`).
- The token is an MCP-client session with a fixed 30-day lifetime (`apps/server/src/auth/EnvironmentAuth.ts:79`,
  issued at `:1347`, called from `apps/server/src/auth/McpOAuth.ts:546`; `expires_in` at `McpOAuth.ts:564`).
- The MCP OAuth server supports only `authorization_code` (`McpOAuth.ts:97`) and refuses every other grant
  (`McpOAuth.ts:510-511`): **there is no refresh token and no renewal.**
- After 30 days the link is `expired` (`PeerLinks.ts:593`, `:645-648`: "Link it again with a new pairing code"), and the
  linking side checks its own stored expiry before every call (`apps/server/src/peer/PeerLinkRequests.ts:106-112`).
  `PeerMcpClient.ts:215` renews only the MCP protocol session id, not the sign-in.
- Upstream issues and PRs: issue #33183 (open: sessions expire after a hard-coded 30 days; asks for a setting or sliding
  renewal; many reports), issue #9884 (open: session lifetimes, no renewal for direct clients), PR #31755 (closed
  unmerged: a TTL setting for pairing-redeemed sessions), PR #26757 (open: Tailscale identity sign-in with no pairing
  code, for clients, in `apps/server/src/environment/TailnetPeers.ts`; it does not touch peer links), PR #25024 (open:
  the desktop app renews its own local session only). **No open upstream PR adds renewal of peer links.**

**Fork option, thin and upstream-shaped:**

1. **Refresh while in use (default).** The computer's MCP OAuth also issues a **refresh token** for peer-link clients
   (standard OAuth `refresh_token` grant, RFC 6749 §6), stored on the agent machine in the same secret store as the
   link token. The agent machine refreshes when the link has under 7 days left **and** was used in the last 7 days,
   so a link renews only while both sides are online and in use. Each refresh rotates the refresh token; the
   previous one is accepted once more within 60 s (a lost reply must not break the link). Revoking the link revokes
   both tokens. A link idle for its whole lifetime still expires, as today. The client side follows the same pattern
   as upstream's own cloud CLI refresh (`apps/server/src/cloud/CliTokenManager.ts:503`). Written as a small change to
   `McpOAuth.ts` and `PeerLinks.ts` that upstream could take; if upstream ships renewal for #33183, we drop ours.
2. **Longer lifetime setting.** A server setting on the computer, `peerLinkSessionDays` (default 30, up to 365),
   for the MCP-client sessions it issues to peer links. Simplest fallback; it does not renew.

`host_status` and the tray warn 3 days before a link expires, and say whether it will renew.

## 11. Activity-aware runners (runner side, not host tools)

The PC also runs background work moved there from the server: CI jobs on fleet runners (#13), builds, other project
runners. That work must **scale itself down and back up** from what you are doing, so you never change CI load by hand.
Host tools do not use this (§4).

**Signals** (the activity service, inside the helper in your session, reads them every 2 s):

| Signal                  | Windows                                                          | macOS                                                      | Linux                                                             |
| ----------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------- | ----------------------------------------------------------------- |
| full-screen / game      | `SHQueryUserNotificationState` (D3D full screen, presentation)   | front window covers its screen; presentation options       | `_NET_WM_STATE_FULLSCREEN` on X11; compositor-specific on Wayland |
| GPU busy                | PDH "GPU Engine" counters                                        | IOKit accelerator performance statistics                   | NVML, or `gpu_busy_percent` in sysfs                              |
| CPU and memory pressure | PDH processor and memory counters, minus runner processes        | `host_statistics`, memory pressure, minus runner processes | PSI (`/proc/pressure/*`) and cgroup stats, minus the runner slice |
| your input              | `GetLastInputInfo`                                               | `CGEventSourceSecondsSinceLastEventType`                   | XScreenSaver idle on X11, `ext-idle-notify-v1` on Wayland         |
| named heavy apps        | process list against the policy's list (games, editors, renders) | same                                                       | same                                                              |
| your override           | tray: full / normal / off for 1 h, 4 h, until tomorrow           | same                                                       | same                                                              |

**Levels.** `full` (you are away or idle: all slots), `light` (you are active with light load: 1 slot, lowest
priority, CPU and memory capped), `off` (a game, full screen, or heavy load: no new jobs). Down at once (within one
read), up only after the lower load has held for 5 minutes, so it does not flap. If the activity service stops
reporting while someone is signed in, runners treat it as `off` and the tray says why; with nobody signed in, `full`.

**Limits that hold at every level:** runner processes run at low priority and under a cap (Windows job object CPU
rate and memory limit, `BELOW_NORMAL`/EcoQoS; macOS `taskpolicy -b`; Linux `nice`, `cpu.weight`, `cpu.max`,
`io.weight`, `memory.high` on the runner slice).

**How runners follow it.** The service writes `activity.json` (`at`, `level`, `reasons`, `until`) where only you write
and the runner dispatcher (system) reads. For fleet runners (#13) the dispatcher's heartbeat already reports
`free` slots and `auto` routing sends jobs only to pools with a free slot (`.github/fork-fleet.md`); the dispatcher
caps `free` at the level's slots, so new jobs go to the server pool or GitHub-hosted runners with no config change.
Other runner kinds read the same file.

**Stopping a running job without loss** when the level drops below what is running. Each job kind declares one
behaviour in the runner config (general, per job, with a default):

1. **finish** (default when the job is expected to end within a short time from its history, or is in a step marked
   not restartable): it keeps running, throttled to the `light` cap (or lower) until done.
2. **checkpoint**: the job gets a stop signal with a grace time to save its progress (a job declares it can), and its
   next run resumes.
3. **requeue elsewhere** (default for pure jobs such as tests): the dispatcher writes the job to a requeue ledger,
   cancels it, and asks for a re-run of that run attempt routed away from this pool (the #13 sweep already re-runs
   cancelled fleet jobs GitHub-hosted). Jobs with outside effects (deploys, releases) never run on a personal PC.

Suspending a job (freezing its processes) is not used for CI jobs: a frozen runner loses its connection and the job
fails; it is an option only for local jobs with no connection to keep.

**Wind down** (§12) for runners is "level `off`, then finish / checkpoint / requeue every running job".

## 12. Tray icon and wind down

One tray icon per computer, from the session helper, covering host tools and runner work: in the Windows notification
area (it may sit in the hidden-icons overflow by the clock; first run tells you how to pin it), the macOS menu bar, and
the Linux status area (StatusNotifierItem / AppIndicator; GNOME needs the AppIndicator extension, to verify). Built on
Electron's `Tray`; upstream's desktop app has no tray today.

**The menu:**

- **Status:** host tools (idle, working, acting in _app_, paused, winding down, safe to shut down), link expiry,
  runners (jobs running, level and why).
- **Pause / Resume.**
- **Wind down.**
- **Kill switch** (asks once: "stop every agent process on this computer now?").
- **Runner level override** (§11), open log, open policy, recipes, bridges waiting to be allowed.

**Wind down, step by step:**

1. The state file says `winding_down` (with the time and what was running). From now on nothing new starts: host tools
   and new delegated turns are refused with "winding down", runners take no new jobs.
2. Running work goes to a safe point: `host_run` jobs run to their end or timeout; a running batch finishes its current
   step, releases input and stops; delegated turns on the computer finish their current turn (upstream's
   `untilDelegatedWorkSettled` wait, `apps/server/src/peer/PeerForwarding.ts:211`, tells when); runner jobs finish,
   checkpoint or requeue (§11). A snapshot is taken of allowed folders changed since the last one.
3. When nothing is left, or after the deadline (default 10 minutes, setting): the tray shows **"Safe to shut down"**,
   with a list of anything that could not stop cleanly (what, why, and the choice: wait longer or kill it).
4. Wind-down ends when you choose Resume, or at the next sign-in after a restart, back to the state before it (unless
   "stay paused after restart" is ticked).

## 13. Speed of the parts, and language

- **Engine: one native binary per OS, in Rust** (recommendation). Speed first: the tree cache, event handlers, batches
  and input need direct OS calls with no runtime start or bridge in between (UI Automation with cache requests,
  AXObserver, AT-SPI over D-Bus, `SendInput`/`CGEvent`/libei). Rust has maintained bindings for all three (Windows
  crates, Apple frameworks, D-Bus and AT-SPI) and one language serves every OS. Alternatives: C# (Windows-strong,
  weaker on macOS accessibility), Go (fine, but its C interop for COM and AppKit is more work), TypeScript in the
  Electron process (slowest for UIA and blocks the helper). Cost: a Rust build per OS in fork CI.
- **Helper, toolkit and core:** TypeScript inside T3 and the desktop app's main process, as before. No new runtime to
  install for the person.
- **Per call:** one round trip over the link, plus the engine's own time (§8.1). Today a link whose first address is
  silent costs 10 s per call (upstream #35519), so the computer's saved routes list the tailnet address first.

## 14. Threat model and what still leaks

| Threat                                   | What stops it                                                                                      | What is left                                                                                             |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Prompt injection from files it reads     | the account's reach, secrets denied, no admin, undo, log, pause, kill switch                       | it can change files in read-write folders and send what it read anywhere on the internet                 |
| Prompt injection from what is on screen  | per-app `act`, built-in refusals, batch checked before it runs, indicator, take-over, log          | in an `act` app it can do what you could there (send a message, press a button)                          |
| Acting as a way around folder rules      | per-app rules; unknown apps not `act`; acting off by default                                       | an `act` app that opens files can reach your files with your rights; choose `act` apps with that in mind |
| A command changes T3's own rules         | rules that must hold are enforced by the OS, the engine, the helper or guard tasks, not the server | pause is server- and helper-side; the kill switch is not                                                 |
| Clicking its own permissions wider       | the helper's, tray's and T3's host-tools screens are refused for seeing and acting                 |                                                                                                          |
| Reading your other files                 | OS permissions; the install reach check                                                            | folders every user may read that you chose to keep                                                       |
| Secrets inside allowed folders           | built-in patterns + deny entries + the watcher                                                     | a secret in an unknown format or ordinary file name; about one second after you create a matching file   |
| Passwords on screen                      | password fields never read or typed into; password managers refused; `hidden` apps blacked out     | a password shown as plain text in an ordinary field of a `see` app                                       |
| Damage to allowed folders                | snapshots + change log + `host_undo`                                                               | edits between two snapshots; changes inside apps that do not save to allowed folders                     |
| Abuse of the engine's channel            | pipe/socket ACL, typed verbs, no free-form scripts as you, its own checks and log                  | any agent-account process can ask it for what the policy allows                                          |
| A fake bridge                            | only bridges listed in the policy are used                                                         | a program already running as you could pretend to be a listed bridge; it could do worse anyway           |
| Stolen link session on the agent machine | revocation on the computer; access ceiling; refresh only while in use                              | until revoked, whatever the link allows                                                                  |
| Network misuse from the agent account    | LAN blocked for the account                                                                        | internet is open: anything readable can leave                                                            |

**What still leaks, plainly:** files in allowed and read-only folders, and what `see` apps show (trees, values,
screenshots), can reach the model provider and the internet. Everything every user may read on the computer (after the
reach check, only what you accepted). Process names and window titles of all apps. Data read goes into transcripts.

## 15. Failure table (state the feature changes)

What happens when it runs again, stops half-way, or meets old or empty state:

| Step that writes                         | Crash or stop half-way                                                                                                                                                                                                                                                                                              | Run again                                                                                                                                                          | Old / empty / half-written state                                                                                                                                                        | Test                  |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| install: create account                  | account exists, nothing else                                                                                                                                                                                                                                                                                        | finds it, keeps it, re-checks group membership (refuses if it is an admin)                                                                                         | an account of that name that is an admin: refused                                                                                                                                       | fake OS layer         |
| install: folder permissions              | some folders granted                                                                                                                                                                                                                                                                                                | re-applies all (idempotent); removes grants for folders no longer listed                                                                                           | a folder deleted since: reported, skipped                                                                                                                                               | fake ACL layer        |
| install: service task                    | task exists, not started                                                                                                                                                                                                                                                                                            | replaced with the same definition                                                                                                                                  | an upstream "as me" task for the same T3 home: refused with the reason                                                                                                                  |                       |
| policy edit (helper)                     | write to temp + atomic rename                                                                                                                                                                                                                                                                                       | new policy or old one, never half                                                                                                                                  | unreadable policy: server and engine refuse all write and acting tools and say why                                                                                                      |                       |
| `host_write`                             | temp beside target, target unchanged                                                                                                                                                                                                                                                                                | same content: OK; other content: compare-and-swap refuses                                                                                                          | target changed since read: refused                                                                                                                                                      |                       |
| snapshot request                         | snapshot taken or not; log line says which                                                                                                                                                                                                                                                                          | next request within 5 min reuses it                                                                                                                                | snapshot store full: writes and runs go on, `host_status` red                                                                                                                           |                       |
| `host_undo`                              | some files restored                                                                                                                                                                                                                                                                                                 | re-run restores the rest; already-restored files compare equal                                                                                                     | snapshot expired: refused with the oldest available time                                                                                                                                |                       |
| log line                                 | line before the action and after; a call whose log write fails is refused                                                                                                                                                                                                                                           |                                                                                                                                                                    | half line at the end: closed with a `truncated` marker, chain continues                                                                                                                 |                       |
| `host_do` batch                          | journal says which steps are done; a started-but-unconfirmed step is `unknown`                                                                                                                                                                                                                                      | same idempotency key: journalled results, no step re-runs; a new key is a new batch                                                                                | journal from before a restart: kept 24 h for retries, then dropped; empty step list: `ok`, nothing run                                                                                  | fake engine back end  |
| **you grab the mouse/keyboard mid-step** | step stops, all held keys and buttons released, queued steps dropped, `interrupted_by_user` with what ran and a diff                                                                                                                                                                                                | a new acting batch waits for your idle time or "let the agent continue"; never resumes by itself                                                                   | a take-over during a `wait`: the wait ends as interrupted too                                                                                                                           | input hook fake       |
| **engine or helper dies mid-drag**       | the engine writes every key/button it presses to a held-input file before sending it; the helper (or the engine's next start) sends the matching ups, then deletes the file; the indicator goes with the dying engine and a "control ended unexpectedly" note shows                                                 | after restart the batch is `unknown` from the journal and is not re-sent                                                                                           | a held-input file from before a reboot: dropped (the OS has reset input); none: nothing to release                                                                                      | kill test per OS      |
| **rerun of a half-done UI action**       | the journal marks it `unknown`                                                                                                                                                                                                                                                                                      | same key: returns `unknown` with a fresh diff, does not re-send; a new batch built on an old snapshot fails as `stale` if the target window moved or changed focus | element id gone: `stale`; recipe selector no longer matches: stops there, recipe marked `needs repair`                                                                                  |                       |
| **speculative batch**                    | the journal lists each alternative's sandbox path and declared write paths **before** they are made; after a crash, on restart, every listed sandbox is deleted and snapshot-covered paths are restored; a winner whose apply did not finish is re-applied file by file (compare-and-swap makes done files a no-op) | same key: the journalled winner and outcome, nothing re-runs; a key whose alternatives never finished: `unknown`, sandboxes cleaned                                | sandbox folders with no journal entry (older engine, manual copy): left alone and reported; a real file changed during the run: `conflict`, nothing applied; zero alternatives: refused | fake engine + fake FS |
| **window pops in front** (§6.6)          | the engine hides it and gives the front back; if the engine dies first, the window stays and the next start logs it                                                                                                                                                                                                 | the same program again is started the other way the result told the agent; a repeat offender is stopped                                                            | a recipe that was fine on an older app version and now shows a window: `visible_side_effect`, recipe marked `needs repair`                                                              | no-pop-in test        |
| recipe save                              | temp + atomic rename into the store                                                                                                                                                                                                                                                                                 | same content: no-op (content hash); new content: new version                                                                                                       | a recipe for an older app version: runs, and a failing step marks it `needs repair`; empty store: fine                                                                                  |                       |
| pause / kill switch                      | file present or account disabled                                                                                                                                                                                                                                                                                    | idempotent                                                                                                                                                         | kill switch with no agent processes: OK                                                                                                                                                 |                       |
| **wind down**                            | the state file (`winding_down`, start time, items) is written first; after a helper crash it is read back and wind-down continues; items already requeued are in the ledger and not requeued again                                                                                                                  | pressing it again shows progress, starts nothing new                                                                                                               | a `winding_down` file older than the last boot: cleared at sign-in (back to the state before, unless "stay paused"); nothing running: "safe to shut down" at once                       | fake clock + fakes    |
| **runner scale-down**                    | the requeue ledger line (job, run, attempt) is written before the cancel; on restart, ledger lines without "re-run asked" get it; a job already re-run (newer attempt exists) is marked done                                                                                                                        | same level again: no-op; flapping held off by the 5-minute rise; a second drop does not requeue the same attempt twice                                             | `activity.json` stale or unreadable while someone is signed in: `off` with a tray note; unknown format: `off`; no jobs: only the heartbeat changes                                      | fake dispatcher       |
| link refresh                             | the computer rotated the refresh token but the agent machine did not save the new one: the old one works once more within 60 s                                                                                                                                                                                      | refresh with the same token inside 60 s returns the same new pair                                                                                                  | a link made before this feature has no refresh token: it expires as today, with the 3-day warning                                                                                       | fake clock            |
| availability "off until"                 | setting saved                                                                                                                                                                                                                                                                                                       |                                                                                                                                                                    | time in the past: treated as on; bad value: off                                                                                                                                         |                       |

## 16. Tests (when built)

- Core on Linux CI with a fake OS layer: policy checks (files and per-app), secret patterns (a hand-written fixture of
  expected secret paths per pattern, so weakening a pattern turns a test red), step-list checks and limits, journal and
  idempotency, recipes, snapshot diffs, log chain, pause, wind-down, availability, undo index, link refresh.
- The toolkit through upstream's MCP test kit, including a forwarded call over a test link and a read-only link
  refusing write tools.
- Per-OS layers: a real run on each OS before that OS is enabled (Windows first): the account cannot read a file in
  your profile or write outside allowed folders or read a denied `.env`; the kill switch ends its processes; a snapshot
  restores a deleted file; LAN blocked; refusals (lock screen, password field, own policy screen); take-over releases
  held input; the §8.2 micro-benchmarks; the **no pop-in test** of §6.6 (required, blocks enabling an OS).
- Speculative batches: classing refuses every never-speculative step kind; a loser's process tree and sandbox are gone
  after the pick; a crash between sandbox creation and cleanup leaves nothing behind after restart; a speculative `run`
  cannot reach the network; a conflicting real-file change applies nothing.
- Runners: the level logic with a fake clock and fake signals; finish / checkpoint / requeue with a fake dispatcher.

## 17. Costs and open points

- **The beta switch.** Linked environments is an upstream beta, off by default, and it must be on in **both** T3
  installs (agent machine and computer). Off on either side refuses link calls.
- **MCP tool-list size.** Upstream measures the tool list in CI (closed PR #16049 tripped it). The host tools add
  several KB, so they are **hidden unless at least one linked computer reports host tools on**, and then deferred
  behind `tool_search`.
- **Two T3 installs on the computer.** If you also use the desktop app there, it keeps its own local environment (or
  none). The agent account's T3 is a separate install with its own data.
- **Provider logins for delegated agents.** Only needed if full agents run on the computer; host tools need none.
- **A new native component** (the engine) with a Rust build per OS in fork CI.
- **Wayland** input needs the RemoteDesktop portal, which asks the person once per session on some desktops; to verify
  per desktop.

**Open for the owner** (first one first):

1. **Acting: how far?** Off, `elements` (element and app actions, no synthetic mouse/keyboard), or `full` (synthetic
   input too, for games and canvases without a bridge)? Recommendation: `full`, but per app, default off until you
   turn it on in the policy.
2. Unknown apps: `see` (recommended) or `hidden`?
3. After you take over: resume after 5 s of idle (recommended), another time, or only when you press "let the agent
   continue"?
4. Link renewal: refresh while in use (recommended), the longer-lifetime setting, or both?
5. Wind-down deadline (10 minutes?) and whether to offer "shut down when safe".
6. Runner levels: the slot count and caps for `light`, and the default stop behaviour per job kind.
7. The default read-write and read-only folders; the snapshot size cap and days; whether the agent account's internet
   should be limited further.
8. Speculative batches: the caps (4 alternatives per call, 8 processes per computer) and whether snapshot-covered
   writes to real folders (§7.4 class 3) are allowed at all, or sandboxes only.

**Follow-up research (named here, not done in this design):** which command languages and tools agents should use on
each OS for speed and reliability, general across projects: whether agents should write Python at all; how good the
Python, PowerShell and shell they write is in practice; built-in commands versus scripts; and a curated, fast toolset
per OS (startup time, output size, error behaviour) that `host_status` could advertise to agents. The result feeds the
tool descriptions and the recipe guidance.

## General vs project

Everything in this design is general. Machine names, addresses, accounts, folders, apps, games, bridges, recipes and
named commands are local policy and settings on each install; examples use placeholders only.
