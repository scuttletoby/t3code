# One machine for new work (an "anchor"), with upstream load balancing

Fork note. No fork setting is needed: upstream's load balancing already expresses "start new threads of a shared
project on this machine". It does not cover threads without a project yet (see below).

## Set it up (per client: web, desktop)

1. **Settings → Environments → Load balancing**: turn on **Automatically balance load**.
2. Set the machine that should take new work to **Prefer**.
3. Set every other machine to **Manual only**. A machine you add later starts at **Normal** and joins automatic
   selection, so set it to **Manual only** as well.

New threads in a project that several machines share then start on the preferred machine.

## Threads without a project: not covered yet

Until upstream fixes its issue #31048, threads without a project ignore the **Prefer** and **Manual only** weights.
They start on the primary machine whenever it offers a "No project" folder, even when it is set to **Manual only**;
only when it offers none do they start on the machine you start them from. The composer offers no **Auto balance**,
and **Send** goes to that machine without a warning. To use another machine, pick it in the composer; its "No
project" folder opens first.

The fix exists as upstream PR #31049. The fork does not carry it yet, because it conflicts with upstream code that
is already released (upstream PR #35658). It is parked in
[scuttletoby/t3code#43](https://github.com/scuttletoby/t3code/pull/43) until upstream updates or merges it.

## What happens when (threads in a shared project)

| Situation                                                                                                                                     | Result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| --------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Preferred machine connected, with CPU and memory free                                                                                         | The draft shows **Auto balance** and moves there.                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| Preferred machine offline, busy (CPU at 95% or more, or 5% memory or less), without a usable provider, or without a recent CPU/memory reading | The draft stays where it opened. **Send** refuses with "Choose a machine to continue" ("Checking machine resources" while the check still runs); pick a machine in the composer. "Without a usable provider": the chosen provider is not installed there, signed out, in error, unavailable, or turned off for this project. When the reading cannot be fetched, the composer shows **Auto balance unavailable**.                                                                                                            |
| A file is attached before the draft has moved                                                                                                 | The draft stays on the machine it opened on, and **Send** goes there, even if that machine is **Manual only** (upstream rule: attachments stay on their machine).                                                                                                                                                                                                                                                                                                                                                            |
| A thread is referenced that lives on a **Prefer** or **Normal** machine                                                                       | That machine is the only one that can read it, so the draft moves there.                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| A thread is referenced that lives on a **Manual only** machine                                                                                | No machine is picked. **Send** refuses with "Choose a machine to continue"; pick that machine in the composer.                                                                                                                                                                                                                                                                                                                                                                                                               |
| A thread on another machine is referenced after the draft has moved                                                                           | The draft does not move while you type. Without a file attached, **Send** picks again first (see below): it moves the draft to the thread's machine when that machine qualifies as in the first row, else refuses with "Choose a machine to continue". With a file attached, or after a hand pick, **Send** refuses with "Attached thread is on another machine"; remove the thread or pick its machine. The second refusal does not happen when the thread's machine is linked to the draft's machine: **Send** goes ahead. |
| You pick a machine, branch or worktree by hand                                                                                                | That choice wins until you choose **Auto balance** again.                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Only one machine has the project                                                                                                              | No balancing; the draft uses that machine.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Load balancing off                                                                                                                            | Upstream's order: the primary machine first.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| Thread without a project                                                                                                                      | Not balanced; see above.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |

**Send** picks again. Just before an automatically moved draft without a file sends, the client reads the machines'
load again and picks again among the machines that qualify (upstream PR #35658). With the setup above that is only
the preferred machine: if it has gone offline or become busy since the draft moved, **Send** refuses with "Choose a
machine to continue" rather than send to a **Manual only** machine.

## After upstream fixes #31048

Threads without a project will then follow the same weights. The preferred machine's "No project" folder opens
first, then the draft moves. One case can still send to a machine you did not choose: if that open fails or is cut
off (for example, the preferred machine fails to create the folder), the draft can settle on the machine it is
on. The composer shows **Auto balance**, and **Send** goes to that machine even if it is **Manual only**. A failed
open shows "Could not switch machine"; a cut-off open shows nothing. Pick the machine by hand if it matters. This is
upstream's behaviour in #31049; the fork does not change it.

## Removed fork setting

An earlier draft of this fork change added a client setting `anchorEnvironmentId` (**Settings → Connections →
Anchor machine**). It was removed before it shipped. Settings saved with it still load: the key is ignored, the
other settings stay, and the next save drops it (tests `clientPersistenceStorage.removedKeys.test.ts` and
`DesktopClientSettings.removedKeys.test.ts`).
