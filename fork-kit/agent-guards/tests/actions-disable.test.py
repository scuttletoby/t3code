#!/usr/bin/env python3
"""Tests of fork-kit/agent-guards/bin/actions-disable-guard (the Claude Code PreToolUse hook that refuses disabling
GitHub Actions). The guard is loaded as a module with a machine file of the test's own (guards_fixture.py), whose
actionsDisable.refuseScripts names one script (example_garden.py --fix); the runs as a hook read no machine file.

  node .github/scripts/fork-ci-tests.cjs fork-kit/agent-guards/

(a test runner handed the folder, not `python3 this-file` from an agent's shell: the guard reads the script files an
agent runs, and this one is full of the patterns it refuses.)"""
import datetime as dt, json, os, subprocess, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402

HOOK = os.path.join(fx.BIN, 'actions-disable-guard')
GARDEN = {'name': 'example_garden.py', 'flag': '--fix', 'why': 'disables every workflow it does not know'}
# the old escape hatch (a session variable and a bare marker): it opens nothing any more (lens B major: a nested
# session inherited the variable)
MARK_ENV, MARK_CMD = 'CLAUDE_GUARD_OWNER_ALLOW_ACTIONS_DISABLE', '#owner-approved:actions-disable'


_MACHINE = tempfile.TemporaryDirectory(prefix='actions-disable-test-')


def load():
    """The guard as a module, with one refuseScripts row (example_garden.py --fix) from a machine file of its own."""
    path = fx.machine_file(_MACHINE.name, {'actionsDisable': {'refuseScripts': [GARDEN]}})
    return fx.load_hook('actions-disable-guard', path)


def today():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')


DENY = [
    # gh workflow disable, every spelling
    'gh workflow disable 415816119 -R example/repo',
    'gh workflow disable fork-tests.yml',
    'gh "workflow" \'disable\' 1',
    'gh work"flow" dis\'able\' 1',
    'g\\h workflow disable 1',
    '/usr/bin/gh workflow disable 1',
    'GH_TOKEN=x GH_REPO=a/b gh workflow disable 1',
    'env GH_REPO=a/b gh workflow disable 1',
    'env -i PATH=/usr/bin gh workflow disable 1',
    'sudo -u someone gh workflow disable 1',
    'command gh workflow disable 1',
    'exec gh workflow disable 1',
    'nohup gh workflow disable 1 &',
    'timeout 30 gh workflow disable 1',  # timing: guard input, not a wait
    'timeout -k 5 30s gh workflow disable 1',  # timing: guard input, not a wait
    'xargs -n1 gh workflow disable < ids.txt',
    'printf "1\\n2\\n" | xargs -I{} gh workflow disable {} -R a/b',
    'gh workflow -R a/b disable 1',
    'gh workflow --repo=a/b disable 1',
    'gh workflow --repo a/b disable 1',
    # shells, chains, substitutions
    'bash -c "gh workflow disable 1"',
    "bash -lc 'gh workflow disable 1'",
    'sh -c "cd /tmp && gh workflow disable 1"',
    'zsh -c "true; gh workflow disable 1"',
    'cd /tmp && gh workflow disable 1',
    'true; gh workflow disable 1',
    'false || gh workflow disable 1',
    'gh workflow list | head -1 && gh workflow disable 1',
    'echo $(gh workflow disable 1)',
    'echo `gh workflow disable 1`',
    '(gh workflow disable 1)',
    'for i in 1 2; do gh workflow disable $i; done',
    'if true; then gh workflow disable 1; fi',
    'echo "gh workflow disable 1" | sh',
    'echo gh workflow disable 1 | bash -s',
    'eval "gh workflow disable 1"',
    'W=disable; gh workflow $W 1',
    'W=disable && gh workflow ${W} 1',
    'bash <<EOF\ngh workflow disable 1\nEOF',
    'gh workflow \\\n  disable 1',
    # gh api to the disable endpoint
    'gh api -X PUT repos/a/b/actions/workflows/1/disable',
    'gh api --method PUT /repos/a/b/actions/workflows/fork-tests.yml/disable',
    'gh api --method=PUT repos/a/b/actions/workflows/1/disable',
    'gh api -XPUT repos/a/b/actions/workflows/1/disable',
    'gh api "repos/{owner}/{repo}/actions/workflows/1/disable" -X PUT',
    'gh api https://api.github.com/repos/a/b/actions/workflows/1/disable --method put',
    'gh api repos/a/b/actions/workflows/1/disable',
    'ID=415816119; gh api -X PUT repos/example/repo/actions/workflows/$ID/disable',
    'B=repos/a/b/actions/workflows/1; gh api -X PUT "$B/disable"',
    'GH_HOST=github.com gh api -X PUT repos/a/b/actions/workflows/1/disable',
    # curl / wget / httpie to the disable endpoint
    'curl -X PUT -H "Authorization: Bearer $(gh auth token)" https://api.github.com/repos/a/b/actions/workflows/1/disable',
    'curl -sS --request PUT https://api.github.com/repos/a/b/actions/workflows/1/disable',
    'curl -XPUT https://api.github.com/repos/a/b/actions/workflows/1/disable',
    'wget --method=PUT -O- https://api.github.com/repos/a/b/actions/workflows/1/disable',
    'http PUT https://api.github.com/repos/a/b/actions/workflows/1/disable',
    # turning Actions off for a repo / org, or changing the default workflow permissions
    'gh api -X PUT repos/a/b/actions/permissions -F enabled=false',
    'gh api repos/a/b/actions/permissions -F enabled=false',
    'gh api -X PATCH repos/a/b/actions/permissions',
    'gh api --method PUT repos/a/b/actions/permissions --input body.json',
    'gh api -X PUT orgs/o/actions/permissions -f enabled_repositories=none',
    'gh api -X PUT repos/a/b/actions/permissions/workflow -f default_workflow_permissions=read',
    'gh api -X DELETE orgs/o/actions/permissions/repositories/123',
    'gh api -X PUT orgs/o/actions/permissions/repositories -F selected_repository_ids[]=1',
    'gh api -X PUT repos/a/b/actions/permissions -F enabled=true -f allowed_actions=selected',
    'gh api -H "X-HTTP-Method-Override: PUT" repos/a/b/actions/permissions',
    'curl -X PUT -d \'{"enabled":false}\' https://api.github.com/repos/a/b/actions/permissions',
    'curl -d \'{"enabled":false}\' https://api.github.com/repos/a/b/actions/permissions',
    'curl --json \'{"enabled":false}\' https://api.github.com/repos/a/b/actions/permissions',
    'curl -X PATCH https://api.github.com/orgs/o/actions/permissions',
    # inline code
    'python3 -c "import urllib.request as u; u.urlopen(u.Request(\'https://api.github.com/repos/a/b/actions/workflows/1/disable\', method=\'PUT\'))"',
    "python3 - <<'EOF'\nimport requests\nrequests.put('https://api.github.com/repos/a/b/actions/permissions', json={'enabled': False})\nEOF",
    'node -e "fetch(\'https://api.github.com/repos/a/b/actions/permissions\', {method: \'PUT\'})"',
    'python3 -c "import subprocess; subprocess.run([\'gh\', \'workflow\', \'disable\', \'1\'])"',
    # aliases
    "gh alias set wd 'workflow disable'",
    "gh alias set --shell wd 'gh api -X PUT repos/a/b/actions/workflows/$1/disable'",
    # seen in real agent transcripts (2026-11-12)
    'cd /srv/work/x && for w in 415770881 415770880 415770879; do gh workflow disable $w --repo '
    'example/repo; done',
    'cd /srv/work/x; gh api -X PUT repos/example/repo/actions/permissions/'
    'fork-pr-contributor-approval -f approval_policy=all_external_contributors',
    # heredocs that run
    'cat > /tmp/x.sh <<EOF\ngh workflow disable 1\nEOF\nbash /tmp/x.sh',
    'cat <<\'EOF\' > run.sh && chmod +x run.sh && ./run.sh\ngh api -X PUT repos/a/b/actions/workflows/1/disable\nEOF',
    'gh alias import - <<EOF\nwd: workflow disable\nEOF',
    'gh api -X PUT repos/a/b/actions/permissions --input - <<EOF\n{"enabled": false}\nEOF',
    # the marker in the command alone (agents can type it) opens nothing
    f'{MARK_ENV}={today()} gh workflow disable 1 {MARK_CMD}',
    f'export {MARK_ENV}={today()}; gh workflow disable 1 {MARK_CMD}',
]

ALLOW = [
    'gh workflow enable 415816119 -R example/repo',
    'gh workflow list --all',
    'gh workflow view fork-tests.yml',
    'gh workflow run fork-tests.yml -R example/repo',
    'gh api -X PUT repos/a/b/actions/workflows/1/enable',
    'gh api --method PUT repos/a/b/actions/workflows/fork-tests.yml/enable',
    'gh api repos/a/b/actions/workflows',
    'gh api repos/a/b/actions/workflows/1',
    'gh api repos/a/b/actions/permissions',
    'gh api repos/a/b/actions/permissions/workflow',
    'gh api --method GET orgs/o/actions/permissions',
    'gh api -X GET repos/a/b/actions/permissions -F per_page=100',
    'gh api -X PUT repos/a/b/actions/permissions -F enabled=true',
    'gh api -X PUT repos/a/b/actions/permissions -F enabled=true -f allowed_actions=all',
    'curl -q -s https://api.github.com/repos/a/b/actions/permissions',   # -q first: ~/.curlrc is not read
    'curl -s -H "Accept: application/vnd.github+json" https://api.github.com/repos/a/b/actions/workflows',
    "rg -n 'workflow disable|/disable\\b|actions/permissions' /srv/projects /usr/local/bin",
    'grep -rn "gh api -X PUT .*/disable" docs/',
    'systemctl --user disable foo.timer',
    'git commit -m "docs: the guard refuses actions/permissions writes"',
    'echo disable',
    'gh pr list --state open',
    'gh run list -R example/repo -w fork-tests.yml',
    'python3 -I -m unittest discover -s ~/.local/bin -p "test_actions_disable_guard.py"',
    'python3 -I scripts/dev/run_tests.py tests/actions-disable.test.py',
    'ls -la; cat README.md',
    'gh issue create --title "workflow got disabled" --body-file body.md',
    'gh alias set co "pr checkout"',
    'curl -X PUT https://api.github.com/repos/a/b/actions/workflows/1/enable',
    'jq . ~/.claude/settings.json',
    '',
    # heredocs that only write text (briefs, notes, PR bodies) are data, not commands
    "cat > notes.md <<'EOF'\nFound: gh workflow disable 415770881 --repo example/repo ran.\n"
    "gh workflow disable 1\ngh api -X PUT repos/a/b/actions/permissions -F enabled=false\nEOF",
    "cat >> research/plan.md <<EOF\npython3 -I tool.py\nrequests.put('https://api.github.com/repos/a/b/actions/"
    "permissions')\nEOF\ngit add research/plan.md",
    "gh pr create --title t --body-file - <<'EOF'\nThe guard refuses gh workflow disable.\nEOF",
    'echo x <<< "gh workflow disable 1"',
]


# ---- attack cases (#1436). P is the endpoint that turns Actions off for a repo, U the same as a URL. Every case
# here is a call that disables something, or one the guard cannot read fully next to a guarded path (it fails
# closed). The files the cases name are written by AttackTest.setUp. No case needs a token or the network: the
# guard only reads text.
P = 'repos/a/b/actions/permissions'
U = 'https://api.github.com/' + P
D = 'repos/a/b/actions/workflows/1/disable'
OFF = '-F enabled=false'
GQL = "mutation { deleteBranchProtectionRule(input: {branchProtectionRuleId: \"x\"}) { clientMutationId } }"
GQL_OFF = ('mutation { updateRepositoryRuleset(input: {repositoryRulesetId: "x", enforcement: DISABLED}) '
           '{ clientMutationId } }')

