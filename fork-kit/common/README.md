# common: code every kit shares

Not a kit of its own: each kit's installer copies these files into its version folder (and lists them in its
`MANIFEST.sha256`), so an installed kit never reads code from outside its own folder.

## The machine file

Kits are public and general; the facts of one machine (unit names, patterns, ages, on/off switches) are not.
They live in one private file per machine, which the owner writes by hand and no tool ever writes:

| OS      | Path                                                 |
| ------- | ---------------------------------------------------- |
| Linux   | `/etc/fork-kit/machine.json`                         |
| macOS   | `/Library/Application Support/fork-kit/machine.json` |
| Windows | `C:\ProgramData\fork-kit\machine.json`               |

The path is fixed: shipped commands have no option or environment variable that names another file.

```json
{
  "version": 1,
  "heavy": { "sweep": { "stop": false, "testPatterns": ["(^|/)pytest( |$)"] } }
}
```

[machine.example.json](machine.example.json) has every key a kit knows today, with made-up values.

Rules (all checked by [machine_file.py](machine_file.py); a kit that finds one broken refuses, names the file and
the reason, and changes nothing):

- **Owned by root**, and neither the file nor its folder writable by group or others. Every folder above it, up
  to `/`, is root-owned and not writable by group or others (a sticky folder such as `/tmp` is allowed), and none
  of them is a link, so nobody but root can swap a folder on the way. These checks exist only on Linux and macOS:
  on Windows the loader refuses the file until they do (no kit reads it there yet).
- **A plain file of at most 1 MiB, not a symbolic link, in a folder that is not one either.** The loader opens the
  file without following a link and checks the file it opened (owner, mode, kind), not the path, so a file
  swapped in between is caught.
- **Shape:** a JSON object with no key given twice and no deeper nesting than Python's JSON reader takes,
  `"version"` a whole number, one section per kit named after the kit's folder
  (`"heavy"`, ...), camelCase keys.
- **Version:** a file newer than the kit knows is refused ("update the kit"); the kit never guesses.
- **Each kit reads `version` and its own section only.** A missing section, an unknown key in it, or a value of
  the wrong type is refused, so a typo is never silently ignored.
- **No secrets.** A key whose name says secret, token, password or private key may only hold `"file:<path>"`.

Write it as root, for example:

```
install -d -m 755 /etc/fork-kit && install -m 644 /dev/stdin /etc/fork-kit/machine.json <<'EOF'
{ "version": 1, "heavy": { "sweep": { "stop": false } } }
EOF
```

## free/v1

The "is this machine free?" contract, its one reader and example scripts: [FREE.md](FREE.md).

## Loader

```python
import machine_file
section = machine_file.read_section('heavy')                  # the fixed path, owner root
sweep = machine_file.fields(section, {'sweep': (dict, machine_file.REQUIRED, None)}, 'heavy')
```

`read_section(kit, path=None, owner_uid=0)` returns the kit's section; `path` and `owner_uid` are for tests only.
`read_protected_text(path, owner_uid=0)` reads another file a kit trusts (a policy file the machine file names)
under the same file-safety rules (root owner, no link, plain file, 1 MiB, UTF-8).
`fields(obj, spec, where)` checks one object against `{key: (types, default or REQUIRED, check or None)}` and fills
in the defaults. All three raise `MachineFileError` with a message that names the file and the key. Plain Python 3
standard library.

## The installer

`kit_install.py` is the installer of the agent-guards, cleaner, gardener, golive, schedule-kick and update-policy kits; each kit's `sbin/<kit>-install` is a small
subclass that names its paths, links, files and modules, and adds its own steps. `install --source FOLDER --sha
SHA`, as root:

1. **check**: refuses, before writing anything, a link path that is not the kit's (a kit may keep a plain old
   file there instead: `KEEP_OLD`), a folder that is a file or a link, a bad `--sha`, a source file that is
   missing or reached through a link, and a release whose `sbin/<kit>-install` or `common/kit_install.py` differs
   from the installer that runs (an installed installer would copy its own file list, not the release's: install
   that release with the bootstrap, which runs the release's own installer);
2. **copy**: the files into `.tmp-*` beside `versions/<sha>/`, read with no link followed, root-owned, with
   `MANIFEST.sha256`; **self-test**: every module imported, and the help of the command and of the kit's own
   installer run from the copy (a copy whose installer cannot load could never be updated or removed by it); then
   the copy is
   swapped in, in one step (`renameat2` exchange; two renames where the filesystem has none). A version folder
   that is whole and matches is kept as it is; one that differs (a stop half-way, an older installer) is made
   again beside it;
3. **switch**: `current` to it in one step; **links**: each of the kit's links in one step; the kit's own steps;
4. **record**: `installed.tsv`, what `self-check` compares.

Safe to run again (nothing changes), and to stop at any step (the next run finishes it); one installer at a time
(`<etc>/install.lock`, `uninstall` too). Old version folders stay: a run of the kit may still be using one
(installing the same release over a changed copy swaps in the same code). `uninstall` puts back what `KEEP_OLD`
kept last (`replaced/order`) over its link in one step, with the mode and owner the old file had, then removes the
other links, the libexec folder, the installer's own link and the etc folder. Stopped before the libexec folder
goes, it is finished by running it again; stopped while that folder goes, install again with the bootstrap, then
uninstall. It refuses while a process (its command line, with `./`, `../` and doubled slashes resolved) or a
transient systemd unit (systemd-run's unit file: a detached run, a timer still pending) names `<libexec>/` (or its
real path, where a folder above it is a link) or one of the kit's links; this uninstall and the processes that
started it do not count, and a process list it cannot read refuses. The unit folders are opened with no link
followed and never waited on, each step owned by root (or, below `<run>/user/<uid>`, that user) and on one
filesystem; a unit folder or file it cannot read that way refuses. Known limit: `<run>/user/<uid>` itself is that
user's, so where `/etc/fuse.conf` has `user_allow_other` they can mount a FUSE filesystem of their own over it, and
root's `uninstall` can then wait there forever, with both locks held (as it can reading the command line of a
process whose memory is FUSE-backed). It fails safe: nothing is removed yet, and Ctrl-C ends it and frees the
locks. Updates go through the bootstrap.

`bootstrap.sh --kit agent-guards|cleaner|gardener|golive|schedule-kick|update-policy --repo OWNER/REPO --sha SHA [kit options]` checks the commit is on the protected
release branch, downloads exactly that commit and runs the kit's installer; it passes on only the kit's own options.

## Tests

`python3 -I -B fork-kit/common/tests/kit_install.test.py` runs the installer contract for every kit that uses
`kit_install.py`, and the bootstrap's options (a fake host in a temporary folder, no root); its red check breaks each
rule in a copy of the engine or the bootstrap and shows its test then fails.
`python3 -I -B fork-kit/common/tests/machine_file.test.py` (temporary files only, no root). Its red check breaks
each file-safety rule in a copy of the loader and shows the test for that rule then fails.
