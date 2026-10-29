"""Offline fixtures execute the Python embedded in the actual workflow.

No GitHub requests, model invocation, credentials, or repository code execution.
Run with python -B scripts/test_review_workflow.py from this worktree.
"""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / '.github/workflows/reusable-opus-pr-review.yml').read_text(encoding='utf-8')
HEAD, BASE = 'a' * 40, 'b' * 40

def scripts(tag):
    pattern = rf"(?ms)^          (?:if ! )?python3 - <<'{tag}'\n(.*?)^          {tag}$"
    return ['\n'.join(line[10:] for line in match.splitlines()) + '\n'
            for match in re.findall(pattern, TEXT)]

def pr():
    return {'state': 'open', 'draft': False,
            'head': {'sha': HEAD, 'repo': {'full_name': 'owner/repo'}}, 'base': {'sha': BASE}}

def comment(body='@claude review'):
    return {'id': 23, 'user': {'login': 'owner'}, 'body': body,
            'issue_url': 'https://api.github.com/repos/owner/repo/issues/132'}

class WorkflowFixtures(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.fixture-tmp'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.assertTrue(self.folder.is_relative_to(ROOT.resolve()))

    def execute(self, tag, env):
        code = scripts(tag)
        self.assertTrue(code, tag)
        self.assertTrue(all(item == code[0] for item in code), 'Guards diverged')
        minimal = {'PYTHONUTF8': '1', 'SYSTEMROOT': os.environ.get('SYSTEMROOT', r'C:\Windows')}
        minimal.update(env)
        return subprocess.run([sys.executable, '-B', '-c', code[0]], cwd=self.folder,
                              env=minimal, text=True, capture_output=True, timeout=10)

    def request(self, snapshot=None, request=None, event='issue_comment', comment_id=23):
        (self.folder / 'pr.json').write_text(json.dumps(snapshot or pr()))
        (self.folder / 'comment.json').write_text(json.dumps(request or comment()))
        output = self.folder / 'output.txt'
        if output.exists():
            output.unlink()
        result = self.execute('PY_REQUEST', {'PR_SNAPSHOT': str(self.folder / 'pr.json'),
            'REQUEST_SNAPSHOT': str(self.folder / 'comment.json'), 'REQUEST_COMMENT_ID': str(comment_id),
            'REPO': 'owner/repo', 'REPOSITORY_OWNER': 'owner', 'PR': '132',
            'EVENT_NAME': event, 'GITHUB_OUTPUT': str(output)})
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split('=', 1) for line in output.read_text().splitlines())

    def test_owner_request_freezes_exact_revision_and_accepts_capitalized_mention(self):
        self.assertEqual(self.request(request=comment('@Claude')), {'accepted': 'true', 'head_sha': HEAD, 'base_sha': BASE})

    def test_other_comments_actors_and_wrong_comment_binding_are_noops(self):
        cases = [comment('thanks'), comment('quoted @claude'), comment('@claude implement this'),
                 {**comment(), 'user': {'login': 'someone-else'}},
                 {**comment(), 'id': 24},
                 {**comment(), 'issue_url': 'https://api.github.com/repos/owner/repo/issues/134'}]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(self.request(request=case), {'accepted': 'false'})
        self.assertEqual(self.request(event='issues'), {'accepted': 'false'})

    def test_closed_draft_fork_and_invalid_revision_do_not_reach_model(self):
        cases = []
        for key, value in [('state', 'closed'), ('draft', True)]:
            case = pr(); case[key] = value; cases.append(case)
        case = pr(); case['head']['repo']['full_name'] = 'owner/another-repo'; cases.append(case)
        case = pr(); case['head']['sha'] = 'master'; cases.append(case)
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(self.request(snapshot=case), {'accepted': 'false'})

    def test_legacy_label_caller_still_has_no_mention_requirement(self):
        self.assertEqual(self.request(event='pull_request', comment_id=0)['accepted'], 'true')

    @unittest.skipUnless(os.environ.get('DESK_CALLER_PATH'), 'Optional consumer check: set DESK_CALLER_PATH')
    def test_caller_rejects_unrelated_mentions_before_shared_concurrency(self):
        caller = Path(os.environ['DESK_CALLER_PATH'])
        text = caller.read_text(encoding='utf-8')
        match = re.search(r"contains\(fromJSON\('([^']+)'\), github.event.comment.body\)", text)
        self.assertIsNotNone(match, 'Caller must use exact allowlist before shared concurrency')
        allowed = [item.casefold() for item in json.loads(match.group(1))]
        self.assertIn('@Claude'.casefold(), allowed)
        self.assertIn('@claude review', allowed)
        self.assertNotIn('thanks @claude', allowed)
        self.assertNotIn('@claude implement this', allowed)
        self.assertNotIn("contains(github.event.comment.body, '@claude')", text)

    def test_both_publish_paths_reject_moved_head_base_or_closed_pr(self):
        self.assertEqual(len(scripts('PY_FRESHNESS')), 2)
        cases = [(pr(), 0)]
        case = pr(); case['head']['sha'] = 'c' * 40; cases.append((case, 1))
        case = pr(); case['base']['sha'] = 'c' * 40; cases.append((case, 1))
        case = pr(); case['state'] = 'closed'; cases.append((case, 1))
        for case, expected in cases:
            with self.subTest(case=case):
                file = self.folder / 'current.json'; file.write_text(json.dumps(case))
                result = self.execute('PY_FRESHNESS', {'CURRENT_SNAPSHOT': str(file), 'HEAD_SHA': HEAD, 'BASE_SHA': BASE})
                self.assertEqual(result.returncode, expected, result.stderr)

    def evidence(self, raw, messages=None):
        (self.folder / 'review-result').mkdir(exist_ok=True)
        transcript = self.folder / 'execution.json'
        transcript.write_text(json.dumps(messages or []))
        result = self.execute('PY_EVIDENCE', {'STRUCTURED': raw, 'EXECUTION_FILE': str(transcript),
            'REQUESTED_MODEL': 'claude-opus-5-5', 'REVIEW_SESSION_ID': '', 'HEAD_SHA': HEAD, 'BASE_SHA': BASE})
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.folder / 'review-result'

    def test_failed_action_keeps_invalid_original_and_unknown_model_evidence(self):
        folder = self.evidence('not-json')
        self.assertEqual((folder / 'original-structured-output.json').read_text(), 'not-json')
        observed = json.loads((folder / 'model-observation.json').read_text())
        self.assertEqual(observed['reported_models'], [])
        self.assertIsNone(observed['session_id'])
        self.assertFalse((folder / 'original-review.md').exists())

    def test_sdk_recovery_preserves_original_body_before_hydration(self):
        payload = {'result': 'CHANGES_REQUESTED', 'review_markdown': '(see structured field)',
                   'counts': {'p0': 0, 'p1': 0, 'p2': 1, 'p3': 0}}
        messages = [{'type': 'assistant', 'message': {'model': 'observed-other-model'}},
                    {'type': 'result', 'session_id': 'fixture-session', 'modelUsage': {'observed-other-model': {}},
                     'structured_output': payload, 'tool_results': 'MUST NOT BE RETAINED'}]
        folder = self.evidence('{}', messages)
        self.assertEqual((folder / 'original-action-output.json').read_text(), '{}')
        self.assertEqual(json.loads((folder / 'original-structured-output.json').read_text()), payload)
        self.assertEqual((folder / 'original-review.md').read_text(), '(see structured field)')
        observed = json.loads((folder / 'model-observation.json').read_text())
        self.assertEqual(observed['reported_models'], ['observed-other-model'])
        self.assertEqual(observed['session_id'], 'fixture-session')
        self.assertNotIn('MUST NOT BE RETAINED', (folder / 'model-observation.json').read_text())

    def test_null_and_false_action_shapes_retain_the_sdk_original(self):
        payload = {'result': 'CHANGES_REQUESTED', 'review_markdown': '(see structured field)',
                   'counts': {'p0': 0, 'p1': 0, 'p2': 1, 'p3': 0}}
        for value in [None, False]:
            raw = json.dumps(dict.fromkeys(['result', 'review_markdown', 'counts'], value))
            folder = self.evidence(raw, [{'type': 'result', 'structured_output': payload}])
            self.assertEqual((folder / 'original-action-output.json').read_text(), raw)
            self.assertEqual(json.loads((folder / 'original-structured-output.json').read_text()), payload)
            self.assertEqual((folder / 'original-review.md').read_text(), '(see structured field)')
            self.assertEqual(json.loads((folder / 'model-observation.json').read_text())['original_source'], 'SDK result structured_output')

    def test_publisher_rejects_malformed_counts_before_shell_arithmetic(self):
        folder = self.folder / 'review-result'; folder.mkdir()
        for invalid in ['p1[$(printf SHOULD_NOT_EXECUTE)]', -1, True, 1.5, 2**63]:
            (folder / 'structured_output.json').write_text(json.dumps({'counts': {'p0': invalid, 'p1': 0, 'p2': 0, 'p3': 0}}))
            result = self.execute('PY_COUNTS', {})
            self.assertEqual(result.returncode, 1, result.stderr)
        (folder / 'structured_output.json').write_text(json.dumps({'counts': dict.fromkeys(['p0', 'p1', 'p2', 'p3'], 0)}))
        self.assertEqual(self.execute('PY_COUNTS', {}).returncode, 0)

    def test_happy_report_keeps_exact_original_and_missing_evidence_verdict(self):
        raw = '{"result":"UNABLE_TO_VERIFY","review_markdown":"Missing required CI evidence."}'
        folder = self.evidence(raw)
        self.assertEqual((folder / 'original-structured-output.json').read_text(), raw)
        self.assertEqual((folder / 'original-review.md').read_text(), 'Missing required CI evidence.')

    def test_workflow_binds_guards_and_always_retains_failed_review_artifacts(self):
        self.assertIn("if: needs.claim.outputs.accepted == 'true'", TEXT)
        self.assertIn("needs.claim.outputs.accepted == 'true'", TEXT[TEXT.index('  publish:'):])
        review = TEXT[TEXT.index('  review:'):TEXT.index('  publish:')]
        permissions = review[review.index('    permissions:'):review.index('    steps:')]
        self.assertNotIn(': write', permissions)
        self.assertIn('--tools "Read,Glob,Grep"', review)
        self.assertNotIn('--allowedTools "Read,Glob,Grep,LS,Bash', review)
        self.assertIn('ref: ${{ needs.claim.outputs.head_sha }}', review)
        persist = review.split('name: Persist review result', 1)[1].split('\n      - name:', 1)[0]
        upload = review.split('name: Upload review result', 1)[1].split('\n      - name:', 1)[0]
        self.assertIn('\n        if: always()', persist)
        self.assertIn('\n        if: always()', upload)
        self.assertIn('NEVER set `review_markdown` to a pointer', review)
        self.assertIn('def has_finding_body:', review)
        self.assertIn('PASS|CHANGES_REQUESTED|UNABLE_TO_VERIFY', review)
        self.assertIn('retention-days: 30', review)

if __name__ == '__main__':
    unittest.main(verbosity=2)