ATTACKS = {
    'gh api: a flag value that looks like a flag (#1436)': [
        f'gh api -X PUT -p -XGET {P} {OFF}',
        f'gh api -X PUT -t -XGET {P} {OFF}',
        f'gh api -X PUT -q -XGET {P} {OFF}',
        f'gh api -X PUT --jq --method=GET {P} {OFF}',
        f'gh api --method PUT --template --method=GET {P} {OFF}',
        f'gh api -X PUT --preview -XGET {P} {OFF}',
        f'gh api -XPUT -p --method=GET {P}',
        f'gh api -X PUT -q "-X GET" {P}',
        f'gh api -X PUT -t --method -X PUT {P}',
        f'gh api -X PATCH --jq -XGET {P}',
        f'gh api -X DELETE -p -XGET orgs/o/actions/permissions/repositories/1',
    ],
    'gh api: joined short flags and = forms (#1436)': [
        f'gh api -iXPUT {P} {OFF}',
        f'gh api -iXPUT {P}',
        f'gh api -iX PUT {P}',
        f'gh api -X=PUT {P}',
        f'gh api -iX=PUT {P}',
        f'gh api {P} -iFenabled=false',
        f'gh api {P} -ifenabled=false',
        f'gh api {P} -fenabled=false',
        f'gh api {P} --raw-field=enabled=false',
        f'gh api {P} --field=enabled=false',
        f'gh api {P} --input=body.json',
        f'gh api {P} --input body.json',
        f'gh api --method=put {P}',
        f'gh api -X GET -X PUT {P} {OFF}',
        f'gh api --method GET -XPUT {P}',
        f'gh api -X PUT {OFF} -- {P}',
        f'gh api -XPUT --silent --paginate --slurp {P}',
        f'gh api -X GET -H "X-HTTP-Method-Override: PUT" {P} {OFF}',
        f'gh api -H x-http-method-override:put {P}',
    ],
    'gh: flags before the subcommand': [
        f'gh -X PUT api {P} {OFF}',
        f'gh -XPUT api {P}',
        f'gh --method PUT api {P}',
        f'gh -F enabled=false api {P}',
        'gh -R a/b workflow disable 1',
        'gh --repo a/b workflow disable 1',
        'gh --repo=a/b workflow disable 1',
        'gh workflow disable --repo a/b 1',
        'gh workflow -R a/b -- disable 1',
    ],
    'gh api: fails closed on what it cannot read': [
        f'gh api --frobnicate -X PUT {P}',
        f'gh api -Z {P} {OFF}',
        f'gh api -X "$M" {P} {OFF}',
        f'gh api -X $M {P}',
        f'gh api $FLAGS {P}',
        f'gh api "$@" {P}',
        f'gh api "${{ARGS[@]}}" {P}',
        f'gh api {P} $(cat flags.txt)',
        f'gh api {P}$(cat flags.txt)',
        f'gh api {P} -X',
        f'gh api "$X" {P}',
        f'gh api --"$NAME" PUT {P}',
        f'gh api -"$S" PUT {P}',
        f'M=PUT; gh api -X $M {P} {OFF}',
        f'M=GET; if true; then M=PUT; fi; gh api -X $M {P} {OFF}',
        f'F="-X PUT"; gh api $F {P} {OFF}',
        f'F="{OFF}"; gh api {P} $F',
    ],
    'the re-enable exception takes nothing else': [
        f'gh api -X PUT {P} -F enabled=true -F enabled=false',
        f'gh api -X PUT {P} -F enabled=true --input body.json',
        f'gh api -X PUT {P} -F enabled=@body.json',
        f'gh api -X PUT {P} -F "enabled=$V"',
        f'gh api -X PUT {P} -F enabled=true -F allowed_actions=selected',
        f'gh api -X PUT {P} -F enabled=true -H "X-HTTP-Method-Override: DELETE"',
        f'gh api -X PUT "{P}?enabled=false" -F enabled=true',
        f'gh api -X PUT {P}/access -F enabled=true',
        f'gh api -X PUT orgs/o/actions/permissions -F enabled=true',
        f'gh api -X PUT {P} -F enabled=true {P}/workflow',
        f'gh api -X PUT {P}',
        f"gh api -X PUT {P} --input - <<< '{{\"enabled\": false}}'",
        f'gh api -X PUT {P} -F enabled=true --input - <<EOF\n{{"enabled": false}}\nEOF',
        f'gh api --hostname github.com -X PUT {P} {OFF}',
        f'gh api -XPUT {P} --cache 1h',
        f'gh api -X PUT {P}/workflow -f default_workflow_permissions=read',
        f'gh api -X PUT {P}/selected-actions -F github_owned_allowed=false',
    ],
    'quoting': [
        'gh $\'work\\x66low\' disable 1',
        'gh workflow $\'dis\\141ble\' 1',
        'gh workflow $\'\\x64isable\' 1',
        'gh workflow dis""able 1',
        'gh workflow d\\isable 1',
        "gh 'workflow' \"disable\" 1",
        f'gh api -X P""UT {P} {OFF}',
        f'gh api -"X" PUT {P} {OFF}',
        f"gh api --me'th'od PUT {P} {OFF}",
        f'gh api -X PUT repos/a/b/actions/per"mis"sions {OFF}',
        f'gh api -X PUT repos/a/b/act\\\nions/permissions {OFF}',
        f'gh api -X PUT $\'repos/a/b/actions/\\x70ermissions\' {OFF}',
        f'gh api -H "X: -X GET" -X PUT {P} {OFF}',
        f"gh api -X PUT {P} -f 'x=a b' {OFF}",
    ],
    'heredocs, comments and line joins that hide the next command': [
        "cat <<'EOF'\nsome text\nEOF\ngh workflow disable 1",
        'cat <<EOF\nsome text\nEOF\ngh workflow disable 1',
        'cat <<-EOF\n\ttext\n\tEOF\ngh workflow disable 1',
        'echo hi # <<EOF\ngh workflow disable 1\nEOF',
        'echo "<<EOF"\ngh workflow disable 1\nEOF',
        "echo '<<EOF'; gh workflow disable 1\nEOF",
        'cat <<A <<B\na\nA\nb\nB\ngh workflow disable 1',
        'cat <<EOF; gh workflow disable 1\ntext\nEOF',
        'cat <<EOF\n$(gh workflow disable 1)\nEOF',
        'cat <<EOF\n`gh workflow disable 1`\nEOF',
        'echo a \\\n; gh workflow disable 1',
        'echo a # note \\\ngh workflow disable 1',
        'gh workflow disable 1 # only a note',
        'echo "a\nb"; gh workflow disable 1',
        "echo 'a\n# b'\ngh workflow disable 1",
    ],
    'chains and substitutions': [
        'true&gh workflow disable 1',
        'true|gh workflow disable 1',
        'true|&gh workflow disable 1',
        '{ gh workflow disable 1; }',
        '! gh workflow disable 1',
        'case x in x) gh workflow disable 1;; esac',
        'while read w; do gh workflow disable "$w"; done < ids.txt',
        'until gh workflow disable 1; do sleep 1; done',
        'x=$(gh workflow disable 1)',
        'echo "${X:-$(gh workflow disable 1)}"',
        'echo "$(echo "$(gh workflow disable 1)")"',
        'cat <(gh workflow disable 1)',
        'echo ok > >(gh workflow disable 1)',
        'echo $((1+1)); gh workflow disable 1',
        'A=($(gh workflow disable 1))',
        'true && { false || gh workflow disable 1; }',
    ],
    'brace, glob and IFS expansion': [
        'gh workflow {disable,list} 1',
        'gh workflow dis{a,}ble 1',
        'gh workflow d* 1',
        'gh workflow ?isable 1',
        'gh workflow [d]isable 1',
        'gh {workflow,x} disable 1',
        f'gh api -X PUT repos/a/b/actions/{{permissions,x}} {OFF}',
        f'gh api -X PUT repos/a/b/actions/perm* {OFF}',
        f'gh api {{-X,PUT}} {P}',
        'IFS=,; X=workflow,disable,1; gh $X',
        'IFS=/; X=workflow/disable; gh $X 1',
        f'IFS=:; X=-X:PUT; gh api $X {P}',
        'X="workflow disable"; gh $X 1',
    ],
    'variables: every value counts, not the first one': [
        'W=list; W=disable; gh workflow $W 1',
        'W=list; for i in 1 2; do gh workflow $W 1; W=disable; done',
        'if true; then W=disable; fi; gh workflow $W 1',
        'W=list; true && W=disable; gh workflow $W 1',
        'declare W=disable; gh workflow $W 1',
        'declare "W=disable"; gh workflow $W 1',
        'export W=disable; gh workflow $W 1',
        'readonly W=disable; gh workflow $W 1',
        'W=list; declare -n W=X; X=disable; gh workflow $W 1',
        'W=list; read W <<< disable; gh workflow $W 1',
        'W=list; printf -v W disable; gh workflow $W 1',
        'W=dis; W+=able; gh workflow $W 1',
        'W=dis; W=${W}able; gh workflow $W 1',
        'W=able; W=dis$W; gh workflow "$W" 1',
        'W=list; eval "W=dis""able"; gh workflow $W 1',
        'for W in list disable; do gh workflow $W 1; done',
        'for W in $LIST; do gh workflow $W 1; done',
        'W=${X:-disable}; gh workflow $W 1',
        'A=dis; B=able; gh workflow $A$B 1',
        'A=dis; gh workflow ${A}able 1',
        'V=workflow; W=disable; gh $V $W 1',
        'gh workflow "$W" 1',
        'gh workflow $W',
        f'E=permissions; gh api -X PUT repos/a/b/actions/$E {OFF}',
    ],
    'the command word is hidden or wrapped': [
        '/usr/bin/gh workflow disable 1',
        '~/bin/gh workflow disable 1',
        '\\gh workflow disable 1',
        "'gh' workflow disable 1",
        'g\\h workflow disable 1',
        'command gh workflow disable 1',
        'exec gh workflow disable 1',
        'nohup gh workflow disable 1 &',
        'timeout 5 gh workflow disable 1',  # timing: guard input, not a wait
        '(gh workflow disable 1)',
        'if gh workflow disable 1; then :; fi',
        'GH_REPO=a/b gh workflow disable 1',
        'GH_HOST=github.com GH_PROMPT_DISABLED=1 gh workflow disable 1',
        'gh workflow disable "CI and tests"',
        'GH=gh; $GH workflow disable 1',
        'G=/usr/bin/gh; "$G" workflow disable 1',
        '"$GH" workflow disable 1',
        '$(command -v gh) workflow disable 1',
        '"${GH:-gh}" workflow disable 1',
        f'"$GH" api -X PUT {P} {OFF}',
        'g() { gh "$@"; }; g workflow disable 1',
        'function g { gh workflow "$@"; }; g disable 1',
        f'put() {{ gh api -X PUT "$@"; }}; put {P} {OFF}',
        "alias g='gh workflow'; g disable 1",
        'A=(workflow disable 1); gh "${A[@]}"',
        'set -- workflow disable 1; gh "$@"',
        "bash -c 'gh \"$0\" \"$1\" 1' workflow disable",
        'find . -name x -exec gh workflow disable 1 \\;',
        'echo disable 1 | xargs gh workflow',
        'echo 1 | xargs gh workflow disable',
        'xargs -a ids.txt gh workflow disable',
        f'echo {P} | xargs gh api -X PUT {OFF}',
        "ssh host 'gh workflow disable 1'",
        'ssh host gh workflow disable 1',
        "trap 'gh workflow disable 1' EXIT",
        'env -S "gh workflow disable 1"',
        'env --split-string="gh workflow disable 1"',
        'nice -n 5 gh workflow disable 1',
        'stdbuf -oL gh workflow disable 1',
        'setsid -f gh workflow disable 1',
        'flock /tmp/l gh workflow disable 1',
        'time gh workflow disable 1',
        'script -q -c "gh workflow disable 1" /dev/null',
        "su - someone -c 'gh workflow disable 1'",
        "sudo sh -c 'gh workflow disable 1'",
        "busybox sh -c 'gh workflow disable 1'",
        "parallel 'gh workflow disable {}' ::: 1 2",
        'parallel gh workflow disable ::: 1 2',
        'eval gh workflow disable 1',
        'eval "$(echo gh workflow disable 1)"',
        "git -c alias.x='!gh workflow disable 1' x",
        "git config alias.x '!gh workflow disable 1'",
        'python3 -c "import os; os.system(\'gh workflow disable 1\')"',
        "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['gh', 'workflow', 'disable', '1'])\nEOF",
        f'python3 -c "import urllib.request as u; u.urlopen(u.Request(\'{U}\', method=\'PUT\', data=b\'{{}}\'))"',
        'node -e "require(\'child_process\').execSync(\'gh workflow disable 1\')"',
        "bash <<'EOF'\ngh workflow disable 1\nEOF",
        'bash <<< "gh workflow disable 1"',
        'echo "gh workflow disable 1" | bash',
        'echo "gh workflow disable 1" | sh -s',
        'bash < sub/dis.sh',
        'sh -s < sub/dis.sh',
        'C="gh workflow disable 1"; bash <<< "$C"',
        'C="gh workflow disable 1"; bash -c "$C"',
        'C="gh workflow disable 1"; eval "$C"',
        'C="gh workflow disable 1"; $C',
    ],
    'the gh config folder is swapped for one with an alias': [
        'GH_CONFIG_DIR=evil gh wd 1',
        'GH_CONFIG_DIR=./evil gh wd 1',
        'env GH_CONFIG_DIR=evil gh wd 1',
        'export GH_CONFIG_DIR=evil; gh wd 1',
        'export GH_CONFIG_DIR; GH_CONFIG_DIR=evil; gh wd 1',
        'XDG_CONFIG_HOME=xdg gh wd 1',
        'HOME=h gh wd 1',
        'C=evil; GH_CONFIG_DIR=$C gh wd 1',
        'GH_CONFIG_DIR="$PWD/evil" gh wd 1',
        'GH_CONFIG_DIR=$(pwd)/evil gh wd 1',
        'env GH_CONFIG_DIR="$C" gh wd 1',
        'export GH_CONFIG_DIR="$C"; gh wd 1',
        'cd evil && GH_CONFIG_DIR=. gh wd 1',
        'cd "$X" && GH_CONFIG_DIR=. gh wd 1',
        'GH_CONFIG_DIR=evil gh off a/b',
        'GH_CONFIG_DIR=evil gh pr wd 1',
        'GH_CONFIG_DIR=block gh wd 1',
        'GH_CONFIG_DIR=block gh off',
        'GH_CONFIG_DIR=inline gh wd 1',
        'GH_CONFIG_DIR=odd gh wd 1',
    ],
    'gh aliases': [
        "gh alias set wd --clobber 'workflow disable'",
        "gh alias set --clobber wd 'workflow disable'",
        "gh alias set 'pr wd' 'workflow disable'",
        'gh alias set wd workflow disable',
        "gh alias set wd 'workflow \"$1\"'",
        "gh alias set -s wd 'gh workflow disable $1'",
        "gh alias set wd '!gh workflow disable $1'",
        f"gh alias set off 'api -X PUT {P} {OFF}'",
        f"gh alias set off 'api -X PUT repos/$1/actions/permissions {OFF}'",
        'gh alias set wd "$TEXT"',
        'gh alias set wd - <<EOF\nworkflow disable\nEOF',
        "gh alias set wd - <<'EOF'\n!gh workflow disable \"$1\"\nEOF",
        "gh alias set wd - <<< 'workflow disable'",
        'gh alias set wd - <<< "$TEXT"',
        "echo 'workflow disable' | gh alias set wd -",
        'cat al.txt | gh alias set wd -',
        'gh alias set wd - < al.txt',
        'gh alias import al.yml',
        'gh alias import --clobber al.yml',
        'gh alias import - < al.yml',
        'cat al.yml | gh alias import -',
        'cat al.yml | gh alias import',
        'gh alias import not-there-yet.yml',
        'gh alias import "$F"',
        # set up and used in one command: the alias is not in the gh config yet when the guard reads the text
        'gh alias set w workflow && gh w disable 1',
        'gh alias set w workflow; gh w disable 1',
        f"gh alias set put 'api -X PUT' && gh put {P} {OFF}",
        'gh alias import w.yml && gh w disable 1',
        "gh alias set w - <<< workflow; gh w disable 1",
        # alias text the shell fills in: not known when the guard reads it
        "T='workflow disable'; gh alias set wd - <<EOF\n$T\nEOF",
        'T=list; T="workflow disable"; gh alias set wd - <<< "$T"',
        'gh alias set wd - <<EOF\n$(cat al.txt)\nEOF',
    ],
    'the endpoint, spelled another way': [
        f'gh api -X PUT repos/a/b/actions/%70ermissions {OFF}',
        f'gh api -X PUT repos/a/b/actions%2Fpermissions {OFF}',
        f'gh api -X PUT repos/a/b/actions/%2570ermissions {OFF}',
        f'gh api -X PUT repos/a/b/actions/./permissions {OFF}',
        f'gh api -X PUT repos/a/b/actions/x/../permissions {OFF}',
        f'gh api -X PUT repos/a/b//actions//permissions {OFF}',
        f'gh api -X PUT REPOS/A/B/ACTIONS/PERMISSIONS {OFF}',
        f'gh api -X PUT /{P}/ {OFF}',
        f'gh api -X PUT "{P}?x=1" {OFF}',
        f'gh api -X PUT "{P}#x" {OFF}',
        f'gh api -X PUT {U} {OFF}',
        f'gh api -X PUT "repos/{{owner}}/{{repo}}/actions/permissions" {OFF}',
        f'gh api -X PUT repos/$R/actions/permissions {OFF}',
        f'gh api -X PUT "repos/a/b/actions/$X" {OFF}',
        f'gh api -X PUT "repos/a/b/$X/permissions" {OFF}',
        f'gh api -X PUT "repos/a/b/actions/perm$X" {OFF}',
        f'gh api -X PUT "$BASE/permissions" {OFF}',
        f'gh api -X PUT "$BASE/actions/permissions" {OFF}',
        f'gh api -X PUT "$URL" {OFF}',
        f'gh api "$URL" {OFF}',
        f'gh api -X PUT orgs/o/actions/permissions/selected-actions --input body.json',
        f'gh api -X PUT enterprises/e/actions/permissions -f enabled_organizations=none',
        f'gh api -X PUT repositories/123/actions/permissions {OFF}',
        f'gh api -X PUT organizations/1/actions/permissions -f enabled_repositories=none',
        'gh api -X PUT repos/a/b/actions/workflows/ci.yml/disable',
        'gh api -X PUT repos/a/b/actions/workflows/1/%64isable',
        'gh api -X PUT repos/a/b/actions/workflows/1/disable/',
        'gh api -X PUT "repos/a/b/actions/workflows/1/disable?x=1"',
        'gh api -X PUT "repos/a/b/actions/workflows/$ID/$VERB"',
        'gh api -X PUT "$W/disable"',
        'gh api -X PUT "repos/a/b/actions/workflows/1/$V"',
        'V=disable; gh api -X PUT "repos/a/b/actions/workflows/1/$V"',
    ],
    'request bodies in files': [
        'gh api -X PUT repos/a/b/rulesets/1 --input rs_off.json',
        'gh api -X PUT repos/a/b/rulesets/1 --input not-there-yet.json',
        'gh api -X PATCH repos/a/b/rulesets/1 --input "$F"',
        'curl -X PUT -d \'{"enforcement":"disabled"}\' https://api.github.com/repos/a/b/rulesets/1',
        'curl -X PUT -d @rs_off.json https://api.github.com/repos/a/b/rulesets/1',
        'curl -X PUT --data-binary @not-there-yet.json https://api.github.com/repos/a/b/rulesets/1',
        'gh api -X PUT repos/a/b/rulesets/1 --input - <<EOF\n{"enforcement": "disabled"}\nEOF',
        'gh api -X PUT repos/a/b/rulesets/1 --input - < rs_off.json',
        'gh api -X PUT repos/a/b/rulesets/1 -f enforcement=disabled',
        'gh api -X PUT repos/a/b/rulesets/1 -f "enforcement=$E"',
        'gh api -X PUT repos/a/b/rulesets/1 -F enforcement=@off.txt',
        'gh api graphql -F query=@q.graphql',
        'gh api graphql --input gql.json',
    ],
    'branch protection and rulesets (added with #1436)': [
        'gh api -X DELETE repos/a/b/branches/main/protection',
        'gh api --method=delete repos/a/b/branches/main/protection',
        'gh api -X DELETE repos/a/b/branches/main/protection/required_status_checks',
        'gh api -X DELETE repos/a/b/branches/main/protection/enforce_admins',
        'gh api -X DELETE "repos/a/b/branches/$B/protection"',
        'gh api -X DELETE "repos/a/b/branches/main/$X"',
        'gh api -X "$M" repos/a/b/branches/main/protection',
        'gh api -X DELETE repos/a/b/rulesets/12',
        'gh api --method DELETE orgs/o/rulesets/3',
        'gh api -X DELETE "repos/a/b/rulesets/$ID"',
        f"gh api graphql -f query='{GQL}'",
        "gh api graphql -f query='mutation { deleteRepositoryRuleset(input: {repositoryRulesetId: \"x\"}) "
        "{ clientMutationId } }'",
        f"Q='{GQL}'; gh api graphql -f query=\"$Q\"",
        'curl -X DELETE https://api.github.com/repos/a/b/branches/main/protection',
        'curl -sS --request DELETE https://api.github.com/repos/a/b/rulesets/12',
        'curl -X "$M" https://api.github.com/repos/a/b/branches/main/protection',
    ],
    'curl, wget, httpie': [
        f'curl -O -XPUT {U}',
        f'curl -sS -o -XPUT {U}',
        f'curl -T body.json {U}',
        f'curl --upload-file body.json {U}',
        f'curl -K curl.cfg {U}',
        f'curl --config curl.cfg {U}',
        f'curl -sXPUT {U}',
        f'curl -sX PUT {U}',
        f'curl {U} -X PUT',
        f'curl --request "PUT" {U}',
        f'curl -X $M {U}',
        f'curl -X "$M" {U}',
        f'curl $OPTS {U}',
        f'curl -H "X-HTTP-Method-Override: PUT" {U}',
        f'curl --data-binary @body.json {U}',
        f'curl -d@body.json {U}',
        f'curl -F a=b {U}',
        f'curl --json @body.json {U}',
        f'curl --next -X PUT {U}',
        f'curl -q -X PUT {U}',
        f'curl -s "$API/{P}" -X PUT',
        f'curl -s -X PUT "https://api.github.com/repos/$R/actions/$X"',
        f'"$CURL" -X PUT {U}',
        f'wget --method=PUT {U}',
        f'wget --method PUT {U}',
        f'wget --meth=PUT {U}',
        f"wget --post-data='{{\"enabled\":false}}' {U}",
        f'wget --post-file=body.json {U}',
        f'wget -e method=PUT {U}',
        f'wget -i urls.txt --method=PUT {U}',
        f'http PUT {U} enabled:=false',
        f'http {U} enabled:=false',
        f'https PUT api.github.com/{P} enabled:=false',
        f'xh put {U} enabled:=false',
        f'curlie -X PUT {U}',
    ],
    'script files, also after cd': [
        'cd sub && bash dis.sh',
        'cd sub; ./dis.sh',
        'cd sub && cd .. && cd sub && sh dis.sh',
        'pushd sub; bash dis.sh',
        'cd sub && python3 dis.py',
        'bash "$HOME/sub/dis.sh"',
        '$HOME/sub/dis.sh',
        'D=sub; bash $D/dis.sh',
        'cp sub/dis.sh /tmp/x.sh; bash sub/dis.sh',
    ],
    'a script written and run by the same command': [
        "cat > x.sh <<'EOF'\ngh workflow disable 1\nEOF\nbash x.sh",
        "cat > x.sh <<'EOF'\n#!/bin/sh\ngh workflow disable 1\nEOF\nchmod +x x.sh; ./x.sh",
        "cat > x <<'EOF'\n#!/usr/bin/env python3\ngh workflow disable 1\nEOF\nbash x",   # bash ignores the #! line
        "tee x.sh <<'EOF'\ngh workflow disable 1\nEOF\n. ./x.sh",
        "cat > /tmp/x.sh <<'EOF'\ngh workflow disable 1\nEOF\ncd /tmp && source x.sh",
        "f=$(mktemp); cat > \"$f\" <<'EOF'\ngh workflow disable 1\nEOF\nbash \"$f\"",
        "cat > x.py <<'EOF'\nimport subprocess\nsubprocess.run(['gh', 'workflow', 'disable', '1'])\nEOF\npython3 x.py",
        "cat > x.py <<'EOF'\n#!/usr/bin/env python3\nimport os\nos.system('gh workflow disable 1')\nEOF\n"
        "chmod +x x.py && ./x.py",
        "echo 'gh workflow disable 1' > x.sh; sh x.sh",
        "echo 'gh workflow disable 1' > x.sh && chmod +x x.sh && ./x.sh",
        'echo gh workflow disable 1 > x.sh; bash x.sh',
        "printf '%s\\n' 'gh workflow disable 1' > x.sh; bash x.sh",
        "printf 'true\\ngh workflow disable 1\\n' > x.sh; bash x.sh",
        "echo 'gh workflow disable 1' | tee x.sh; bash x.sh",
        "echo 'gh workflow disable 1' | cat > x.sh; bash x.sh",
        "printf 'true\\ngh workflow disable 1\\n' | sh",
        "echo \"import os; os.system('gh workflow disable 1')\" > x.py; python3 x.py",
        f"echo 'gh api -X PUT {P} {OFF}' >> x.sh; bash x.sh",
    ],
    # ---- lens round on #1543 (security NOT SAFE). The guard now fails closed by construction: a guarded token
    # passes only where the guard can prove the use harmless. Every case below passed on 7c3268aef7.
    'lens blocker: a command word the guard does not know in front': [
        'taskset 1 bash sub/dis.sh',
        'chrt -b 0 ./sub/dis.sh',
        'taskset 1 ./sub/dis.sh',
        'at -f sub/dis.sh now',
        'bash -e sub/dis.sh',
        f"flock /tmp/l curl -q -sS -X PUT {U} -d '{{\"enabled\":false}}'",
        'sg users -c "gh workflow $W 1"',
        f"sometool run \"curl -X PUT {U} -d '{{}}'\"",
        f'faketime now gh api {P}',
        "tmux new -d 'gh workflow disable 1'",
        f"docker run --rm alpine sh -c 'curl -X PUT {U}'",
        "cat > off.sh <<'EOF'\ngh workflow disable 1\nEOF\ntaskset 1 bash off.sh",
        "cat > j.txt <<'EOF'\ngh workflow disable 1\nEOF\nat -f j.txt now",
    ],
    'lens blocker: token text that goes on to a word the guard cannot read': [
        "echo 'gh workflow disable 1' | at now",
        "printf 'gh workflow disable 1\\n' > job.txt; batch < job.txt",
        "export RUN='gh workflow disable 1'; sometool",
        "PAGER='gh workflow disable 1' git log",
        "GIT_SSH_COMMAND='gh workflow disable 1' git fetch",
        "echo 'gh workflow disable 1' > notes.md; make test",
        "f() { sh -c \"$1\"; }; f 'gh workflow disable 1'",
    ],
    'lens major: gh under another name or path': [
        'cp /usr/bin/gh /tmp/x && /tmp/x workflow disable 1',
        f'ln -s "$(command -v gh)" g; ./g api -X PUT {P} {OFF}',
        'install -m755 /usr/bin/gh ./tool && ./tool workflow disable 1',
    ],
    'lens major: gh config in YAML the reader does not follow': [
        'GH_CONFIG_DIR=tag gh wd 1',
        'GH_CONFIG_DIR=qkey gh wd 1',
        'GH_CONFIG_DIR=esc gh wd 1',
    ],
    'lens major: escapes in a body or a path': [
        'gh api -X PUT repos/a/b/rulesets/1 --input rs_esc.json',
        "gh api -X PUT repos/a/b/rulesets/1 --input - <<< '{\"enforcement\": \"\\u0064isabl\\u0065d\"}'",
        "curl -q -X PUT -d '{\"enforcement\":\"\\u0064isabled\"}' https://api.github.com/repos/a/b/rulesets/1",
        f"gh api -X PUT 'repos/a/b/actions/\\u0070ermissions' {OFF}",
        "curl -q -X PUT 'https://api.github.com/repos/a/b/actions/\\u0070ermissions' -d '{\"enabled\":false}'",
    ],
    'own find: the GraphQL endpoint in another spelling, with a query the guard cannot read': [
        'gh api https://api.github.com/graphql -F query=@$S/q.graphql',
        'gh api /graphql/ -F query=@$S/q.graphql',
        "gh api 'graphql?x=1' -F query=@$S/q.graphql",
        'gh api repos/../graphql -F query=@$S/q.graphql',
        'gh api %67raphql -F query=@$S/q.graphql',
        'gh api "gra$X" -F query=@$S/q.graphql',
        'gh api "$H/graphql" -F query=@$S/q.graphql',
        'gh api {owner}/graphql -F query=@$S/q.graphql',
        # the word that looks like the path may be a flag value: the hidden word in front may be `graphql -H`
        'gh api $A repos/a/b/issues/1/comments -F query=@not-there.graphql',
        'gh api "$A" repos/a/b/issues/1/comments -F query=@not-there.graphql',
        'gh api -F a=$1 repos/a/b/issues/1/comments -F query=@not-there.graphql',
        'gh api -X$M repos/a/b/issues/1/comments -F query=@not-there.graphql',
        'gh api -H -- -XPOST graphql -F query=@$f',
    ],
    'lens major: GraphQL switches a ruleset off': [
        'gh api graphql -F query=@upd.graphql',
        f"gh api graphql -f query='{GQL_OFF}'",
        'curl -q -X POST https://api.github.com/graphql -d @upd.json',
    ],
    'lens major: code in another language deletes protection or a ruleset': [
        'python3 off.py',
        'python3 rs.py',
        'python3 -m off',
        'taskset 1 python3 off.py',
        'ipython off.py',
        'deno run off.ts',
        'node -r ./off.ts main.js',
        'tsx off.ts',
        "ruby -e 'require \"net/http\"; Net::HTTP.new(\"api.github.com\", 443).delete(\"/repos/a/b/rulesets/12\")'",
        "perl -e 'system(\"gh\",\"api\",\"-X\",\"DELETE\",\"repos/a/b/branches/main/protection\")'",
    ],
    'lens major: curl and wget read their own config files': [
        f'curl -sS {U}',
        f'CURL_HOME=cfg curl -sS {U}',
        f'wget -qO- {U}',
        f'WGETRC=wrc wget -qO- {U}',
    ],
    'lens minor: a branch name with a slash': [
        'gh api -X DELETE repos/a/b/branches/release/1.0/protection',
        'curl -q -X DELETE https://api.github.com/repos/a/b/branches/feat/x/y/protection',
        'gh api -X DELETE repos/a/b/branches/a/b/protection/required_status_checks',
    ],
}

