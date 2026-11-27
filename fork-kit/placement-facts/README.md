# placement-facts: example `factsCommand` scripts

A machine's `placement.factsCommand` (Settings; [docs/fork/README.md](../../docs/fork/README.md#serverplacement))
is one program that says whether the machine is free for work now and what it has. T3 runs it for the machine's own
facts read (`t3_environment_read`), which placement on a linked machine reads before it sends work. These are
starting points, one per OS. Like the review kit, this kit has no installer: copy one, edit it, point the setting at it.

| File               | OS      | Run as                                                             |
| ------------------ | ------- | ------------------------------------------------------------------ |
| `facts-linux.sh`   | Linux   | the script itself (`chmod 755`)                                    |
| `facts-macos.sh`   | macOS   | the script itself (`chmod 755`)                                    |
| `facts-windows.cs` | Windows | an `.exe` built with the `csc.exe` that ships with Windows (below) |

**`facts-windows.cs` has never been compiled or run.** It was written without a Windows machine. Build it once and
check its answers (free, busy file, on battery, `gpu`) before a machine relies on it.

What each one does, in this order (the settings at the top of each file change it):

1. **Not available** (exit 1, the reason on the error output) while the busy file is there
   (`~/.config/t3-placement/busy` on Linux and macOS, `%LOCALAPPDATA%\t3-placement\busy` on Windows; its first line
   is the reason), while a program in the busy list runs (a game, a video call), or while the machine runs on
   battery.
2. **Free** (exit 0) otherwise, printing one label per line: the extra labels from the settings, then `gpu` and
   `cuda` when `nvidia-smi` lists an NVIDIA GPU (Linux, Windows), and `ci-runner` when a GitHub Actions
   self-hosted runner runs there.

The busy file is a switch a person (or their own scripts) can flip without opening Settings: `echo "gaming" >
~/.config/t3-placement/busy` stops new work there, deleting it frees the machine again.

## Rules T3 holds the program to

- The setting is an absolute path to one program: no shell, no arguments. On Windows only an `.exe` runs, which is
  why the Windows example is C# source: build it with
  `& "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /out:C:\t3-placement\facts.exe facts-windows.cs`.
- No answer within 5 seconds is "not available". T3 then stops the program and everything it started; one that
  ignores the stop is killed a second later, so a read can wait about 6 seconds. A machine that is asked by another
  one is asked with a short wait, so keep the program fast (each example answers in well under a second,
  `nvidia-smi` included).
- The answer is complete when the program's output closes, not when it exits: start no background program that
  keeps the output open, or the read waits out the 5 seconds and says "did not answer".
- One answer serves every facts read for 15 seconds: a machine that turns busy says so on the read after that. The
  read's `factsAt` is the time of the read, so the program's answer in it can be up to about 15 seconds older.
- Without `pgrep` the busy-program list cannot be checked, so the Linux and macOS examples say not available when
  the list is not empty. A Linux process name is cut to 15 characters; the Linux example matches a longer name by
  those. A desktop's wireless mouse or controller battery (scope `Device`) is not "on battery".
- It runs with the T3 server's own rights. Keep it in a folder only you can write to.

## Tests

`python3 -I -B fork-kit/placement-facts/tests/scripts.test.py` runs the Linux and macOS scripts with stand-in
`pgrep`, `nvidia-smi` and `pmset`, a stand-in power-supply folder and busy file. The Windows example is only read as text (its
settings, and no syntax newer than C# 5); no test compiles or runs it.
