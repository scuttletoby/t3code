# claude-settings: the same chosen keys in every Claude Code user settings file

One command that merges a list of keys into a user's Claude Code `settings.json` and checks that they are still
there. For a fleet of machines that should start agents the same way: a setting put in by hand on one machine is
missing on the next one (a skill list trimmed on one machine only costs every session on the others thousands of
tokens a turn). The keys come from one config file you keep outside the fork, so every machine gets the same list.

Python 3 standard library only; Linux, macOS and Windows. Like the review kit, it has no installer: run it from a
checkout of the fork (or a copy of `bin/claude-settings`).

```sh
claude-settings apply --config CONFIG [--file SETTINGS ...]   # merge the config's keys in, backup first
claude-settings check --config CONFIG [--file SETTINGS ...]   # exit 1 when a file is not in step, write nothing
```

`SETTINGS` defaults to `$CLAUDE_CONFIG_DIR/settings.json`, else `~/.claude/settings.json` (on Windows
`%USERPROFILE%\.claude\settings.json`). Give `--file` once per settings folder when a machine runs more than one
(`--file ~/.claude/settings.json --file ~/.claude-other/settings.json`). On Windows run it as
`py -3 bin\claude-settings ...` (or `python`).

## The config

```json
{
  "version": 1,
  "settings": {
    "disableClaudeAiConnectors": true,
    "skillOverrides": { "claude-approvals": "user-invocable-only", "code-review": "name-only" }
  }
}
```

[claude-settings.example.json](claude-settings.example.json) is a fuller example. `settings` is merged into the
file: an object (such as `skillOverrides`) merges key by key, so entries the config does not name stay as they are;
any other value (a boolean, a string, a list) is set as given. The config wins over the file for the keys it names,
which is what keeps machines in step. Nothing is removed. A key the config does not name is never touched.

## What it does to the file

- **A second run changes nothing**: when every key is already in place the file is not written and no backup is made
  (`in step`).
- **Backup first**: before a change it copies the file to `settings.json.bak-claude-settings-<UTC time>` (same
  mode). Undo = copy that back.
- **Whole file or old file**: the new text goes to a temp file in the same folder, which is renamed over the old one.
  A run stopped before the rename leaves the old file whole; rerun it. If the file changed while the tool ran (Claude
  Code or a person wrote it), nothing is written: exit 2, run again.
- **Refuses** a file that is not a JSON object (exit 2, file left as it is) and a missing folder (Claude Code not set
  up for that user). A missing or empty file is created with only the config's keys, mode 0600.
- **A link stays a link**: a `settings.json` that is a symlink is written through to the file it points to.
- **Prints keys, never values**: settings files can hold `env` values (tokens). The output names only the key paths
  the config sets (`set skillOverrides.claude-approvals`, `not in step: disableClaudeAiConnectors`).
- The file is written back as 2-space JSON: the first change may reformat it (the backup keeps the original).

Exit 0 in step or brought in step, 1 not in step (`check`), 2 an error. With several `--file`s, an error in one does
not stop the others.

## Tests

```sh
python3 -I -B fork-kit/claude-settings/tests/settings.test.py
```