# Harmless, but refused: the guard cannot prove it from the text. Each refusal says what to change (the hint).
OVER_REFUSED = [
    (f'curl -sS {U}', '-q'),
    (f'wget -qO- {U}', '--no-config'),
    (f'http GET {U}', 'gh api'),
    ("python3 -c \"print('gh workflow disable')\"", 'gh api'),
    ("python3 - <<'EOF'\nprint('the guard refuses gh workflow disable')\nEOF", 'edit or search tool'),
    ("cat > x.py <<'EOF'\nimport re\nprint(re.sub(r'gh workflow (enable|disable)', 'gh workflow', "
     "open('n.md').read()))\nEOF\npython3 x.py", 'edit or search tool'),
    ("sometool -m 'text about actions/permissions'", 'does not know'),
    ("awk '/actions\\/permissions/' notes.md", 'gh api'),
    ("alias t=sometool; t 'x actions/permissions'", 'does not know'),
    ("f() { echo \"$1\"; }; f 'about gh workflow disable'", 'workflow disable'),   # a function may run its words
    ("export NOTE='see actions/permissions'; gh pr list", 'exported variable'),
    ("echo '{\"query\":\"query { viewer { login } }\"}' | gh api graphql --input -", 'cannot read'),
    ('pytest tests/test_guard.py', 'name its folder'),
    ("sometool commit -m 'fix the guard' tests/test_guard.py", 'name its folder'),
]

# What still has to pass: reads, the re-enable, and ordinary writes that only look alike.
ALLOW_MORE = [
    f'gh api {P} --jq .enabled',
    f'gh api {P} -q .enabled -i',
    f'gh api -H "Accept: application/vnd.github+json" {P}',
    f'gh api -iXGET {P}',
    f'gh api -X=GET {P}',
    f'gh api --method=GET {P} -f per_page=1',
    f'gh api -X PUT -X GET {P}',
    f'gh api -p -XPUT {P}',
    f'M=GET; gh api -X $M {P}',
    f'gh api "repos/$R/actions/permissions"',
    f'gh api "repos/$R/actions/permissions" --jq .enabled',
    'gh api --paginate repos/a/b/actions/workflows --jq \'.workflows[] | select(.state != "active") | .path\'',
    'gh api -X PUT "repos/$R/actions/workflows/$W/enable"',
    'gh api "repos/$R/actions/workflows/$W"',
    f'gh api -X PUT {P} -F enabled=true -F allowed_actions=all',
    f'gh api --method PUT /{P}/ -f enabled=true',
    'gh api -X POST repos/a/b/issues/1/comments -f body="see actions/permissions and gh workflow disable"',
    'gh api -X POST repos/a/b/issues -f title=x -F body=@body.json',
    'gh api -X POST "repos/$REPO/issues/$N/comments" -f body="$B"',
    # a comment with its text from a file named by a variable is no GraphQL call (real commands of the corpus)
    'gh api -X PATCH repos/a/b/issues/comments/$id -F body=@"$S/c$id.md" --jq \'.body\'',
    'gh api -X PATCH "repos/a/b/issues/comments/$id" -F body=@"$S/c.md"',
    'gh api repos/a/b/pulls/974/comments/$id/replies -F body=@$S/r39.md --jq .html_url',
    "gh api repos/a/b/pulls/983/comments -F in_reply_to=$1 -F body=@$SP/$2.md -q '.html_url'",
    'gh api repos/a/b/issues/$n/comments --input $f',
    'gh api -X "$M" repos/a/b/issues/1/comments -F body=@$f',
    'gh api -H "Accept: $A" --hostname github.com repos/a/b/issues/1/comments -F body=@$f',
    'gh api repos/{owner}/{repo}/issues/$n/comments -F body=@$f',
    'gh api graphql -F query=@ok.graphql -F n=$n',
    'gh api -X PATCH repos/a/b -f has_issues=true',
    'gh api -X DELETE repos/a/b/git/refs/heads/work/x',
    'gh api -X DELETE "repos/a/b/git/refs/heads/$BR"',
    'gh api "$URL"',
    'gh api "$URL" --jq .state',
    "gh api graphql -f query='query { viewer { login } }'",
    "gh api graphql -f query='mutation($id:ID!){ markPullRequestReadyForReview(input:{pullRequestId:$id})"
    "{ clientMutationId } }' -f id=\"$ID\"",
    'gh api graphql -F query=@ok.graphql -f owner=a',
    'gh api repos/a/b/branches/main/protection',
    'gh api -X PUT repos/a/b/branches/main/protection --input prot.json',
    'gh api -X PATCH repos/a/b/branches/main/protection/required_status_checks -f strict=true',
    'gh api repos/a/b/rulesets',
    'gh api repos/a/b/rulesets/12',
    'gh api -X POST repos/a/b/rulesets --input rs_on.json',
    'gh api -X PUT repos/a/b/rulesets/12 --input rs_on.json',
    'gh api -X PUT repos/a/b/rulesets/12 -f enforcement=active',
    'gh -R a/b workflow list',
    'gh workflow list --json name,state -q \'.[] | select(.state=="disabled_manually")\'',
    'gh workflow view "$W"',
    'gh workflow run ci.yml -f reason="disable the flaky test"',
    'gh workflow enable "$W" -R a/b',
    'W=enable; gh workflow $W 1',
    'for w in $(gh workflow list --json id -q ".[].id"); do gh workflow enable "$w"; done',
    'gh pr view 12 --json state -q .state',
    'gh pr list --search "disable in:title"',
    'gh pr comment 12 --body "do not disable the workflow"',
    'gh run view 1 --log | rg disable',
    'gh "$SUB" list',
    'gh alias list',
    'gh alias delete co',
    "gh alias set bugs 'issue list --label=bug'",
    "gh alias set co - <<< 'pr checkout'",
    "gh alias set mine - <<< 'issue list --author=\"$1\" --label=\"$2\"'",
    'bash < sub/ok.sh',
    'bash "$HOME/sub/ok.sh"',
    'cat <<< "gh workflow disable 1"',
    'grep -c disable < al.txt',
    'gh alias import ok.yml',
    'gh alias import ok.yml && gh co 12',
    'GH_CONFIG_DIR=empty gh pr list',
    'GH_CONFIG_DIR=evil gh co 12',
    'GH_CONFIG_DIR="$(mktemp -d)" python3 -I tests.py',
    # curl and wget next to a guarded path: only with their own config switched off (-q first, --no-config)
    f'curl -q -sSL -o out.json -H "Authorization: Bearer $T" {U}',
    f'curl -q -fsS --max-time 20 {U}',
    f'curl -q -sI {U}',
    f'curl -q -s -X GET {U}',
    f'curl -q -sSLo out.json {U}',
    f'curl --disable -fsS {U}',
    f'curl -qsS {U}',
    f'CURL_HOME=cfg curl -q -s {U}',
    'curl -q -fsS https://api.github.com/repos/a/b/branches/main/protection',
    'curl -q -X PUT -d @prot.json https://api.github.com/repos/a/b/branches/main/protection',
    'curl -X POST https://api.github.com/repos/a/b/issues -d \'{"title":"x"}\'',
    'curl -sS https://api.github.com/repos/a/b/pulls',
    f'wget --no-config -qO- {U}',
    f'wget --no-config -q --header="Accept: application/json" -O out.json {U}',
    f'WGETRC=wrc wget --no-config -qO- {U}',
    'wget --no-config -qO- https://api.github.com/repos/a/b/rulesets/12',
    # lens round on #1543: what the fail-closed rules must still let through
    'ln -s /usr/bin/gh g; ./g pr list',
    'GH_CONFIG_DIR=tag gh pr list',
    'gh api repos/a/b/branches/release/1.0/protection',
    'gh api graphql -F query=@upd_on.graphql',
    "gh api graphql --input - <<< '{\"query\":\"query { viewer { login } }\"}'",
    'gh ruleset list',
    'gh ruleset view 12',
    "echo 'gh workflow disable 1' | tee notes.md",
    "echo 'gh workflow disable 1' | grep -c disable",
    'make test',
    'taskset 1 make -j4',
    "rg -n 'deleteBranchProtectionRule|updateRepositoryRuleset' scripts/",
    "git log --grep='workflow disable'",
    "gh pr comment 12 --body 'the guard now refuses updateRepositoryRuleset with DISABLED'",
    "git commit -m \"$(cat <<'EOF'\nguard: refuse gh workflow disable behind taskset\nEOF\n)\"",
    f'gh api {P} | jq .enabled',
    f"gh api {P} | python3 -c 'import json,sys; print(json.load(sys.stdin))'",
    f'X=$(gh api {P} --jq .enabled); echo "$X"',
    f'watch -n 5 gh api {P}',
    f'time gh api {P}',
    f'timeout 10 gh api {P}',  # timing: guard input, not a wait
    "gh api repos/a/b/rulesets --jq '.[].id' | while read id; do gh api \"repos/a/b/rulesets/$id\"; done",
    "export NOTE='see actions/permissions'; echo \"$NOTE\"",
    "NOTE='see actions/permissions'; gh pr comment 1 --body \"$NOTE\"",
    'python3 run_tests.py tests/test_guard.py',        # the script is read; what it is handed is its own business
    'pytest tests/',
    'sometool lint docs/guard.md',
    'git add tests/test_guard.py off.py',
    'wc -l tests/test_guard.py off.py',
    'python3 -m unittest tests/test_guard.py',
    # correctness lens m1: `.`, eval and source hide variables as the COMMAND word only, not as any word
    'for r in a-app b-app; do gh api repos/o/$r/actions/permissions --jq .; done',
    'git add . && for r in a b; do gh api repos/o/$r/actions/permissions; done',
    "find . -name '*.md'; R=a/b; gh api repos/$R/actions/permissions",
    f'M=GET; echo source vars.sh; gh api -X $M {P} {OFF}',
    f'M=GET; echo eval; gh api -X $M {P}',
    "echo 'the . and eval words'; for r in a b; do gh api repos/o/$r/actions/permissions --jq .; done",
    'git commit -m "guard: refuse gh workflow disable via alias"',
    "git config alias.st 'status -sb'",
    "git -c alias.l='!git log --oneline' l",
    'echo "gh api -X PUT repos/a/b/actions/permissions -F enabled=false" >> notes.md',
    'rg -n "X-HTTP-Method-Override|actions/permissions" scripts/',
    "find . -name '*.yml' -exec grep -l disable {} +",
    'xargs -n1 gh pr view < prs.txt',
    "ssh vm 'systemctl disable foo.timer'",
    'cd sub && bash ok.sh',
    'IFS=,; X=a,b; echo $X',
    # a config folder from command output, as test briefs ask for: fine with a real gh command
    'GH_CONFIG_DIR=$(mktemp -d) gh pr list',
    'export GH_CONFIG_DIR=$(mktemp -d); gh auth status',
    'export GH_CONFIG_DIR="$TMP/gh"; gh api repos/a/b --jq .name',
    # a command word nobody can read here, with words nobody can read here: no sign of gh or of disabling
    '"$CMD" "$@"',
    '"$TOOL" set "$KEY" "$VALUE"',
    '"$BIN" import "$FILE"',
    'run() { "$@"; }; run make test',
    'gh alias set --help',
    'gh alias import --help',
    # a Python script written and run by the same command is code, not shell text
    "cat > x.py <<'EOF'\n#!/usr/bin/env python3\nwords = ['a; b', 'c | d && e']\nprint(len(words))\nEOF\n"
    "chmod +x x.py && ./x.py",
    "cat > notes.md <<'EOF'\nThe guard refuses `gh workflow disable 1`.\nEOF\ncat notes.md",
    "echo 'gh workflow list' > x.sh; bash x.sh",
    "cat > x.sh <<'EOF'\ngh workflow enable 1\nEOF\nbash x.sh",
    'gh api "repos/a/b/rulesets/$ID" --jq .enforcement',
]


class Case(unittest.TestCase):
    """A scratch folder as cwd and HOME, and the guard loaded as a module. No token, no gh login, no network."""

    @classmethod
    def setUpClass(cls):
        cls.g = load()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.env = {'HOME': self.dir, 'GH_CONFIG_DIR': os.path.join(self.dir, 'gh')}

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self, cmd, env=None):
        return self.g.decide(cmd, self.dir, self.env if env is None else env)

    def write(self, name, text, mode=0o755):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write(text)
        os.chmod(p, mode)
        return p


EVIL_CONFIG = ('version: 1\naliases:\n    co: pr checkout\n    wd: workflow disable\n    pr wd: workflow disable\n'
               '    off: \'!gh api -X PUT "repos/$1/actions/permissions" -F enabled=false\'\n')
FILES = {
    'body.json': '{"enabled": false}\n',
    'flags.txt': '-X PUT\n',
    'ids.txt': '1\n2\n',
    'prs.txt': '12\n13\n',
    'urls.txt': U + '\n',
    'curl.cfg': 'request = PUT\n',
    'evil/config.yml': EVIL_CONFIG,                 # GH_CONFIG_DIR=evil
    'xdg/gh/config.yml': EVIL_CONFIG,               # XDG_CONFIG_HOME=xdg
    'h/.config/gh/config.yml': EVIL_CONFIG,         # HOME=h
    'empty/config.yml': 'version: 1\naliases:\n    co: pr checkout\n',
    # YAML block values: the line breaks hide the command from a line-by-line reader
    'block/config.yml': 'aliases:\n    wd: >-\n        workflow\n        disable\n    off: |\n'
                        f'        !gh api -X PUT {P} \\\n          -F enabled=false\n',
    'inline/config.yml': 'aliases: {co: pr checkout, wd: workflow disable}\n',
    # YAML this guard's small reader does not follow (an anchor): it must not pass as "no such alias"
    'odd/config.yml': 'x: &off workflow disable\naliases:\n    wd: *off\n',
    'al.txt': 'workflow disable\n',
    'al.yml': 'wd: workflow disable\n',
    'w.yml': 'w: workflow\n',
    'ok.yml': 'co: pr checkout\n',
    'rs_off.json': '{"name": "main", "enforcement": "disabled", "rules": []}\n',
    'rs_on.json': '{"name": "main", "enforcement": "active", "rules": []}\n',
    'off.txt': 'disabled\n',
    'prot.json': '{"required_status_checks": {"strict": true, "contexts": ["ci"]}, "enforce_admins": true,\n'
                 ' "required_pull_request_reviews": null, "restrictions": null}\n',
    'q.graphql': GQL + '\n',
    'gql.json': json.dumps({'query': GQL}) + '\n',
    'ok.graphql': 'query($owner: String!) { repositoryOwner(login: $owner) { login } }\n',
    'tests.py': 'print("ok")\n',
    'sub/dis.sh': '#!/bin/sh\ngh workflow disable 1\n',
    'sub/dis.py': "import subprocess\nsubprocess.run(['gh', 'workflow', 'disable', '1'])\n",
    'sub/ok.sh': '#!/bin/sh\ngh workflow list\n',
    # lens round on #1543
    'tag/config.yml': 'aliases:\n    wd: !!str workflow disable\n',     # gh reads all three as  wd: workflow disable
    'qkey/config.yml': 'aliases:\n    ? wd\n    : workflow disable\n',
    'esc/config.yml': 'aliases:\n    "w\\x64": workflow disable\n',
    'rs_esc.json': '{"enforcement": "\\u0064isabled"}\n',
    'upd.graphql': GQL_OFF + '\n',
    'upd.json': json.dumps({'query': GQL_OFF}) + '\n',
    'upd_on.graphql': GQL_OFF.replace('DISABLED', 'ACTIVE') + '\n',
    'off.py': "import requests\nrequests.delete('https://api.github.com/repos/a/b/branches/main/protection')\n",
    'rs.py': "import urllib.request as u\nu.urlopen(u.Request('https://api.github.com/repos/a/b/rulesets/12', "
             "method='DELETE'))\n",
    'off.ts': "await fetch('https://api.github.com/repos/a/b/rulesets/12', {method: 'DELETE'})\n",
    'main.js': 'console.log(1)\n',
    'run_tests.py': 'import sys\nprint(sys.argv)\n',
    'tests/test_guard.py': "CASES = ['gh workflow disable 1', 'gh api -X PUT repos/a/b/actions/permissions']\n",
    'docs/guard.md': 'PUT repos/a/b/actions/permissions enabled=false is refused\n',
    'wrc': 'method = PUT\n',
    'cfg/.curlrc': 'request = PUT\n',
    'vars.sh': 'M=PUT\n',
}


