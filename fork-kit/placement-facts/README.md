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
- No answer within 5 seconds is "not available". A machine that is asked by another one is asked with a short wait,
  so keep the program fast (each example answers in well under a second, `nvidia-smi` included).
- One answer serves every facts read for 15 seconds: a machine that turns busy says so on the read after that.
- It runs with the T3 server's own rights. Keep it in a folder only you can write to.

## Tests

`python3 -I -B fork-kit/placement-facts/tests/scripts.test.py` runs the Linux and macOS scripts with stand-in
`pgrep`, `nvidia-smi` and `pmset`, a stand-in power-supply folder and busy file. The Windows example is checked for
its settings and for C# 5 syntax only; build it on Windows to run it.
