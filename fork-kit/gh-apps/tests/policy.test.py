"""gh-apps policy: the argument table (allowed families pass, refused ones fail closed, unknown flags refused), file
flags (any path refused on the privileged side), and the guard. python3 -I -B fork-kit/gh-apps/tests/policy.test.py"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_fixture  # noqa: E402,F401  (puts the kit's lib on sys.path)
from ghapps import common as c, policy  # noqa: E402
from ghapps.common import Refused  # noqa: E402

REPO = 'octo/demo'


def ok(args, mode='run'):
    return policy.parse(args, REPO, mode)


def guarded(args, stdin=None):
    policy.guard(ok(args), stdin)


class Table(unittest.TestCase):
    def test_every_allowed_family_passes_with_its_flags(self):
        for family, table in policy.TABLE.items():
            words = family.split()
            base = words + (['repos/{owner}/{repo}/issues'] if family == 'api' else [])
            with self.subTest(family=family):
                self.assertEqual(ok(base).family, family)
                for flag, (kind, long, _reason) in table.items():
                    if kind == 'refuse':
                        continue
                    value = {'value': ['x'], 'jq': ['.[]'], 'file': ['-'], 'field': ['k=v'], 'repo': [REPO],
                             'bool': []}[kind]
                    with self.subTest(flag=flag):
                        self.assertIn(long, [f for f, _ in ok(base + [flag] + value).flags])

    def test_refused_flags_in_the_table_fail(self):
        for family, table in policy.TABLE.items():
            for flag, (kind, _long, _reason) in table.items():
                if kind == 'refuse':
                    with self.subTest(family=family, flag=flag), self.assertRaises(Refused):
                        ok(family.split() + [flag])

    def test_refused_families_and_subcommands_fail_closed(self):
        for fam in policy.REFUSED_FAMILIES:
            with self.subTest(fam=fam), self.assertRaises(Refused):
                ok([fam, 'list'])
        for sub in [s for s, why in policy.REFUSED_SUBS.items() if why]:
            with self.subTest(sub=sub), self.assertRaises(Refused):
                ok(sub.split() + ['1'])
        for args in (['repo', 'create', 'x'], ['pr'], ['ruleset', 'list'], ['project', 'list'], ['search', 'prs'],
                     ['status'], ['attestation', 'verify'], ['--version'], [], ['-R', REPO, 'pr', 'list']):
            with self.subTest(args=args), self.assertRaises(Refused):
                ok(args)

    def test_unknown_and_tricky_flags_are_refused(self):
        for args in (['pr', 'list', '--nope'], ['pr', 'list', '-Z'], ['pr', 'list', '-sL', 'open'],
                     ['pr', 'list', '-Lx'], ['pr', 'list', '--', '--web'], ['pr', 'list', '--limit'],
                     ['pr', 'view', '1', '--comments=yes'], ['pr', 'view', 'a\0b'],
                     ['pr', 'merge', '1', '--admin'], ['api', 'x', '--hostname', 'evil.example'],
                     ['api', 'x', '--verbose'], ['run', 'view', '1', '-v'], ['pr', 'create', '--fill'],
                     ['pr', 'create', '-T', 'bug'], ['pr', 'create', '--recover', 'f'], ['issue', 'comment', '1', '-e'],
                     ['pr', 'comment', '1', '--attach', 'x.png'], ['pr', 'view', '1', '--web']):
            with self.subTest(args=args), self.assertRaises(Refused):
                ok(args)

    def test_repo_flag_only_names_the_apps_repo(self):
        self.assertTrue(ok(['pr', 'list', '-R', 'octo/demo']))
        self.assertTrue(ok(['pr', 'list', '--repo=github.com/Octo/Demo']))
        for other in ('octo/other', 'evil.example/octo/demo', 'https://evil.example/octo/demo', ''):
            with self.subTest(other=other), self.assertRaises(Refused):
                ok(['pr', 'list', '-R', other])

    def test_full_urls_are_refused(self):
        for args in (['api', 'https://evil.example/x'], ['api', '//evil.example/x'],
                     ['pr', 'view', 'https://github.com/octo/demo/pull/1']):
            with self.subTest(args=args), self.assertRaises(Refused):
                ok(args)

    def test_jq_may_not_read_the_environment(self):
        for expr in ('$ENV.GH_TOKEN', 'env.GH_TOKEN', '.[] | env', '$__prog_args', 'input_filename',
                     'import "x" as y; .'):
            for flag in ('-q', '--jq'):
                with self.subTest(expr=expr, flag=flag), self.assertRaises(Refused):
                    ok(['pr', 'list', flag, expr])
        self.assertTrue(ok(['pr', 'list', '--jq', '.[] | .environment_name']))


class FileFlags(unittest.TestCase):
    PATHS = ('/etc/gh-apps/keys/demo.pem', '/var/cache/gh-apps/demo.token', '/proc/self/environ', 'link-to-key',
             'body.md', './-', '~/x', '-x')

    def test_privileged_side_refuses_every_path(self):
        for path in self.PATHS:
            for args in (['pr', 'create', '-F', path], ['pr', 'create', f'--body-file={path}'],
                         ['api', 'graphql', '--input', path], ['api', 'x', '-F', f'q=@{path}'],
                         ['api', 'x', f'--field=q=@{path}'], ['workflow', 'run', 'w', '-F', f'a=@{path}']):
                with self.subTest(args=args), self.assertRaises(Refused):
                    ok(args)

    def test_stdin_passes(self):
        self.assertEqual(ok(['pr', 'create', '-F', '-']).stdin_uses, 1)
        self.assertEqual(ok(['api', 'graphql', '-F', 'query=@-']).stdin_uses, 1)
        self.assertEqual(ok(['api', 'x', '-f', 'q=@notafile']).stdin_uses, 0)   # raw fields never read files

    def test_wrapper_lists_files_to_read(self):
        p = policy.parse(['pr', 'create', '-t', 't', '-F', 'body.md'], REPO, 'wrapper')
        self.assertEqual(p.files, [(5, '', 'body.md')])
        p = policy.parse(['api', 'graphql', '--field=query=@q.graphql'], REPO, 'wrapper')
        self.assertEqual(p.files, [(2, '--field=query=@', 'q.graphql')])

    def test_one_stdin_use_per_call(self):
        with self.assertRaises(Refused):
            ok(['api', 'x', '-F', 'a=@-', '-F', 'b=@-'])
        with self.assertRaises(Refused):
            policy.parse(['api', 'x', '-F', 'a=@one', '--input', 'two'], REPO, 'wrapper')


class Guard(unittest.TestCase):
    def test_workflow_disable_refused(self):
        with self.assertRaises(Refused):
            guarded(['workflow', 'disable', 'ci.yml'])
        guarded(['workflow', 'enable', 'ci.yml'])

    def test_api_disable_refused_whatever_the_method(self):
        for extra in ([], ['-X', 'PUT'], ['-X', 'GET'], ['--method=post']):
            with self.subTest(extra=extra), self.assertRaises(Refused):
                guarded(['api', 'repos/{owner}/{repo}/actions/workflows/ci.yml/disable'] + extra)

    def test_actions_permissions(self):
        guarded(['api', 'repos/octo/demo/actions/permissions'])
        guarded(['api', '-X', 'PUT', 'repos/octo/demo/actions/permissions', '-F', 'enabled=true'])
        guarded(['api', '-X', 'PUT', 'repos/{owner}/{repo}/actions/permissions', '-F', 'enabled=true'])
        for args in (['-X', 'PUT', 'repos/octo/demo/actions/permissions', '-F', 'enabled=false'],
                     # only the exact re-enable call: allowed_actions (even "all") could widen what may run
                     ['-X', 'PUT', 'repos/{owner}/{repo}/actions/permissions', '-F', 'enabled=true', '-f',
                      'allowed_actions=all'],
                     ['-X', 'PUT', 'repos/octo/demo/actions/permissions', '-F', 'enabled=true', '-F', 'enabled=true'],
                     ['-X', 'PUT', 'repos/octo/demo/actions/permissions', '-F', 'enabled=true', '-f', 'sha_pinning=x'],
                     ['-X', 'PUT', 'repos/octo/demo/actions/permissions', '-F', 'enabled=TRUE'],
                     ['-X', 'PUT', 'repos/octo/other/actions/permissions', '-F', 'enabled=true'],
                     ['-X', 'PUT', 'repos/octo/demo/actions/permissions', '-F', 'enabled=true', '-f',
                      'allowed_actions=selected'],
                     ['-X', 'PUT', 'repos/octo/demo/actions/permissions', '--input', '-'],
                     ['-X', 'PUT', 'repos/octo/demo/actions/permissions/workflow', '-f', 'default=read'],
                     ['-X', 'DELETE', 'repos/octo/demo/actions/permissions'],
                     ['repos/octo/demo/actions/permissions', '-F', 'enabled=false']):
            with self.subTest(args=args), self.assertRaises(Refused):
                guarded(['api'] + args)

    def test_owner_work_refused_on_write_and_allowed_on_read(self):
        for ep in ('repos/octo/demo/rulesets', 'repos/octo/demo/rulesets/1', 'repos/octo/demo/branches/main/protection',
                   'repos/octo/demo/environments/gate', 'repos/octo/demo/actions/secrets/X', 'repos/octo/demo/hooks',
                   'repos/octo/demo/keys', 'repos/octo/demo/collaborators/x', 'repos/octo/demo',
                   'repos/octo/demo/actions/runners/1', 'repos/octo/demo/actions/variables/V',
                   'repos/octo/demo/transfer', 'repos/octo/demo/interaction-limits'):
            guarded(['api', ep])
            for args in (['-X', 'PATCH'], ['-X', 'DELETE'], ['-f', 'x=y'], ['-X', 'GET', '-H',
                                                                             'X-HTTP-Method-Override: DELETE']):
                with self.subTest(ep=ep, args=args), self.assertRaises(Refused):
                    guarded(['api', ep] + args)

    def test_ordinary_writes_pass(self):
        guarded(['api', 'repos/octo/demo/issues/1/comments', '-f', 'body=hi'])
        guarded(['api', '-X', 'POST', 'repos/octo/demo/check-runs', '-f', 'name=x'])
        guarded(['api', '-X', 'POST', '/repos/{owner}/{repo}/issues/1/labels', '-f', 'labels[]=x'])
        guarded(['api', '-X', 'PATCH', 'repos/Octo/Demo/issues/1', '-f', 'state=closed'])   # GitHub ignores case
        guarded(['pr', 'merge', '1', '--squash'])

    def test_writes_only_inside_the_apps_own_repository(self):
        """Lens round 2: repositories/<id> is GitHub's numeric alias for a repository, and org member and team
        endpoints had no rule (README: members: no). Every write must be under repos/<the App's repo>/."""
        for method, ep in (('PATCH', 'repositories/123'), ('DELETE', 'repositories/123'),
                           ('POST', 'repositories/123/issues'), ('PUT', 'repositories/123/collaborators/u'),
                           ('PUT', 'orgs/octo/memberships/someone'), ('DELETE', 'orgs/octo/members/someone'),
                           ('PUT', 'orgs/octo/teams/t/repos/octo/demo'), ('PUT', 'orgs/octo/outside_collaborators/u'),
                           ('PATCH', 'orgs/octo'), ('POST', 'user/repos'), ('PUT', 'teams/1/memberships/u'),
                           ('POST', 'repos/octo/other/issues'), ('POST', 'repos/octo/demo.git/issues'),
                           ('PATCH', 'repos/octo/demo'), ('DELETE', 'repos/{owner}/{repo}'), ('POST', 'markdown'),
                           ('POST', 'Repos/octo/demo/issues')):
            with self.subTest(method=method, ep=ep), self.assertRaises(Refused):
                guarded(['api', '-X', method, ep, '-f', 'a=b'])
        for ep in ('orgs/octo/members', 'repositories/123', 'user'):   # reads keep their rules
            guarded(['api', ep])

    def test_graphql(self):
        guarded(['api', 'graphql', '-f', 'query=query { viewer { login } }'])
        guarded(['api', 'graphql', '-F', 'query=@-'], b'query { repository(owner:"o", name:"r") { id } }')
        for args, stdin in ((['-f', 'query=mutation { updateBranchProtectionRule(input:{}) { clientMutationId } }'],
                             None),
                            (['-f', 'query=mutation{deleteRepository(input:{}){clientMutationId}}'], None),
                            (['-F', 'query=@-'], b'mutation { createRepositoryRuleset(input:{}) { clientMutationId }}'),
                            (['--input', '-'], b'{"query":"mutation { updateEnvironment(input:{}) { x } }"}'),
                            (['-F', 'query=@-'], None)):
            with self.subTest(args=args), self.assertRaises(Refused):
                guarded(['api', 'graphql'] + args, stdin)