class AttackTest(Case):
    """#1436: the bypasses. Each ATTACKS case must be refused, each ALLOW_MORE case must pass."""

    def setUp(self):
        super().setUp()
        self.env = {'HOME': self.dir}   # gh's own config folder ($HOME/.config/gh) is not there: no aliases
        for name, text in FILES.items():
            self.write(name, text, 0o755 if name.endswith('.sh') else 0o644)

    def test_attacks_are_refused(self):
        for group, cmds in ATTACKS.items():
            for cmd in cmds:
                with self.subTest(group=group, cmd=cmd):
                    self.assertIsNotNone(self.decide(cmd), cmd)

    def test_the_rest_still_passes(self):
        for cmd in ALLOW_MORE:
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd), cmd)

    def test_known_over_refusals_say_what_to_change(self):
        # harmless, but not provable from the text: refused, and the message names the way that passes
        for cmd, hint in OVER_REFUSED:
            with self.subTest(cmd=cmd):
                self.assertIn(hint, self.decide(cmd) or '', cmd)

    def test_issue_1436_cases_name_what_was_read(self):
        # the exact calls of the issue: the PUT is read as a PUT, and the reason says so
        for cmd in (f'gh api -X PUT -p -XGET {P} {OFF}', f'gh api -iXPUT {P} {OFF}'):
            self.assertIn('a PUT with', self.decide(cmd), cmd)

    def test_api_parse_reads_flags_as_gh_does(self):
        parse = self.g.api_parse
        for args, flags, pos in (
                (['-X', 'PUT', '-p', '-XGET', 'p'], [('method', 'PUT'), ('preview', '-XGET')], ['p']),
                (['--jq', '--method=GET', 'p'], [('jq', '--method=GET')], ['p']),
                (['-iXPUT', 'p'], [('method', 'PUT')], ['p']),
                (['-iX', 'PUT', 'p'], [('method', 'PUT')], ['p']),
                (['-X=PUT', 'p'], [('method', 'PUT')], ['p']),
                (['-XPUT', 'p'], [('method', 'PUT')], ['p']),
                (['--method=put', 'p'], [('method', 'put')], ['p']),
                (['--method', 'PUT', 'p'], [('method', 'PUT')], ['p']),
                (['p', '-iFa=b', '-fc=d'], [('field', 'a=b'), ('raw-field', 'c=d')], ['p']),
                (['p', '--input=-', '--paginate', '--slurp'], [('input', '-')], ['p']),
                (['-F', 'a=b', '--', '-X', 'p'], [('field', 'a=b')], ['-X', 'p']),
                (['-H', 'A: b', '-q', '.x', '-t', '{{.x}}', 'p'],
                 [('header', 'A: b'), ('jq', '.x'), ('template', '{{.x}}')], ['p']),
                (['p', '-'], [], ['p', '-'])):
            with self.subTest(args=args):
                self.assertEqual(parse(args), (flags, pos, ''))
        for args in (['--nope', 'p'], ['-Z', 'p'], ['p', '-X'], ['p', '--method'], ['-iZXPUT', 'p'], ['--meth=PUT', 'p'],
                     ['--Method', 'PUT', 'p']):
            with self.subTest(args=args):
                self.assertTrue(parse(args)[2], args)   # fails closed: the caller counts it as a write

    def test_words_are_read_as_bash_reads_them(self):
        # the lexer against the real shell: harmless words only, each as bash hands them to a command
        bash = '/bin/bash' if os.path.exists('/bin/bash') else '/usr/bin/bash'
        if not os.path.exists(bash):
            self.skipTest('no bash here')
        cases = (
            "args a 'b c' \"d e\" f\\ g",
            "args 'a'\"b\"c\\d \"\" ''",
            "args $'a\\x41\\tb' $'\\101\\u0042' $'it\\'s'",
            "args \"a\\\"b\" \"a\\$b\" \"a\\b\" 'a\\b'",
            "args a\\\nb c \\\n d",
            "args a#b # c d\n",
            "args 'a\nb' \"c\nd\"",
            "X=1; args \"$X\" ${X}2 '$X' \"${X}\" $X$X",
            "X='a b'; args \"$X\" $X",
            "X=a; Y=${X}b; args $Y \"$Y$X\"",
            "args a;args b",
            "args a&&args b",
            "args <<EOF a\ntext $X\nEOF\nargs b",
            "args <<'EOF' a\ngh not a command $(x)\nEOF\nargs b",
            "args a 2> /dev/null b 2>&1 < /dev/null c",
            "args --flag=value -x -- -y",
            "args \"a 'b' c\" 'a \"b\" c'",
            "args a~b a=b =c",
        )
        for text in cases:
            with self.subTest(text=text):
                r = subprocess.run([bash, '--norc', '--noprofile', '-c',
                                    'args() { printf "%s\\0" "$@"; }\n' + text],
                                   capture_output=True, text=True, cwd=self.dir, timeout=60,
                                   env={'PATH': '/usr/bin:/bin', 'HOME': self.dir})
                want = r.stdout.split('\0')[:-1]
                got = [t for seg in self.g.segments(text) if seg[:1] == ['args'] for t in seg[1:]]
                self.assertEqual(got, want, r.stderr)

    def test_a_long_command_is_decided_in_time(self):
        # the hook has a time limit; a long, nested, many-valued command must not run it out
        import time
        cmd = '; '.join(f'V{i}=a{i}; W{i}=${{V{i}}}{{x,y}}$(echo "$(echo {i})")' for i in range(60))
        cmd += '; ' + ' '.join(f'"$V{i}"$W{i}' for i in range(60)) + '; gh workflow $W1 $W2 1; gh api "$V1/$W2" -X GET'
        cmd += '\n' + 'echo text of a long heredoc line\n' * 3000
        t = time.monotonic()
        self.decide(cmd)
        self.assertLess(time.monotonic() - t, 8)   # timing: the hook's 10 s limit; a slower guard is the bug

    def test_out_of_time_fails_closed(self):
        # A hook that is not done at its time limit does not stop the command. So the guard stops itself first
        # and refuses: what it could not read to the end (a script the command runs, a word put together from
        # variables) may be the disabling call.
        for cmd in ('A=dis; B=able; gh workflow $A$B 1', 'bash sub/dis.sh', 'bash sub/ok.sh', 'ls -la $X'):
            with self.subTest(cmd=cmd):
                self.assertIn('could not finish', self.g.decide(cmd, self.dir, self.env, budget=-1) or '', cmd)
        # #1752: a command the fast exit passes needs no time at all (it reads nothing), so it passes out of time too
        self.assertIsNone(self.g.decide('ls -la', self.dir, self.env, budget=-1))
        self.assertIsNone(self.g.decide('gh pr list', self.dir, self.env, budget=60))
        self.assertIsNone(self.g.decide('gh pr list', self.dir, self.env))
        self.assertFalse(issubclass(self.g.Slow, Exception))   # an `except Exception` in the guard cannot swallow it
        self.assertLessEqual(self.g.BUDGET, 5.0)   # #1752: well under the 10 s limit the hook is registered with

    def test_a_script_is_read_once_however_often_it_is_run(self):
        # a script that runs its helper two hundred times (and itself) must not take two hundred reads
        import time
        self.write('helper.sh', '#!/bin/sh\n' + 'gh pr view "$1" --json state\n' * 40)
        self.write('big.sh', '#!/bin/sh\n' + './helper.sh 1\n' * 200 + './big.sh again\n')
        t = time.monotonic()
        self.assertIsNone(self.decide('./big.sh'))
        self.assertLess(time.monotonic() - t, 4)   # timing: one read per script; 200 reads would take far longer
        self.write('helper.sh', '#!/bin/sh\ngh workflow disable "$1"\n')
        why = self.decide('./big.sh')
        self.assertIn('big.sh', why)
        self.assertIn('helper.sh', why)   # the reason names the script that holds the call

    def test_an_exact_copy_of_the_guard_is_the_guard(self):
        # the repo copy next to the installed one: running it (for a manual check) is not "a script that disables"
        with open(HOOK, encoding='utf-8') as f:
            text = f.read()
        self.write('copy/actions-disable-guard', text)
        self.assertIsNone(self.decide('echo {} | python3 -I copy/actions-disable-guard'))
        self.write('copy/other-guard', text + "\nsubprocess.run(['gh', 'workflow', 'disable', '1'])\n")
        self.assertIsNotNone(self.decide('python3 -I copy/other-guard'))

    def test_alias_from_config_and_from_the_text_itself(self):
        # an alias of the gh config next to one set in the same command: both are followed
        self.write('.config/gh/config.yml', 'aliases:\n    w: workflow\n', 0o644)
        self.assertIsNotNone(self.decide('gh w disable 1'))
        self.assertIsNone(self.decide('gh w list'))
        self.assertIsNotNone(self.decide("gh alias set d 'w disable' && gh d 1"))
        self.assertIsNotNone(self.decide("gh alias set d 'w disable'"))

    def test_body_files_are_read_when_the_call_is_checked(self):
        # the same call passes or is refused by what the file holds now
        self.write('rs.json', FILES['rs_on.json'], 0o644)
        cmd = 'gh api -X PUT repos/a/b/rulesets/1 --input rs.json'
        self.assertIsNone(self.decide(cmd))
        self.write('rs.json', FILES['rs_off.json'], 0o644)
        self.assertIsNotNone(self.decide(cmd))
        # a file written by the same command is not there yet: its text is read from the command
        for cmd in ("echo '{\"enforcement\": \"disabled\"}' > new.json && gh api -X PUT repos/a/b/rulesets/1 "
                    "--input new.json",
                    'printf \'{\\"enforcement\\":\\"disabled\\"}\' > new.json; gh api -X PUT repos/a/b/rulesets/1 '
                    '--input new.json',
                    'gh api -X PUT repos/a/b/rulesets/1 --input "$(ls *.json | head -1)"',
                    'python3 gen.py | gh api -X PUT repos/a/b/rulesets/1 --input -'):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)


