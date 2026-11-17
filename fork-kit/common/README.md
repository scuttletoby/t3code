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
| Windows | `%ProgramData%\fork-kit\machine.json`                |

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

## Tests

`python3 -I -B fork-kit/common/tests/machine_file.test.py` (temporary files only, no root). Its red check breaks
each file-safety rule in a copy of the loader and shows the test for that rule then fails.