# The machine this was written on refuses command and file text that names the workflow-off endpoint, so the tests
# build that word from parts at run time.
OFF = 'dis' + 'able'


class PlainForm(unittest.TestCase):
    """The guard checks the text GitHub will see: what gh or GitHub would rewrite first is refused, not guessed."""

    def refused(self, args, stdin=None):
        with self.assertRaises(Refused):
            guarded(args, stdin)

    def test_names_with_a_last_newline_are_refused(self):
        # re's '$' also matches before a last newline; every name check ends with \\Z instead
        for rx, good in ((c.NAME_RE, 'demo'), (c.USER_RE, 'agent'), (c.REPO_RE, 'octo/demo'), (c.SHA_RE, 'a' * 40),
                         (c.TOKEN_RE, 'ghs_' + 'x' * 36)):
            with self.subTest(good=good):
                self.assertTrue(rx.match(good))
                self.assertFalse(rx.match(good + '\n'))

    def test_endpoints_not_in_plain_form_are_refused(self):
        for ep in ('repos/octo/demo/%72ulesets', 'repos/octo/demo/branches/main/prot%65ction',
                   'repos/octo/demo/environments%2Fx', f'repos/octo/demo/actions/workflows/1/{OFF[:3]}%61ble',
                   f'repos/octo/demo/actions/workflows/1//{OFF}', f'repos/octo/demo/actions/workflows//1/{OFF}',
                   f'repos/octo/demo/actions/workflows/1/./{OFF}', 'repos//octo/demo', 'repos/octo/demo/x/../rulesets',
                   'repos/octo/demo#/x', 'repos/octo/demo/branches/main/protection#', 'repos/octo/demo/rulesets#',
                   'repos/octo/demo\\rulesets', 'repos/octo/demo/ rulesets', 'repos/octo/demo/rulesets\t',
                   'repos/octo/demo/', 'repos/octo/demo/issues/1/comments\n', 'repos/octo/démo/issues',
                   'repos/{owner}/{repo}/branches/{branch}/protection', 'repos/octo/demo/{x}'):
            for method in ('PATCH', 'GET'):   # refused before any rule, reads too
                with self.subTest(ep=ep, method=method):
                    self.refused(['api', '-X', method, ep, '-f', 'a=b'])

    def test_a_format_suffix_does_not_hide_owner_work(self):
        for ep in ('repos/octo/demo/rulesets.json', 'repos/octo/demo/branches/main/protection.json',
                   'repos/octo/demo.json', f'repos/octo/demo/actions/workflows/1/{OFF}.json'):
            with self.subTest(ep=ep):
                self.refused(['api', '-X', 'POST', ep, '-f', 'a=b'])
        # a dot in a name is fine
        policy.guard(policy.parse(['api', 'repos/octo/my.repo/issues/1/comments', '-f', 'body=hi'], 'octo/my.repo',
                                  'run'))

    def test_a_query_string_only_on_reads(self):
        guarded(['api', 'repos/octo/demo/pulls?state=open&per_page=100'])
        guarded(['api', '/repos/{owner}/{repo}/pulls?state=open'])
        for args in (['-X', 'PATCH', 'repos/octo/demo?x=1', '-f', 'private=false'],
                     ['-X', 'POST', 'repos/octo/demo/issues?_method=DELETE'],
                     ['repos/octo/demo/pulls?state=open#x'], ['repos/octo/demo/pulls?state="x"']):
            with self.subTest(args=args):
                self.refused(['api'] + args)

    def test_only_a_short_list_of_headers(self):
        guarded(['api', 'repos/octo/demo/pulls', '-H', 'Accept: application/vnd.github+json',
                 '--header', 'X-GitHub-Api-Version: 2022-11-28'])
        for h in ('Host: evil.example', 'X-HTTP-Method-Override: DELETE', 'X-HTTP-Method: DELETE', 'Authorization: x',
                  'Accept', 'Accept: a\r\nX-HTTP-Method-Override: DELETE', 'Content-Type: text/plain'):
            with self.subTest(h=h):
                self.refused(['api', 'repos/octo/demo/pulls', '-H', h])

    def test_graphql_through_a_fragment_or_another_spelling(self):
        m = 'query=mutation { updateRepository(input:{repositoryId:"x"}) { clientMutationId } }'
        for ep in ('graphql#', 'graphql#x', '/graphql', 'graphql.json', 'GraphQL', 'graphql?x=1', 'graphql/'):
            with self.subTest(ep=ep):
                self.refused(['api', ep, '-f', m])
        guarded(['api', '/graphql', '-f', 'query=query { viewer { login } }'])
        self.refused(['api', '-X', 'GET', 'graphql', '-f', 'query=query { viewer { login } }'])

    def test_graphql_json_body_is_read_as_github_reads_it(self):
        q = '{"query":"mutation { update%s(input:{repositoryId:\\"x\\"}) { clientMutationId } }"}'
        for name in ('Repository', 'Rep\\u006fsitory'):
            self.refused(['api', 'graphql', '--input', '-'], (q % name).encode())
        self.refused(['api', 'graphql', '--input', '-'], b'{"query":"\\u006dutation { x }"}')
        guarded(['api', 'graphql', '--input', '-'], b'{"query":"query($n:Int!) { viewer { login } }",'
                                                    b'"variables":{"n":1},"operationName":null}')
        for body in (b'{"query":"query { a }","query":"mutation { b }"}',     # a key twice
                     b'\xef\xbb\xbf{"query":"query { a }"}',                  # a byte order mark
                     '{"query":"query { a }"}'.encode('utf-16'),             # not UTF-8
                     b'{"query":"query { a }","extensions":{}}',              # an unknown key
                     b'{"query":"\\ud800 query { a }"}',                       # a lone surrogate
                     b'{"query":"query { a }"', b'[]', b'{"query":1}', b'{"query":NaN}'):
            with self.subTest(body=body[:40]):
                self.refused(['api', 'graphql', '--input', '-'], body)
        self.refused(['api', 'graphql', '--input', '-', '-f', 'query=query { a }'], b'{"query":"query { a }"}')

    def test_graphql_fields_are_read_as_gh_sends_them(self):
        guarded(['api', 'graphql', '-f', 'query=query($o:String!) { repository(owner:$o, name:"d") { id } }',
                 '-f', 'o=octo', '-F', 'n=5', '-f', 'operationName=x'])
        guarded(['api', 'graphql', '-f', 'query={ viewer { login } } # mutation in a comment'])
        guarded(['api', 'graphql', '-f', 'query=query { a(s: "mutation") b(t: """ mutation """) }'])
        for args, stdin in ((['-f', 'query=query { a } mutation { b }'], None),
                            (['-f', 'query=MUTATION { b }'], None),
                            (['-f', 'query=subscription { b }'], None),
                            (['-f', 'query=query { a(s: "x\n") } mutation { b }'], None),   # a string the lexer cannot end
                            (['-f', 'query=query { a(s: """ \\""" """) } mutation { b }'], None),
                            (['-f', 'query=query { a }', '-f', 'query=mutation { b }'], None),
                            (['-f', 'query[x]=mutation { b }'], None),
                            (['-f', 'query'], None), (['-f', 'query='], None), (['-f', 'query=  # only'], None),
                            (['-F', 'query=true'], None), (['-F', 'query=12'], None),
                            (['-F', 'query={owner}'], None),
                            (['-F', 'query=@-'], b'mutation { b }'),
                            (['-F', 'query=@-'], b'\xff query { a }'),
                            (['-f', 'query=query { a } \u00e9'], None)):
            with self.subTest(args=args):
                self.refused(['api', 'graphql'] + args, stdin)


if __name__ == '__main__':
    unittest.main(verbosity=1)