class GuardTest(Case):
    def test_deny(self):
        for cmd in DENY:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)

    def test_allow(self):
        for cmd in ALLOW:
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd), cmd)

    def test_reason_names_rule_owner_and_marker(self):
        why = self.decide('gh workflow disable 1')
        for part in ('owner', 'refuse', 'sudo agent-guards approve', MARK_CMD + ':<code>', 'must not',
                     'without this machine\'s hooks'):
            self.assertIn(part, why)
        self.assertNotIn(MARK_ENV, why)

    # scripts the command runs
    def test_shell_script_file(self):
        self.write('dis.sh', '#!/bin/bash\nset -e\nfor i in 1 2; do\n  gh workflow disable "$i" -R a/b\ndone\n')
        for cmd in ('bash dis.sh', 'sh ./dis.sh', './dis.sh', 'source dis.sh', '. dis.sh',
                    f'{self.dir}/dis.sh', 'cd /tmp && bash ' + os.path.join(self.dir, 'dis.sh'), 'timeout 9 ./dis.sh'):  # timing: guard input, not a wait
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)

    def test_python_script_file(self):
        self.write('a.py', "import subprocess\nsubprocess.run(['gh', 'workflow', 'disable', '1'])\n")
        self.write('b.py', "import urllib.request as u\nu.urlopen(u.Request("
                           "'https://api.github.com/repos/a/b/actions/workflows/1/disable', method='PUT'))\n")
        self.write('c.py', "requests.put(f'{API}/repos/{repo}/actions/permissions', json={'enabled': False})\n")
        for cmd in ('python3 a.py', 'python3 -u b.py --x', './c.py', 'uv run c.py', 'python3 -I a.py'):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)

    def test_script_on_path_in_home(self):
        self.write('.local/bin/wfoff', '#!/bin/sh\ngh api -X PUT "repos/$1/actions/workflows/$2/disable"\n')
        env = dict(self.env, PATH=os.path.join(self.dir, '.local/bin') + ':/usr/bin')
        self.assertIsNotNone(self.decide('wfoff a/b 1', env))

    def test_harmless_scripts_pass(self):
        # messages that only mention the command (a sync script, example_garden.py read-only) are no call
        self.write('ok.py', "print(f'disable: `gh workflow disable {wid} -R {repo}`')\n"
                            "subprocess.run(['gh', 'workflow', 'enable', wid])\n"
                            "gh('api', api_path('repos', repo, 'actions/permissions'))\n")
        self.write('ok.sh', '#!/bin/sh\necho "run gh workflow enable"\ngh api repos/a/b/actions/permissions\n')
        self.write('plain.py', "print('hello')\n")
        for cmd in ('python3 plain.py', './plain.py', 'taskset 1 ./plain.py', 'bash ok.sh', './ok.sh',
                    'python3 missing.py', 'bash'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd), cmd)
        # Code in another language that names a guarded token: the guard reads shell, not Python, so it cannot
        # tell the mention from the call (lens round on #1543: requests.delete(...) of branch protection passed).
        # Known over-refusal; the tests of such a tool run through run_tests.py, which is read and names none.
        self.assertIn('cannot prove', self.decide('python3 ok.py'))

    def test_text_files_and_the_guard_itself_are_not_scripts(self):
        self.write('docs/plan.md', 'Then PUT repos/a/b/actions/permissions enabled=false.\n', 0o644)
        self.write('notes.txt', 'gh workflow disable 1\n', 0o644)
        for cmd in ('docs/plan.md', './notes.txt', f'{self.dir}/docs/plan.md',
                    'echo {} | python3 -I ' + HOOK, HOOK + ' < event.json'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd), cmd)
        self.assertIsNotNone(self.decide('bash notes.txt'))   # run through a shell, any file is a script

    def test_a_listed_script_with_its_flag_is_refused_read_only_passes(self):
        self.write('scripts/dev/example_garden.py', "print('read-only checks')\n")
        self.assertIsNotNone(self.decide('python3 scripts/dev/example_garden.py --fix'))
        self.assertIsNotNone(self.decide('python3 scripts/dev/example_garden.py --only minestom --fix --days 3'))
        self.assertIsNone(self.decide('python3 scripts/dev/example_garden.py'))
        self.assertIsNone(self.decide('python3 scripts/dev/example_garden.py --only minestom'))
        self.assertIsNone(self.decide('echo "the gardener ran example_garden.py --fix" >> notes.md'))
        self.assertIsNone(self.decide('gh issue comment 1 --body "held (see #719; run example_garden.py --fix later)"'))

    def test_stdin_script_argument_is_data(self):
        self.write('body.md', 'PUT repos/a/b/actions/permissions enabled=false\n', 0o644)
        self.assertIsNone(self.decide("python3 - body.md <<'EOF'\nimport sys\nprint(open(sys.argv[1]).read())\nEOF"))

    def test_existing_gh_alias_is_expanded(self):
        self.write('gh/config.yml', 'version: 1\naliases:\n    co: pr checkout\n    wd: workflow disable\n'
                                    '    off: \'!gh api -X PUT repos/$1/actions/permissions -F enabled=false\'\n', 0o644)
        self.assertIsNotNone(self.decide('gh wd 1'))
        self.assertIsNotNone(self.decide('gh off a/b'))
        self.assertIsNone(self.decide('gh co 12'))

    def test_alias_import_file(self):
        self.write('al.yml', 'wd: workflow disable\n', 0o644)
        self.assertIsNotNone(self.decide('gh alias import al.yml'))

    # the owner's approval (lens B major): a root-owned file `sudo agent-guards approve` writes, and the code it
    # prints in the command. A session variable reached every nested agent; the old one now opens nothing.
    def approve(self, hours=1, now=None):
        """An approval file in this test's folder, owned by the test user (approval.PATH, approval.OWNER_UID);
        (the approval module, the marker with its code)."""
        from agent_guards import approval
        for name, value in (('PATH', os.path.join(self.dir, 'etc', 'approval.json')), ('OWNER_UID', os.geteuid())):
            self.addCleanup(setattr, approval, name, getattr(approval, name))
            setattr(approval, name, value)
        code = approval.new_code()
        approval.write(code, hours, now=now)
        return approval, approval.MARK + code

    def test_the_old_variable_and_marker_open_nothing(self):
        env = dict(self.env, **{MARK_ENV: today()})
        for cmd in (f'gh workflow disable 1 -R a/b {MARK_CMD}', f'gh workflow disable 1 {MARK_CMD}:',
                    f'gh workflow disable 1 {MARK_CMD}:abc'):
            with self.subTest(cmd):
                self.assertIsNotNone(self.decide(cmd, env))
        self.write('.claude/settings.json', json.dumps({'env': {MARK_ENV: today()}}), 0o644)
        self.assertIsNotNone(self.decide(f'gh workflow disable 1 {MARK_CMD}', env))

    def test_an_approval_needs_the_owners_file_and_its_code(self):
        approval, mark = self.approve()
        cmd = 'gh workflow disable 1 -R a/b'
        self.assertIsNone(self.decide(f'{cmd} {mark}'))
        self.assertIsNone(self.decide(f'{cmd}  {mark} (the owner asked for it in issue 9)'))
        self.assertIsNotNone(self.decide(cmd))                                   # no code: refused
        why = self.decide(f'{cmd} {approval.MARK}{approval.new_code()}')         # a made-up code
        self.assertIn('not the one the owner approved', why)
        self.assertIsNotNone(self.decide(f'{cmd} {mark}x'))                      # not the code: longer
        self.assertIsNotNone(self.decide(f'{cmd} {mark[:-1]}'))                  # not the code: shorter
        with open(approval.PATH) as f:
            self.assertNotIn(mark.rpartition(':')[2], f.read())                  # only its hash is kept
        self.assertTrue(approval.revoke())
        self.assertIn('not there', self.decide(f'{cmd} {mark}'))

    def test_an_approval_that_is_over_or_not_safe_opens_nothing(self):
        import time
        approval, mark = self.approve(hours=1, now=time.time() - 7200)
        self.assertIn('the approval is over', self.decide(f'gh workflow disable 1 {mark}'))
        approval, mark = self.approve()
        cmd = f'gh workflow disable 1 {mark}'
        os.chmod(approval.PATH, 0o666)                                            # an agent could write it
        self.assertIn('writable by group or others', self.decide(cmd))
        os.chmod(approval.PATH, 0o644)
        os.rename(approval.PATH, approval.PATH + '.real')
        os.symlink(approval.PATH + '.real', approval.PATH)
        self.assertIn('symbolic link', self.decide(cmd))
        os.unlink(approval.PATH)
        for text in ('', '{', '[]', '{"version": 1}', '{"version": 2, "sha256": "' + 'a' * 64 + '", "until": 9e9}',
                     '{"version": 1, "sha256": "x", "until": 9e9}', '{"version": 1, "sha256": "' + 'a' * 64
                     + '", "until": "9e9"}'):
            with self.subTest(text):
                with open(approval.PATH, 'w') as f:
                    f.write(text)
                self.assertIsNotNone(self.decide(cmd))
        with self.assertRaises(ValueError):
            approval.write(approval.new_code(), approval.MAX_HOURS + 1)

    # nested agent sessions (lens B minor): one without this machine's hooks would run anything
    def test_a_nested_session_without_the_hooks_is_refused(self):
        refused = ['claude --bare -p "x"', 'claude -p --bare x', 'claude --safe-mode -p x', 'claude --bare=1 -p x',
                   'CLAUDE_CODE_SIMPLE=1 claude -p x', 'env CLAUDE_CODE_SIMPLE=1 claude -p x',
                   'export CLAUDE_CODE_SIMPLE=1; claude -p x',
                   'claude -p --settings \'{"env": {"CLAUDE_CODE_SIMPLE": "1"}}\' x',
                   '/usr/local/bin/claude --bare -p x', 'bash -c "claude --bare -p x"',
                   'codex exec "x"', 'codex "x"', 'codex', 'codex --full-auto exec x', 'cd /tmp && codex exec x',
                   'timeout 60 codex exec x', 'bash -c "codex exec x"']
        for cmd in refused:
            with self.subTest(cmd):
                why = self.decide(cmd)
                self.assertIsNotNone(why, cmd)
                self.assertIn('without this machine\'s hooks' if 'claude' in cmd else 'runs no Claude Code hooks',
                              why)
        # managed hooks run in a nested `claude -p` whatever its own settings say: disableAllHooks in a session's
        # or user's settings does not turn managed hooks off
        passed = ['claude -p "x"', 'claude -p --settings \'{"disableAllHooks": true}\' x', 'claude --version',
                  'claude mcp list', 'codex login', 'codex --version', 'codex --help', 'codex help exec',
                  'echo "codex exec x"', 'git commit -m "no claude --bare here"', 'grep -rn CLAUDE_CODE_SIMPLE .']
        for cmd in passed:
            with self.subTest(cmd):
                self.assertIsNone(self.decide(cmd), cmd)

    def test_the_owner_can_approve_a_nested_session_too(self):
        _approval, mark = self.approve()
        self.assertIsNone(self.decide(f'codex exec x {mark}'))

    # rulesets: "evaluate" only reports, so it switches the rules off as "disabled" does (lens B minor)
    def test_a_ruleset_set_to_evaluate_is_refused(self):
        self.write('rs_eval.json', '{"name": "main", "enforcement": "evaluate", "rules": []}\n', 0o644)
        for cmd in ('gh api -X PUT repos/a/b/rulesets/1 -f enforcement=evaluate',
                    'gh api -X PUT repos/a/b/rulesets/1 -f enforcement=EVALUATE',
                    'gh api -X PUT repos/a/b/rulesets/1 --input rs_eval.json',
                    'gh api -X PUT repos/a/b/rulesets/1 --input - <<EOF\n{"enforcement": "evaluate"}\nEOF',
                    'curl -X PUT -d \'{"enforcement":"evaluate"}\' https://api.github.com/repos/a/b/rulesets/1',
                    'curl -X PUT -d @rs_eval.json https://api.github.com/repos/a/b/rulesets/1',
                    "gh api graphql -f query='mutation { updateRepositoryRuleset(input: {repositoryRulesetId: \"x\", "
                    "enforcement: EVALUATE}) { clientMutationId } }'"):
            with self.subTest(cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)
        self.assertIsNone(self.decide('gh api -X PUT repos/a/b/rulesets/1 -f enforcement=active'))

    # the hook as Claude Code runs it: JSON on stdin, JSON decision on stdout
    def pipe(self, payload, env=None):
        e = {'PATH': '/usr/bin:/bin', 'HOME': self.dir}
        e.update(env or {})
        return subprocess.run([sys.executable, '-I', HOOK], input=payload, capture_output=True, text=True,
                              env=e, timeout=60)

    def event(self, cmd, tool='Bash'):
        return json.dumps({'session_id': 't', 'hook_event_name': 'PreToolUse', 'tool_name': tool, 'cwd': self.dir,
                           'tool_input': {'command': cmd, 'description': 'x'}})

    def test_pipe_deny(self):
        r = self.pipe(self.event('gh workflow disable 999999999 -R example/no-such-scratch-repo'))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)['hookSpecificOutput']
        self.assertEqual(out['hookEventName'], 'PreToolUse')
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('owner', out['permissionDecisionReason'])

    def test_pipe_deny_for_a_monitor_command(self):
        # lens B major: the Monitor tool runs its command as shell text too; Write is not a command and passes
        r = self.pipe(self.event('gh workflow disable 1 -R example/repo', tool='Monitor'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        r = self.pipe(self.event('gh workflow disable 1 -R example/repo', tool='Write'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('permissionDecision', r.stdout)

    def test_pipe_refusal_is_logged_and_a_failed_log_still_refuses(self):
        home = os.path.join(self.dir, 'loghome')
        os.makedirs(home)
        log = os.path.join(home, '.local', 'state', 'actions-disable-guard', 'log')
        for n in (1, 2):   # no folder yet: it is made; a second refusal adds a line
            r = self.pipe(self.event('gh workflow disable 1'), {'HOME': home})
            self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
            with open(log) as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), n)
            self.assertIn('gh workflow disable 1', json.loads(lines[-1].split(' ', 1)[1])['cmd'])
        self.pipe(self.event('ls'), {'HOME': home})   # what passes writes nothing
        with open(log) as f:
            self.assertEqual(len(f.read().splitlines()), 2)
        blocked = os.path.join(self.dir, 'nolog')
        os.makedirs(blocked)
        with open(os.path.join(blocked, '.local'), 'w') as f:   # the log folder cannot be made
            f.write('a file, not a folder')
        r = self.pipe(self.event('gh workflow disable 1'), {'HOME': blocked})
        self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_pipe_allow_is_silent(self):
        for payload in (self.event('gh workflow enable 1'), self.event('ls'),
                        self.event('gh workflow disable 1', tool='Read')):
            r = self.pipe(payload)
            self.assertEqual((r.returncode, r.stdout), (0, ''), r.stderr)

    def test_pipe_bad_input_fails_closed_only_when_it_looks_dangerous(self):
        r = self.pipe('{"tool_name": "Bash", "tool_input": {"command": ')   # cut off
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        r = self.pipe('not json but gh workflow disable 1')
        self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_pipe_internal_error_fails_closed(self):
        r = self.pipe(self.event('gh workflow disable 1'), {'ACTIONS_DISABLE_GUARD_TEST_CRASH': '1'})
        self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        r = self.pipe(self.event('ls -la'), {'ACTIONS_DISABLE_GUARD_TEST_CRASH': '1'})
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        # a call the plain word search would miss: gh with the word put together from variables
        r = self.pipe(self.event('A=dis; gh "work""flow" ${A}able 1'), {'ACTIONS_DISABLE_GUARD_TEST_CRASH': '1'})
        self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_hook_out_of_time_refuses(self):
        # the whole hook, with no time at all: it answers "deny" itself instead of running into the hook limit
        import contextlib, io
        from unittest import mock
        for cmd, verdict in (('bash x.sh', 'deny'), ('ls', None)):   # #1752: the fast exit needs no time
            out = io.StringIO()
            with mock.patch.object(self.g, 'BUDGET', -1), \
                    mock.patch.object(sys, 'stdin', io.StringIO(self.event(cmd))), \
                    mock.patch.dict(os.environ, {'HOME': self.dir}), contextlib.redirect_stdout(out):
                self.assertEqual(self.g.main(), 0)
            if verdict is None:
                self.assertEqual(out.getvalue(), '', cmd)
                continue
            got = json.loads(out.getvalue())['hookSpecificOutput']
            self.assertEqual(got['permissionDecision'], 'deny')
            self.assertIn('could not finish', got['permissionDecisionReason'])

    # correctness lens m2 on #1543: the time budget counts from the start of the hook process, not from decide()
    def test_time_already_used_before_checking_counts(self):
        import contextlib, io
        from unittest import mock
        out = io.StringIO()
        with mock.patch.object(self.g, 'process_age', lambda: self.g.BUDGET + 1), \
                mock.patch.object(sys, 'stdin', io.StringIO(self.event('bash x.sh'))), \
                mock.patch.dict(os.environ, {'HOME': self.dir}), contextlib.redirect_stdout(out):
            self.assertEqual(self.g.main(), 0)
        got = json.loads(out.getvalue())['hookSpecificOutput']
        self.assertEqual(got['permissionDecision'], 'deny')
        self.assertIn('could not finish', got['permissionDecisionReason'])

    def test_process_age_is_never_less_than_the_time_since_load(self):
        import time
        since = time.monotonic() - self.g.START
        self.assertGreaterEqual(self.g.since_start(), since - 0.05)
        self.assertEqual(self.g.process_age(), 0.0)   # loaded as a module (tests, gh-as): no hook clock
        self.assertLessEqual(self.g.BUDGET, 5.0)      # #1752: at least 5 s under Claude Code's 10 s hook limit

    def test_a_slow_start_of_the_hook_eats_the_budget(self):
        # The real hook as a process that is slow before it checks anything (a loaded machine, slow imports, a slow
        # stdin): with a 1 s budget and 1.3 s gone before main() runs it must refuse, not start a fresh budget.
        # #1752: a command the fast exit passes still passes (it is decided without any file or clock).
        with open(HOOK, encoding='utf-8') as f:
            text = f.read()
        last = "\nif __name__ == '__main__':"
        self.assertIn('\nBUDGET = 5.0\n', text)
        self.assertIn(last, text)
        body, _, tail = text.replace('\nBUDGET = 5.0\n', '\nBUDGET = 1.0\n', 1).rpartition(last)
        self.write('slow-hook', body + '\ntime.sleep(1.3)' + last + tail)
        for cmd in ('bash x.sh', 'ls -la'):
            r = subprocess.run([sys.executable, '-I', os.path.join(self.dir, 'slow-hook')], input=self.event(cmd),
                               capture_output=True, text=True, env={'PATH': '/usr/bin:/bin', 'HOME': self.dir},
                               timeout=60)
            if cmd == 'ls -la':   # passed: no decision (a copy without the kit's lib only says so, config.notice)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn('permissionDecision', r.stdout)
                continue
            got = json.loads(r.stdout)['hookSpecificOutput']
            self.assertEqual(got['permissionDecision'], 'deny')
            self.assertIn('could not finish', got['permissionDecisionReason'])


class LensRound1Test(Case):
    """Lens round on #1543: what the lists above do not hold (files with the executable bit, state, m1)."""

    def setUp(self):
        super().setUp()
        self.env = {'HOME': self.dir}
        for name, text in FILES.items():
            self.write(name, text, 0o755 if name.endswith('.sh') else 0o644)

    def test_an_executable_script_in_another_language(self):
        self.write('offx.py', '#!/usr/bin/env python3\n' + FILES['off.py'])
        self.write('okx.py', "#!/usr/bin/env python3\nprint('hello')\n")
        for cmd in ('./offx.py', 'taskset 1 ./offx.py', 'sometool run offx.py'):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)
        for cmd in ('./okx.py', 'taskset 1 ./okx.py', 'sometool run okx.py', 'cp offx.py /tmp/x.py',
                    "git add offx.py && git commit -m 'fix the guard' offx.py", 'sometool commit -m fix .'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd), cmd)

    def test_source_and_eval_as_the_command_word_still_hide_variables(self):
        # correctness lens m1: the other half. These set M where the guard cannot see it: the call counts as a write
        for cmd in (f'M=GET; . ./vars.sh; gh api -X $M {P} {OFF}',
                    f'M=GET; source vars.sh; gh api -X $M {P} {OFF}',
                    f'M=GET; eval "$Z"; gh api -X $M {P} {OFF}',
                    f'M=GET; command eval "$Z"; gh api -X $M {P} {OFF}',
                    f'M=GET; builtin source vars.sh; gh api -X $M {P} {OFF}',
                    f'M=GET; true && . vars.sh; gh api -X $M {P} {OFF}',
                    f"M=GET; trap 'M=PUT' EXIT; gh api -X $M {P} {OFF}",
                    f'M=GET; f() {{ . ./vars.sh; }}; f; gh api -X $M {P} {OFF}'):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)

    def test_curl_and_wget_config_rule(self):
        # (c) of the round: the flag rule. It is read from the command text alone, so it holds whatever the rc
        # files say, wherever the tool looks for them, and whatever the same command writes there first.
        self.write('.curlrc', 'request = PUT\ndata = "{\\"enabled\\": false}"\n', 0o644)
        self.write('.wgetrc', 'method = PUT\n', 0o644)
        self.assertIn('-q', self.decide(f'curl -sS {U}'))
        self.assertIn('--no-config', self.decide(f'wget -qO- {U}'))
        for cmd in (f'curl -s -q {U}',          # -q is only honoured as the first argument
                    f"printf '%s\\n' 'request = PUT' > ~/.curlrc; curl -sS {U}",
                    f'ionice -c3 wget --method=PUT --no-config {U}',
                    f'curl -q -X PUT {U} -d @body.json'):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)
        for cmd in (f'curl -q -sS {U}', f'curl --disable -sS {U}', f'wget --no-config -qO- {U}',
                    f"printf '%s\\n' 'request = PUT' > ~/.curlrc; curl -q -sS {U}"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd), cmd)

    def test_a_file_under_the_name_of_a_wrapper_or_a_shell(self):
        # found while fixing the round (the old guard passes them too): `bash nice` runs the FILE nice of the
        # folder, `./sudo` is a file of the project. Neither is the wrapper of that name.
        for name in ('nice', 'sudo', 'sh', 'wrap/env'):
            self.write(name, 'gh workflow disable 1\n')
        self.write('fine/sudo', 'echo hi\n')
        self.write('ok.sh', '#!/bin/sh\necho hi\n')
        for cmd in ('bash nice', 'bash sh', 'sh -x nice', 'env bash nice', 'timeout 5 bash sh', 'source nice',  # timing: guard input, not a wait
                    '. sudo', './sudo', './sudo ls', 'wrap/env ls', 'nice -n 5 ./sudo'):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)
        for cmd in ('sudo ls', 'nice -n 5 ls', '/usr/bin/env ls', 'busybox sh -c "nice ls"', 'bash -c "nice ls"',
                    'fine/sudo ls', 'bash ok.sh', 'env -i bash ok.sh'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd), cmd)

    def test_a_file_that_names_a_token_goes_on_under_another_name(self):
        # (a) of the round: a file the command reads or writes. cp/mv/ln give its text a new name, and the same
        # command then runs a word the guard cannot read.
        self.write('ok.sh', '#!/bin/sh\necho hi\n')
        self.write('ok.py', "print('hello')\n", 0o644)
        for cmd in ('cp sub/dis.sh run.sh && bash run.sh', 'cp off.py run.py && python3 run.py',
                    'mv sub/dis.sh x; sometool x', 'ln -s sub/dis.sh x && ./x', 'cat sub/dis.sh > x; sometool x'):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)
        for cmd in ('cp sub/dis.sh keep.txt', 'cp off.py backup/ && git status', 'mv off.py lib/off.py; ls lib',
                    'cp ok.sh run.sh && bash run.sh', 'cp ok.py x.py && sometool x.py'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd), cmd)

    def test_arguments_that_come_from_a_file(self):
        # curl -K, wget -i, xargs -a: the request is in a file, the command text names no token at all
        self.write('k.cfg', f'url = "{U}"\nrequest = PUT\ndata = "{{\\"enabled\\": false}}"\n', 0o644)
        self.write('urls.txt', U + '\n', 0o644)
        self.write('fine.cfg', 'url = "https://api.github.com/repos/a/b/pulls"\n', 0o644)
        for cmd in ('curl -q -K k.cfg', 'curl -q -sK k.cfg', 'curl -q -Kk.cfg', 'curl -qK k.cfg',
                    'curl -q --config k.cfg', 'curl -q --config=k.cfg', 'wget --no-config -i urls.txt --method=PUT',
                    'wget --no-config --input-file=urls.txt --post-data=enabled=false', 'xargs -a k.cfg curl -q',
                    'X=$(cat urls.txt); curl -q -X PUT "$X" -d x', 'curl -q -X PUT "$(cat urls.txt)" -d x'):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)
        for cmd in ('curl -q -K fine.cfg', 'curl -q -sK fine.cfg', 'cat urls.txt', 'xargs -a fine.cfg echo',
                    'curl -q -s "https://api.github.com/repos/$R/pulls"',
                    'curl -q -s -H "Authorization: Bearer $(gh auth token)" ' + U):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd), cmd)

    def test_state_half_written_empty_and_missing(self):
        # State question of the round: the guard keeps no state of its own; what it meets on disk may be old,
        # half-written or missing.
        self.write('half/config.yml', "version: 1\naliases:\n    co: pr checkout\n    wd: 'workflow dis", 0o644)
        self.write('nul/config.yml', '', 0o644)
        self.assertIsNotNone(self.decide('GH_CONFIG_DIR=half gh wd 1'))    # the alias may be the cut-off one
        self.assertIsNotNone(self.decide('GH_CONFIG_DIR=half gh co 1'))    # known over-refusal: no gh command
        self.assertIsNone(self.decide('GH_CONFIG_DIR=half gh pr list'))    # a real gh command needs no alias table
        for cfg in ('nul', 'nowhere'):   # an empty file, no folder: no aliases, gh itself rejects the word
            self.assertIsNone(self.decide(f'GH_CONFIG_DIR={cfg} gh pr list'))
            self.assertIsNotNone(self.decide(f'GH_CONFIG_DIR={cfg} gh workflow disable 1'))
        for env in ({}, {'HOME': ''}, {'HOME': os.path.join(self.dir, 'gone')}):   # no HOME at all
            with self.subTest(env=env):
                self.assertIsNone(self.g.decide('gh pr list', self.dir, dict(env)))
                self.assertIsNone(self.g.decide(f'curl -q -sS {U}', self.dir, dict(env)))
                self.assertIsNotNone(self.g.decide('gh workflow disable 1', self.dir, dict(env)))
                self.assertIsNotNone(self.g.decide('taskset 1 bash sub/dis.sh', self.dir, dict(env)))
        # run again: the same answer every time, and nothing written by a command that passes
        before = sorted(os.listdir(self.dir))
        self.assertEqual([self.decide(f'gh api {P}') for _ in range(3)], [None] * 3)
        self.assertEqual(sorted(os.listdir(self.dir)), before)


class LensSecurityR1Test(Case):
    """Lens security (guard bypass) on #1543: every command here must be refused; each fails on 7c3268a."""

    def refused(self, *cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)

    def test_unlisted_launcher_runs_shell_text_or_a_client(self):
        self.refused("taskset -c 0 bash -c 'gh workflow disable 1'",
                     "chrt 0 sh -c 'gh workflow disable 1'",
                     "unshare -r bash -c 'gh workflow disable 1'",
                     "sg users -c 'gh workflow disable 1'",
                     "screen -dm bash -c 'gh workflow disable 1'",
                     "tmux new-session -d 'gh workflow disable 1'",
                     "at now <<< 'gh workflow disable 1'",
                     "expect -c 'spawn gh workflow disable 1'",
                     "taskset 1 curl -sS -X PUT https://api.github.com/repos/a/b/actions/permissions "
                     "-d '{\"enabled\":false}'",
                     "taskset 1 python3 -c \"import requests; "
                     "requests.put('https://api.github.com/repos/a/b/actions/permissions')\"")

    def test_gh_binary_under_another_name(self):
        self.refused('ln -sf /usr/bin/gh ./g && ./g workflow disable 1',
                     'cp /usr/bin/gh ./hub; ./hub api -X PUT repos/a/b/actions/permissions -F enabled=false')

    def test_gh_config_yaml_forms_gh_reads(self):
        # gh 2.102 `gh alias list` shows both as  wd: workflow disable
        for text in ('aliases:\n    wd: !!str workflow disable\n', 'aliases:\n    ? wd\n    : workflow disable\n'):
            with self.subTest(text=text):
                self.write('gh/config.yml', text, 0o644)
                self.assertIsNotNone(self.decide('gh wd 1'))

    def test_json_escapes_and_graphql_ruleset_off(self):
        self.write('rs.json', '{"enforcement": "\\u0064isabled"}\n', 0o644)
        self.write('gq.json', '{"query": "mutation { deleteBranchProtectionRul\\u0065(input: '
                              '{branchProtectionRuleId: \\"x\\"}) { clientMutationId } }"}\n', 0o644)
        self.refused('gh api -X PUT repos/a/b/rulesets/1 --input rs.json',
                     "gh api -X PUT repos/a/b/rulesets/1 --input - <<'E'\n{\"enforcement\": \"\\u0064isabled\"}\nE",
                     'gh api graphql --input gq.json',
                     "gh api graphql -f query='mutation { updateRepositoryRuleset(input: "
                     "{repositoryRulesetId: \"x\", enforcement: DISABLED}) { clientMutationId } }'")

    def test_code_that_deletes_protection_or_a_ruleset(self):
        self.refused("python3 -c \"import requests; "
                     "requests.delete('https://api.github.com/repos/a/b/branches/main/protection')\"",
                     "node -e \"fetch('https://api.github.com/repos/a/b/rulesets/1', {method: 'DELETE'})\"")

    def test_curlrc_makes_a_plain_read_a_write(self):
        self.write('.curlrc', 'request = PUT\ndata = "{\\"enabled\\": false}"\n', 0o644)
        self.refused('curl -sS https://api.github.com/repos/a/b/actions/permissions')

    def test_branch_name_with_a_slash(self):
        self.refused('gh api -X DELETE repos/a/b/branches/release/1.0/protection')



class LensCorrR2Test(Case):
    """Lens correctness re-check on #1543 (head b592fb8f): bash runs each command below; each is let through."""

    def refused(self, *cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd[-70:]):
                self.assertIsNotNone(self.decide(cmd), cmd[-70:])

    def test_more_than_400_substitutions_hide_the_rest(self):
        for n in (399, 1000):
            self.refused('echo ' + '$(:) ' * n + '; $(gh workflow disable 1)',
                         'echo ' + '$(:) ' * n + '; echo "$(gh api -X PUT repos/a/b/actions/permissions '
                                                 '-F enabled=false)"',
                         'echo ' + '$(' * (n + 1) + 'gh workflow disable 1' + ')' * (n + 1))

    def test_script_over_max_file_runs_unread(self):
        self.write('run.sh', '#!/bin/bash\n' + ('#' * 100 + '\n') * 21000 + 'gh workflow disable 1\n')
        self.refused('bash run.sh', './run.sh', 'source run.sh', 'bash < run.sh', 'cat run.sh | bash')

    def test_alarm_is_off_before_the_slow_handlers(self):
        import io, time
        g = load()
        g.process_age = lambda: 5.5
        g.decide = lambda *a: (_ for _ in ()).throw(RuntimeError('crash'))
        g.log = lambda *a: time.sleep(1.5)
        out, old_in, old_out = io.StringIO(), sys.stdin, sys.stdout
        sys.stdin = io.StringIO(json.dumps({'tool_name': 'Bash', 'cwd': self.dir,
                                            'tool_input': {'command': 'gh workflow disable 1'}}))
        sys.stdout = out
        try:
            g.main()
        except BaseException as e:
            self.fail(f'{type(e).__name__} left main(): the hook exits 1 and the command runs')
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        self.assertIn('"deny"', out.getvalue())


class Round3Test(Case):
    """Fix round 3 on #1543: where the guard stops reading (a limit, a file too big, time, its own crash) it never
    passes a guarded command; data, binaries and token-free text keep passing (lockout care). Red on b592fb8."""
    T = 'gh workflow disable 1'
    PAD = ('#' * 100 + '\n') * 21000   # 2.1 MB: over MAX_FILE

    def refused(self, *cmds, says=''):
        for cmd in cmds:
            with self.subTest(cmd=cmd[-70:]):
                why = self.decide(cmd)
                self.assertIsNotNone(why, cmd[-70:])
                self.assertIn(says, why, cmd[-70:])

    def passes(self, *cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd[-70:]):
                self.assertIsNone(self.decide(cmd), cmd[-70:])

    def main_out(self, g, cmd):
        import contextlib, io
        from unittest import mock
        out = io.StringIO()
        ev = json.dumps({'tool_name': 'Bash', 'cwd': self.dir, 'tool_input': {'command': cmd}})
        with mock.patch.object(sys, 'stdin', io.StringIO(ev)), mock.patch.dict(os.environ, {'HOME': self.dir}), \
                contextlib.redirect_stdout(out):
            try:
                g.main()
            except BaseException as e:
                self.fail(f'{type(e).__name__} left main(): the hook exits 1 and the command runs')
        return out.getvalue()

    def test_slow_in_the_handlers_still_denies(self):
        def slow(*a):
            raise g.Slow()
        for where in ('named', 'log', 'deny'):
            with self.subTest(where=where):
                g, calls = load(), []
                if where == 'named':   # decide crashes, then the crash handler runs out of time
                    g.decide = lambda *a: (_ for _ in ()).throw(RuntimeError('crash'))
                    g.named = slow
                elif where == 'log':
                    g.log = slow
                else:   # the first answer runs out of time half-way; the second one is the answer
                    real = g.deny
                    g.deny = lambda why: calls.append(why) or (real(why) if len(calls) > 1 else slow())
                self.assertIn('"deny"', self.main_out(g, self.T))

    def test_a_script_too_big_to_read_whole(self):
        self.write('run.py', 'import os\n' + self.PAD + f'os.system("{self.T}")\n', 0o644)
        self.write('run.sh', '#!/bin/sh\n' + self.PAD + self.T + '\n')
        self.write('ok.sh', '#!/bin/sh\n' + self.PAD + 'echo hi\n')
        head = '#!/bin/sh\n'   # the token across the first 1 MB piece: the pieces overlap
        self.write('edge.sh', head + '#' * ((1 << 20) - len(head) - 10) + '\n' + self.T + '\n' + self.PAD)
        self.write('empty.sh', '')
        self.refused('python3 run.py', 'uv run run.py', 'at -f run.sh now', 'bash edge.sh', 'nice ./run.sh',
                     says='read in pieces, it names')
        self.passes('bash ok.sh', 'bash empty.sh', 'bash no-such.sh')
        from unittest import mock
        with mock.patch.object(self.g, 'HARD_FILE', 2_500_000):
            self.write('huge.sh', '#!/bin/sh\n' + self.PAD * 2)
            self.refused('bash huge.sh', 'source huge.sh', says='it is a file over')
        if os.geteuid() != 0:
            self.write('nr.sh', '#!/bin/sh\necho hi\n', 0o000)
            self.refused('bash nr.sh', 'source nr.sh', says='cannot open it')

    def test_big_data_and_binaries_keep_passing(self):
        self.write('big.log', self.PAD + self.T + '\n', 0o644)
        with open(os.path.join(self.dir, 'bin'), 'wb') as f:   # a compiled program: a NUL in the first 8 KB
            f.write(b'\x7fELF\0\0' + b'x' * 9000 + self.T.encode() + b'\n' + self.PAD.encode())
        os.chmod(os.path.join(self.dir, 'bin'), 0o755)
        self.passes('cat big.log | grep x', 'grep -c x big.log', 'mysql < big.log', 'tar czf a.tgz big.log', './bin')
        # an interpreter whose script is not found here: its data file is not read as a script (corpus case)
        self.passes('python3 -I nothere/rt.py --make-hint big.log')

    def test_files_that_carry_arguments_or_config(self):
        self.write('k.cfg', self.PAD + f'url = "{U}"\nrequest = PUT\n', 0o644)
        self.write('urls.txt', self.PAD + U + '\n', 0o644)
        self.refused('curl -q -K k.cfg', 'wget --no-config -i urls.txt --method=PUT', 'xargs -a urls.txt curl -q -X PUT')
        # gh_config_now: a config over 2 MB or one it cannot open may hold the alias
        self.write('gh/config.yml', 'aliases:\n' + ('    #' + 'x' * 90 + '\n') * 22000 + '    wd: workflow disable\n',
                   0o644)
        self.refused('gh wd 1')
        self.passes('gh pr list')
        if os.geteuid() != 0:
            os.chmod(os.path.join(self.dir, 'gh/config.yml'), 0)
            self.refused('gh co 1')
            self.passes('gh pr list')

    def test_every_limit_fails_closed(self):
        self.passes('echo ' + '$(:) ' * 1000, 'echo ' + '$(' * 9 + 'date' + ')' * 9)   # read to the end: no token
        for a, b in (('p', 'q'), ('q', 'r'), ('r', 's')):   # a chain deeper than MAX_DEPTH, no token anywhere
            self.write(f'{a}.sh', f'#!/bin/sh\nbash {b}.sh\n')
        self.write('s.sh', '#!/bin/sh\necho hi\n')
        self.passes('bash p.sh')   # lockout care: deep and harmless passes
        self.write('s.sh', '#!/bin/sh\necho hi   # notes in n.py\n')   # past the limit: only the scripts the deep
        self.write('n.py', '#!/usr/bin/env python3\n# the cases are in t.py\n')  # file names directly are read, no crawl
        self.write('t.py', f'#!/usr/bin/env python3\nCASE = "{self.T}"\n')
        self.passes('bash p.sh')
        self.write('dis.sh', f'#!/bin/sh\n{self.T}\n')
        deep = os.path.join(*[f'd{i}' for i in range(1, 19)])
        self.write(os.path.join(deep, 'deep.sh'), f'#!/bin/sh\n{self.T}\n')
        for a, b in (('a', 'b'), ('b', 'c'), ('c', 'e')):   # a.sh runs b.sh runs c.sh runs e.sh: deeper than MAX_DEPTH
            self.write(f'{a}.sh', f'#!/bin/sh\nbash {b}.sh\n')
        self.write('e.sh', f'#!/bin/sh\n{self.T}\n')
        files = ' '.join(self.write(f'f{i}.txt', 'x\n', 0o644) and f'f{i}.txt' for i in range(1, 17))
        self.write('urls.txt', U + '\n', 0o644)
        self.write('gh/config.yml', 'aliases:\n    a1: a2\n    a2: a3\n    a3: a4\n    a4: workflow disable\n', 0o644)
        cds = ' && '.join(f'cd d{i}' for i in range(1, 19))
        self.refused(f'{cds} && ./deep.sh', f'{cds} && bash deep.sh',    # cwds 16: the folder of every cd is kept
                     'bash a.sh',                                        # MAX_DEPTH: e.sh is read for tokens only
                     f'mytool {files} dis.sh',                           # args[:16] dropped: every argument
                     f'cat {files} urls.txt | xargs curl -q -X PUT -d x',    # args[:8] dropped: every file
                     'gh a1 1',                                          # gh alias at MAX_DEPTH
                     'gh api -X DELETE repos/a/b/rulesets/9' + '/x' * 25 + '/..' * 25,   # forms() had 20 rounds
                     'gh api -X DELETE repos/a/b/rulesets/9/.')
        self.passes(f'{cds} && ls')

    def test_text_for_a_tool_the_guard_does_not_know_says_what_to_do(self):
        for cmd in (f"projtool commit -m 'Guard: refuse {self.T}' -- x", f"claude -p 'explain {self.T}'"):
            with self.subTest(cmd=cmd):
                self.assertIn('put it in a file', self.decide(cmd) or '')

    def test_gh_as_is_read_as_gh(self):
        self.passes(f"gh-as example-bot -- gh issue comment 1 --body 'fix {P}; {self.T} is refused'",
                    "gh-as --repo a/b -- gh pr comment 1 --body x")
        self.refused(f'gh-as example-bot -- {self.T}', f'gh-as example-bot -- gh api -X PUT {P} {OFF}')


LINE = '2026-11-14 12:00:00 INFO [Server] player joined from /10.0.0.1 permissions ok 50% done\n'


class LensCorrR3Test(Case):
    def passes(self, *cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd), cmd)

    def test_big_log_into_an_ordinary_tool_passes(self):
        # stands for /var/log/syslog on the dev VM (638 MB, readable through the adm group)
        with mock.patch.object(self.g, 'HARD_FILE', 2_500_000):
            self.write('big.log', LINE * 40000, 0o644)   # 3.4 MB > patched HARD_FILE, names nothing
            self.passes("tail -n 5000 big.log | grep -i oom | awk '{print $1}'",
                        "cat big.log | awk '/oom/'",
                        'tail -n 200 big.log | python3 -c "import sys"',
                        'cp big.log /tmp/s && python3 a.py /tmp/s',
                        'mv big.log archive/ ; java -jar tool.jar',
                        'python3 load.py < big.log')   # load.py is the script; the log is its stdin data

    def test_mid_size_log_with_a_url_path_passes(self):
        # 2-64 MB: the piece scan also takes LOOSE_RE hits, so an ordinary `/disable` URL in a web log counts
        self.write('web.log', LINE * 36000 + 'GET /api/v1/feature/disable 200\n', 0o644)
        self.write('web-small.log', LINE * 20000 + 'GET /api/v1/feature/disable 200\n', 0o644)
        self.passes("cat web-small.log | awk '{print $7}' | sort | uniq -c",   # under 2 MB: passes today
                    "cat web.log | awk '{print $7}' | sort | uniq -c")          # 3 MB: refused at 118a3a1

    @unittest.skipIf(os.geteuid() == 0, 'root opens a 0o000 file')
    def test_unreadable_data_file_passes(self):
        self.write('secret.db', 'x\n', 0o000)
        self.passes('sudo cat secret.db | python3 -c "import sys"',
                    "sudo cat secret.db | awk '{print}'",
                    'sudo cp secret.db copy.db && ./gradlew test')


class LensCorrR3TimeTest(Case):
    def test_plain_copy_of_a_60_mb_log_passes_through_the_hook(self):
        # data_gate scans every cp/mv/ln source with file_token(): ~0.12 s per MB, so a plain copy of a 60 MB text
        # log runs into the 7 s budget and main() denies it ("could not finish reading ... within 7 s")
        import json, subprocess
        with open(os.path.join(self.dir, 'huge.log'), 'w') as f:
            for _ in range(60):
                f.write(LINE * 12000)
        for cmd in ('cp huge.log old.log', 'mv huge.log logs/old.log'):
            with self.subTest(cmd=cmd):
                ev = json.dumps({'tool_name': 'Bash', 'cwd': self.dir, 'tool_input': {'command': cmd}})
                out = subprocess.run([sys.executable, '-I', '-B', HOOK], input=ev, capture_output=True, text=True,
                                     env={'HOME': self.dir, 'GH_CONFIG_DIR': os.path.join(self.dir, 'gh'),
                                          'PATH': '/usr/bin:/bin'}, timeout=60).stdout
                self.assertNotIn('"deny"', out, out[:300])


class LensCorrR3DepthTest(Case):
    def test_deep_helper_called_many_times_is_read_once(self):
        # file_reason's MAX_DEPTH branch returns before the memo: a depth-2 helper called N times re-scans every file
        # it names N times (100 calls x 30 files of 100 KB = 6.8 s through main(), next to the 7 s budget)
        for i in range(5):
            self.write(f'lib/m{i}.py', '#!/usr/bin/env python3\nx = 1\n')
        self.write('helper.sh', '#!/bin/sh\n' + ''.join(f'python3 lib/m{i}.py\n' for i in range(5)))
        self.write('mid.sh', '#!/bin/sh\n' + 'bash helper.sh\n' * 20)
        self.write('top.sh', '#!/bin/sh\nbash mid.sh\n')
        real, seen = self.g.file_token, []
        with mock.patch.object(self.g, 'file_token', lambda p: seen.append(p) or real(p)):
            self.assertIsNone(self.decide('bash top.sh'))
        self.assertLessEqual(seen.count(os.path.join(self.dir, 'lib/m0.py')), 1, 'helper.sh re-read for each call')

    @unittest.skipIf(os.geteuid() == 0, 'root opens /etc/sudoers')
    def test_deep_script_naming_a_root_only_file_passes(self):
        # script_like() is True on OSError and file_token() gives NO_OPEN: any root-only file a deep script names
        if os.access('/etc/sudoers', os.R_OK) or not os.path.isfile('/etc/sudoers'):
            self.skipTest('needs an unreadable /etc/sudoers')
        self.write('a.sh', '#!/bin/sh\nbash b.sh\n')
        self.write('b.sh', '#!/bin/sh\nbash c.sh\n')
        self.write('c.sh', '#!/bin/sh\nsudo visudo -c -f /etc/sudoers\n')
        self.assertIsNone(self.decide('bash a.sh'))


# ---------- #1752: the hook answers well inside its 10 s limit under load

# Commands the fast exit lets through before any file is read (the most common Bash calls of agents).
QUICK_PASS = [
    'ls', 'ls -la', 'pwd', 'git status', 'git status --short', 'git --no-pager log --oneline -5',
    'git -C sub diff --stat', 'git log -p -- a.txt', 'git diff origin/main...HEAD -- scripts',
    "grep -rn 'foo bar' src", 'grep -c x a.txt b.txt', 'cat a.txt | head -5', 'cd sub && ls', 'sleep 1; echo done',
    "find . -name '*.py' -newer x", "sed -n '1,20p' f.txt", 'wc -l a b > out.txt 2>&1', 'ls 2>/dev/null',
    'rg -n TODO scripts', 'if test -f x; then cat x; fi', 'while true; do sleep 1; done', "jq '.a | {b}' f.json",
    "echo 'a;b|c&d' && pwd", 'echo "it\'s ok"', 'head -n 5 a.txt; tail -n 5 a.txt', 'mkdir -p out && cp a.txt out/',
    'ls -la | sort | uniq -c', 'ps aux | grep java', 'du -sh .', 'date -u', 'stat a.txt', 'diff a.txt b.txt',
    'rm -f out.txt', 'mv a.txt b.txt', 'touch x', 'git add a.txt && git commit -m "fix: words"', 'tree -L 2',
    'echo ok\nls', 'cat a.txt > b.txt', 'cp a.txt b.txt',
]
# Spellings that must never take the fast exit: quoting, escapes, variables, prefixes, subshells, functions, code
# that runs code, files under hook folders, and every word that may name a guarded call.
TRICKY = [
    '"g"h workflow disable 1', "g''h workflow disable 1", 'gh api -X PUT repos/a/b/actions/workflows/1/disable',
    "gh api -X PUT 'repos/a/b/actions/workflows/1/dis''able'", 'echo work""flow dis\'\'able',
    'X=1 ls', 'GH_TOKEN=x ls', 'env ls', 'sudo ls', 'timeout 60 ls', 'nice ls', 'command ls', 'exec ls', 'builtin ls',
    '(cd x && ./run.sh)', '(ls)', '{ ls; }', 'ls; bash x.sh', 'ls && ./x.sh', 'ls || sh x.sh', 'ls & ./x', 'ls | sh',
    'cat x | bash', 'cat x |& sh', 'ls $(./x)', 'ls `./x`', 'ls "$(./x)"', 'ls $X', 'ls ${X}', '\\ls', 'l\\s',
    "l''s", '"ls"', 'ls\n./x.sh', 'ls\n\n bash x', 'ls # x\n./x.sh', "ls # it's\n./x.sh\n# '", 'ls #x',
    'ls() { ./x.sh; }; ls', 'function ls { ./x.sh; }; ls', 'alias ls=./x.sh; ls', 'ls {,./x}', 'b{a,}sh x.sh',
    'find . -exec ./x.sh {} +', 'find . -execdir sh -c x ;', 'find . -ok ./x ;',
    'cp /usr/bin/g? ./ls; ls wd', 'cp /usr/bin/gh ./x', 'ln -s /usr/bin/g* g', 'mv /usr/bin/[g]h x',
    'git wd', 'git -c alias.x=!./x.sh x', 'git -c core.pager=./x.sh log', 'git log --exec=x', 'git grep -O./x foo',
    'git --exec-path=. status', 'git config core.pager ./x', 'git rebase --exec ./x main', 'git difftool',
    'git --git-dir=x status', "sed '1e ./x.sh' f", "sed -n 's/a/b/e' f", 'sed -f script.sed f', 'sed --file=s f',
    'sed -nf s f', 'sort --compress-program=./x f', 'rg --pre ./x foo', 'rg --pre=./x foo',
    'echo é', 'echo  x', 'echo %64isable', "cat <<EOF\n'\nEOF\nbash x.sh\n'", 'cat <<<x', "echo 'unclosed",
    'xargs ls', 'ls | xargs ./x', '. ./x', 'source x', 'ls 2>&1 | ./x', 'cat <(./x)', 'ls >(./x)',
    'cat x > .git/hooks/pre-commit', 'cp n.txt .git/hooks/post-checkout', 'cp n.txt .husky/pre-commit',
    'cp n.txt .git/config', "cp n.txt '.g''it/config'",
    'curl -q https://api.github.com', 'wget x', 'http GET x', 'xh x', 'httpie x', 'ls; python3 x.py',
    'ls; node x.js', 'ls; perl x', 'ls; ./projtool state', 'projtool state', 't3-capacity 2048', 'make', 'ls\r; ./x',
    'ls\x0b./x', '! ./x', 'time ./x', 'if ./x.sh; then ls; fi', 'while ./x; do ls; done', 'for f in a; do ./x; done',
    'case x in x) ./x;; esac', 'printf -v GIT_PAGER ./x; git log', 'read -r X; git log', 'export PAGER=./x; git log',
    'declare -x PAGER=./x', 'cd ' + ' && cd '.join('abcdefghij') + ' && cat f > out', 'ls ' + 'a' * 5000,
    'echo examplegarden', 'python3 scripts/dev/example_garden.py --fix', 'echo protection', 'echo rulesets',
    'echo graphql mutation', 'echo permissions', 'echo github', 'ls >x.sh; bash x.sh', 'ls\x00; ./x',
    "find . -exec ./x.sh ';'", "find . -ex\\ec ./x.sh ';'", "sort --c\\ompress-program=./x f", 'rg --hostname-bin=./x a',
    "'do' ls", '"then" ls', 'cp n.txt hooks/pre-commit', 'cp n.txt .githooks/pre-commit',
    'echo bash run.sh > ~/.local/bin/ls; chmod +x ~/.local/bin/ls; ls',
    'cat <run.sh >~/.local/bin/date; chmod +x ~/.local/bin/date; date',
    'tee ~/.local/bin/date <p.txt; chmod +x ~/.local/bin/date; date', 'cat run.sh > .local/bin/ls; ls',
    'cp run.sh ~/.local/bin/ls && ls', 'git diff --output=bin/ls; ls', 'mv x bin/cat; cat a',
    "sed '1eid' a.txt", "sed '1ebash run.sh' a.txt", "sed -n '/x/e id' a", "sed 's/a/b/w out' a",
    "sed -e 1d -e '2e id' a", "sed 's|a|b|e' a", "sed '1!e id' a", "sed -n '$e id' a",
    'git grep --open=bash run.sh hello', 'git grep --open-files=bash run.sh x', 'git grep -nObash x',
    'git fetch --upl=./x', 'git log --ex=./x', "git commit -m '!bash run.sh'", 'sed -f p a.txt',
    "sed -n -e p -e '1eid' p",   # the file name p reads as a script too: only the -e scripts are scripts
    'tee ~/.local/bin/l? < p.txt; ls', 'tee ~/.local/bin/l? <p.txt >/dev/null; ls -la',
    'echo bash run.sh > ~/.local/bin/l?; ls', 'cat run.sh | tee ~/.local/bin/[l]s; ls', 'tee bin/*s <p.txt; ls',
    'tee bin/[[:lower:]]s <p.txt; ls', 'tee bin/[^x]s <p.txt; ls', "tee bin/'l'? <p.txt; ls", 'tee bin/c?? <p; cat',
    'git diff --output=bin/l?; ls', 'git grep -1Obash x', 'git grep -5nObash x', 'git grep -n1Obash x',
    'cd ~/.local/bin; echo bash ~/run.sh >|ls; chmod -R +x .; ls', 'echo x >|ls; ls', 'echo x >| ls; ls',
    'echo x 1>|ls; ls -la', 'echo x &>|ls; ls',
]
# Spellings the full reading refuses with the files of FastExitTest.test_refused_spellings_stay_refused
QUICK_REFUSED = [
    'cp evil.sh hooks/pre-commit', 'cat evil.sh > .githooks/pre-commit', "find . -exec python3 evil.py ';'",
    "find . -ex\\ec python3 evil.py ';'", "find . -e'x'ec python3 evil.py ';'",
]


class LensSecurityR2Test(Case):
    """PR 847, lens B round 2 (security boundary): each test names the finding it closes."""

    def test_r2_minor1_graphql_update_with_evaluate_in_a_variable_is_refused(self):
        mut = ('mutation($e: RuleEnforcement!) { updateRepositoryRuleset(input: {repositoryRulesetId: \\"x\\", '
               'enforcement: $e}) { clientMutationId } }')
        mut_i = 'mutation($i: UpdateRepositoryRulesetInput!) { updateRepositoryRuleset(input: $i) { clientMutationId } }'
        self.write('vars.json', json.dumps({'query': mut, 'variables': {'e': 'EVALUATE'}}) + '\n', 0o644)
        for cmd in (f"gh api graphql -f query='{mut}' -F e=EVALUATE",
                    f"gh api graphql -f query='{mut}' -f e=evaluate",
                    f"gh api graphql -f query='{mut}' --raw-field e=EVALUATE",
                    f"gh api graphql -f query='{mut}' --field=e=EVALUATE",
                    f"gh api graphql -f query='{mut_i}' -F i[repositoryRulesetId]=x -F i[enforcement]=EVALUATE",
                    'gh api graphql --input vars.json'):
            with self.subTest(cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)
        # the same call that keeps the ruleset on passes
        self.assertIsNone(self.decide(f"gh api graphql -f query='{mut}' -F e=ACTIVE"))


class FastExitTest(Case):
    """#1752 fix point 1: plain commands pass before any file is read or the clock matters, and nothing the full
    reading refuses takes that way out."""

    def setUp(self):
        super().setUp()
        self.env = {'HOME': self.dir}
        for name, text in FILES.items():
            self.write(name, text, 0o755 if name.endswith('.sh') else 0o644)

    def test_plain_commands_pass_without_reading_anything(self):
        def boom(*_a, **_k):
            raise AssertionError('the fast exit read a file')
        for cmd in QUICK_PASS:
            with self.subTest(cmd=cmd):
                self.assertTrue(self.g.quick_pass(cmd), cmd)
                with mock.patch.object(self.g, 'read_text', boom), mock.patch.object(self.g, 'gh_config_now', boom), \
                        mock.patch.object(self.g, 'decide_text', boom):
                    self.assertIsNone(self.decide(cmd), cmd)

    def test_refused_spellings_stay_refused(self):
        self.write('evil.sh', 'gh workflow disable 1\n', 0o755)
        self.write('evil.py', 'x = "actions/permissions"\n', 0o644)
        for cmd in QUICK_REFUSED:
            with self.subTest(cmd=cmd):
                self.assertFalse(self.g.quick_pass(cmd), cmd)
                self.assertIsNotNone(self.decide(cmd), cmd)

    def test_a_late_start_still_passes_plain_commands_at_once(self):
        # the clock is looked at after the fast exit: a hook that starts late answers a plain command with a pass
        import contextlib, io
        self.write('evil.py', 'x = "actions/permissions"\n', 0o644)
        for cmd, refused in (('ls -la', False), ('git status', False), ('python3 evil.py', True),
                             ('python3 ok.py', True)):
            with self.subTest(cmd=cmd):
                out = io.StringIO()
                ev = json.dumps({'tool_name': 'Bash', 'cwd': self.dir, 'tool_input': {'command': cmd}})
                with mock.patch.object(self.g, 'process_age', lambda: self.g.BUDGET + 1.0), \
                        mock.patch.object(sys, 'stdin', io.StringIO(ev)), \
                        mock.patch.dict(os.environ, {'HOME': self.dir}), contextlib.redirect_stdout(out):
                    self.assertEqual(self.g.main(), 0)
                if refused:
                    self.assertIn('could not finish', json.loads(out.getvalue())['hookSpecificOutput'][
                        'permissionDecisionReason'])
                else:
                    self.assertEqual(out.getvalue(), '')

    def test_a_write_named_like_a_command_word_of_the_same_text_is_read(self):
        # lens round 1: the text writes ~/.local/bin/ls, then runs ls (found there first on PATH)
        self.write('run.sh', 'gh workflow dis' + 'able 1\n', 0o755)
        self.write('p.txt', 'bash run.sh\n', 0o644)
        os.makedirs(os.path.join(self.dir, '.local', 'bin'))
        env = dict(self.env, PATH=os.path.join(self.dir, '.local', 'bin') + ':/usr/bin:/bin')
        for cmd in ('echo bash run.sh > ~/.local/bin/ls; chmod +x ~/.local/bin/ls; ls',
                    'cat <run.sh >~/.local/bin/date; chmod +x ~/.local/bin/date; date',
                    'tee ~/.local/bin/date <p.txt; chmod +x ~/.local/bin/date; date'):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.g.quick_pass(cmd), cmd)
                self.assertIsNotNone(self.g.decide(cmd, self.dir, env), cmd)
        # lens round 2: the target as a glob the shell turns into an existing ~/.local/bin/ls
        self.write('.local/bin/ls', 'echo hi\n', 0o755)
        for cmd in ('tee ~/.local/bin/l? < p.txt; ls', 'echo bash run.sh > ~/.local/bin/l?; ls',
                    'cat run.sh | tee ~/.local/bin/[l]s; ls'):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.g.quick_pass(cmd), cmd)
                self.assertIsNotNone(self.g.decide(cmd, self.dir, env), cmd)
        # Fable audit after round 2: >| (write even with noclobber) is a redirection, not a pipe. The hook's own
        # HOME is the user's, as in the hook (~ in a script word is expanded with it)
        for cmd in ('cd ~/.local/bin; echo bash ~/run.sh >|ls; chmod -R +x .; ls',
                    'cd ~/.local/bin; echo bash ~/run.sh >| ls; ls -la'):
            with self.subTest(cmd=cmd), mock.patch.dict(os.environ, {'HOME': self.dir}):
                self.assertFalse(self.g.quick_pass(cmd), cmd)
                self.assertIsNotNone(self.g.decide(cmd, self.dir, env), cmd)
        # globs that cannot name a command word of the text, and real pipes, stay fast
        for cmd in ('ls *.txt', 'cat a?.txt', "grep -n '[0-9]' a.txt", 'wc -l *.py', 'ls | wc -l', 'ls 2>&1 | head',
                    'ls |& wc'):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.g.quick_pass(cmd), cmd)

    def test_git_flags_that_run_a_command_are_seen_in_every_form(self):
        # lens round 1: git takes a long flag cut short (--open=less) and -O inside a cluster of short flags
        tok = "'actions/per" + "missions'"
        for cmd in (f'git grep --open=less {tok}', f'git grep --open-files=less {tok}', f'git grep -nOless {tok}',
                    f'git grep -O less {tok}', f'git grep -1Oless {tok}', f'git grep -5nOless {tok}'):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd), cmd)
        for cmd in (f'git commit -m {tok}', f'git grep -n {tok}', f'git log --oneline --grep={tok}'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd), cmd)

    def test_random_commands_that_are_fast_pass_the_full_reading(self):
        # lens round 1: a fixed list cannot show "fast means the full reading passes"; random commands built from
        # every fast word, file names of the attack tests, names of command words under PATH folders, separators
        # and redirections
        import random
        rnd = random.Random(1752)
        self.write('run.sh', 'gh workflow dis' + 'able 1\n', 0o755)
        bin_ = os.path.join(self.dir, '.local', 'bin')
        os.makedirs(bin_)
        env = dict(self.env, PATH=bin_ + ':/usr/bin:/bin')
        heads = sorted(self.g.QUICK_WORDS) + ['git ' + s for s in sorted(self.g.QUICK_GIT)]
        words = list(FILES) + ['run.sh', '-la', '-n', '-r', '-i', "'a b'", '"x"', '~/.local/bin/ls', '.local/bin/date',
                               'bin/cat', 'out.txt', '-o', 'x.sh', '--output=bin/ls', "'1,5p'", "'1eid'", "'s/a/b/'",
                               "'s/a/b/e'", '-e', '--open=ls', '-O', '.', 'sub', '-exec', 'bash', "';'", '+x',
                               '~/.local/bin/l?', '.local/bin/[d]ate', 'bin/c*', '*.txt', '-1O']
        ops = [' ; ', ' && ', ' || ', ' | ', '\n', ' & ']
        redirs = ['', '', '', ' > {}', ' >> {}', ' 2>&1', ' < {}', ' &> {}', ' >{}']
        fast = []
        for _ in range(3000):
            parts = []
            for _ in range(rnd.randint(1, 3)):
                p = rnd.choice(heads) + ''.join(' ' + rnd.choice(words) for _ in range(rnd.randint(0, 3)))
                parts.append(p + rnd.choice(redirs).format(rnd.choice(words)))
            cmd = parts[0] + ''.join(rnd.choice(ops) + p for p in parts[1:])
            if self.g.quick_pass(cmd):
                fast.append(cmd)
        self.assertGreater(len(fast), 300)
        with mock.patch.object(self.g, 'QUICK', False):
            for cmd in fast:
                with self.subTest(cmd=cmd):
                    self.assertIsNone(self.g.decide(cmd, self.dir, env), cmd)

    def test_a_long_sed_script_is_answered_at_once(self):
        # lens round 2: two \\s* side by side made the sed check try every split of the blanks (5 s, then a refusal)
        import time
        for script, fast in (("'" + '  p;' * 16 + "  y/ab/cd/'", False), ("'" + ' s/a/b/;' * 22 + " s/c/d/m'", False),
                             ("'" + '  p;' * 200 + "'", True), ("'" + ' 1,2 s/a/b/g ;' * 200 + "'", True)):
            with self.subTest(script=script[:20]):
                t = time.monotonic()
                self.assertEqual(self.g.quick_pass('sed -n ' + script + ' a.txt'), fast)
                self.assertLess(time.monotonic() - t, 2.0)   # timing: in-process, 1.5 ms at most: 1000x margin
        # Fable audit: a run of * in a glob word, matched against the command words
        for cmd, fast in (('sha256sum a.txt ' + '*' * 40 + 'x', True), ('cat ' + '*?' * 30 + 'q; cat', True),
                          ('cat ' + '*' * 40 + 't; cat', False)):
            with self.subTest(cmd=cmd[:20]):
                t = time.monotonic()
                self.assertEqual(self.g.quick_pass(cmd), fast)
                self.assertLess(time.monotonic() - t, 2.0)   # timing: in-process, 1.5 ms at most: 1000x margin

    def test_tricky_spellings_are_never_fast(self):
        for cmd in TRICKY:
            with self.subTest(cmd=cmd):
                self.assertFalse(self.g.quick_pass(cmd), cmd)
        for bad in (None, 5, b'ls', ['ls']):
            self.assertFalse(self.g.quick_pass(bad))

    def test_no_refused_case_is_fast(self):
        refused = DENY + [c for cmds in ATTACKS.values() for c in cmds] + [c for c, _ in OVER_REFUSED]
        for cmd in refused:
            with self.subTest(cmd=cmd):
                self.assertFalse(self.g.quick_pass(cmd), cmd)

    def test_what_is_fast_the_full_reading_passes_too(self):
        # whatever the files hold: every fast command, over the files of the attack tests, read in full
        corpus = QUICK_PASS + TRICKY + DENY + ALLOW + ALLOW_MORE + [c for cmds in ATTACKS.values() for c in cmds]
        corpus += [f'{w} {f}' for w in sorted(self.g.QUICK_WORDS) for f in FILES]
        corpus += [f'git {s} {f}' for s in sorted(self.g.GIT_SAFE) for f in FILES]
        corpus += [c.format(f) for f in FILES for c in ('cat {} > run.sh', 'cp {} run.sh', 'cat {} | sort',
                                                        'tee x.sh < {}', 'ln -s {} y', 'cd sub && cat {} > z')]
        fast = [c for c in corpus if self.g.quick_pass(c)]
        self.assertGreater(len(fast), 500)
        with mock.patch.object(self.g, 'QUICK', False):
            for cmd in fast:
                with self.subTest(cmd=cmd):
                    self.assertIsNone(self.decide(cmd), cmd)

    def test_fast_words_are_words_the_full_reading_trusts(self):
        self.assertLessEqual(self.g.QUICK_WORDS, self.g.DATA)
        self.assertFalse(self.g.QUICK_WORDS & (self.g.SETTERS | self.g.TAINTERS | self.g.WRAPPERS | {'alias', 'hash'}))
        self.assertLessEqual(self.g.QUICK_GIT, self.g.GIT_SAFE)


RACY = 0.05


def settle():
    """Wait until the files written so far are old enough to cache."""
    import time
    time.sleep(RACY + 0.03)


class CacheCase(Case):
    """A scratch HOME with a code file that names a guarded token (evil.py) and one that does not (ok.py), both old
    enough to cache."""

    def setUp(self):
        super().setUp()
        self.env = {'HOME': self.dir}
        self.ok = self.write('ok.py', 'x = "' + 'a' * 30 + '"\n', 0o644)
        self.evil = self.write('evil.py', 'x = "actions/permissions/' + 'a' * 10 + '"\n', 0o644)
        self.assertEqual(os.path.getsize(self.ok), os.path.getsize(self.evil))
        # A file is cached only when it was last changed CACHE_RACY seconds ago (a change in the same clock tick as
        # the read would not show in the times). Tests use a short wait instead of the hook's 2 s.
        p = mock.patch.object(self.g, 'CACHE_RACY', RACY)
        p.start()
        self.addCleanup(p.stop)
        settle()

    def cache_path(self):
        return os.path.join(self.dir, '.local', 'state', 'actions-disable-guard', 'cache-v1.json')

    def reads(self, cmd):
        """(the verdict, the files the guard read for it)."""
        real, seen = self.g.read_text, []
        with mock.patch.object(self.g, 'read_text', lambda p: seen.append(p) or real(p)):
            return self.decide(cmd), seen

    def cache(self):
        with open(self.cache_path()) as f:
            return json.load(f)

    def put(self, data, mode=0o600):
        with open(self.cache_path(), 'w') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        os.chmod(self.cache_path(), mode)


class CacheTest(CacheCase):
    """#1752 fix point 2 and the state question: the hook runs again (a cache hit), a script changed after it was
    cached, an old, empty, broken, future-dated or other-user cache, a write that stops half-way, two hooks at once."""

    def test_second_run_reads_nothing_and_answers_the_same(self):
        for path, verdict in ((self.ok, None), (self.evil, 'names `.../actions/permissions`')):
            first, seen = self.reads(f'python3 {os.path.basename(path)}')
            self.assertIn(path, seen)
            again, seen = self.reads(f'python3 {os.path.basename(path)}')
            self.assertNotIn(path, seen)
            self.assertEqual(first, again)
            if verdict is None:
                self.assertIsNone(first)
            else:
                self.assertIn(verdict, first)
        st = os.stat(self.cache_path())
        self.assertEqual(st.st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self.cache_path())).st_mode & 0o777, 0o700)

    def test_a_script_changed_after_it_was_cached_is_read_again(self):
        self.assertIsNone(self.decide('python3 ok.py'))
        st = os.stat(self.ok)
        with open(self.evil) as f:
            evil = f.read()
        with open(self.ok, 'w') as f:   # same size, same mtime: only the change time tells
            f.write(evil)
        os.utime(self.ok, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertIsNotNone(self.decide('python3 ok.py'))
        self.write('ok2.py', 'x = "' + 'a' * 30 + '"\n', 0o644)
        settle()
        self.assertIsNone(self.decide('python3 ok2.py'))
        os.replace(self.evil, os.path.join(self.dir, 'ok2.py'))   # another file under the cached name
        self.assertIsNotNone(self.decide('python3 ok2.py'))

    def test_a_file_written_just_now_is_not_cached(self):
        self.write('ok.py', 'x = 1\n', 0o644)
        os.utime(self.ok, (1, 1))   # an old mtime (cp -p, tar): the change time still says "just now"
        cfg = self.write('.config/gh/config.yml', 'aliases:\n    co: pr checkout\n', 0o644)
        with mock.patch.object(self.g, 'CACHE_RACY', 2.0):
            self.assertIsNone(self.decide('python3 ok.py'))
            self.decide('gh co 1')
        c = self.cache() if os.path.exists(self.cache_path()) else {}
        self.assertNotIn(self.ok, c.get('files', {}))
        self.assertFalse(any(cfg in k for k in c.get('gh', {})), c.get('gh'))

    def test_an_entry_lives_60_s(self):
        self.assertIsNotNone(self.decide('python3 evil.py'))
        import time as t
        now = t.time()
        for later, read in ((30, False), (61, True)):
            with mock.patch.object(self.g.time, 'time', lambda: now + later):
                _, seen = self.reads('python3 evil.py')
            self.assertEqual(self.evil in seen, read, later)

    def forged(self):
        """The cache after a real run, with evil.py's entry turned into "names nothing": if the guard trusted it,
        evil.py would pass. Every bad cache below carries this entry, so a bad cache that is used lets evil.py by."""
        self.assertIsNotNone(self.decide('python3 evil.py'))
        c = self.cache()
        self.assertIn(self.evil, c['files'])
        c['files'][self.evil]['named'] = None
        return c

    def test_bad_cache_files_are_ignored(self):
        import copy, time as t
        good = self.forged()
        def entry(**kw):
            c = copy.deepcopy(good)
            c['files'][self.evil].update(kw)
            return c
        half = json.dumps(good)
        bad = {
            'empty': '', 'not json': 'not json {', 'half written': half[:len(half) // 2], 'a list': '[]',
            'an old hook version': dict(good, v=0), 'another guard': dict(good, guard=[0, 0, 0, 0, 0]),
            'no guard': {k: v for k, v in good.items() if k != 'guard'},
            'from the future': entry(t=t.time() + 300), 'stale': entry(t=t.time() - 61),
            'a time that is no number': entry(t='now'), 'a bool time': entry(t=True),
            'a label the guard does not have': entry(named='nothing'), 'text not a bool': entry(text=1),
            'own not a bool': entry(own=1), 'a #! program that is no text': entry(bang=['bash']),
            'another file under the name': entry(sig=[1, 2, 3, 4, 5]), 'files not a map': dict(good, files=[]),
            'deep nesting': '[' * 100000 + ']' * 100000,
        }
        for name, data in bad.items():
            with self.subTest(name):
                self.put(data)
                why, seen = self.reads('python3 evil.py')
                self.assertIsNotNone(why, name)
                self.assertIn(self.evil, seen, name)

    def test_a_cache_file_that_is_not_the_users_own_plain_file_is_ignored(self):
        good = json.dumps(self.forged())
        cases = {'group-writable': lambda: self.put(good, 0o664), 'world-writable': lambda: self.put(good, 0o646)}
        def link():
            other = os.path.join(self.dir, 'other.json')
            with open(other, 'w') as f:
                f.write(good)
            os.chmod(other, 0o600)
            os.unlink(self.cache_path())
            os.symlink(other, self.cache_path())
        def fifo():
            os.unlink(self.cache_path())
            os.mkfifo(self.cache_path(), 0o600)   # an open that waits for a writer would hang the hook
        def folder():
            os.unlink(self.cache_path())
            os.mkdir(self.cache_path())
        def huge():
            self.put(good[:-1] + ', "pad": "' + 'x' * 1_100_000 + '"}')
        cases.update({'a link': link, 'a fifo': fifo, 'a folder': folder, 'over 1 MB': huge})
        def clear():
            if os.path.isdir(self.cache_path()) and not os.path.islink(self.cache_path()):
                os.rmdir(self.cache_path())
            elif os.path.lexists(self.cache_path()):
                os.unlink(self.cache_path())
        for name, make in cases.items():
            with self.subTest(name):
                clear()
                self.put(good)
                make()
                why, seen = self.reads('python3 evil.py')
                self.assertIsNotNone(why, name)
                self.assertIn(self.evil, seen, name)
        clear()
        self.put(good)
        with mock.patch.object(self.g.os, 'geteuid', lambda: os.getuid() + 1):   # another user's cache and folder
            why, seen = self.reads('python3 evil.py')
        self.assertIsNotNone(why)
        self.assertIn(self.evil, seen)
        self.assertEqual(json.dumps(self.cache()), good)   # and it does not write into another user's folder

    def test_missing_or_empty_state_folder_is_made_again(self):
        state = os.path.dirname(os.path.dirname(self.cache_path()))
        self.assertFalse(os.path.exists(state))
        self.assertIsNotNone(self.decide('python3 evil.py'))
        self.assertIn(self.evil, self.cache()['files'])
        import shutil
        shutil.rmtree(os.path.dirname(self.cache_path()))
        os.makedirs(os.path.dirname(self.cache_path()))
        os.chmod(os.path.dirname(self.cache_path()), 0o777)
        why, seen = self.reads('python3 evil.py')
        self.assertIsNotNone(why)
        self.assertIn(self.evil, seen)
        self.assertIn(self.evil, self.cache()['files'])
        self.assertEqual(os.stat(os.path.dirname(self.cache_path())).st_mode & 0o777, 0o700)

    def test_a_write_that_stops_half_way_leaves_no_bad_cache(self):
        self.assertIsNone(self.decide('python3 ok.py'))
        before = self.cache()
        d = os.path.dirname(self.cache_path())
        for where in ('replace', 'write'):
            with self.subTest(where):
                target = {'replace': (self.g.os, 'replace'), 'write': (self.g.os, 'write')}[where]
                def stop(*_a, **_k):
                    raise OSError(28, 'no space left on the disk')
                with mock.patch.object(*target, stop):
                    self.assertIsNotNone(self.decide('python3 evil.py'))   # the answer does not change
                self.assertEqual(self.cache(), before)
                self.assertEqual([n for n in os.listdir(d) if n.endswith('.tmp')], [])
        # a temp file a killed writer left behind is no cache, and an old one is cleared by the next write
        with open(os.path.join(d, '.cache-1-dead.tmp'), 'w') as f:
            f.write('{"v": 1, "files": {')
        os.utime(os.path.join(d, '.cache-1-dead.tmp'), (1, 1))
        with open(os.path.join(d, '.cache-2-live.tmp'), 'w') as f:
            f.write('{"v": 1')
        self.assertIsNotNone(self.decide('python3 evil.py'))
        self.assertEqual(sorted(n for n in os.listdir(d) if n.endswith('.tmp')), ['.cache-2-live.tmp'])

    def test_a_file_that_changes_while_it_is_read_is_not_cached(self):
        # read the harmless text, then the file becomes the evil one: the facts of the old text must not be kept
        # under the new file's stat
        import time
        real, done = self.g.read_text, []
        def swap(p):
            text = real(p)
            if p == self.ok and not done:
                done.append(p)
                with open(self.evil) as f, open(self.ok, 'w') as g:
                    g.write(f.read())
                time.sleep(RACY * 3)   # old enough to cache by the time the reading ends
            return text
        with mock.patch.object(self.g, 'read_text', swap):
            self.assertIsNone(self.decide('python3 ok.py'))
        settle()
        self.assertIsNotNone(self.decide('python3 ok.py'))

    def test_no_cache_without_an_absolute_home(self):
        here = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, here)
        for home in ('rel', '', '.'):
            with self.subTest(home=home):
                self.assertIsNotNone(self.decide('python3 evil.py', {'HOME': home}))
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'rel')))
        self.assertFalse(os.path.exists(os.path.join(self.dir, '.local')))

    def test_a_held_lock_skips_the_write(self):
        import fcntl
        d = os.path.dirname(self.cache_path())
        os.makedirs(d)
        fd = os.open(os.path.join(d, 'cache.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertIsNotNone(self.decide('python3 evil.py'))   # waits at most 0.5 s, then answers without
            self.assertFalse(os.path.exists(self.cache_path()))
        finally:
            os.close(fd)
        self.assertIsNotNone(self.decide('python3 evil.py'))
        self.assertIn(self.evil, self.cache()['files'])

    def test_a_save_that_is_cut_off_never_changes_the_answer(self):
        def slow(*_a, **_k):
            raise self.g.Slow()
        with mock.patch.object(self.g, 'cache_save', slow):
            self.assertIsNone(self.decide('python3 ok.py'))
            self.assertIsNotNone(self.decide('python3 evil.py'))
        with mock.patch.object(self.g.os, 'replace', slow):
            self.assertIsNone(self.decide('python3 ok.py'))
        d = os.path.dirname(self.cache_path())
        self.assertEqual([n for n in os.listdir(d) if n.endswith('.tmp')], [])
        self.assertFalse(os.path.exists(self.cache_path()))

    def test_a_reading_cut_off_keeps_nothing(self):
        for i in range(3):
            self.write(f'ok{i}.py', f'x = {i}\n', 0o644)
        settle()
        real, done = self.g.read_text, []
        def alarm(p):   # the hook's alarm going off in the third read, after two files were read in full
            done.append(p)
            if len(done) == 3:
                raise self.g.Slow()
            return real(p)
        with mock.patch.object(self.g, 'read_text', alarm):
            why = self.g.decide('python3 ok.py; python3 ok0.py; python3 ok1.py; python3 ok2.py', self.dir, self.env)
        self.assertIn('could not finish', why)
        self.assertFalse(os.path.exists(self.cache_path()))

    def test_the_merge_reads_the_cache_under_the_lock(self):
        import fcntl
        self.assertIsNone(self.decide('python3 ok.py'))
        d, real, held = os.path.dirname(self.cache_path()), self.g.cache_load, []
        def spy(env):
            fd = os.open(os.path.join(d, 'cache.lock'), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
            return real(env)
        with mock.patch.object(self.g, 'cache_load', spy):
            self.assertIsNotNone(self.decide('python3 evil.py'))
        self.assertEqual(held, [False, True])   # the reading's look, then the merge under the lock
        self.assertLessEqual({self.ok, self.evil}, set(self.cache()['files']))   # nothing lost

    def test_a_guard_file_replaced_while_the_hook_runs_neither_saves_nor_loads(self):
        # lens round 1: the code in memory is the old guard; its facts must not be kept under the new file's stat,
        # and the new guard's cache is not for it
        self.assertIsNotNone(self.decide('python3 evil.py'))
        with open(self.cache_path()) as f:
            before = f.read()
        with mock.patch.object(self.g, 'GUARD_AT_START', [0, 0, 0, 0, 0]):
            verdict, seen = self.reads('python3 evil.py')
            self.assertIsNotNone(verdict)
            self.assertIn(self.evil, seen)
            self.assertIsNone(self.decide('python3 ok.py'))
        with open(self.cache_path()) as f:
            self.assertEqual(f.read(), before)

    def test_a_failing_cache_never_changes_the_answer(self):
        blocked = os.path.join(self.dir, '.local')
        with open(blocked, 'w') as f:   # the state folder cannot be made
            f.write('a file')
        self.assertIsNotNone(self.decide('python3 evil.py'))
        self.assertIsNone(self.decide('python3 ok.py'))
        os.unlink(blocked)
        with mock.patch.object(self.g.json, 'dumps', side_effect=ValueError('x')):
            self.assertIsNotNone(self.decide('python3 evil.py'))

    def test_the_gh_config_is_cached_and_read_again_when_it_changes(self):
        cfg = self.write('.config/gh/config.yml', 'aliases:\n    wd: workflow disable\n', 0o644)
        settle()
        self.assertIsNotNone(self.decide('gh wd 1'))
        _, seen = self.reads('gh wd 1')
        self.assertNotIn(cfg, seen)
        self.write('.config/gh/config.yml', 'aliases:\n    wd: workflow list   \n', 0o644)   # same size
        self.assertIsNone(self.decide('gh wd 1'))
        os.unlink(cfg)
        self.assertIsNone(self.decide('gh pr list'))
        self.write('.config/gh/config.yml', 'aliases:\n    wd: workflow disable\n', 0o644)   # was missing
        self.assertIsNotNone(self.decide('gh wd 1'))

    def test_two_hooks_at_once_keep_one_whole_cache(self):
        hook = slow_copy(self, racy=RACY)
        names = [self.write(f'm{i}.py', f'x = {i}\n', 0o644) for i in range(6)]
        settle()
        ev = lambda c: json.dumps({'tool_name': 'Bash', 'cwd': self.dir, 'tool_input': {'command': c}})
        for _ in range(3):
            runs = [(c, subprocess.Popen([sys.executable, '-I', hook], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, cwd=self.dir,
                                         env={'PATH': '/usr/bin:/bin', 'HOME': self.dir}))
                    for i in range(6) for c in (f'python3 m{i}.py', 'python3 evil.py')]
            for c, p in runs:
                out, err = p.communicate(ev(c), timeout=60)
                self.assertEqual('"deny"' in out, c == 'python3 evil.py', (c, out, err))
        c = self.cache()
        self.assertEqual(c['v'], 1)
        self.assertIn(self.evil, c['files'])
        self.assertTrue(set(names) & set(c['files']))
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.cache_path())) if n.endswith('.tmp')], [])


def slow_copy(case, read=0.0, gh=0.0, racy=None):
    """The real hook, with every whole-file read (read_text) and every gh config read taking `read` / `gh` seconds
    more: a loaded machine, a slow disk. Each call adds a line ('read' or 'gh') to slow_calls(case) before it waits:
    a test counts work, which load cannot change, instead of timing it (#1523)."""
    with open(HOOK, encoding='utf-8') as f:
        text = f.read()
    calls = os.path.join(case.dir, 'slow-calls.log')
    for head, delay, name in (('def read_text(path):\n', read, 'read'), ('def gh_config_now(dirs):\n', gh, 'gh')):
        case.assertIn(head, text)
        text = text.replace(head, f"{head}    with open({calls!r}, 'a') as _f: _f.write({name!r} + '\\n')\n"
                                  f'    time.sleep({delay})\n', 1)
    if racy is not None:
        case.assertIn('\nCACHE_RACY = 2.0\n', text)
        text = text.replace('\nCACHE_RACY = 2.0\n', f'\nCACHE_RACY = {racy}\n', 1)
    return case.write('slow-hook', text, 0o755)


def slow_calls(case):
    """The slow calls a slow_copy hook started so far, in order ('read', 'gh')."""
    try:
        with open(os.path.join(case.dir, 'slow-calls.log'), encoding='utf-8') as f:
            return f.read().split()
    except FileNotFoundError:
        return []


class SlowBackendTest(CacheCase):
    """#1752 fix points 3 and 4: with a slow backend the hook still answers well inside its 10 s limit."""

    def run_hook(self, hook, cmd):
        import time
        ev = json.dumps({'session_id': 't', 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'cwd': self.dir,
                         'tool_input': {'command': cmd, 'description': 'x'}})
        t = time.monotonic()
        r = subprocess.run([sys.executable, '-I', hook], input=ev, capture_output=True, text=True, timeout=60,
                           env={'PATH': '/usr/bin:/bin', 'HOME': self.dir}, cwd=self.dir)
        took = time.monotonic() - t
        # a copy without the kit's lib says so (config.notice: a systemMessage and context, no decision): passed
        out = (json.loads(r.stdout).get('hookSpecificOutput') or {}) if r.stdout else {}
        return (out if 'permissionDecision' in out else None), took

    def test_plain_commands_do_not_wait_for_a_slow_disk(self):
        hook = slow_copy(self, read=5.0, gh=5.0)
        self.write('a.txt', 'text\n', 0o644)
        for cmd in ('ls -la', 'git status', 'cat a.txt > b.txt', 'cp a.txt c.txt', 'grep -rn x . | head'):
            with self.subTest(cmd=cmd):
                out, _ = self.run_hook(hook, cmd)
                self.assertIsNone(out, cmd)
                self.assertEqual(slow_calls(self), [], cmd)   # no disk or gh read at all: nothing to wait for

    def test_a_slow_disk_ends_in_a_refusal_well_inside_the_limit(self):
        hook = slow_copy(self, read=2.0)
        for i in range(6):
            self.write(f'm{i}.py', f'x = {i}\n', 0o644)
        out, _ = self.run_hook(hook, '; '.join(f'python3 m{i}.py' for i in range(6)))   # 12 s of reads
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('could not finish', out['permissionDecisionReason'])
        # The budget is 5 s from the start of the process (the limit is 10 s) and every read takes 2 s or more, so a
        # hook that keeps its budget starts at most 3 of the 6 reads; a loaded machine only makes that fewer.
        self.assertLessEqual(slow_calls(self).count('read'), 3, slow_calls(self))

    def test_a_slow_log_never_holds_or_doubles_the_answer(self):
        # the refusal is written out before the log line, and the log gets 2 s past the budget at most
        import select, time
        hook = slow_copy(self)
        with open(hook, encoding='utf-8') as f:
            text = f.read()
        head = "        d = state_dir(env) or state_dir({'HOME': os.path.expanduser('~')})\n"
        self.assertEqual(text.count(head), 1)
        self.write('slow-hook', text.replace(head, '        time.sleep(600)\n' + head), 0o755)   # a log that never ends
        ev = json.dumps({'tool_name': 'Bash', 'cwd': self.dir, 'tool_input': {'command': 'python3 evil.py'}})
        t = time.monotonic()
        p = subprocess.Popen([sys.executable, '-I', hook], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, cwd=self.dir,
                             env={'PATH': '/usr/bin:/bin', 'HOME': self.dir})
        try:
            p.stdin.write(ev)
            p.stdin.close()
            ready, _, _ = select.select([p.stdout], [], [], 4)
            self.assertTrue(ready, 'no answer before the log')
            self.assertEqual(json.loads(p.stdout.readline())['hookSpecificOutput']['permissionDecision'], 'deny')
            p.wait(timeout=60)   # the hook's alarm ends the log 2 s past the budget; waiting for it would take 600 s
            self.assertLess(time.monotonic() - t, 60)
            self.assertEqual(p.stdout.read(), '')   # one answer only
        finally:
            p.kill()
            p.wait()
            p.stdout.close()
            p.stderr.close()
        # a log that fails after the answer adds no second answer (two JSON objects are no answer at all)
        import contextlib, io
        out = io.StringIO()
        with mock.patch.object(self.g, 'log', side_effect=self.g.Slow()), \
                mock.patch.object(self.g, 'process_age', lambda: 0.0), \
                mock.patch.object(sys, 'stdin', io.StringIO(ev)), \
                mock.patch.dict(os.environ, {'HOME': self.dir}), contextlib.redirect_stdout(out):
            self.assertEqual(self.g.main(), 0)
        self.assertEqual(json.loads(out.getvalue())['hookSpecificOutput']['permissionDecision'], 'deny')
        # and a log that comes too late writes nothing
        with mock.patch.object(self.g, 'process_age', lambda: self.g.BUDGET + 2.5):
            self.g.log('x', 'y', {'HOME': self.dir})
        self.assertFalse(os.path.exists(os.path.join(self.dir, '.local', 'state', 'actions-disable-guard', 'log')))

    def test_the_second_run_is_fast_with_the_same_answer(self):
        # 3 s of slow reads in a 5 s budget: the run that pays for them must still leave them in the cache
        slow_read = slow_copy(self, read=3.0, racy=RACY)
        os.rename(slow_read, slow_read + '-read')
        slow_read += '-read'
        slow_gh = slow_copy(self, gh=3.0, racy=RACY)
        self.write('.config/gh/config.yml', 'aliases:\n    wd: workflow disable\n    co: pr checkout\n', 0o644)
        settle()
        for cmd, verdict, hook in (('python3 ok.py', None, slow_read), ('python3 evil.py', 'deny', slow_read),
                                   ('gh wd 1', 'deny', slow_gh), ('gh co 1', None, slow_gh)):
            with self.subTest(cmd=cmd):
                if os.path.exists(self.cache_path()):   # each command starts with no cache
                    os.unlink(self.cache_path())
                first, took1 = self.run_hook(hook, cmd)
                again, took2 = self.run_hook(hook, cmd)
                self.assertEqual(first, again)
                self.assertEqual(first and first['permissionDecision'], verdict)
                self.assertGreater(took1, 3.0)
                self.assertLess(took2, 0.9)


if __name__ == '__main__':
    unittest.main()
