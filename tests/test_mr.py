import json
import importlib.util
import io
from contextlib import redirect_stderr
from unittest.mock import patch
import os
import re
import fcntl
import shutil
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / 'scripts/mr.py'
spec = importlib.util.spec_from_file_location('mr', CLI)
mr = importlib.util.module_from_spec(spec)
with patch.object(sys, 'dont_write_bytecode', True):
    spec.loader.exec_module(mr)


class CLIExamples(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.run = self.root / 'multi-review-test'
        self.run.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin), GIT_CONFIG_NOSYSTEM='1',
                        GIT_CONFIG_GLOBAL=os.devnull, PYTHONDONTWRITEBYTECODE='1')
        for name in ('git', 'diff', 'bash', 'tr'):
            (self.bin / name).symlink_to(shutil.which(name))
        self.fake('claude', "print('問題なし')\n")
        self.fake('codex', "import sys\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('-o')+1]).write_text('問題なし')\n")
        self.call('init', self.run, '--cwd', self.repo, '--path', '.')
        self.iter = self.run / 'iter-1'

    def write_json(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def read_json(self, path):
        return json.loads(path.read_text(encoding='utf-8'))

    def call(self, *args, code=0):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        if code == 1:
            self.assertTrue(result.stderr.strip(), 'failure needs a diagnostic')
            self.assertNotIn('Traceback', result.stderr)
        self.last_stderr = result.stderr
        return json.loads(result.stdout) if result.stdout else None

    def fail_replacement(self, target, *args):
        launcher = """import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv.pop(1))
import mr
original = mr.os.replace
target = Path(sys.argv.pop(1))
def replace(source, destination):
    if Path(destination) == target:
        raise OSError('injected replacement failure')
    return original(source, destination)
with patch.object(mr.os, 'replace', replace):
    sys.exit(mr.main())
"""
        result = subprocess.run([sys.executable, '-c', launcher, str(CLI.parent), str(target), *map(str, args)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('injected replacement failure', result.stderr)

    def fail_tree_generation(self):
        self.fake('git', 'import os,sys\n'
                  "if 'write-tree' in sys.argv: sys.exit('injected tree failure')\n"
                  + 'os.execv(' + repr(shutil.which('git')) + ', ["git", *sys.argv[1:]])\n')

    def review_after_start(self):
        # Start the timeout only after the fake CLI and its child have reported readiness.
        launcher = """import sys,time,subprocess
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv.pop(1))
import mr
review_cwd = Path(sys.argv.pop(1))
original = subprocess.Popen.communicate
def communicate(process, *args, **kwargs):
    if kwargs.get('timeout') is not None:
        marker = review_cwd / ('started-' + str(process.pid))
        end = time.monotonic() + 15
        while not marker.is_file() and time.monotonic() < end:
            time.sleep(.01)
        assert marker.is_file(), 'fake CLI did not start'
    return original(process, *args, **kwargs)
with patch.object(subprocess.Popen, 'communicate', communicate):
    sys.exit(mr.main())
"""
        result = subprocess.run([sys.executable, '-c', launcher, str(CLI.parent), str(self.repo), 'review', str(self.iter)],
                                env=self.env, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('all reviewers failed', result.stderr)
        return self.read_json(self.iter / 'review.json')

    def replace_review_output(self, directory, text, as_cli='claude'):
        # Deliberately corrupt a completed report while keeping its transport integrity valid.
        review = self.read_json(directory / 'review.json')
        job = next(j for j in review['reviewers'] if j['cli'] == 'claude')
        Path(job['output']).write_text(text)
        job.update(cli=as_cli, outputHash=mr.digest(text.encode()))
        self.write_json(directory / 'review.json', review)

    def fake(self, name, body):
        path = self.bin / name
        self.assertEqual(path.parent, self.bin)
        path.unlink(missing_ok=True)
        path.write_text('#!' + sys.executable + '\n' + body, encoding='utf-8')
        path.chmod(0o755)

    def assert_process_stopped(self, pid):
        process = subprocess.run(['ps', '-o', 'stat=', '-p', pid], capture_output=True, text=True)
        self.assertEqual(process.stderr, '')
        self.assertTrue(not process.stdout.strip() or process.stdout.strip().startswith('Z'),
                        'child is still running: ' + pid)

    def fake_render_guard(self):
        self.fake('jev-guard', "import sys\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('--out')+1]).write_text('ledger')\n")

    def finding(self, **overrides):
        return dict({'severity': '中', 'source': 'codex-general', 'file': '日本語.py',
                     'line': 3, 'problem': '不備'}, **overrides)

    def add(self, **overrides):
        source = self.root / 'input.json'
        self.write_json(source, self.finding(**overrides))
        return self.call('ledger', self.run, 'add', '--input', source)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, env=self.env,
                              capture_output=True, check=True).stdout

    def repository(self, commit=True):
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.repo / 'a.txt').write_text('original\n')
        if commit:
            self.git('add', '.')
            self.git('commit', '-m', 'initial')
        marker = self.repo / '.fixture-init'
        marker.touch()
        self.initialize()
        marker.unlink()
        return self.iter

    def use_copy_mode(self, paths):
        run = self.read_json(self.run / 'run.json')
        run.update(git=False, mode='paths', paths=paths)
        self.write_json(self.run / 'run.json', run)

    def initialize(self, *options, code=0):
        (self.run / 'run.json').unlink()
        (self.run / 'findings.json').unlink()
        return self.call('init', self.run, '--cwd', self.repo, *options, code=code)

    def changed_repository(self, *options):
        self.repository()
        (self.repo / 'a.txt').write_text('change\n')
        return self.initialize(*options)

    def ja_initialize(self, *options):
        return self.initialize('--ja-review', '--ja-skill', self.ja_skill(), '--roles', '', *options)

    def review_plan(self, jobs=None):
        plan = self.call('prompts', self.iter, '--roles', '')
        plan['reviewers'] = jobs or [self.job('claude', 'claude-general')]
        self.write_json(self.iter / 'prompts.json', plan)
        return self.iter

    def job(self, cli, source, timeout=2):
        prompt = self.iter / (source + '.prompt.md')
        prompt.parent.mkdir(exist_ok=True)
        prompt.write_text('review')
        return {'cli': cli, 'source': source, 'model': 'test', 'effort': 'medium',
                'prompt': str(prompt), 'output': str(prompt.parent / (source + '.md')),
                'timeout': timeout}

    def ready_diff(self):
        self.repository()
        self.call('snapshot', self.iter)
        (self.repo / 'a.txt').write_text('fixed\n')
        self.call('fixdiff', self.iter)

    def verdict(self, **overrides):
        return dict({'verdict': 'needed', 'verdictId': 'v1', 'reason': 'new_risk',
                     'findings': [{'id': 'F1', 'resolved': True, 'rationale': 'resolved'}],
                     'newRisk': {'value': True, 'rationale': 'risk'},
                     'sections': [{'path': 'a.txt', 'newRiskProbability': .5,
                                   'newRiskRationale': 'section risk'}]}, **overrides)

    def fake_gate(self, value, code=0):
        self.fake('jev-guard', 'import sys\nprint(' + repr(json.dumps(value)) + ')\nsys.exit(' + str(code) + ')\n')

    def ja_skill(self):
        skill = self.root / 'ja-skill'
        (skill / 'references').mkdir(parents=True)
        (skill / 'SKILL.md').write_text('ja skill')
        (skill / 'references/prompt.md').write_text('校閲して\n')
        self.fake('agy', "print('指摘なし')\n")
        return skill

    def collection_fixture(self, text, patch=None):
        directory = self.run / 'iter-2'
        self.call('prompts', directory, '--roles', '')
        self.fake('claude', 'print(' + repr(text) + ')')
        self.call('review', directory)
        if patch is not None:
            previous = self.run / 'iter-1'
            (previous / 'fix.diff').write_text(patch)
            (previous / 'snapshot.mode').write_text('copy')
            self.write_json(previous / 'fixdiff.json', {'mode': 'copy', 'empty': not patch,
                'diffHash': mr.digest(patch.encode()), 'state': {'files': {}, 'diffHash': mr.digest(patch.encode())},
                'afterCheck': False, 'changed': False})
        return self.call('collect', directory)

    def report(self, line, problem='不備', path='a.py'):
        return f'### 指摘1（重要度: 低）\n- ファイル: {path}:{line}\n- 問題: {problem}\n- 修正案: 直す\n'

    def test_fake_replaces_links_and_files_without_writing_link_targets(self):
        target = self.root / 'real-cli'
        target.write_bytes(b'original executable\n')
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                destination = self.root / ('missing-cli' if dangling else 'real-cli')
                cli = self.bin / 'replacement'
                cli.unlink(missing_ok=True)
                cli.symlink_to(destination)
                self.fake('replacement', "print('first')\n")
                self.assertFalse(cli.is_symlink())
                self.fake('replacement', "print('second')\n")
                self.assertIn("print('second')", cli.read_text(encoding='utf-8'))
                self.assertEqual(target.read_bytes(), b'original executable\n')
                if dangling:
                    self.assertFalse(destination.exists())

    def test_add_and_fix_preserve_schema_and_iteration(self):
        self.fake_render_guard()
        result = self.add()
        self.assertEqual(result['ids'], ['F1'])
        self.call('ledger', self.run, 'fix', 'F1', '--iteration', '1', '--summary', '検証を追加')
        finding = self.read_json(self.run / 'findings.json')['findings'][0]
        self.assertEqual(finding['status'], 'fixed')
        self.assertEqual(finding['fixedIteration'], 1)
        self.assertEqual(finding['fixSummary'], '検証を追加')
        self.assertNotIn('reason', finding)

    def test_add_line_unknown_and_repeated_input(self):
        self.fake_render_guard()
        self.add(line=None)
        self.add(line=None)
        items = self.read_json(self.run / 'findings.json')['findings']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['line'], 1)
        self.assertEqual(items[0]['problem'], '［行なし］不備')

    def test_snapshot_includes_existing_changes_and_is_immutable(self):
        self.repository()
        (self.repo / 'a.txt').write_text('before delegation\n')
        (self.repo / '未追跡.txt').write_text('untracked\n')
        index = self.git('ls-files', '--stage')
        result = self.call('snapshot', self.iter)
        self.assertEqual(result['mode'], 'tree')
        tree = (self.iter / 'snapshot.tree').read_bytes()
        self.assertEqual(self.git('cat-file', '-p', tree.decode().strip() + ':未追跡.txt'), b'untracked\n')
        (self.repo / 'a.txt').write_text('after delegation\n')
        self.call('snapshot', self.iter, code=1)
        self.assertEqual(self.last_stderr.strip(), 'snapshot already saved')
        self.assertEqual((self.iter / 'snapshot.tree').read_bytes(), tree)
        self.assertEqual(self.git('ls-files', '--stage'), index)

    def test_snapshot_without_head(self):
        self.repository(commit=False)
        self.call('snapshot', self.iter)
        tree = (self.iter / 'snapshot.tree').read_text(encoding='utf-8').strip()
        self.assertEqual(self.git('cat-file', '-p', tree + ':a.txt'), b'original\n')

    def test_copy_snapshot_limits_files(self):
        (self.repo / 'a.txt').write_text('a\n')
        (self.repo / 'outside.txt').write_text('outside\n')
        self.initialize('--path', 'a.txt')
        self.call('snapshot', self.iter)
        self.assertEqual((self.iter / 'snapshot.mode').read_text(encoding='utf-8').strip(), 'copy')
        self.assertEqual((self.iter / 'snapshot.files').read_bytes(), b'a.txt\0')
        self.assertFalse((self.iter / 'snapshot/outside.txt').exists())

    def test_fixdiff_excludes_original_review_changes_and_checks_once(self):
        self.repository()
        (self.repo / 'a.txt').write_text('review change\n')
        self.call('snapshot', self.iter)
        (self.repo / 'a.txt').write_text('fix\n')
        result = self.call('fixdiff', self.iter)
        self.assertFalse(result['empty'])
        patch = (self.iter / 'fix.diff').read_text(encoding='utf-8')
        self.assertIn('-review change', patch)
        self.assertNotIn('-original', patch)
        before = (self.iter / 'snapshot.tree').read_bytes()
        (self.repo / 'a.txt').write_text('formatter\n')
        result = self.call('fixdiff', self.iter, '--after-check')
        self.assertTrue(result['changed'])
        self.assertIn('+formatter', (self.iter / 'fix.diff').read_text(encoding='utf-8'))
        self.assertEqual((self.iter / 'snapshot.tree').read_bytes(), before)
        self.call('fixdiff', self.iter, '--after-check', code=1)
        self.assertIn('after-check already completed', self.last_stderr)

    def test_empty_tree_diff_and_invalid_mode(self):
        self.repository()
        self.call('snapshot', self.iter)
        self.assertTrue(self.call('fixdiff', self.iter)['empty'])
        (self.iter / 'snapshot.mode').write_text('invalid')
        self.call('fixdiff', self.iter, code=1)
        self.assertIn('invalid snapshot.mode', self.last_stderr)
        self.assertFalse((self.iter / 'fixdiff.json').exists())

    def test_copy_empty_deletion_detected_after_check(self):
        self.repository()
        (self.repo / 'empty').touch()
        self.use_copy_mode(['empty'])
        self.call('snapshot', self.iter)
        self.call('fixdiff', self.iter)
        (self.repo / 'empty').unlink()
        result = self.call('fixdiff', self.iter, '--after-check')
        self.assertTrue(result['empty'])
        self.assertTrue(result['changed'])

    def test_init_uncommitted_and_prompts_read_canonical_text(self):
        self.changed_repository('--roles', 'test-quality', '--exclude', 'ja-review')
        self.call('prompts', self.iter)
        plan = self.read_json(self.iter / 'prompts.json')
        self.assertEqual(len(plan['reviewers']), 3)
        general = (self.iter / 'claude.prompt.md').read_text(encoding='utf-8')
        self.assertEqual(general, (self.iter / 'role-general.prompt.md').read_text(encoding='utf-8'))
        self.assertIn('+change', general)
        self.assertIn('### 指摘N（重要度: 高/中/低）', general)
        self.assertIn('あなたはテスト品質', (self.iter / 'role-test-quality.prompt.md').read_text(encoding='utf-8'))

    def test_init_no_git_requires_explicit_target(self):
        self.initialize(code=1)
        self.assertEqual(self.last_stderr.strip(), 'non-git target requires --path')

    def test_init_large_diff_preserved(self):
        self.repository()
        (self.repo / 'a.txt').write_text('日本語\n' * 40000)
        self.initialize('--diff-budget', '10')
        self.call('prompts', self.iter)
        prompt = (self.iter / 'claude.prompt.md').read_text(encoding='utf-8')
        self.assertIn(str(self.iter / 'target.diff'), prompt)
        self.assertGreater((self.iter / 'target.diff').stat().st_size, mr.DIFF_BUDGET)
        self.assertEqual((self.iter / 'target.diff').read_text(encoding='utf-8').splitlines().count('+日本語'), 40000)

    def test_codex_profile_exclusion_and_no_ja_skill(self):
        self.repository()
        self.ja_skill()
        (self.repo / 'a.txt').write_text('日本語\n')
        self.initialize('--profile', 'codex', '--roles', 'bug-hunt', '--exclude', 'claude',
                        '--ja-review', '--ja-skill', str(self.root / 'missing'))
        self.call('prompts', self.iter)
        items = self.read_json(self.iter / 'prompts.json')['reviewers']
        self.assertEqual([i['source'] for i in items], ['codex-general', 'codex-bug-hunt'])
        self.assertEqual([i['effort'] for i in items], ['medium', 'high'])
        run = self.read_json(self.run / 'run.json')
        run['jaSkill'] = str(self.root / 'ja-skill')
        self.write_json(self.run / 'run.json', run)
        (self.bin / 'agy').unlink()
        self.assertFalse(any(j['cli'] == 'agy' for j in self.call('prompts', self.iter)['reviewers']))

    def test_review_retry_and_collect_multiple_locations(self):
        self.review_plan()
        self.fake('claude', """from pathlib import Path
p=Path('count'); n=int(p.read_text(encoding='utf-8'))+1 if p.exists() else 1; p.write_text(str(n))
print('挨拶' if n==1 else '### 指摘1（重要度: 低）\\n- ファイル: 日本語.py:3（他: 日本語.py:9）\\n- 問題: 不備\\n- 修正案: 修正')
""")
        result = self.call('review', self.iter)
        self.assertEqual(len(result['reviewers'][0]['attempts']), 2)
        collected = self.call('collect', self.iter)
        self.assertEqual(len(collected['findings']), 1)
        self.assertIsNone(collected['findings'][0]['nearPreviousFix'])
        self.assertEqual(len(collected['findings'][0]['locations']), 2)

    def test_review_timeout_and_missing_are_absent(self):
        self.review_plan([self.job('claude', 'claude-general', .05), self.job('not-installed', 'missing')])
        self.fake('claude', "import os,time\nfrom pathlib import Path\nPath('started-'+str(os.getpid())).touch()\ntime.sleep(60)\n")
        self.review_after_start()
        result = self.read_json(self.iter / 'review.json')
        self.assertTrue(all(i['status'] == 'absent' for i in result['reviewers']))
        self.assertEqual(len(result['reviewers'][0]['attempts']), 2)
        self.assertEqual(result['reviewers'][1]['reason'], 'not installed')
        self.assertEqual(result['reviewers'][1]['attempts'], [])
        self.call('collect', self.iter, code=1)
        self.assertIn('no successful reviews', self.last_stderr)

    def test_collect_no_findings_with_previous_diff(self):
        result = self.collection_fixture('問題なし', '--- a/a.py\n+++ b/a.py\n@@ -10 +10 @@\n-old\n+new\n')
        self.assertEqual(result['findings'], [])

    def test_task_generation_and_rules_stale_body(self):
        self.ready_diff()
        task = self.root / 'task.md'
        task.write_text('直す')
        self.call('task', self.iter, '--input', task)
        self.call('fixdiff', self.iter)
        self.fake('jev-guard', "import sys\nprint('candidate')\nsys.exit(10)\n")
        result = self.call('rules', self.iter, code=10)
        self.assertEqual(result['status'], 'candidates')
        (self.iter / 'fix-task.md').write_text('古い本文')
        result = self.call('rules', self.iter)
        self.assertEqual(result['reason'], 'stale delegation task')
        self.call('task', self.iter, '--input', self.root / 'missing', code=1)
        self.assertIn('missing', self.last_stderr)
        self.assertEqual(self.read_json(self.run / 'run.json')['taskSequence'], 2)
        self.call('fixdiff', self.iter)
        self.assertIn('task.json', self.call('rules', self.iter)['reason'])
        task.write_text(' \n')
        self.call('task', self.iter, '--input', task, code=1)
        self.assertIn('empty delegation task', self.last_stderr)
        self.assertFalse((self.iter / 'task.json').exists())

    def test_gate_eligibility_and_api_key_wait(self):
        self.ready_diff()
        self.fake_render_guard()
        self.add(file='a.txt')
        self.call('ledger', self.run, 'fix', 'F1', '--iteration', 1, '--summary', 'fixed')
        self.fake_gate(self.verdict())
        result = self.call('gate', self.iter)
        self.assertTrue(result['overruleEligible'])
        self.assertEqual(len(result['grounds']), 2)
        self.assertEqual([g['kind'] for g in result['grounds']], ['newRisk', 'section'])
        self.fake_gate(self.verdict(reason='fix_not_resolved'))
        self.assertTrue(self.call('gate', self.iter)['overruleEligible'])
        self.fake_gate(self.verdict(reason='no_fixed_findings'))
        self.assertFalse(self.call('gate', self.iter)['overruleEligible'])
        self.fake_gate({}, code=2)
        result = self.call('gate', self.iter, code=2)
        self.assertEqual(result['status'], 'awaiting_instruction')
        self.assertEqual(result['next'], 'awaiting_instruction')
        self.assertEqual(result['reason'], 'api key missing')

    def test_gate_invalid_json_and_tampered_diff(self):
        self.ready_diff()
        self.fake_gate({'verdict': 'unknown'})
        result = self.call('gate', self.iter)
        self.assertEqual(result['reason'], 'invalid gate verdict or ID')
        self.assertEqual(result['next'], 'rereview')
        (self.iter / 'snapshot.mode').write_text('copy')
        result = self.call('gate', self.iter)
        self.assertEqual(result['reason'], 'diff mode changed')
        self.assertEqual(result['next'], 'abort')
        (self.iter / 'snapshot.mode').write_text('tree')
        tree = (self.iter / 'after.tree').read_bytes()
        (self.iter / 'after.tree').write_text('changed')
        result = self.call('gate', self.iter)
        self.assertEqual(result['reason'], 'after tree changed')
        self.assertEqual(result['next'], 'abort')
        (self.iter / 'after.tree').write_bytes(tree)
        (self.iter / 'fix.diff').write_text('tampered')
        result = self.call('gate', self.iter)
        self.assertEqual(result['reason'], 'diff changed after generation')
        self.assertEqual(result['next'], 'abort')

    def test_ledger_reject_hold_update_and_reason_constraints(self):
        self.add()
        self.add(problem='別の指摘')
        original = (self.run / 'findings.json').read_bytes()
        self.call('ledger', self.run, 'fix', 'F1', 'F99', '--iteration', 1, '--summary', 'fix', code=1)
        self.assertIn('unknown finding ID', self.last_stderr)
        self.assertEqual((self.run / 'findings.json').read_bytes(), original)
        result = self.call('ledger', self.run, 'fix', 'F1', 'F2', '--iteration', 1, '--summary', 'fix')
        self.assertEqual(result['ids'], ['F1', 'F2'])
        self.assertTrue(all(i['status'] == 'fixed' for i in self.read_json(self.run / 'findings.json')['findings']))
        self.call('ledger', self.run, 'reject', 'F1', '--reason', '規約どおり')
        self.assertIn('規約どおり', (self.run / 'ledger.md').read_text(encoding='utf-8'))
        self.call('ledger', self.run, 'hold', 'F1', '--reason', '確認待ち')
        self.call('ledger', self.run, 'fix', 'F1', '--iteration', 1, '--summary', '直した')
        item = self.read_json(self.run / 'findings.json')['findings'][0]
        self.assertNotIn('reason', item)
        patch = self.root / 'patch.json'
        self.write_json(patch, {'file': '別.py', 'line': None, 'problem': '全体'})
        self.call('ledger', self.run, 'update', 'F1', '--input', patch)
        item = self.read_json(self.run / 'findings.json')['findings'][0]
        self.assertEqual((item['line'], item['problem']), (1, '［行なし］全体'))
        self.write_json(patch, {'problem': '更新した本文'})
        self.call('ledger', self.run, 'update', 'F1', '--input', patch)
        item = self.read_json(self.run / 'findings.json')['findings'][0]
        self.assertEqual((item['line'], item['problem']), (1, '［行なし］更新した本文'))
        for action in ('reject', 'hold'):
            with self.subTest(action=action):
                self.call('ledger', self.run, action, 'F1', '--reason', '確認')
                item = self.read_json(self.run / 'findings.json')['findings'][0]
                self.assertNotIn('fixSummary', item)
                self.assertNotIn('fixedIteration', item)
                self.call('ledger', self.run, 'fix', 'F1', '--iteration', 1, '--summary', '直した')
        original = (self.run / 'findings.json').read_bytes()
        for argv, reason in [(('reject', 'F1', '--reason', ''), 'reason required'),
                             (('hold', 'F1'), 'reason required'),
                             (('fix', 'F1', '--iteration', 2, '--summary', 'x'), 'fix iteration must be current'),
                             (('fix', 'F1', '--iteration', 1, '--summary', ''), 'fix summary required'),
                             (('fix', 'F99', '--iteration', 1, '--summary', 'x'), 'unknown finding ID')]:
            with self.subTest(argv=argv):
                self.call('ledger', self.run, *argv, code=1)
                self.assertIn(reason, self.last_stderr)
                self.assertEqual((self.run / 'findings.json').read_bytes(), original)
        for line in (0, -1, True, 1.5, '3', [], {}):
            with self.subTest(line=line):
                self.write_json(patch, {'line': line})
                self.call('ledger', self.run, 'update', 'F1', '--input', patch, code=1)
                self.assertIn('line must be a positive integer', self.last_stderr)
                self.assertEqual((self.run / 'findings.json').read_bytes(), original)
        for changes, reason in [({'severity': 'high'}, 'invalid severity or status'),
                                ({'file': ''}, 'empty finding string'),
                                ({'source': ''}, 'empty finding string')]:
            with self.subTest(changes=changes):
                self.write_json(patch, changes)
                self.call('ledger', self.run, 'update', 'F1', '--input', patch, code=1)
                self.assertIn(reason, self.last_stderr)
                self.assertEqual((self.run / 'findings.json').read_bytes(), original)

    def test_ledger_invalid_json_unknown_fields_status_and_types(self):
        target = self.run / 'findings.json'
        valid = target.read_bytes()
        for broken in ['{', '{"reviewId":"r","reviewId":"s","iteration":1,"findings":[]}']:
            with self.subTest(broken=broken):
                target.write_text(broken)
                try:
                    self.call('ledger', self.run, 'render', code=1)
                    self.assertIn('duplicate JSON key' if 'reviewId' in broken else 'Expecting property name',
                                  self.last_stderr)
                    self.assertEqual(target.read_text(encoding='utf-8'), broken)
                finally:
                    target.write_bytes(valid)
        for changes, reason in [({'unknown': 1}, 'unknown finding fields'),
                                ({'status': 'held'}, 'invalid severity or status'),
                                ({'line': True}, 'line must be a positive integer'),
                                ({'line': 0}, 'line must be a positive integer'),
                                ({'line': -1}, 'line must be a positive integer'),
                                ({'line': '3'}, 'line must be a positive integer'),
                                ({'line': 1.5}, 'line must be a positive integer'),
                                ({'source': ''}, 'empty finding string'),
                                ({'reason': None}, 'invalid reason'),
                                ({'fixSummary': 1}, 'invalid fixSummary'),
                                ({'severity': 'high'}, 'invalid severity or status'),
                                ({'status': 'unresolved', 'fixedIteration': 1}, 'invalid fixedIteration'),
                                ({'status': 'fixed', 'fixedIteration': 2}, 'invalid fixedIteration')]:
            with self.subTest(changes=changes):
                source = self.root / 'bad.json'
                self.write_json(source, self.finding(**changes))
                self.call('ledger', self.run, 'add', '--input', source, code=1)
                self.assertIn(reason, self.last_stderr)
                self.assertEqual(target.read_bytes(), valid)
        for key in ('problem', 'line'):
            with self.subTest(missing=key):
                item = self.finding()
                del item[key]
                self.write_json(self.root / 'bad.json', item)
                self.call('ledger', self.run, 'add', '--input', self.root / 'bad.json', code=1)
                self.assertIn('line and problem required', self.last_stderr)
                broken = self.read_json(target)
                broken['findings'] = [dict(self.finding(), id='F1')]
                del broken['findings'][0][key]
                self.write_json(target, broken)
                try:
                    self.call('ledger', self.run, 'render', code=1)
                    self.assertIn('missing finding fields', self.last_stderr)
                finally:
                    target.write_bytes(valid)

    def test_duplicate_id_and_same_location_different_problem(self):
        self.add(id='F1')
        self.add(problem='別の不備')
        self.assertEqual(len(self.read_json(self.run / 'findings.json')['findings']), 2)
        source = self.root / 'duplicate.json'
        self.write_json(source, self.finding(id='F1'))
        self.call('ledger', self.run, 'add', '--input', source, code=1)
        self.assertIn('duplicate finding ID', self.last_stderr)

    def test_concurrent_mutation_refused(self):
        with (self.run / '.mr.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.call('ledger', self.run, 'render', code=1)
            self.assertIn('another command is updating this review', self.last_stderr)

    def test_ledger_failed_atomic_replacement_keeps_original(self):
        self.add()
        target = self.run / 'findings.json'
        original = target.read_bytes()
        self.fail_replacement(target, 'ledger', self.run, 'fix', 'F1', '--iteration', 1, '--summary', 'fix')
        self.assertEqual(target.read_bytes(), original)
        source = self.root / 'input.json'
        self.write_json(source, self.finding(problem='次の指摘'))
        self.fail_replacement(self.run / 'run.json', 'ledger', self.run, 'add', '--input', source)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.call('ledger', self.run, 'add', '--input', source)['ids'], ['F2'])
        self.assertEqual(self.call('ledger', self.run, 'add', '--input', source)['ids'], ['F2'])
        original = target.read_bytes()
        self.fail_replacement(self.run / 'run.json', 'prompts', self.run / 'iter-2')
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse((self.run / 'iter-2/prompts.json').exists())
        self.write_json(source, self.finding(problem='割り込み中の追加'))
        save = mr.save_json
        def interrupted_save(path, value):
            if path == self.run / 'run.json':
                raise KeyboardInterrupt
            return save(path, value)
        for argv in [('ledger', self.run, 'add', '--input', source),
                     ('prompts', self.run / 'iter-2')]:
            with self.subTest(argv=argv):
                saved = [(self.run / name).read_bytes() for name in ('run.json', 'findings.json')]
                with patch.object(sys, 'argv', ['mr', *map(str, argv)]), \
                        patch.dict(os.environ, self.env), patch.object(mr, 'save_json', interrupted_save), \
                        patch.object(mr.signal, 'signal'), \
                        redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(mr.main(), 1)
                self.assertIn('interrupted', stderr.getvalue())
                self.assertEqual([(self.run / name).read_bytes() for name in ('run.json', 'findings.json')], saved)

    def test_snapshot_index_flags_ignored_and_untracked(self):
        self.repository()
        (self.repo / '.gitignore').write_text('*.ignored\n')
        (self.repo / 'tracked.ignored').write_text('old\n')
        self.git('add', '-f', 'tracked.ignored')
        self.git('commit', '-m', 'ignored tracked')
        self.git('update-index', '--assume-unchanged', 'a.txt')
        self.git('update-index', '--skip-worktree', 'tracked.ignored')
        index_path = Path(self.git('rev-parse', '--git-path', 'index').decode().strip())
        index_path = self.repo / index_path
        index = index_path.read_bytes()
        (self.repo / 'tracked.ignored').write_text('tracked change\n')
        (self.repo / 'a.txt').write_text('flagged change\n')
        (self.repo / 'outside.ignored').write_text('ignored\n')
        (self.repo / 'new.txt').write_text('new\n')
        self.call('snapshot', self.iter)
        tree = (self.iter / 'snapshot.tree').read_text(encoding='utf-8').strip()
        self.assertEqual(self.git('cat-file', '-p', tree + ':a.txt'), b'flagged change\n')
        self.assertEqual(self.git('cat-file', '-p', tree + ':tracked.ignored'), b'tracked change\n')
        self.assertIn(b'new.txt', self.git('ls-tree', '-r', tree))
        self.assertNotIn(b'outside.ignored', self.git('ls-tree', '-r', tree))
        self.assertEqual(index_path.read_bytes(), index)

    def test_worktree_snapshot_and_clean_filter(self):
        self.repository()
        self.git('config', 'filter.upper.clean', 'tr a-z A-Z')
        self.git('config', 'filter.upper.smudge', 'cat')
        (self.repo / '.gitattributes').write_text('a.txt filter=upper\n')
        self.call('snapshot', self.iter)
        tree = (self.iter / 'snapshot.tree').read_text(encoding='utf-8').strip()
        self.assertEqual(self.git('cat-file', '-p', tree + ':a.txt'), b'ORIGINAL\n')
        checkout = self.root / 'worktree'
        self.git('worktree', 'add', '-b', 'work', str(checkout))
        run = self.read_json(self.run / 'run.json')
        run['cwd'] = str(checkout)
        self.write_json(self.run / 'run.json', run)
        second = self.run / 'iter-2'
        second.mkdir()
        self.call('snapshot', second)
        self.assertTrue((second / 'snapshot.tree').is_file())

    def test_nested_git_falls_back_to_copy_and_failure_is_not_saved(self):
        self.repository()
        (self.repo / 'a.txt').write_text('review change\n')
        nested = self.repo / 'nested'
        nested.mkdir()
        self.git('init', str(nested))
        result = self.call('snapshot', self.iter)
        self.assertEqual(result['mode'], 'copy')
        self.assertIn('git', result['treeError'])
        self.assertTrue((self.iter / 'snapshot/a.txt').is_file())

    def test_copy_destination_collision_failure_is_explicit(self):
        self.repository()
        unreadable = self.repo / 'private'
        unreadable.write_text('secret')
        self.fail_tree_generation()
        (self.iter / 'snapshot').write_text('collision')
        self.call('snapshot', self.iter, code=1)
        self.assertIn('File exists', self.last_stderr)
        self.assertFalse((self.iter / 'snapshot.mode').exists())

    def test_unusual_paths_and_tree_binary_crlf_no_final_newline(self):
        self.repository()
        names = ['日本語 空白.txt', "quote'\"$.txt", '改\n行.txt', '-leading.txt']
        for name in names:
            (self.repo / name).write_bytes(b'a\r\nb')
        (self.repo / 'binary').write_bytes(b'a\0b')
        (self.repo / 'link').symlink_to('a.txt')
        self.call('snapshot', self.iter)
        for name in names:
            (self.repo / name).write_bytes(b'new\r\nvalue')
        (self.repo / 'binary').write_bytes(b'c\0d')
        (self.repo / 'link').unlink()
        (self.repo / 'link').symlink_to('binary')
        self.call('fixdiff', self.iter)
        patch = (self.iter / 'fix.diff').read_bytes()
        self.assertIn(b'No newline at end of file', patch)
        self.assertIn(b'Binary files', patch)
        report = ''.join(self.report(1, path=json.dumps(name, ensure_ascii=False)) for name in names)
        findings = self.collection_fixture(report, patch.decode('utf-8'))['findings']
        self.assertEqual({finding['file'] for finding in findings}, set(names))
        self.assertTrue(all(finding['nearPreviousFix'] for finding in findings))
        self.assertIn(b'+binary', patch)

    def test_fixdiff_failed_tree_generation_invalidates_old_output(self):
        self.ready_diff()
        (self.repo / 'bad').write_text('x')
        self.fail_tree_generation()
        self.call('fixdiff', self.iter, code=1)
        self.assertIn('injected tree failure', self.last_stderr)
        self.assertFalse((self.iter / 'after.tree').exists())
        self.assertFalse((self.iter / 'fixdiff.json').exists())

    def test_copy_diff_binary_symlink_and_missing_manifest(self):
        self.repository()
        self.use_copy_mode(['a.txt', 'binary', 'link'])
        (self.repo / 'binary').write_bytes(b'a\0b')
        (self.repo / 'link').symlink_to('a.txt')
        self.call('snapshot', self.iter)
        self.assertTrue((self.iter / 'snapshot/link').is_symlink())
        (self.repo / 'binary').write_bytes(b'c\0d')
        (self.repo / 'a.txt').write_text('fixed\n')
        (self.repo / 'link').unlink()
        (self.repo / 'link').write_text('a.txt\n')
        self.call('fixdiff', self.iter)
        text = (self.iter / 'fix.diff').read_text(encoding='utf-8')
        self.assertIn('Binary files', text)
        self.assertIn('old mode 120000\nnew mode 100644', text)
        result = self.collection_fixture(self.report(1, path='a.txt') + self.report(1, path='binary'), text)
        self.assertIs(result['findings'][0]['nearPreviousFix'], True)
        self.assertIsNone(result['findings'][1]['nearPreviousFix'])
        (self.repo / 'binary').write_bytes(b'e\0f')
        result = self.call('fixdiff', self.iter, '--after-check')
        self.assertTrue(result['changed'])
        self.assertEqual((self.iter / 'fix.diff').read_text(encoding='utf-8'), text)
        second = self.run / 'iter-2'
        self.call('snapshot', second)
        (self.repo / 'link').unlink()
        (self.repo / 'link').symlink_to('new-target')
        self.call('fixdiff', second)
        text = (second / 'fix.diff').read_text()
        self.assertIn('old mode 100644\nnew mode 120000', text)
        self.assertIn('+new-target', text)
        (second / 'snapshot.files').unlink()
        self.call('fixdiff', second, code=1)
        self.assertIn('snapshot.files', self.last_stderr)

    def test_copy_diff_missing_external_diff_is_error(self):
        self.repository()
        self.use_copy_mode(['a.txt'])
        self.call('snapshot', self.iter)
        (self.bin / 'diff').unlink()
        self.call('fixdiff', self.iter, code=1)
        self.assertIn("'diff'", self.last_stderr)

    def test_after_check_no_change_and_missing_baseline(self):
        self.repository()
        self.call('snapshot', self.iter)
        self.call('fixdiff', self.iter, '--after-check', code=1)
        self.assertIn('after-check requires one fresh fixdiff', self.last_stderr)
        self.call('fixdiff', self.iter)
        self.assertFalse(self.call('fixdiff', self.iter, '--after-check')['changed'])

    def test_init_clean_repository_empty_scope_and_missing_branch(self):
        self.repository()
        self.initialize(code=1)
        self.assertIn('empty review scope', self.last_stderr)
        self.assertFalse((self.run / 'run.json').exists())
        self.assertFalse((self.run / 'findings.json').exists())
        self.git('branch', '-m', 'topic')
        self.call('init', self.run, '--cwd', self.repo, code=1)
        self.assertEqual(self.last_stderr.strip(), 'default branch not found; specify target')

    def test_init_default_branch_priority_and_base_rereview_cumulative_scope(self):
        self.repository()
        self.git('checkout', '-b', 'master')
        (self.repo / 'a.txt').write_text('master change\n')
        self.git('commit', '-am', 'master')
        main = self.git('rev-parse', 'main').decode().strip()
        self.git('checkout', '-b', 'topic', 'main')
        (self.repo / 'a.txt').write_text('committed base change\n')
        self.git('commit', '-am', 'topic')
        self.initialize('--roles', '', '--ja-review')
        self.assertEqual(self.read_json(self.run / 'run.json')['mode'], 'base')
        self.assertEqual(self.read_json(self.run / 'run.json')['base'], main)
        self.assertEqual(self.read_json(self.run / 'run.json')['baseSummary'], 'base: topic, main; 1 files')
        self.call('prompts', self.iter, '--roles', 'bug-hunt', '--no-ja-review')
        (self.repo / 'additional.txt').write_text('new fix\n')
        second = self.run / 'iter-2'
        self.call('prompts', second)
        saved = self.read_json(self.run / 'run.json')
        self.assertEqual(saved['roles'], ['bug-hunt'])
        self.assertTrue(saved['jaReview'])
        text = (second / 'claude.prompt.md').read_text(encoding='utf-8')
        self.assertIn('+committed base change', text)
        self.assertIn('new fix', text)
        self.assertIn('additional.txt', text)
        self.assertEqual(self.read_json(self.run / 'findings.json')['iteration'], 2)
        for command in ('snapshot', 'task', 'fixdiff', 'review', 'rules', 'gate', 'collect'):
            for name in ('iter-0', 'iter-9', 'typo'):
                with self.subTest(command=command, name=name):
                    directory = self.run / name
                    options = ['--input', self.root / 'missing'] if command == 'task' else []
                    self.call(command, directory, *options, code=1)
                    self.assertIn('iteration must be iter-1 through', self.last_stderr)
                    self.assertFalse(directory.exists())
        self.call('prompts', self.run / 'iter-4', code=1)
        self.assertIn('invalid iteration transition', self.last_stderr)
        self.call('prompts', self.iter, code=1)
        self.assertIn('invalid iteration transition', self.last_stderr)
        for number in (0, mr.MAX_ITERATIONS + 1):
            with self.subTest(number=number):
                directory = self.run / f'iter-{number}'
                self.call('prompts', directory, code=1)
                self.assertIn('iteration must be iter-1 through', self.last_stderr)
                self.assertFalse((directory / 'prompts.json').exists())
        self.assertEqual(self.read_json(self.run / 'findings.json')['iteration'], 2)

    def test_init_origin_head_precedes_main(self):
        self.repository()
        self.git('checkout', '-b', 'topic')
        (self.repo / 'a.txt').write_text('topic\n')
        self.git('commit', '-am', 'topic')
        origin = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('update-ref', 'refs/remotes/origin/trunk', 'HEAD')
        (self.repo / 'a.txt').write_text('next topic\n')
        self.git('commit', '-am', 'next')
        self.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/trunk')
        run = self.initialize()
        self.assertEqual(run['base'], origin)
        self.assertIn('topic, refs/remotes/origin/trunk;', run['baseSummary'])
        self.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/missing')
        self.assertEqual(self.initialize()['base'], self.git('rev-parse', 'main').decode().strip())
        remote = os.fsdecode(b'refs/remotes/origin/trunk-\xff')
        branch = os.fsdecode(b'topic-\xfe')
        self.git('update-ref', remote, origin)
        self.git('symbolic-ref', 'refs/remotes/origin/HEAD', remote)
        self.git('branch', '-m', branch)
        run = self.initialize()
        self.assertEqual(run['base'], origin)
        self.assertIn(branch + ', ' + remote + ';', run['baseSummary'])

    def test_init_explicit_paths_non_git_and_no_head_prompts(self):
        (self.repo / '日本語.txt').write_text('本文\n')
        self.initialize('--path', '日本語.txt', '--roles', '')
        self.call('prompts', self.iter)
        self.assertIn('日本語.txt', (self.iter / 'claude.prompt.md').read_text(encoding='utf-8'))
        self.assertFalse(self.read_json(self.run / 'run.json')['git'])
        self.add(file='日本語.txt')
        self.call('ledger', self.run, 'reject', 'F1', '--reason', '検討済み')
        second = self.run / 'iter-2'
        self.call('prompts', second)
        text = (second / 'claude.prompt.md').read_text(encoding='utf-8')
        self.assertIn('検討済み', text)
        self.assertNotIn('=== FILE:', text)
        self.assertNotIn('本文', text)

    def test_init_subdirectory_and_literal_pathspecs(self):
        self.repository()
        sub = self.repo / 'sub'
        sub.mkdir()
        for name in ('file[1]*?.txt', 'file1extra.txt'):
            (sub / name).write_text('original\n')
        self.git('add', '.')
        self.git('commit', '-m', 'paths')
        target = self.root / 'sub-review'
        run = self.call('init', target, '--cwd', sub, '--path', 'file[1]*?.txt', '--roles', '')
        self.assertEqual(run['cwd'], str(self.repo))
        self.assertEqual(run['paths'], ['sub/file[1]*?.txt'])
        first = target / 'iter-1'
        self.call('prompts', first)
        self.call('snapshot', first)
        (sub / 'file[1]*?.txt').write_text('literal fix\n')
        self.call('fixdiff', first)
        second = target / 'iter-2'
        patch = (first / 'fix.diff').read_bytes()
        (first / 'fix.diff').write_text('tampered')
        self.call('prompts', second, code=1)
        self.assertIn('diff changed after generation', self.last_stderr)
        (first / 'fix.diff').write_bytes(patch)
        self.call('prompts', second)
        text = (second / 'claude.prompt.md').read_text()
        self.assertIn('+literal fix', text)
        self.assertNotIn('file1extra.txt', text)

    def test_non_utf8_names_preserve_paths_in_prompts_ledger_and_copy_diff(self):
        self.repository()
        renamed = self.root / os.fsdecode(b'review-\xfe')
        self.run.rename(renamed)
        self.run = renamed
        self.iter = renamed / 'iter-1'
        name = os.fsdecode(b'bad-\xff.txt')
        (self.repo / name).write_text('日本語\n')
        self.initialize('--path', name, '--ja-review', '--ja-skill', self.ja_skill(),
                        '--roles', '', '--diff-budget', '1')
        self.call('prompts', self.iter)
        text = (self.iter / 'claude.prompt.md').read_text()
        self.assertIn('\\udcff', text)
        self.fake('claude', 'print(' + repr(self.report(1, path=mr.display_path(name))) + ')')
        self.call('review', self.iter)
        self.assertEqual(self.call('collect', self.iter)['findings'][0]['file'], name)
        self.assertIn('\\udcfe', (self.iter / 'collected.md').read_text())
        self.add(file=name)
        self.assertEqual(self.read_json(self.run / 'findings.json')['findings'][0]['file'], name)
        self.use_copy_mode([name])
        self.call('snapshot', self.iter)
        (self.repo / name).write_text('修正\n')
        self.call('fixdiff', self.iter)
        self.assertIn('\\377', (self.iter / 'fix.diff').read_text())
        self.git('add', '.')
        self.git('commit', '-m', 'non-utf8')
        (self.repo / name).write_text('再修正\n')
        run = self.read_json(self.run / 'run.json')
        run.update(git=True, mode='uncommitted', paths=[])
        self.write_json(self.run / 'run.json', run)
        plan = self.call('prompts', self.run / 'iter-2')
        text = (self.run / 'iter-2/claude.prompt.md').read_text()
        self.assertNotIn('\ufffd', text)
        self.assertIn('\\udcfe', text)
        self.assertNotIn('\ufffd', (self.run / 'iter-2/target.diff').read_text())
        self.assertTrue(any(job['cli'] == 'agy' for job in plan['reviewers']))

    def test_committed_gitlink_snapshot_copies_nested_content(self):
        self.repository()
        nested = self.repo / 'nested'
        self.git('init', '-b', 'main', str(nested))
        for args in [('config', 'user.email', 'test@example.invalid'),
                     ('config', 'user.name', 'Test')]:
            self.git('-C', str(nested), *args)
        (nested / 'file').write_text('before\n')
        self.git('-C', str(nested), 'add', '.')
        self.git('-C', str(nested), 'commit', '-m', 'nested')
        self.git('add', 'nested')
        self.git('commit', '-m', 'gitlink')
        (nested / 'file').write_text('inside only\n')
        self.initialize()
        result = self.call('snapshot', self.iter)
        self.assertEqual(result['mode'], 'copy')
        self.assertIn('cannot snapshot gitlink', result['treeError'])
        self.assertEqual((self.iter / 'snapshot/nested/file').read_text(), 'inside only\n')
        (nested / 'file').write_text('fixed inside\n')
        self.assertFalse(self.call('fixdiff', self.iter)['empty'])
        self.assertIn('+fixed inside', (self.iter / 'fix.diff').read_text())
        for mode in ('uncommitted', 'base', 'paths'):
            with self.subTest(mode=mode):
                run = self.read_json(self.run / 'run.json')
                run.update(mode=mode, base=os.fsdecode(self.git('rev-parse', 'HEAD')).strip(),
                           paths=['nested'] if mode == 'paths' else [])
                self.write_json(self.run / 'run.json', run)
                second = self.run / 'iter-2'
                self.call('prompts', second, '--roles', '')
                self.assertIn('+fixed inside', (second / 'claude.prompt.md').read_text())
                self.assertIn('nested/file', self.read_json(second / 'target.json')['files'])
        self.call('snapshot', second)
        self.assertIn(b'nested/file\0', (second / 'snapshot.files').read_bytes())

    def test_no_head_prompts_embed_all_files(self):
        self.repository(commit=False)
        self.initialize('--roles', '')
        self.call('prompts', self.iter)
        self.assertIn('original', (self.iter / 'claude.prompt.md').read_text(encoding='utf-8'))

    def test_init_invalid_configuration_and_roots(self):
        saved = [(self.run / name).read_bytes() for name in ('run.json', 'findings.json')]
        self.call('init', self.run, '--cwd', self.repo, '--path', '.', code=1)
        self.assertIn('review already initialized', self.last_stderr)
        self.assertEqual([(self.run / name).read_bytes() for name in ('run.json', 'findings.json')], saved)
        self.repository()
        (self.repo / 'a.txt').write_text('change\n')
        cases = [
            (('--profile', 'unknown'), 'invalid choice', 2),
            (('--profile',), 'expected one argument', 2),
            (('--roles', 'unknown'), 'unknown or repeated specialist role', 1),
            (('--roles', 'bug-hunt,bug-hunt'), 'unknown or repeated specialist role', 1),
            (('--diff-budget', '0'), 'limits must be positive', 1),
            (('--diff-budget', '-1'), 'limits must be positive', 1),
            (('--timeout', 'inf'), 'limits must be positive', 1),
            (('--ja-timeout', 'inf'), 'limits must be positive', 1),
            (('--timeout', 'nan'), 'limits must be positive', 1),
            (('--timeout', '0'), 'limits must be positive', 1),
            (('--timeout', '-1'), 'limits must be positive', 1),
            (('--ja-timeout', '0'), 'limits must be positive', 1),
            (('--ja-timeout', '-1'), 'limits must be positive', 1),
            (('--roles', 'security,bug-hunt,architecture,performance,test-quality'), 'too many specialist roles', 1),
            (('--only', 'ja-review'), 'all reviewers excluded', 1),
            (('--exclude', 'claude', '--exclude', 'codex', '--exclude', 'ja-review'), 'all reviewers excluded', 1)]
        for options, reason, code in cases:
            with self.subTest(options=options):
                target = self.root / ('invalid-' + str(len(list(self.root.glob('invalid-*')))))
                self.call('init', target, '--cwd', self.repo, *options, code=code)
                self.assertIn(reason, self.last_stderr)
                self.assertFalse((target / 'run.json').exists())
                self.assertFalse((target / 'findings.json').exists())
        optional = ['security', 'bug-hunt', 'architecture', 'performance']
        for roles in (optional, [*mr.DEFAULT_SPECIALISTS, *optional]):
            with self.subTest(roles=roles):
                target = self.root / ('valid-' + str(len(roles)))
                run = self.call('init', target, '--cwd', self.repo, '--roles', ','.join(roles))
                self.assertEqual(run['roles'], roles)
        self.assertEqual(len(optional), mr.MAX_OPTIONAL_SPECIALISTS)
        self.assertEqual(len(optional) + len(mr.DEFAULT_SPECIALISTS), mr.MAX_SPECIALISTS)
        outside = self.root / 'outside'
        outside.mkdir()
        self.call('init', self.root / 'run-outside', '--cwd', self.repo, '--path', outside, code=1)
        self.assertEqual(self.last_stderr.strip(), 'target spans multiple roots')
        nested = self.repo / 'nested'
        nested.mkdir()
        self.git('init', str(nested))
        self.call('init', self.root / 'run-nested', '--cwd', self.repo, '--path', nested, code=1)
        self.assertEqual(self.last_stderr.strip(), 'target spans multiple repositories')
        self.call('init', self.repo / 'bad-run', '--cwd', self.repo, code=1)
        self.assertIn('review directory must be outside target', self.last_stderr)
        self.assertFalse((self.repo / 'bad-run').exists())
        for cwd in (self.root / 'missing-cwd', self.repo / 'a.txt'):
            with self.subTest(cwd=cwd):
                before = (self.run / 'findings.json').read_bytes()
                self.call('init', self.root / 'invalid-cwd', '--cwd', cwd, code=1)
                self.assertIn('cwd must be a directory', self.last_stderr)
                self.assertEqual((self.run / 'findings.json').read_bytes(), before)

    def test_prompts_all_cli_unavailable(self):
        self.changed_repository('--roles', '')
        (self.bin / 'claude').unlink()
        (self.bin / 'codex').unlink()
        self.call('prompts', self.iter, code=1)
        self.assertIn('no installed reviewer selected', self.last_stderr)
        self.assertFalse((self.iter / 'prompts.json').exists())

    def test_rereview_reconsider_and_previous_fix_summaries(self):
        self.changed_repository('--roles', '')
        self.add()
        self.add(problem='直した指摘')
        self.call('ledger', self.run, 'hold', 'F1', '--reason', '保留の理由')
        self.call('ledger', self.run, 'fix', 'F2', '--iteration', 1, '--summary', '検証した修正')
        second = self.run / 'iter-2'
        self.call('prompts', second)
        text = (second / 'claude.prompt.md').read_text(encoding='utf-8')
        self.assertIn('保留の理由', text)
        self.assertIn('検討済みのため再報告不要', text)
        self.call('prompts', second, '--reconsider', 'F1')
        text = (second / 'claude.prompt.md').read_text(encoding='utf-8')
        self.assertNotIn('保留の理由', text)
        self.assertIn('検証した修正', text)
        self.call('prompts', second, '--reconsider', 'F99', code=1)
        self.assertIn('unknown reconsider ID', self.last_stderr)

    def test_huge_diff_threshold_exact_boundary(self):
        self.changed_repository('--roles', '')
        self.call('prompts', self.iter)
        text = (self.iter / 'claude.prompt.md').read_text(encoding='utf-8')
        self.assertNotIn('target.diff', text)
        # Get the exact embedding size without reconstructing the implementation's diff.
        run = self.read_json(self.run / 'run.json')
        run['diffBudget'] = 1
        self.write_json(self.run / 'run.json', run)
        self.call('prompts', self.iter)
        size = (self.iter / 'target.diff').stat().st_size
        for budget, external in [(size, False), (size - 1, True)]:
            with self.subTest(budget=budget):
                run['diffBudget'] = budget
                self.write_json(self.run / 'run.json', run)
                self.call('prompts', self.iter)
                self.assertEqual('差分全文を必ず読む:' in (self.iter / 'claude.prompt.md').read_text(encoding='utf-8'), external)

    def test_ja_batches_large_file_preserve_numbers_and_limits(self):
        self.repository()
        (self.repo / 'ja.txt').write_text(('日本語' * 50 + '\n') * 1000)
        self.ja_initialize()
        self.call('prompts', self.iter)
        jobs = [j for j in self.read_json(self.iter / 'prompts.json')['reviewers'] if j['cli'] == 'agy']
        self.assertGreater(len(jobs), 1)
        texts = [Path(j['prompt']).read_text(encoding='utf-8') for j in jobs]
        self.assertTrue(all(Path(j['prompt']).stat().st_size <= mr.JA_BATCH_LIMIT for j in jobs))
        self.assertIn('1000: ', ''.join(texts))
        self.assertTrue(all(j['timeout'] == mr.JA_TIMEOUT for j in jobs))
        self.assertTrue(all('=== FILE: ja.txt' in text for text in texts))
        self.assertTrue(self.call('review', self.iter)['success'])
        result = self.call('collect', self.iter)
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['absent'], [])

    def test_ja_single_oversized_line_errors_without_truncation(self):
        self.repository()
        (self.repo / 'ja.txt').write_text('語' * 40000)
        self.ja_initialize()
        self.call('prompts', self.iter, code=1)
        self.assertIn('single ja line exceeds batch limit', self.last_stderr)
        self.assertFalse((self.iter / 'prompts.json').exists())

    def test_ja_partial_batch_failure_and_native_format(self):
        self.repository()
        (self.repo / 'ja.txt').write_text(('日本語' * 50 + '\n') * 1000)
        self.ja_initialize()
        self.call('prompts', self.iter)
        parameters = {Path(job['prompt']).read_text(): job['model'] for job in
                      self.read_json(self.iter / 'prompts.json')['reviewers'] if job['cli'] == 'agy'}
        self.fake('agy', 'parameters=' + repr(parameters) + '\n' + """import sys
assert sys.argv[sys.argv.index('--mode')+1] == 'plan'
assert sys.argv[sys.argv.index('--model')+1] == parameters[sys.argv[sys.argv.index('-p')+1]]
assert sys.argv[sys.argv.index('--model')+1] == 'gemini-3.8-flash-high'
if '1000: ' in sys.argv[-1]:
    for severity in ('high', 'medium', 'low'):
        print(f'### {severity} ja.txt:1000\\n- 分類: 冗長\\n- 原文: `原文`\\n- 修正案: `修正`\\n- 理由: 長い')
else:
    sys.exit(1)
""")
        self.call('review', self.iter)
        result = self.call('collect', self.iter)
        self.assertEqual(result['findings'][0]['source'], 'ja-review')
        self.assertEqual([f['severity'] for f in result['findings']], ['高', '中', '低'])
        self.assertNotIn('ja-review', result['absent'])
        self.assertTrue(result['failedBatches'])

    def test_review_nonzero_empty_malformed_and_partial_success(self):
        for body, reason in [("print('問題なし'); raise SystemExit(1)", 'exit 1'),
                             ('', 'no valid report'),
                             ("print('### 指摘1（重要度: 高）')", 'incomplete common report'),
                             ("print('### 指摘1（重要度: 最高）')", 'malformed report heading')]:
            with self.subTest(body=body):
                self.review_plan([self.job('claude', 'claude-general'), self.job('codex', 'codex-general')])
                self.fake('claude', body)
                self.call('review', self.iter)
                results = self.read_json(self.iter / 'review.json')['reviewers']
                self.assertEqual(results[0]['status'], 'absent')
                self.assertEqual(len(results[0]['attempts']), 2)
                self.assertEqual([a['reason'] for a in results[0]['attempts']], [reason] * mr.MAX_ATTEMPTS)
                self.assertEqual(results[1]['status'], 'success')

    def test_review_parallel_and_readonly_arguments_stdin_cwd(self):
        self.review_plan([self.job('claude', 'claude-general', 20), self.job('codex', 'codex-general', 20)])
        self.fake('claude', """import sys,time
from pathlib import Path
Path('claude-started').touch()
end=time.monotonic()+15
while not Path('codex-started').exists() and time.monotonic()<end: time.sleep(.01)
assert Path('codex-started').exists()
assert sys.stdin.read()=='review'
assert sys.argv[sys.argv.index('--tools')+1]=='Read,Glob,Grep'
print('問題なし')
""")
        self.fake('codex', """import sys,time
from pathlib import Path
Path('codex-started').touch()
end=time.monotonic()+15
while not Path('claude-started').exists() and time.monotonic()<end: time.sleep(.01)
assert Path('claude-started').exists()
assert sys.stdin.read()=='review'
assert sys.argv[sys.argv.index('-s')+1]=='read-only'
assert '--skip-git-repo-check' in sys.argv
Path(sys.argv[sys.argv.index('-o')+1]).write_text('問題なし')
""")
        self.call('review', self.iter)
        self.assertTrue(all(j['status'] == 'success' for j in self.read_json(self.iter / 'review.json')['reviewers']))

    def test_timeout_kills_children_and_does_not_reuse_stale_output(self):
        self.review_plan([self.job('claude', 'claude-general', .1)])
        stale = self.iter / 'claude-general.md'
        stale.write_text('問題なし')
        child = """import os,time
from pathlib import Path
Path('started-'+str(os.getppid())).write_text(str(os.getpid()))
time.sleep(60)
Path('leaked').touch()
"""
        self.fake('claude', 'import subprocess,sys\n'
                  + 'child=subprocess.Popen([sys.executable,"-c",' + repr(child) + '])\nchild.wait()\n')
        result = self.review_after_start()
        self.assertEqual(result['reviewers'][0]['status'], 'absent')
        self.assertEqual(len(result['reviewers'][0]['attempts']), mr.MAX_ATTEMPTS)
        self.assertTrue(all(a['reason'] == 'timeout' for a in result['reviewers'][0]['attempts']))
        markers = list(self.repo.glob('started-*'))
        self.assertEqual(len(markers), 2)
        for marker in markers:
            with self.subTest(marker=marker.name):
                pid = marker.read_text(encoding='utf-8')
                self.assert_process_stopped(pid)
        self.assertFalse((self.repo / 'leaked').exists())
        self.assertFalse(stale.exists())

    def test_review_interruption_does_not_publish_completion(self):
        self.review_plan([self.job('claude', 'claude-general', 5)])
        self.fake('claude', "from pathlib import Path\nimport os,time\nPath('started').write_text(str(os.getpid()))\ntime.sleep(5)\n")
        process = subprocess.Popen([sys.executable, str(CLI), 'review', str(self.iter)],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            end = time.monotonic() + 3
            while not (self.repo / 'started').exists() and time.monotonic() < end:
                time.sleep(.01)
            self.assertTrue((self.repo / 'started').exists())
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=3)
            self.assertEqual(process.returncode, 1)
            self.assertFalse((self.iter / 'review.json').exists())
            pid = (self.repo / 'started').read_text()
            self.assert_process_stopped(pid)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_collect_plus_minus_five_six_context_and_deletion_boundary(self):
        patch = '--- a/a.py\n+++ b/a.py\n@@ -7,7 +7,7 @@\n c\n c\n c\n-old\n+new\n c\n c\n c\n'
        for line, near in [(4, False), (5, True), (15, True), (16, False)]:
            with self.subTest(line=line):
                result = self.collection_fixture(self.report(line), patch)
                self.assertIs(result['findings'][0]['nearPreviousFix'], near)
                text = (self.run / 'iter-2/collected.md').read_text()
                self.assertIn('[近傍]' if near else '[近傍外]', text)
        deletion = '--- a/a.py\n+++ b/a.py\n@@ -8,2 +8,0 @@\n-old\n-old2\n'
        self.assertTrue(self.collection_fixture(self.report(13), deletion)['findings'][0]['nearPreviousFix'])
        self.assertFalse(self.collection_fixture(self.report(14), deletion)['findings'][0]['nearPreviousFix'])

    def test_collect_unknown_binary_unknown_line_sorting_and_duplicates(self):
        text = self.report('-', path='z.py') + self.report(2, path='z.py') + self.report(1, path='a.py')
        patch = 'diff --git a/a.py b/a.py\nBinary files a/a.py and b/a.py differ\n'
        result = self.collection_fixture(text, patch)
        self.assertEqual([f['line'] for f in result['findings']], [1, 2, None])
        self.assertIsNone(result['findings'][0]['nearPreviousFix'])
        self.assertIsNone(result['findings'][2]['nearPreviousFix'])
        self.assertIs(result['findings'][1]['nearPreviousFix'], False)
        result = self.collection_fixture(self.report(2) * 2, '')
        self.assertEqual(len(result['findings']), 2)

    def test_collect_first_line_multiple_hunks_and_multiple_locations(self):
        patch = '--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+first\n@@ -30 +31 @@\n-old\n+new\n'
        text = self.report(20).replace('a.py:20', 'a.py:20（他: a.py:36, a.py:100）')
        result = self.collection_fixture(text, patch)['findings'][0]
        self.assertEqual([p['nearPreviousFix'] for p in result['locations']], [False, True, False])
        self.assertTrue(result['nearPreviousFix'])
        for separator in (',', '、'):
            with self.subTest(separator=separator):
                text = self.report(20).replace('a.py:20', 'a.py:20（他: a.py:36' + separator + 'a.py:100）')
                parsed = self.collection_fixture(text, patch)['findings'][0]
                self.assertEqual([p['line'] for p in parsed['locations']], [20, 36, 100])
        text = self.report(20).replace('a.py:20', 'a.py:20（他: "a,b.py":3、"a、b.py":4）')
        parsed = self.collection_fixture(text, patch)['findings'][0]
        self.assertEqual([p['file'] for p in parsed['locations']], ['a.py', 'a,b.py', 'a、b.py'])
        text = self.report('-').replace('a.py:-', 'a.py:-（他: a.py:100）')
        result = self.collection_fixture(text, patch)['findings'][0]
        self.assertEqual([p['nearPreviousFix'] for p in result['locations']], [None, False])
        self.assertIsNone(result['nearPreviousFix'])
        self.assertIn('[判定不能]', (self.run / 'iter-2/collected.md').read_text())
        self.assertTrue(self.collection_fixture(self.report(6), patch)['findings'][0]['nearPreviousFix'])

    def test_collect_preserves_mixed_report_and_refuses_unfinished_review(self):
        self.collection_fixture('問題なし')
        self.fake('claude', 'print(' + repr(self.report(2) + '\n### 指摘2（重要度: 高）\n- 問題: 欠落\n') + ')')
        self.call('review', self.run / 'iter-2')
        result = self.call('collect', self.run / 'iter-2')
        self.assertEqual(len(result['findings']), 2)
        self.assertIn('parseError', result['findings'][0])
        (self.run / 'iter-2' / 'review.json').unlink()
        self.call('collect', self.run / 'iter-2', code=1)
        self.assertIn('review.json', self.last_stderr)

    def test_collect_crlf_preface_and_unknown_report_line(self):
        result = self.collection_fixture(('前置き\n' + self.report('-', path='日本語.txt')).replace('\n', '\r\n'))
        self.assertIsNone(result['findings'][0]['line'])
        self.assertIn('日本語.txt:-', (self.run / 'iter-2' / 'collected.md').read_text(encoding='utf-8'))

    def test_canonical_format_and_roles_missing_fail_closed(self):
        isolated = self.root / 'skill'
        shutil.copytree(CLI.parent.parent, isolated,
                        ignore=shutil.ignore_patterns('.git', '__pycache__'))
        self.changed_repository('--roles', '')
        for filename, reason in [('references/roles.md', 'invalid role catalog'),
                                 ('SKILL.md', 'common report heading missing')]:
            with self.subTest(filename=filename):
                path = isolated / filename
                original = path.read_text(encoding='utf-8')
                path.write_text('no matching headings')
                try:
                    result = subprocess.run([sys.executable, str(isolated / 'scripts/mr.py'),
                                             'prompts', str(self.iter)], env=self.env, capture_output=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(reason.encode(), result.stderr)
                    self.assertFalse((self.iter / 'prompts.json').exists())
                finally:
                    path.write_text(original)
        path = isolated / 'references/roles.md'
        original = path.read_text()
        for slug, code in [('extra', 0), ('general', 1)]:
            with self.subTest(slug=slug):
                path.write_text(original + f'\n## 14. 追加（role-{slug}.md）\n追加の視点\n')
                try:
                    result = subprocess.run([sys.executable, str(isolated / 'scripts/mr.py'),
                                             'prompts', str(self.iter)], env=self.env, capture_output=True)
                    self.assertEqual(result.returncode, code, result.stderr)
                    if code:
                        self.assertIn(b'invalid role catalog', result.stderr)
                finally:
                    path.write_text(original)

    def test_rules_missing_guard_copy_and_exit_codes(self):
        self.ready_diff()
        self.assertEqual(self.call('rules', self.iter)['reason'], 'jev-guard not installed')
        task = self.root / 'task'
        task.write_text('fix')
        self.call('task', self.iter, '--input', task)
        self.call('fixdiff', self.iter)
        for code, status in [(0, 'clear'), (10, 'candidates'), (1, 'error'), (2, 'error')]:
            with self.subTest(code=code):
                self.fake('jev-guard', f"import sys\nprint('output')\nprint('err',file=sys.stderr)\nsys.exit({code})\n")
                result = self.call('rules', self.iter, code=10 if code == 10 else 0)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['stdout'], 'output\n')
                self.assertEqual(result['stderr'], 'err\n')
        valid = (self.iter / 'task.json').read_bytes()
        for entry in ([], {'sequence': 1}):
            with self.subTest(entry=entry):
                try:
                    self.write_json(self.iter / 'task.json', entry)
                    self.assertEqual(self.call('rules', self.iter)['reason'], 'invalid delegation task')
                finally:
                    (self.iter / 'task.json').write_bytes(valid)
        (self.iter / 'snapshot.mode').write_text('copy')
        state = self.read_json(self.iter / 'fixdiff.json')
        state['mode'] = 'copy'
        state['state'] = {'files': {}, 'diffHash': state['diffHash']}
        self.write_json(self.iter / 'fixdiff.json', state)
        self.assertEqual(self.call('rules', self.iter)['reason'], 'copy mode')

    def test_rules_task_number_mismatch_and_new_delegation(self):
        self.ready_diff()
        task = self.root / 'task'
        task.write_text('first')
        self.call('task', self.iter, '--input', task)
        task.write_text('second')
        self.call('task', self.iter, '--input', task)
        self.assertFalse((self.iter / 'fixdiff.json').exists())
        self.assertEqual(self.call('gate', self.iter)['next'], 'abort')
        self.assertEqual(self.call('rules', self.iter)['status'], 'not_performed')
        self.call('fixdiff', self.iter)
        self.fake('jev-guard', "print('clear')")
        entry = self.read_json(self.iter / 'task.json')
        self.assertEqual(entry['sequence'], 2)
        entry['sequence'] = 1
        self.write_json(self.iter / 'task.json', entry)
        self.assertEqual(self.call('rules', self.iter)['reason'], 'stale delegation task')

    def test_copy_diff_detects_executable_bit_without_content_change(self):
        path = self.repo / 'script'
        path.write_text('echo test\n')
        path.chmod(0o644)
        self.initialize('--path', 'script')
        self.call('snapshot', self.iter)
        self.assertTrue(self.call('fixdiff', self.iter)['empty'])
        path.chmod(0o755)
        result = self.call('fixdiff', self.iter)
        self.assertFalse(result['empty'])
        self.assertTrue(result['changed'])
        self.assertIn('old mode 100644\nnew mode 100755', (self.iter / 'fix.diff').read_text())
        self.assertFalse(self.call('fixdiff', self.iter)['changed'])
        path.chmod(0o644)
        result = self.call('fixdiff', self.iter, '--after-check')
        self.assertTrue(result['changed'])
        self.assertTrue(result['empty'])

    def test_invalid_run_prompts_and_review_states_are_diagnosed(self):
        self.review_plan()
        self.call('review', self.iter)
        for path, command in [(self.run / 'run.json', ('task', self.iter, '--input', self.root / 'task')),
                              (self.iter / 'prompts.json', ('review', self.iter)),
                              (self.iter / 'review.json', ('collect', self.iter))]:
            valid = self.read_json(path)
            cases = [[], {}]
            cases.extend({key: value for key, value in valid.items() if key != missing}
                         for missing in (('tasks', 'baseSummary', 'taskSequence') if path.name == 'run.json'
                                         else ('reviewers',)))
            if path.name == 'run.json':
                cases.extend(dict(valid, **{key: []}) for key in ('tasks', 'baseSummary', 'profile'))
                cases.append(dict(valid, tasks={'iter-1': []}))
            else:
                cases.extend([dict(valid, reviewers={}), dict(valid, reviewers=[{}]),
                              dict(valid, reviewers=[[]])])
                for key in ('cli', 'source', 'output', 'timeout'):
                    job = dict(valid['reviewers'][0])
                    del job[key]
                    cases.append(dict(valid, reviewers=[job]))
                if path.name == 'review.json':
                    cases.append(dict(valid, reviewers=[dict(valid['reviewers'][0], cli='unknown')]))
            for state in cases:
                with self.subTest(path=path.name, state=state):
                    try:
                        self.write_json(path, state)
                        self.call(*command, code=1)
                        self.assertIn('invalid ' + ('run state' if path.name == 'run.json' else path.name), self.last_stderr)
                    finally:
                        self.write_json(path, valid)

    def test_review_stops_children_after_leader_exits_successfully(self):
        self.review_plan([self.job('claude', 'claude-general')])
        self.fake('claude', "import subprocess,sys\nfrom pathlib import Path\n"
                  "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
                  "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                  "Path('child').write_text(str(child.pid))\nprint('問題なし')\n")
        result = self.call('review', self.iter)
        self.assertEqual(result['reviewers'][0]['status'], 'success')
        self.assert_process_stopped((self.repo / 'child').read_text())

    def test_gate_schema_type_matrix_and_no_automatic_overrule(self):
        self.ready_diff()
        for changes, reason in [
                ({'verdict': 'other'}, 'invalid gate verdict or ID'),
                ({'verdictId': ''}, 'invalid gate verdict or ID'),
                ({'findings': {}}, 'invalid gate arrays or newRisk'),
                ({'newRisk': []}, 'invalid gate arrays or newRisk'),
                ({'sections': {}}, 'invalid gate arrays or newRisk'),
                ({'reason': None}, 'missing gate reason'),
                ({'findings': [{'id': 'F1', 'resolved': 'true'}]}, 'invalid finding verdict'),
                ({'findings': [{'resolved': True}]}, 'invalid finding verdict'),
                ({'findings': [{'id': 'F1'}]}, 'invalid finding verdict'),
                ({'newRisk': {'value': 1}}, 'invalid newRisk'),
                ({'newRisk': {}}, 'invalid newRisk'),
                ({'findings': [{'id': 'F1', 'resolved': True, 'rationale': 1}]}, 'invalid finding rationale'),
                ({'newRisk': {'value': False, 'rationale': 1}}, 'invalid newRisk rationale'),
                ({'sections': [{}]}, 'invalid section'),
                ({'sections': [{'path': ''}]}, 'invalid section'),
                ({'sections': [{'path': 'a', 'error': 1}]}, 'invalid section error'),
                ({'sections': [{'path': 'a', 'newRiskRationale': 1}]}, 'invalid section rationale'),
                ({'sections': [{'path': 'a', 'newRiskProbability': True}]}, 'invalid section risk probability'),
                ({'sections': [{'path': 'a', 'newRiskProbability': 1.1}]}, 'invalid section risk probability'),
                ({'verdict': 'skippable', 'reviewPrompt': 1}, 'missing gate reviewPrompt')]:
            with self.subTest(changes=changes):
                self.fake_gate(self.verdict(**changes))
                result = self.call('gate', self.iter)
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['next'], 'rereview')
        for changes in [{'findings': [{'id': 'F1', 'resolved': None}]},
                        {'sections': [{'path': 'a', 'error': ''}]},
                        {'reason': 'no_fixed_findings'}]:
            with self.subTest(changes=changes):
                self.fake_gate(self.verdict(**changes))
                result = self.call('gate', self.iter)
                self.assertFalse(result['overruleEligible'])
                self.assertEqual(result['next'], 'caller_decision')
                self.assertEqual([g['kind'] for g in result['grounds']],
                                 ['newRisk'] if 'sections' in changes else ['newRisk', 'section'])
        self.fake_gate(self.verdict(newRisk={'value': None}, findings=[{'id': 'F1', 'resolved': None}]))
        result = self.call('gate', self.iter)
        self.assertEqual([g['kind'] for g in result['grounds']], ['section'])
        self.fake_gate(self.verdict(verdict='skippable', reviewPrompt='確認する'))
        self.assertFalse(self.call('gate', self.iter)['overruleEligible'])

    def test_gate_missing_fields_bad_json_nonzero_and_missing_guard(self):
        self.ready_diff()
        result = self.call('gate', self.iter)
        self.assertEqual(result['reason'], 'jev-guard not installed')
        self.assertEqual(result['next'], 'rereview')
        for key in ['verdict', 'verdictId', 'findings', 'newRisk', 'sections', 'reason']:
            with self.subTest(key=key):
                value = self.verdict()
                del value[key]
                self.fake_gate(value)
                reason = 'invalid gate verdict or ID' if key in ('verdict', 'verdictId') else \
                    'missing gate reason' if key == 'reason' else 'invalid gate arrays or newRisk'
                result = self.call('gate', self.iter)
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['next'], 'rereview')
        self.fake('jev-guard', "print('{bad json')")
        result = self.call('gate', self.iter)
        self.assertIn('Expecting property name', result['reason'])
        self.assertEqual(result['next'], 'rereview')
        self.fake('jev-guard', "import sys\nprint('output')\nprint('err', file=sys.stderr)\nsys.exit(1)")
        result = self.call('gate', self.iter)
        self.assertEqual(result['exitCode'], 1)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['next'], 'rereview')
        self.assertEqual(result['stdout'], 'output\n')
        self.assertEqual(result['stderr'], 'err\n')

        self.fake('jev-guard', "import sys\nsys.stdout.buffer.write(b'\\xff')")
        result = self.call('gate', self.iter)
        self.assertEqual(result['status'], 'not_performed')
        self.assertEqual(result['next'], 'rereview')

    def test_gate_empty_tree_copy_limit_order_and_incomplete_ledger(self):
        self.repository()
        self.call('snapshot', self.iter)
        self.call('fixdiff', self.iter)
        self.assertEqual(self.call('gate', self.iter)['next'], 'finish')
        self.assertEqual(self.call('rules', self.iter)['reason'], 'empty fix diff')
        (self.repo / 'a.txt').write_text('fix\n')
        self.call('fixdiff', self.iter)
        self.fake_render_guard()
        self.add()
        ledger = self.read_json(self.run / 'findings.json')
        ledger['findings'][0]['status'] = 'fixed'
        self.write_json(self.run / 'findings.json', ledger)
        result = self.call('gate', self.iter)
        self.assertEqual(result['reason'], 'fixed finding needs summary and iteration')
        self.assertEqual(result['next'], 'rereview')
        ledger['iteration'] = mr.MAX_ITERATIONS
        ledger['findings'] = []
        self.write_json(self.run / 'findings.json', ledger)
        self.assertEqual(self.call('gate', self.iter)['next'], 'limit')
        ledger['iteration'] = mr.MAX_ITERATIONS - 1
        self.write_json(self.run / 'findings.json', ledger)
        self.fake_gate(self.verdict())
        self.assertEqual(self.call('gate', self.iter)['next'], 'caller_decision')

    def test_gate_recheck_and_all_grounds_preserved(self):
        self.ready_diff()
        self.fake_gate(self.verdict(findings=[{'id': 'F1', 'resolved': False, 'rationale': 'not fixed'}],
                                    rechecks=[{'findingIds': ['F1'], 'paths': ['a.txt'], 'mode': 'full'},
                                              {'findingIds': ['F2'], 'paths': ['b.txt'], 'mode': 'full'}]))
        result = self.call('gate', self.iter)
        self.assertEqual(len(result['grounds']), 3)
        self.assertEqual([g['kind'] for g in result['grounds']], ['finding', 'newRisk', 'section'])
        self.assertEqual(result['grounds'][0]['rechecks'][0]['findingIds'], ['F1'])
        self.assertEqual(len(result['grounds'][0]['rechecks']), 1)
        self.assertTrue(result['overruleEligible'])

    def test_same_finding_reported_again_after_previous_fix_gets_new_id(self):
        self.add()
        self.call('ledger', self.run, 'fix', 'F1', '--iteration', 1, '--summary', 'fix')
        ledger = self.read_json(self.run / 'findings.json')
        ledger['iteration'] = 2
        self.write_json(self.run / 'findings.json', ledger)
        self.assertEqual(self.add()['ids'], ['F2'])
        self.assertEqual(self.add()['ids'], ['F2'])

    def test_new_ledger_entries_require_status_dependent_fields(self):
        original = (self.run / 'findings.json').read_bytes()
        for changes, reason in [({'status': 'rejected'}, 'rejection/hold reason required'),
                                ({'status': 'on_hold', 'reason': ''}, 'rejection/hold reason required'),
                                ({'status': 'fixed'}, 'fixed finding needs summary and iteration'),
                                ({'status': 'fixed', 'fixSummary': 'x'}, 'fixed finding needs summary and iteration'),
                                ({'status': 'unresolved', 'reason': 'not permitted'}, 'reason is only for rejected/on_hold')]:
            with self.subTest(changes=changes):
                source = self.root / 'bad-entry.json'
                self.write_json(source, self.finding(**changes))
                self.call('ledger', self.run, 'add', '--input', source, code=1)
                self.assertIn(reason, self.last_stderr)
                self.assertEqual((self.run / 'findings.json').read_bytes(), original)

    def test_staged_unstaged_renamed_deleted_files_and_empty_tree_id(self):
        self.repository()
        (self.repo / 'delete.txt').write_text('delete\n')
        self.git('add', '.')
        self.git('commit', '-m', 'more')
        self.git('mv', 'a.txt', 'renamed.txt')
        (self.repo / 'renamed.txt').write_text('staged\n')
        self.git('add', 'renamed.txt')
        (self.repo / 'renamed.txt').write_text('unstaged\n')
        (self.repo / 'delete.txt').unlink()
        self.call('snapshot', self.iter)
        tree = (self.iter / 'snapshot.tree').read_text(encoding='utf-8').strip()
        self.assertEqual(self.git('cat-file', '-p', tree + ':renamed.txt'), b'unstaged\n')
        self.assertNotIn(b'delete.txt', self.git('ls-tree', '-r', tree))
        (self.iter / 'snapshot.tree').write_text('')
        self.call('fixdiff', self.iter, code=1)
        self.assertIn('invalid snapshot tree', self.last_stderr)
        self.assertFalse((self.iter / 'fixdiff.json').exists())

    def test_copy_path_traversal_and_snapshot_output_collision(self):
        self.repository()
        ledger = self.read_json(self.run / 'findings.json')
        ledger['findings'] = [dict(self.finding(file='../outside',
            problem='不備（他: /absolute:1, a.txt:2, missing-line）'), id='F1', status='unresolved')]
        self.write_json(self.run / 'findings.json', ledger)
        self.use_copy_mode(['a.txt'])
        self.call('snapshot', self.iter)
        excluded = self.read_json(self.iter / 'snapshot.json')['excluded']
        self.assertEqual(len(excluded), 3)
        self.assertEqual(sorted(reason.split(':', 1)[0] for reason in excluded),
                         ['invalid location', 'unsafe relative file', 'unsafe relative file'])
        self.assertEqual((self.iter / 'snapshot.files').read_bytes(), b'a.txt\0')
        self.write_json(self.run / 'findings.json', dict(ledger, findings=[]))
        second = self.run / 'iter-2'
        second.mkdir()
        (second / 'snapshot.files').mkdir()
        self.call('snapshot', second, code=1)
        self.assertIn('snapshot.files', self.last_stderr)
        self.assertFalse((second / 'snapshot.mode').exists())

    def test_copy_outside_new_file_is_not_compared_or_gated(self):
        self.repository()
        self.use_copy_mode(['a.txt'])
        self.call('snapshot', self.iter)
        (self.repo / 'outside.txt').write_text('outside\n')
        self.call('fixdiff', self.iter)
        self.assertNotIn('outside.txt', (self.iter / 'fix.diff').read_text(encoding='utf-8'))
        self.assertEqual(self.call('gate', self.iter)['reason'], 'copy mode')
        self.assertEqual(self.call('rules', self.iter)['reason'], 'copy mode')

    def test_collect_git_c_quoted_octal_path(self):
        # A real git patch supplies the path spelling; do not mirror an escaping implementation.
        self.repository()
        name = '日本語\t".txt'
        (self.repo / name).write_text('before\n')
        self.git('add', '.')
        self.git('commit', '-m', 'path')
        (self.repo / name).write_text('after\n')
        patch = self.git('diff').decode()
        result = self.collection_fixture(self.report(1, path=name), patch)
        self.assertTrue(result['findings'][0]['nearPreviousFix'])

    def test_copy_diff_quotes_tabs_and_newlines_for_proximity(self):
        self.repository()
        names = ['tab\tname.txt', 'line\nbreak.txt']
        binary = 'binary and b/part'
        (self.repo / 'binary and b').mkdir()
        (self.repo / binary).write_bytes(b'a\0b')
        for name in names:
            (self.repo / name).write_text('before\n')
        self.use_copy_mode([*names, binary])
        self.call('snapshot', self.iter)
        for name in names:
            (self.repo / name).write_text('after\n')
        (self.repo / binary).write_bytes(b'c\0d')
        self.call('fixdiff', self.iter)
        patch = (self.iter / 'fix.diff').read_text()
        result = self.collection_fixture(''.join(self.report(1, path=json.dumps(name)) for name in [*names, binary]), patch)
        self.assertEqual({f['file'] for f in result['findings']}, {*names, binary})
        for finding in result['findings']:
            self.assertIs(finding['nearPreviousFix'], None if finding['file'] == binary else True)

    def test_unrepresentable_multiline_report_path_is_explicit_failure(self):
        self.collection_fixture('問題なし')
        self.replace_review_output(self.run / 'iter-2', self.report(1, path='line\nbreak.txt'))
        self.call('collect', self.run / 'iter-2', code=1)
        self.assertIn('invalid location', self.last_stderr)

    def test_ja_omits_binary_generated_lock_and_handles_changed_line_ranges(self):
        self.repository()
        for name in ('ja.txt', 'pnpm-lock.yaml', 'binary', 'dist/generated.txt'):
            path = self.repo / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes('本文\n'.encode() + (b'\0' if name == 'binary' else b''))
        self.git('add', '.')
        self.git('commit', '-m', 'ja baseline')
        for name in ('ja.txt', 'pnpm-lock.yaml', 'binary', 'dist/generated.txt'):
            path = self.repo / name
            path.write_bytes('改稿\n'.encode() + (b'\0' if name == 'binary' else b''))
        self.ja_initialize()
        self.call('prompts', self.iter)
        text = (self.iter / 'ja-review/batch-1.txt').read_text(encoding='utf-8')
        self.assertIn('=== 指摘対象の行: 1-1', text)
        self.assertNotIn('pnpm-lock.yaml', text)
        self.assertNotIn('generated.txt', text)
        self.assertNotIn('=== FILE: binary', text)

    def test_render_external_error_is_reported_after_durable_ledger_update(self):
        self.fake('jev-guard', "import sys\nprint('render failure', file=sys.stderr)\nsys.exit(1)")
        source = self.root / 'input.json'
        self.write_json(source, self.finding())
        self.call('ledger', self.run, 'add', '--input', source, code=1)
        self.assertIn('render failure', self.last_stderr)
        self.assertEqual(len(self.read_json(self.run / 'findings.json')['findings']), 1)
        (self.bin / 'jev-guard').unlink()
        self.call('ledger', self.run, 'render')
        self.assertIn('不備', (self.run / 'ledger.md').read_text(encoding='utf-8'))

    def test_ledger_empty_input_and_top_level_schema(self):
        source = self.root / 'empty.json'
        self.write_json(source, [])
        self.assertEqual(self.call('ledger', self.run, 'add', '--input', source)['ids'], [])
        valid = self.read_json(self.run / 'findings.json')
        for value in [[], dict(valid, iteration=True), dict(valid, iteration=0),
                      dict(valid, findings={}), dict(valid, extra=1), dict(valid, reviewId='')]:
            with self.subTest(value=value):
                self.write_json(self.run / 'findings.json', value)
                self.call('ledger', self.run, 'render', code=1)
                reason = 'invalid ledger fields' if isinstance(value, list) or 'extra' in value else \
                    'findings must be an array' if isinstance(value['findings'], dict) else 'invalid ledger identity'
                self.assertIn(reason, self.last_stderr)
        self.write_json(self.run / 'findings.json', valid)
        source.write_text('')
        self.call('ledger', self.run, 'add', '--input', source, code=1)
        self.assertIn('Expecting value', self.last_stderr)

    def test_snapshot_mode_record_failure_and_read_failure(self):
        self.repository()
        self.fail_replacement(self.iter / 'snapshot.mode', 'snapshot', self.iter)
        self.assertTrue((self.iter / 'snapshot.tree').is_file())
        self.assertFalse((self.iter / 'snapshot.json').exists())
        self.assertFalse((self.iter / 'snapshot').exists())
        self.assertFalse((self.iter / 'snapshot.mode').exists())
        self.call('snapshot', self.iter)
        (self.iter / 'snapshot.mode').unlink()
        (self.iter / 'snapshot.mode').mkdir()
        self.call('fixdiff', self.iter, code=1)
        self.assertIn('Is a directory', self.last_stderr)

    def test_missing_specialist_cli_is_not_scheduled(self):
        self.changed_repository('--roles', 'bug-hunt')
        (self.bin / 'claude').unlink()
        self.call('prompts', self.iter)
        self.assertEqual([j['source'] for j in self.read_json(self.iter / 'prompts.json')['reviewers']],
                         ['claude-general', 'codex-general'])

    def test_unreadable_task_and_compare_output_save_failure(self):
        self.ready_diff()
        body = self.root / 'body'
        body.write_text('fix')
        self.call('task', self.iter, '--input', body)
        self.call('fixdiff', self.iter)
        (self.iter / 'fix-task.md').unlink()
        (self.iter / 'fix-task.md').mkdir()
        self.fake('jev-guard', "print('clear')")
        result = self.call('rules', self.iter)
        self.assertEqual(result['status'], 'not_performed')
        self.assertIn('Is a directory', result['reason'])
        (self.iter / 'fix.diff').unlink()
        (self.iter / 'fix.diff').mkdir()
        self.call('fixdiff', self.iter, '--after-check', code=1)
        self.assertIn('Is a directory', self.last_stderr)
        self.assertFalse((self.iter / 'fixdiff.json').exists())

    def test_rereview_exclusion_survives_and_ja_requires_explicit_decision(self):
        self.repository()
        (self.repo / 'a.txt').write_text('日本語\n')
        self.initialize('--exclude', 'codex', '--roles', '', '--ja-skill', self.ja_skill())
        self.call('prompts', self.iter)
        self.assertEqual([j['cli'] for j in self.read_json(self.iter / 'prompts.json')['reviewers']], ['claude'])
        second = self.run / 'iter-2'
        self.call('prompts', second, '--ja-review')
        self.assertEqual([j['cli'] for j in self.read_json(second / 'prompts.json')['reviewers']], ['claude', 'agy'])
        self.assertFalse(self.read_json(self.run / 'run.json')['jaReview'])
        self.call('prompts', self.run / 'iter-3')
        self.assertEqual([j['cli'] for j in self.read_json(self.run / 'iter-3/prompts.json')['reviewers']], ['claude'])

    def test_full_lifecycle_with_fake_guard(self):
        self.repository()
        (self.repo / 'a.txt').write_text('initial review change\n')
        self.initialize('--roles', '', '--base-summary', '今回の変更')
        self.assertEqual(self.read_json(self.run / 'run.json')['diffBudget'], mr.DIFF_BUDGET)
        self.call('snapshot', self.iter)
        (self.repo / 'a.txt').write_text('fixed review change\n')
        self.call('fixdiff', self.iter)
        self.fake('jev-guard', """import json,sys
from pathlib import Path
if sys.argv[1:3]==['ledger','render']:
    ledger=json.loads(Path(sys.argv[sys.argv.index('--findings')+1]).read_text(encoding='utf-8'))
    Path(sys.argv[sys.argv.index('--out')+1]).write_text(str(len(ledger['findings'])))
else:
    assert sys.argv[sys.argv.index('--provider')+1]=='claude'
    assert sys.argv[sys.argv.index('--base-summary')+1]=='今回の変更'
    ledger=json.loads(Path(sys.argv[sys.argv.index('--findings')+1]).read_text(encoding='utf-8'))
    assert ledger['findings'][0]['fixedIteration']==1
    patch=Path(sys.argv[sys.argv.index('--diff')+1]).read_text(encoding='utf-8')
    assert '-initial review change' in patch and '-original' not in patch
    print(json.dumps({'verdict':'skippable','verdictId':'e2e','reviewPrompt':'確認',
      'findings':[{'id':'F1','resolved':True}], 'newRisk':{'value':False},'sections':[]}))
""")
        self.add(file='a.txt', line=1)
        self.call('ledger', self.run, 'fix', 'F1', '--iteration', 1, '--summary', 'fix')
        result = self.call('gate', self.iter)
        self.assertEqual(result['verdict']['verdictId'], 'e2e')
        self.assertEqual(result['next'], 'caller_decision')
        self.assertFalse(result['overruleEligible'])

    def test_conflicting_profiles_and_relative_parent_target_are_rejected(self):
        self.repository()
        (self.repo / 'a.txt').write_text('change\n')
        self.call('init', self.root / 'conflict', '--cwd', self.repo,
                  '--profile', 'default', '--profile', 'codex', code=2)
        self.assertIn('conflicting profiles', self.last_stderr)
        run = self.initialize('--path', 'a.txt', '--profile', 'codex', '--profile', 'codex')
        self.assertEqual(run['profile'], 'codex')
        outside = self.root / 'outside'
        outside.write_text('outside')
        self.call('init', self.root / 'parent-target', '--cwd', self.repo,
                  '--path', '../outside', code=1)
        self.assertEqual(self.last_stderr.strip(), 'target spans multiple roots')

    def test_gate_bad_optional_evidence_types_are_reported_not_traceback(self):
        self.ready_diff()
        for changes, reason in [({'rechecks': [None]}, 'invalid recheck evidence'),
                                ({'rechecks': 'bad'}, 'invalid rechecks'),
                                ({'findings': [{'id': 'F1', 'resolved': False, 'confidence': 'high'}]}, 'invalid confidence'),
                                ({'newRisk': {'value': True, 'probability': 'yes'}}, 'invalid probability')]:
            with self.subTest(changes=changes):
                self.fake_gate(self.verdict(**changes))
                result = self.call('gate', self.iter)
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['next'], 'rereview')
        self.fake_gate(self.verdict(findings=[{'id': 'F1', 'resolved': True, 'confidence': None}]))
        self.assertEqual(self.call('gate', self.iter)['status'], 'evaluated')

    def test_ja_all_batches_fail_is_one_absent_reviewer(self):
        self.repository()
        (self.repo / 'ja.txt').write_text('日本語\n')
        self.ja_initialize()
        self.call('prompts', self.iter)
        self.fake('agy', "import sys\nprint('logging before google.Init',file=sys.stderr)\nprint('x'*" +
                  str(mr.STDERR_TAIL_CHARS + 1) + ",file=sys.stderr)\nprint('failure detail',file=sys.stderr)\nsys.exit(1)")
        self.call('review', self.iter)
        result = self.call('collect', self.iter)
        self.assertEqual(result['absent'], ['ja-review'])
        self.assertEqual(len(result['failedBatches']), 1)
        for attempt in result['failedBatches'][0]['attempts']:
            with self.subTest(attempt=attempt['number']):
                self.assertNotIn('logging before google.Init', attempt['stderrTail'])
                self.assertIn('failure detail', attempt['stderrTail'])
                self.assertEqual(len(attempt['stderrTail']), mr.STDERR_TAIL_CHARS)

    def test_new_prompts_invalidate_completed_reviews(self):
        self.changed_repository('--roles', '')
        self.call('prompts', self.iter)
        self.call('review', self.iter)
        self.call('collect', self.iter)
        saved = {path: path.read_bytes() for path in self.iter.rglob('*') if path.is_file()}
        for options, reason in [(('--roles', 'unknown'), 'unknown or repeated specialist role'),
                                (('--reconsider', 'F99'), 'unknown reconsider ID')]:
            with self.subTest(options=options):
                self.call('prompts', self.iter, *options, code=1)
                self.assertIn(reason, self.last_stderr)
                self.assertEqual({path: path.read_bytes() for path in self.iter.rglob('*') if path.is_file()}, saved)
        plan = self.read_json(self.iter / 'prompts.json')
        self.write_json(self.iter / 'prompts.json', dict(plan, reviewers=[]))
        self.call('review', self.iter, code=1)
        self.assertEqual(self.last_stderr.strip(), 'no reviewers')
        for name in ('review.json', 'collected.json', 'collected.md'):
            self.assertEqual((self.iter / name).read_bytes(), saved[self.iter / name])
        self.write_json(self.iter / 'prompts.json', plan)
        review = self.read_json(self.iter / 'review.json')
        self.write_json(self.iter / 'review.json', dict(review, reviewers=[]))
        self.call('collect', self.iter, code=1)
        self.assertEqual(self.last_stderr.strip(), 'no successful reviews')
        for name in ('collected.json', 'collected.md'):
            self.assertEqual((self.iter / name).read_bytes(), saved[self.iter / name])
        self.write_json(self.iter / 'review.json', review)
        self.call('review', self.iter)
        self.assertFalse((self.iter / 'collected.json').exists())
        self.assertFalse((self.iter / 'collected.md').exists())
        self.call('collect', self.iter)
        (self.repo / 'a.txt').write_text('new target\n')
        self.call('prompts', self.iter)
        self.call('collect', self.iter, code=1)
        self.assertIn('review.json', self.last_stderr)
        saved = {path: path.read_bytes() for path in self.iter.rglob('*') if path.is_file()}
        self.call('prompts', self.run / 'iter-2')
        self.call('prompts', self.iter, code=1)
        self.assertIn('invalid iteration transition', self.last_stderr)
        self.assertEqual({path: path.read_bytes() for path in self.iter.rglob('*') if path.is_file()}, saved)

    def test_ja_exact_byte_limit_and_one_byte_over(self):
        self.repository()
        file = self.repo / 'ja.txt'
        file.write_text('語\n')
        self.ja_initialize('--path', 'ja.txt')
        self.call('prompts', self.iter)
        prompt = self.iter / 'ja-review/batch-1.txt'
        overhead = prompt.stat().st_size
        file.write_text('語' + 'x' * (mr.JA_BATCH_LIMIT - overhead) + '\n')
        self.call('prompts', self.iter)
        self.assertEqual(prompt.stat().st_size, mr.JA_BATCH_LIMIT)
        self.assertTrue(self.call('review', self.iter)['success'])
        job = next(j for j in self.read_json(self.iter / 'review.json')['reviewers'] if j['cli'] == 'agy')
        self.assertEqual(job['status'], 'success')
        file.write_text('語' + 'x' * (mr.JA_BATCH_LIMIT + 1 - overhead) + '\n')
        self.call('prompts', self.iter, code=1)
        self.assertIn('single ja line exceeds batch limit', self.last_stderr)
        self.assertFalse((self.iter / 'prompts.json').exists())

    def test_ja_small_files_cross_batch_limit_and_path_scope_uses_all_lines(self):
        self.repository()
        names = ['ja1.txt', 'ja2.txt', 'ja3.txt']
        for name in names:
            (self.repo / name).write_text('語' + 'x' * 40000 + '\n')
        self.ja_initialize('--path', '.')
        jobs = [j for j in self.call('prompts', self.iter)['reviewers'] if j['cli'] == 'agy']
        self.assertEqual(len(jobs), 2)
        texts = [Path(job['prompt']).read_text() for job in jobs]
        for job in jobs:
            with self.subTest(prompt=job['prompt']):
                self.assertLessEqual(Path(job['prompt']).stat().st_size, mr.JA_BATCH_LIMIT)
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(''.join(texts).count('=== FILE: ' + name + '\n=== 指摘対象の行: 全行'), 1)

    def test_untracked_scope_ignores_user_status_configuration(self):
        self.repository()
        self.git('config', 'status.showUntrackedFiles', 'no')
        (self.repo / 'new.txt').write_text('new')
        self.assertEqual(self.initialize()['mode'], 'uncommitted')

    def test_rejected_inside_target_init_creates_nothing(self):
        self.repository()
        target = self.repo / 'review/new'
        self.call('init', target, '--cwd', self.repo, code=1)
        self.assertIn('review directory must be outside target', self.last_stderr)
        self.assertFalse((self.repo / 'review').exists())

    def test_unknown_line_marker_updates_and_fallback_contract(self):
        self.add(line='-', problem='不備\n  次行')
        patch = self.root / 'patch.json'
        self.write_json(patch, {'problem': '［行なし］不備'})
        for number in range(2):
            with self.subTest(number=number):
                self.call('ledger', self.run, 'update', 'F1', '--input', patch)
        self.assertEqual(self.read_json(self.run / 'findings.json')['findings'][0]['problem'], '［行なし］不備')
        self.write_json(patch, {'line': 7})
        self.call('ledger', self.run, 'update', 'F1', '--input', patch)
        self.assertEqual(self.read_json(self.run / 'findings.json')['findings'][0]['problem'], '不備')
        text = (self.run / 'ledger.md').read_text(encoding='utf-8')
        self.assertTrue(text.startswith('# 台帳\n'))
        self.assertIn('ID: F1 | 重要度: 中 | 出所: codex-general | ファイル: 日本語.py:7 | 問題: 不備', text)
        self.assertEqual([line for line in text.split('\n') if line.startswith('## ')],
                         ['## 未解決', '## 修正済み', '## 保留', '## 却下'])
        self.assertEqual(text.count('\nなし'), 3)
        self.write_json(patch, {'problem': '［行なし］実在行'})
        self.call('ledger', self.run, 'update', 'F1', '--input', patch)
        self.write_json(patch, {'severity': '低'})
        self.call('ledger', self.run, 'update', 'F1', '--input', patch)
        self.assertEqual(self.read_json(self.run / 'findings.json')['findings'][0]['line'], 7)
        self.write_json(patch, {'line': '-'})
        self.call('ledger', self.run, 'update', 'F1', '--input', patch)
        item = self.read_json(self.run / 'findings.json')['findings'][0]
        self.assertEqual(item['line'], 1)
        self.assertEqual(item['problem'], '［行なし］実在行')
        original = (self.run / 'findings.json').read_bytes()
        for line in (0, -1, True, 1.5, '3', [], {}):
            with self.subTest(line=line):
                self.write_json(patch, {'line': line})
                self.call('ledger', self.run, 'update', 'F1', '--input', patch, code=1)
                self.assertIn('line must be a positive integer', self.last_stderr)
                self.assertEqual((self.run / 'findings.json').read_bytes(), original)

    def test_repeated_after_check_preserves_completion(self):
        self.ready_diff()
        self.call('fixdiff', self.iter, '--after-check')
        before = (self.iter / 'fixdiff.json').read_bytes()
        self.call('fixdiff', self.iter, '--after-check', code=1)
        self.assertIn('after-check already completed', self.last_stderr)
        self.assertEqual((self.iter / 'fixdiff.json').read_bytes(), before)
        snapshot = (self.iter / 'snapshot.tree').read_bytes()
        task = self.root / 'task'
        task.write_text('再修正')
        self.call('task', self.iter, '--input', task)
        (self.repo / 'a.txt').write_text('second fix\n')
        self.call('fixdiff', self.iter)
        self.call('fixdiff', self.iter, '--after-check')
        self.assertIn('+second fix', (self.iter / 'fix.diff').read_text())
        self.assertEqual((self.iter / 'snapshot.tree').read_bytes(), snapshot)
        before = (self.iter / 'fixdiff.json').read_bytes()
        self.call('fixdiff', self.iter, code=1)
        self.assertIn('after-check already completed', self.last_stderr)
        self.call('fixdiff', self.iter, '--after-check', code=1)
        self.assertEqual((self.iter / 'fixdiff.json').read_bytes(), before)
        self.assertIn('after-check already completed', self.last_stderr)

    def test_copy_links_compare_link_values_without_following(self):
        self.repository()
        target = self.root / 'secret'
        target.write_text('secret before')
        for name, value in [('absolute', str(target)), ('relative', 'a.txt'), ('directory', '.')]:
            (self.repo / name).symlink_to(value)
        self.use_copy_mode(['absolute', 'relative', 'directory'])
        self.call('snapshot', self.iter)
        target.write_text('secret after')
        (self.repo / 'a.txt').write_text('changed target')
        self.assertTrue(self.call('fixdiff', self.iter)['empty'])
        (self.repo / 'absolute').unlink()
        (self.repo / 'absolute').symlink_to('new-target')
        self.assertFalse(self.call('fixdiff', self.iter)['empty'])
        patch = (self.iter / 'fix.diff').read_text(encoding='utf-8')
        self.assertIn('+new-target', patch)
        (self.repo / 'relative').unlink()
        self.call('fixdiff', self.iter)
        patch = (self.iter / 'fix.diff').read_text()
        self.assertIn('diff --git a/relative b/relative\ndeleted file mode 120000\n', patch)
        self.assertNotIn('new mode 100644', patch)
        self.assertNotIn('secret before', patch)
        self.assertNotIn('secret after', patch)

    def test_path_rereview_never_embeds_outside_paths_or_link_content(self):
        self.repository()
        (self.repo / 'inside').mkdir()
        (self.repo / 'inside/a.txt').write_text('指定本文')
        (self.repo / 'private').write_text('OUTSIDE_SECRET')
        outside = self.root / 'secret'
        outside.write_text('LINK_SECRET')
        (self.repo / 'inside/link').symlink_to(outside)
        self.initialize('--path', 'inside', '--roles', '')
        self.call('prompts', self.iter)
        second = self.run / 'iter-2'
        self.call('prompts', second)
        text = (second / 'claude.prompt.md').read_text(encoding='utf-8')
        self.assertIn('指定本文', text)
        self.assertIn('[リンク]', text)
        self.assertNotIn('OUTSIDE_SECRET', text)
        self.assertNotIn('LINK_SECRET', text)

    def test_hunk_body_resembling_headers_keeps_line_numbers(self):
        patch = '--- a/a.py\n+++ b/a.py\n@@ -10,2 +10,2 @@\n--- SQL\n+++ x\n-old\n+new\n'
        result = self.collection_fixture(self.report(16), patch)
        self.assertTrue(result['findings'][0]['nearPreviousFix'])
        self.assertFalse(self.collection_fixture(self.report(17), patch)['findings'][0]['nearPreviousFix'])

    def test_ja_untracked_hunk_text_non_utf8_and_deleted_lines(self):
        self.repository()
        (self.repo / 'a.txt').write_text('日本語\n残す\n')
        self.git('commit', '-am', 'ja')
        (self.repo / 'a.txt').write_text('残す\n')
        (self.repo / 'new.txt').write_text('@@ arbitrary text\n日本語\n- 箇条書き\n')
        (self.repo / 'latin.txt').write_bytes(b'\xe9')
        self.ja_initialize()
        self.call('prompts', self.iter)
        text = (self.iter / 'ja-review/batch-1.txt').read_text(encoding='utf-8')
        self.assertIn('=== FILE: new.txt\n=== 指摘対象の行: 全行', text)
        self.assertNotIn('=== FILE: a.txt', text)
        self.assertNotIn('latin.txt', text)

    def test_ja_generated_exclusions_and_ordinary_lock_substrings(self):
        self.repository()
        names = ['block.py', 'clock.ts', 'deadlock.md', 'a.min.html', 'package-lock.json', 'x.lock', 'build/a.txt']
        for name in names:
            path = self.repo / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('日本語\n')
        self.ja_initialize()
        self.call('prompts', self.iter)
        text = (self.iter / 'ja-review/batch-1.txt').read_text(encoding='utf-8')
        for name in names[:3]:
            with self.subTest(name=name):
                self.assertIn('=== FILE: ' + name, text)
        for name in names[3:]:
            with self.subTest(name=name):
                self.assertNotIn('=== FILE: ' + name, text)

    def test_prefaced_empty_report_and_range_location(self):
        self.assertEqual(self.collection_fixture('確認しました\n問題なし\n### highlights\n### lowercase\n以上')['findings'], [])
        result = self.collection_fixture(self.report('10-12') + '\n### まとめ\n確認済み')
        self.assertEqual(result['findings'][0]['line'], 10)

    def test_gate_bad_diff_aborts_and_collect_preserves_both_artifacts(self):
        self.ready_diff()
        (self.iter / 'fix.diff').write_text('tampered')
        self.assertEqual(self.call('gate', self.iter)['next'], 'abort')
        self.call('fixdiff', self.iter)
        self.collection_fixture('問題なし')
        saved = [(self.run / 'iter-2' / name).read_bytes() for name in ('collected.json', 'collected.md')]
        (self.run / 'iter-2' / 'review.json').unlink()
        self.call('collect', self.run / 'iter-2', code=1)
        self.assertIn('review.json', self.last_stderr)
        self.assertEqual([(self.run / 'iter-2' / name).read_bytes() for name in ('collected.json', 'collected.md')], saved)

    def test_documented_defaults_match_init_and_batch_limit(self):
        run = self.read_json(self.run / 'run.json')
        docs = (CLI.parent.parent / 'references/execution.md').read_text(encoding='utf-8')
        for key, pattern in [('diffBudget', r'`diffBudget` の既定は(\d+) bytes'),
                             ('timeout', r'`timeout` は(\d+)秒'),
                             ('jaTimeout', r'`jaTimeout` は(\d+)秒')]:
            with self.subTest(key=key):
                self.assertEqual(int(re.search(pattern, docs)[1]), run[key])
        self.assertEqual(int(re.search(r'(\d+) bytes 以下の引数バッチ', docs)[1]), mr.JA_BATCH_LIMIT)
        self.assertEqual(re.search(r'`--roles` 省略時は `([^`]+)`', docs)[1].split(','), run['roles'])
        skill = (CLI.parent.parent / 'SKILL.md').read_text(encoding='utf-8')
        for pattern, value in [(r'同時実行は最大(\d+)ジョブ', mr.MAX_PARALLEL),
                               (r'変更後番号±(\d+)', mr.NEAR_FIX_LINES),
                               (r'ledger render は(\d+)秒', mr.GUARD_TIMEOUT)]:
            self.assertEqual(int(re.search(pattern, docs)[1]), value)
        self.assertEqual(int(re.search(r'（(\d+)イテレーションの上限到達）', skill)[1]), mr.MAX_ITERATIONS)
        self.assertEqual(set(re.findall(r'`next: (\w+)`', skill)), set(mr.NEXT.values()))

    def test_copy_snapshot_includes_only_unresolved_all_positions(self):
        self.repository()
        for name in ('first', 'other', 'a,b.py', 'a、b.py', 'fixed', 'rejected'):
            (self.repo / name).write_text(name)
        self.add(file='first', problem='未解決（他: other:2, "a,b.py":3、"a、b.py":4）')
        self.add(file='fixed', problem='修正済み')
        self.add(file='rejected', problem='却下')
        self.add(file='missing', problem='存在しないファイル')
        self.call('ledger', self.run, 'fix', 'F2', '--iteration', 1, '--summary', 'fix')
        self.call('ledger', self.run, 'reject', 'F3', '--reason', '不要')
        self.use_copy_mode(['a.txt'])
        self.call('snapshot', self.iter)
        self.assertEqual(set((self.iter / 'snapshot.files').read_bytes().split(b'\0')),
                         {b'a.txt', b'first', b'other', b'a,b.py', 'a、b.py'.encode(), b''})

    def test_scope_rename_directory_and_nested_repository(self):
        self.repository()
        self.git('mv', 'a.txt', 'renamed.txt')
        self.call('prompts', self.iter, '--roles', '')
        self.assertEqual(self.read_json(self.iter / 'target.json')['files'], ['a.txt', 'renamed.txt'])
        folder = self.repo / 'folder'
        folder.mkdir()
        (folder / 'a.txt').write_text('nested')
        self.initialize('--path', 'folder', '--roles', '')
        self.call('prompts', self.iter)
        self.assertEqual(self.read_json(self.iter / 'target.json')['files'], ['folder/a.txt'])
        self.git('init', str(folder / 'nested'))
        (folder / 'nested/file').write_text('untracked nested')
        self.call('prompts', self.iter)
        self.assertIn('folder/nested/file', self.read_json(self.iter / 'target.json')['files'])
        self.use_copy_mode(['folder'])
        self.call('snapshot', self.iter)
        names = (self.iter / 'snapshot.files').read_bytes()
        self.assertIn(b'folder/nested/file\0', names)
        self.assertNotIn(b'.git/', names)

    def test_only_and_exclude_control_all_roles(self):
        self.repository()
        for options, excluded, sources in [
                (['--only', 'claude'], ['codex', 'ja-review'], ['claude-general', 'claude-bug-hunt']),
                (['--only', 'claude', '--only', 'codex', '--exclude', 'claude'],
                 ['claude', 'ja-review'], ['codex-general'])]:
            with self.subTest(options=options):
                run = self.initialize('--roles', 'bug-hunt', *options, '--path', 'a.txt')
                self.assertEqual(run['excluded'], excluded)
                jobs = self.call('prompts', self.iter)['reviewers']
                self.assertEqual([j['source'] for j in jobs], sources)

    def test_init_failure_can_be_retried_and_summary_describes_scope(self):
        (self.repo / 'a').write_text('a')
        self.run = self.root / 'new-review'
        self.fail_replacement(self.run / 'run.json', 'init', self.run, '--cwd', self.repo, '--path', 'a')
        self.assertFalse((self.run / 'findings.json').exists())
        self.fake('jev-guard', "raise SystemExit('render failed')")
        self.call('init', self.run, '--cwd', self.repo, '--path', 'a', code=1)
        self.assertIn('render failed', self.last_stderr)
        self.assertFalse((self.run / 'run.json').exists())
        self.assertFalse((self.run / 'findings.json').exists())
        (self.bin / 'jev-guard').unlink()
        run = self.call('init', self.run, '--cwd', self.repo, '--path', 'a')
        self.assertIn('a; 1 files', run['baseSummary'])
        (self.repo / 'a').unlink()
        self.repository()
        (self.repo / 'a.txt').write_text('change\n')
        run = self.initialize()
        self.assertEqual(run['baseSummary'], 'uncommitted: main, HEAD; 1 files')
        self.git('checkout', '--detach')
        run = self.initialize()
        self.assertEqual(run['baseSummary'], 'uncommitted: detached HEAD, HEAD; 1 files')

    def test_no_head_path_rereview_includes_new_changes(self):
        self.repository(commit=False)
        self.initialize('--path', 'a.txt', '--roles', '')
        self.call('prompts', self.iter)
        self.assertNotIn('=== FILE:', (self.iter / 'claude.prompt.md').read_text(encoding='utf-8'))
        (self.repo / 'new').write_text('added after review')
        self.git('add', 'new')
        (self.repo / 'other').write_text('untracked after review')
        second = self.run / 'iter-2'
        self.call('prompts', second)
        text = (second / 'claude.prompt.md').read_text(encoding='utf-8')
        self.assertIn('added after review', text)
        self.assertIn('untracked after review', text)
        self.assertEqual(self.read_json(second / 'target.json')['files'], ['a.txt', 'new', 'other'])
        self.fail_tree_generation()
        self.assertEqual(self.call('snapshot', second)['mode'], 'copy')
        self.assertEqual(set((second / 'snapshot.files').read_bytes().split(b'\0')),
                         {b'a.txt', b'new', b'other', b''})

    def test_path_rereview_includes_scoped_changes_and_fixed_git_prefixes(self):
        self.repository()
        (self.repo / 'folder').mkdir()
        (self.repo / 'folder/a').write_text('before\n')
        self.git('add', '.')
        self.git('commit', '-m', 'folder')
        (self.repo / 'folder/a').write_text('after\n')
        (self.repo / 'folder/new').write_text('new scoped')
        (self.repo / 'a.txt').write_text('OUTSIDE_TRACKED_SECRET')
        self.git('config', 'diff.mnemonicPrefix', 'true')
        self.initialize('--path', 'folder', '--roles', '')
        self.call('prompts', self.iter)
        self.assertNotIn('+after', (self.iter / 'claude.prompt.md').read_text(encoding='utf-8'))
        self.call('snapshot', self.iter)
        (self.repo / 'a.txt').write_text('outside fix\n')
        (self.repo / 'outside-new').write_text('outside new fix\n')
        self.call('fixdiff', self.iter)
        second = self.run / 'iter-2'
        self.call('prompts', second)
        text = (second / 'claude.prompt.md').read_text(encoding='utf-8')
        self.assertIn('+++ b/folder/a', text)
        self.assertIn('+after', text)
        self.assertIn('new scoped', text)
        self.assertIn('-OUTSIDE_TRACKED_SECRET', text)
        self.assertIn('+outside fix', text)
        self.assertIn('outside new fix', text)
        self.assertIn('a.txt', self.read_json(second / 'target.json')['files'])
        self.call('snapshot', second)
        (self.repo / 'folder/a').write_text('third\n')
        self.call('fixdiff', second)
        third = self.run / 'iter-3'
        self.call('prompts', third)
        self.assertIn('+outside fix', (third / 'claude.prompt.md').read_text())

    def test_ja_parent_build_directory_utf8_links_and_git_line_numbers(self):
        parent = self.root / 'build'
        parent.mkdir()
        shutil.move(str(self.repo), parent / 'repo')
        self.repo = parent / 'repo'
        self.repository()
        (self.repo / 'ja.txt').write_text('本文\v続き\u2028同じ行\r\n次行\n')
        secret = self.root / 'secret'
        secret.write_text('外部の秘密本文')
        (self.repo / 'external').symlink_to(secret)
        self.ja_initialize()
        self.call('prompts', self.iter)
        text = (self.iter / 'ja-review/batch-1.txt').read_text(encoding='utf-8')
        self.assertIn('1: 本文\v続き\u2028同じ行\n2: 次行', text)
        self.assertNotIn('外部の秘密本文', text)
        self.assertNotIn('=== FILE: external', text)

    def test_ja_large_contiguous_ranges_and_pure_rename(self):
        self.repository()
        (self.repo / 'ja.txt').write_text('前\n' * 15000)
        self.git('add', '.')
        self.git('commit', '-m', 'baseline')
        (self.repo / 'ja.txt').write_text('後\n' * 15000)
        self.ja_initialize()
        jobs = self.call('prompts', self.iter)['reviewers']
        batches = [Path(j['prompt']) for j in jobs if j['cli'] == 'agy']
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(p.stat().st_size <= mr.JA_BATCH_LIMIT for p in batches))
        for batch in batches:
            with self.subTest(batch=batch.name):
                text = batch.read_text(encoding='utf-8')
                self.assertEqual(text.count('=== 指摘対象の行:'), 1)
                self.assertNotIn('1-1, 2-2', text)
        self.git('commit', '-am', 'baseline2')
        self.git('mv', 'ja.txt', 'renamed.txt')
        self.assertFalse(any(j['cli'] == 'agy' for j in self.call('prompts', self.iter)['reviewers']))

    def test_ja_base_scope_uses_worktree_line_numbers(self):
        self.repository()
        (self.repo / 'a.txt').write_text('一\n二\n三\n')
        self.git('commit', '-am', 'base')
        self.git('checkout', '-b', 'topic')
        (self.repo / 'a.txt').write_text('追加\n一\n二\n三\n')
        self.git('commit', '-am', 'topic')
        self.ja_initialize()
        (self.repo / 'a.txt').write_text('追加\n一\n改稿\n三\n')
        self.call('prompts', self.iter)
        text = (self.iter / 'ja-review/batch-1.txt').read_text(encoding='utf-8')
        self.assertIn('=== 指摘対象の行: 1-1, 3-3', text)

    def test_prompts_remove_stale_diff_and_surplus_ja_batches(self):
        self.repository()
        (self.repo / 'ja.txt').write_text(('語' * 100 + '\n') * 1000)
        self.ja_initialize('--diff-budget', 1)
        self.call('prompts', self.iter)
        self.assertTrue((self.iter / 'target.diff').is_file())
        self.assertTrue((self.iter / 'ja-review/batch-2.txt').is_file())
        run = self.read_json(self.run / 'run.json')
        run['diffBudget'] = mr.DIFF_BUDGET
        self.write_json(self.run / 'run.json', run)
        (self.repo / 'ja.txt').write_text('短い本文\n')
        self.call('prompts', self.iter)
        self.assertFalse((self.iter / 'target.diff').exists())
        self.assertFalse((self.iter / 'ja-review/batch-2.txt').exists())

    def test_models_effort_and_real_argv_for_both_profiles(self):
        self.repository()
        (self.repo / 'a.txt').write_text('change\n')
        expected = {
            'default': [('claude-general', 'claude-opus-5-5', 'medium'),
                        ('codex-general', 'gpt-6.1-sol', 'medium'),
                        ('claude-bug-hunt', 'claude-opus-5-5', 'high')],
            'codex': [('claude-general', 'claude-opus-5-5', 'high'),
                      ('codex-general', 'gpt-6.1-sol', 'medium'),
                      ('codex-bug-hunt', 'gpt-6.1-sol', 'high')]}
        for profile, specs in expected.items():
            with self.subTest(profile=profile):
                self.initialize('--profile', profile, '--roles', 'bug-hunt')
                jobs = self.call('prompts', self.iter)['reviewers']
                self.assertEqual([(j['source'], j['model'], j['effort']) for j in jobs], specs)
                for cli, model_flag in [('claude', '--model'), ('codex', '-m')]:
                    parameters = {Path(j['prompt']).read_text(encoding='utf-8'): (model, effort)
                                  for j, (_, model, effort) in zip(jobs, specs) if j['cli'] == cli}
                    checks = ("import sys\nfrom pathlib import Path\nparameters=" + repr(parameters)
                              + "\nmodel,effort=parameters[sys.stdin.read()]\n"
                              + "assert sys.argv[sys.argv.index(" + repr(model_flag) + ")+1]==model\n")
                    if cli == 'claude':
                        checks += "assert sys.argv[sys.argv.index('--effort')+1]==effort\nprint('問題なし')"
                    else:
                        checks += "assert sys.argv[sys.argv.index('-c')+1]=='model_reasoning_effort=\"'+effort+'\"'\nPath(sys.argv[sys.argv.index('-o')+1]).write_text('問題なし')"
                    self.fake(cli, checks)
                result = self.call('review', self.iter)
                self.assertTrue(all(j['status'] == 'success' for j in result['reviewers']))

    def test_skill_profile_table_matches_review_configuration(self):
        text = (CLI.parent.parent / 'SKILL.md').read_text(encoding='utf-8')
        table = text.split('| 役割 | `default`（省略時） | `codex` |\n', 1)[1]
        rows = [[cell.strip() for cell in line.strip('|').split('|')]
                for line in table.splitlines()[1:4]]
        self.assertEqual(len(rows), 3)
        self.repository()
        (self.repo / 'a.txt').write_text('change\n')
        for column, profile in enumerate(('default', 'codex'), 1):
            with self.subTest(profile=profile):
                self.initialize('--profile', profile, '--roles', 'bug-hunt')
                jobs = self.call('prompts', self.iter)['reviewers']
                self.assertEqual(len(jobs), 3)
                for row, job in zip(rows, jobs):
                    with self.subTest(source=job['source']):
                        documented = row[column].replace('Opus 5.5', 'claude-opus-5-5')
                        model = job['model'] + ' ' + job['effort']
                        expected = job['cli'] + ' / ' + model if 'bug-hunt' in job['source'] else model
                        self.assertEqual(documented, expected)

    def test_gate_section_risk_probability_boundaries(self):
        self.ready_diff()
        for probability, count in [(.49, 0), (.5, 1), (0, 0), (1, 1), (-.01, None)]:
            with self.subTest(probability=probability):
                self.fake_gate(self.verdict(newRisk={'value': False}, sections=[
                    {'path': 'a.txt', 'newRiskProbability': probability}]))
                result = self.call('gate', self.iter)
                if count is None:
                    self.assertEqual(result['status'], 'not_performed')
                    self.assertEqual(result['reason'], 'invalid section risk probability')
                    self.assertEqual(result['next'], 'rereview')
                else:
                    self.assertEqual(len(result['grounds']), count)
                    self.assertEqual([g['kind'] for g in result['grounds']], ['section'] * count)

    def test_gate_empty_at_limit_and_copy_at_limit_priorities(self):
        self.repository()
        self.call('snapshot', self.iter)
        self.call('fixdiff', self.iter)
        ledger = self.read_json(self.run / 'findings.json')
        ledger['iteration'] = mr.MAX_ITERATIONS
        self.write_json(self.run / 'findings.json', ledger)
        self.assertEqual(self.call('gate', self.iter)['next'], 'finish')
        (self.repo / 'a.txt').write_text('fix\n')
        self.call('fixdiff', self.iter)
        state = self.read_json(self.iter / 'fixdiff.json')
        state['mode'] = 'copy'
        state['state'] = {'files': {}, 'diffHash': state['diffHash']}
        self.write_json(self.iter / 'fixdiff.json', state)
        (self.iter / 'snapshot.mode').write_text('copy')
        self.assertEqual(self.call('gate', self.iter)['next'], 'limit')

    def test_master_only_default_branch_fallback(self):
        self.repository()
        self.git('branch', '-m', 'master')
        base = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('checkout', '-b', 'topic')
        (self.repo / 'a.txt').write_text('topic\n')
        self.git('commit', '-am', 'topic')
        self.assertEqual(self.initialize()['base'], base)

    def test_collect_rejects_changed_successful_output(self):
        self.call('prompts', self.iter, '--roles', '')
        self.call('review', self.iter)
        (self.iter / 'claude.md').write_text(self.report(1))
        self.call('collect', self.iter, code=1)
        self.assertIn('review output changed after completion', self.last_stderr)
        review = self.read_json(self.iter / 'review.json')
        review['reviewers'][0].pop('outputHash')
        self.write_json(self.iter / 'review.json', review)
        self.call('collect', self.iter, code=1)
        self.assertIn('missing review output hash', self.last_stderr)

    def test_ledger_action_failures_preserve_findings(self):
        original = (self.run / 'findings.json').read_bytes()
        source = self.root / 'bad.json'
        self.write_json(source, ['bad'])
        self.call('ledger', self.run, 'add', '--input', source, code=1)
        self.assertEqual(self.last_stderr.strip(), 'finding must be an object')
        self.assertEqual((self.run / 'findings.json').read_bytes(), original)
        self.add()
        original = (self.run / 'findings.json').read_bytes()
        self.write_json(source, {'status': 'fixed'})
        self.call('ledger', self.run, 'update', 'F1', '--input', source, code=1)
        self.assertEqual(self.last_stderr.strip(), 'invalid update fields')
        self.assertEqual((self.run / 'findings.json').read_bytes(), original)
        self.call('ledger', self.run, 'fix', code=1)
        self.assertEqual(self.last_stderr.strip(), 'finding IDs required')
        self.assertEqual((self.run / 'findings.json').read_bytes(), original)

    def test_fallback_render_numeric_order_single_line_and_no_fix_summary(self):
        self.add(id='F1²')
        self.add(id='F10', problem='十\n行')
        self.add(id='F2', problem='二\u2028行')
        self.call('ledger', self.run, 'fix', 'F2', '--iteration', 1, '--summary', 'FIX_SUMMARY_ONLY')
        text = (self.run / 'ledger.md').read_text(encoding='utf-8')
        self.assertIn('問題: 十 行', text)
        self.assertIn('問題: 二 行 | 修正イテレーション: 1', text)
        self.assertNotIn('FIX_SUMMARY_ONLY', text)
        self.add(id='F1')
        text = (self.run / 'ledger.md').read_text(encoding='utf-8')
        self.assertLess(text.index('ID: F1 |'), text.index('ID: F10 |'))
        self.add(id='F3')
        text = (self.run / 'ledger.md').read_text(encoding='utf-8')
        self.assertLess(text.index('ID: F3 |'), text.index('ID: F10 |'))
        self.assertIn('ID: F1² |', text)

    def test_ja_missing_template_empty_ranges_and_unsupported_cli(self):
        self.repository()
        (self.repo / 'a.txt').write_text('日本語\n')
        skill = self.ja_skill()
        self.initialize('--ja-review', '--ja-skill', skill / 'SKILL.md', '--roles', '')
        self.assertTrue(any(j['cli'] == 'agy' for j in self.call('prompts', self.iter)['reviewers']))
        (skill / 'references/prompt.md').unlink()
        self.assertFalse(any(j['cli'] == 'agy' for j in self.call('prompts', self.iter)['reviewers']))
        (skill / 'references/prompt.md').write_text('校閲して\n')
        (self.repo / 'a.txt').write_text('original\n')
        self.assertFalse(any(j['cli'] == 'agy' for j in self.call('prompts', self.iter)['reviewers']))
        self.fake('unsupported', "print('問題なし')")
        self.review_plan([self.job('unsupported', 'unsupported')])
        self.call('review', self.iter, code=1)
        result = self.read_json(self.iter / 'review.json')['reviewers'][0]
        self.assertEqual(result['reason'], 'unsupported CLI')
        (self.repo / 'a.txt').write_text('English change\n')
        self.initialize('--exclude', 'claude', '--exclude', 'codex', '--ja-review',
                        '--ja-skill', skill, '--roles', '')
        self.call('prompts', self.iter, code=1)
        self.assertIn('no reviewer selected', self.last_stderr)
        self.assertFalse((self.iter / 'prompts.json').exists())

    def test_collect_invalid_reports_fail_with_specific_reason_and_no_artifacts(self):
        self.collection_fixture(self.report(1))
        directory = self.run / 'iter-2'
        cases = [('挨拶だけ', 'claude', 'no valid report'),
                 (self.report(1, path='"bad\\udcff\\q"'), 'claude', 'invalid JSON path'),
                 (self.report(0), 'claude', 'invalid report line'),
                 (self.report(1) + '- 問題: 二重\n', 'claude', 'duplicate report field'),
                 ('問題なし\n' + self.report(1), 'claude', 'contradictory empty report'),
                 ('問題なし\n### 指摘1（重要度: 不明）\n- 問題: 不備', 'claude', 'contradictory empty report'),
                 ('指摘なし\n### medium\n- 原文: 不備', 'agy', 'contradictory empty report'),
                 ('### 指摘1（重要度: 高）\n- 問題: 必須欄欠落\n', 'claude', 'incomplete common report'),
                 ('### medium a.py:1\n- 原文: 本文\n- 理由: 理由\n', 'agy', 'incomplete ja report')]
        for text, cli, reason in cases:
            with self.subTest(text=text):
                # Reset the source transport; inject only the format fault under test.
                self.collection_fixture(self.report(1))
                self.replace_review_output(directory, text, cli)
                self.call('collect', directory, code=1)
                self.assertIn(reason, self.last_stderr)
                self.assertFalse((directory / 'collected.json').exists())
                self.assertFalse((directory / 'collected.md').exists())

    def test_collect_preserves_malformed_finding_and_ignores_summary_heading(self):
        text = self.report(1, problem='問題なしと判断した箇所は別\n問題なし') + '\n### 指摘2（重要度: 高）\n- 問題: 欠落\n### 指摘3（重要度: 最高）\n- 問題: 形式不正\n### 指摘一覧\n確認済み\n### 指摘まとめ\n完了'
        result = self.collection_fixture(text)
        self.assertEqual(len(result['findings']), 3)
        malformed = next(f for f in result['findings'] if f.get('parseError') == 'incomplete common report')
        self.assertIn('- 問題: 欠落', malformed['raw'])
        self.assertEqual(malformed['parseError'], 'incomplete common report')
        self.assertTrue(any(f.get('parseError') == 'malformed report heading' for f in result['findings']))
        self.assertEqual(next(f for f in result['findings'] if 'parseError' not in f)['line'], 1)

    def test_collect_invalid_previous_completion_marks_proximity_unknown(self):
        self.collection_fixture(self.report(10), '--- a/a.py\n+++ b/a.py\n@@ -10 +10 @@\n-old\n+new\n')
        (self.run / 'iter-1/fix.diff').write_text('stale')
        result = self.call('collect', self.run / 'iter-2')
        self.assertIsNone(result['findings'][0]['nearPreviousFix'])

    def test_ja_prefaced_empty_report_is_accepted(self):
        self.collection_fixture('問題なし')
        self.replace_review_output(self.run / 'iter-2', '確認済み\n 指摘なし。 \n### まとめ\n完了', 'agy')
        self.assertEqual(self.call('collect', self.run / 'iter-2')['findings'], [])
        text = '### medium a.py:1\n- 分類: 表記\n- 原文: 不備\n- 修正案: 修正\n- 理由: 誤字\n### high\n'
        self.call('review', self.run / 'iter-2')
        self.replace_review_output(self.run / 'iter-2', text, 'agy')
        findings = self.call('collect', self.run / 'iter-2')['findings']
        self.assertEqual(len(findings), 2)
        self.assertTrue(any(f.get('parseError') == 'malformed report heading' for f in findings))

    def test_empty_fallback_render_has_four_empty_sections(self):
        text = (self.run / 'ledger.md').read_text(encoding='utf-8')
        self.assertTrue(text.startswith('# 台帳\n'))
        self.assertEqual(text.count('\nなし'), 4)
        self.assertEqual([line for line in text.split('\n') if line.startswith('## ')],
                         ['## 未解決', '## 修正済み', '## 保留', '## 却下'])

    def test_task_input_saves_body(self):
        body = self.root / 'task'
        body.write_text('fix')
        self.call('task', self.iter, '--input', body)
        self.assertEqual((self.iter / 'fix-task.md').read_text(encoding='utf-8'), 'fix')

    def test_invalid_import_receipt_and_missing_render_output_are_diagnosed(self):
        self.add()
        ledger = self.read_json(self.run / 'findings.json')
        ledger['findings'] = []
        self.write_json(self.run / 'findings.json', ledger)
        original = (self.run / 'findings.json').read_bytes()
        self.call('ledger', self.run, 'add', '--input', self.root / 'input.json', code=1)
        self.assertEqual(self.last_stderr.strip(), 'import receipt without finding')
        self.assertEqual((self.run / 'findings.json').read_bytes(), original)
        self.fake('jev-guard', 'pass')
        self.call('ledger', self.run, 'render', code=1)
        self.assertEqual(self.last_stderr.strip(), 'ledger render produced no output')
        self.assertEqual((self.run / 'findings.json').read_bytes(), original)

    def test_copy_snapshot_retry_replaces_saved_link_without_writing_target(self):
        target = self.root / 'original-target'
        target.write_text('preserved')
        source = self.repo / 'link'
        source.symlink_to(target)
        self.initialize('--path', 'link')
        (self.iter / 'snapshot.files').mkdir()
        self.call('snapshot', self.iter, code=1)
        self.assertTrue((self.iter / 'snapshot/link').is_symlink())
        self.assertIn('snapshot.files', self.last_stderr)
        self.assertFalse((self.iter / 'snapshot.mode').exists())
        source.unlink()
        source.write_text('replacement')
        (self.iter / 'snapshot.files').rmdir()
        self.call('snapshot', self.iter)
        self.assertEqual(target.read_text(encoding='utf-8'), 'preserved')
        self.assertFalse((self.iter / 'snapshot/link').is_symlink())
        self.assertEqual((self.iter / 'snapshot/link').read_text(encoding='utf-8'), 'replacement')

    def test_copy_snapshot_retry_refuses_saved_directory_link_parent(self):
        outside = self.root / 'outside-directory'
        outside.mkdir()
        source = self.repo / 'folder'
        source.symlink_to(outside, target_is_directory=True)
        self.initialize('--path', 'folder')
        (self.iter / 'snapshot.files').mkdir()
        self.call('snapshot', self.iter, code=1)
        self.assertIn('snapshot.files', self.last_stderr)
        source.unlink()
        source.mkdir()
        (source / 'file').write_text('replacement')
        (self.iter / 'snapshot.files').rmdir()
        self.call('snapshot', self.iter, code=1)
        self.assertIn('file parent escapes cwd', self.last_stderr)
        self.assertEqual(list(outside.iterdir()), [])

    def test_ledger_missing_input_is_diagnosed_without_changing_findings(self):
        self.add()
        original = (self.run / 'findings.json').read_bytes()
        for action, ids in [('add', []), ('update', ['F1'])]:
            with self.subTest(action=action):
                self.call('ledger', self.run, action, *ids, code=1)
                self.assertEqual(self.last_stderr.strip(), '--input required for ' + action)
                self.assertEqual((self.run / 'findings.json').read_bytes(), original)

    def test_git_blank_context_added_and_deleted_files_keep_proximity(self):
        self.repository()
        (self.repo / 'a.txt').write_text('before\n\nend\n')
        (self.repo / 'deleted').write_text('old\n')
        self.git('add', '.')
        self.git('commit', '-m', 'baseline')
        self.git('config', 'diff.suppressBlankEmpty', 'true')
        (self.repo / 'a.txt').write_text('after\n\nend\n')
        (self.repo / 'deleted').unlink()
        (self.repo / 'added').write_text('new\n')
        self.git('add', 'added')
        self.initialize('--roles', '')
        self.call('prompts', self.iter)
        # The public target contains the same git patch used for line selection.
        text = (self.iter / 'claude.prompt.md').read_text()
        patch = text[text.index('diff --git '):]
        result = self.collection_fixture(self.report(6, path='deleted') +
                                         self.report(7, path='deleted') +
                                         self.report(6, path='added') +
                                         self.report(7, path='added'), patch)
        self.assertEqual([f['nearPreviousFix'] for f in result['findings']], [True, False, True, False])
        self.assertIn('\n \n', patch)
        self.assertNotIn('解析不能', (self.run / 'iter-2/collected.md').read_text())

    def test_init_reviews_directory_link_itself_and_non_utf8_root(self):
        self.repository()
        outside = self.root / 'outside'
        outside.mkdir()
        (self.repo / 'link').symlink_to(outside, target_is_directory=True)
        run = self.initialize('--path', 'link', '--roles', '')
        self.assertEqual(run['paths'], ['link'])
        self.call('prompts', self.iter)
        self.assertEqual(self.read_json(self.iter / 'target.json')['files'], ['link'])
        renamed = self.root / os.fsdecode(b'repo-\xff ')
        self.repo.rename(renamed)
        self.repo = renamed
        self.assertEqual(self.initialize('--path', 'a.txt')['cwd'], str(renamed))

    def test_copy_snapshot_refuses_link_parent_even_inside_saved_root(self):
        (self.repo / 'target').mkdir()
        (self.repo / 'target/x').write_text('preserved')
        (self.repo / 'd').symlink_to('target', target_is_directory=True)
        self.initialize('--path', 'd', '--path', 'target')
        self.add(file='d/x')
        self.call('snapshot', self.iter, code=1)
        self.assertIn('snapshot parent must not be a link', self.last_stderr)
        self.assertFalse((self.iter / 'snapshot.mode').exists())
        self.assertEqual((self.repo / 'target/x').read_text(), 'preserved')

    def test_codex_missing_output_retries_and_is_absent(self):
        self.review_plan([self.job('codex', 'codex-general')])
        self.fake('codex', "print('問題なし')")
        self.call('review', self.iter, code=1)
        job = self.read_json(self.iter / 'review.json')['reviewers'][0]
        self.assertEqual(job['status'], 'absent')
        self.assertEqual(len(job['attempts']), mr.MAX_ATTEMPTS)
        self.assertTrue(all('No such file' in attempt['reason'] for attempt in job['attempts']))
        self.assertFalse(Path(job['output']).exists())

    def test_review_timeout_does_not_wait_for_detached_pipe_holder(self):
        self.review_plan([self.job('claude', 'claude-general', .05)])
        child = "import os,time\nfrom pathlib import Path\nPath('started-'+str(os.getppid())).write_text(str(os.getpid()))\ntime.sleep(60)"
        self.fake('claude', 'import subprocess,sys,time\nsubprocess.Popen([sys.executable,"-c",' +
                  repr(child) + '],start_new_session=True)\ntime.sleep(60)')
        try:
            started = time.monotonic()
            result = self.review_after_start()
            self.assertLess(time.monotonic() - started, 15)
            self.assertEqual(len(result['reviewers'][0]['attempts']), mr.MAX_ATTEMPTS)
            self.assertTrue(all(a['reason'] == 'timeout' for a in result['reviewers'][0]['attempts']))
        finally:
            for marker in self.repo.glob('started-*'):
                try:
                    os.kill(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_review_stop_timeout_preserves_other_reviewer_results(self):
        self.review_plan([self.job('claude', 'claude-general', .05), self.job('codex', 'codex-general')])
        self.fake('claude', 'import time\ntime.sleep(60)')
        launcher = """import sys,subprocess
sys.path.insert(0,sys.argv.pop(1))
import mr
def finish(process):
    process.wait(timeout=5)
    raise subprocess.TimeoutExpired('injected stop failure', 1)
mr.finish_process=finish
sys.exit(mr.main())
"""
        result = subprocess.run([sys.executable, '-c', launcher, str(CLI.parent), 'review', str(self.iter)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        review = self.read_json(self.iter / 'review.json')
        self.assertEqual([job['status'] for job in review['reviewers']], ['absent', 'success'])
        self.assertIn('injected stop failure', review['reviewers'][0]['reason'])

    def test_snapshot_and_manifest_validation_do_not_publish_completion(self):
        (self.iter / 'snapshot.json').write_text('{}')
        self.call('snapshot', self.iter, code=1)
        self.assertIn('snapshot already attempted', self.last_stderr)
        self.assertFalse((self.iter / 'snapshot.mode').exists())
        (self.iter / 'snapshot.json').unlink()
        folder = self.repo / 'folder'
        folder.mkdir()
        self.add(file='folder')
        (folder / 'file').write_text('directory finding')
        self.assertEqual(self.call('snapshot', self.iter)['mode'], 'copy')
        self.assertEqual((self.iter / 'snapshot/folder/file').read_text(), 'directory finding')
        (self.iter / 'snapshot.mode').unlink()
        (self.iter / 'snapshot.json').unlink()
        shutil.rmtree(self.iter / 'snapshot')
        self.call('ledger', self.run, 'hold', 'F1', '--reason', '保留')
        (self.repo / 'a.txt').write_text('a')
        (self.iter / 'snapshot').symlink_to(self.root / 'saved')
        self.call('snapshot', self.iter, code=1)
        self.assertIn('snapshot directory must not be a link', self.last_stderr)
        self.assertFalse((self.root / 'saved').exists())
        (self.iter / 'snapshot').unlink()
        self.call('snapshot', self.iter)
        for manifest, reason in [(b'a.txt', 'invalid snapshot.files'),
                                 (b'/absolute\0', 'unsafe relative file'),
                                 (b'../outside\0', 'unsafe relative file'),
                                 (b'.\0', 'unsafe relative file')]:
            with self.subTest(manifest=manifest):
                (self.iter / 'snapshot.files').write_bytes(manifest)
                self.call('fixdiff', self.iter, code=1)
                self.assertIn(reason, self.last_stderr)
                self.assertFalse((self.iter / 'fixdiff.json').exists())
        self.call('ledger', self.root / 'missing', 'render', code=1)
        self.assertIn('review directory not found', self.last_stderr)
        self.assertFalse((self.root / 'missing').exists())
        source = self.root / 'empty-path.json'
        self.write_json(source, self.finding(file=''))
        original = (self.run / 'findings.json').read_bytes()
        self.call('ledger', self.run, 'add', '--input', source, code=1)
        self.assertIn('empty finding string', self.last_stderr)
        self.assertEqual((self.run / 'findings.json').read_bytes(), original)

    def test_ja_template_and_execution_limits_and_invalid_diff_paths(self):
        self.repository()
        (self.repo / 'a.txt').write_text('日本語\n')
        skill = self.ja_skill()
        self.initialize('--ja-review', '--ja-skill', skill, '--roles', '')
        template = skill / 'references/prompt.md'
        original = template.read_bytes()
        template.write_bytes(b'x' * mr.JA_BATCH_LIMIT)
        self.call('prompts', self.iter, code=1)
        self.assertIn('ja template exceeds batch limit', self.last_stderr)
        self.assertFalse((self.iter / 'prompts.json').exists())
        template.write_bytes(original)
        self.call('prompts', self.iter)
        plan = self.read_json(self.iter / 'prompts.json')
        job = next(j for j in plan['reviewers'] if j['cli'] == 'agy')
        Path(job['prompt']).write_bytes(b'x' * (mr.JA_BATCH_LIMIT + 1))
        plan['reviewers'] = [job]
        self.write_json(self.iter / 'prompts.json', plan)
        self.call('review', self.iter, code=1)
        result = self.read_json(self.iter / 'review.json')
        self.assertEqual(result['reviewers'][0]['status'], 'absent')
        self.assertEqual(result['reviewers'][0]['reason'], 'ja batch exceeds limit')
        real_git = shutil.which('git')
        for patch, reason in [('--- "a/broken\n', 'invalid quoted git path'),
                              ('--- a/a.txt\n+++ b/a.txt\n@@ bad @@\n', 'invalid diff hunk'),
                              ('@@ -1 +1 @@\n', 'invalid diff hunk'),
                              ('--- "a/\\8"\n', 'unknown git path escape'),
                              ('--- "a/\\q"\n', 'unknown git path escape'),
                              ('--- "a/\\08"\n', 'invalid octal git path'),
                              ('Binary files unknown differ\n', 'cannot locate binary diff')]:
            with self.subTest(patch=patch):
                self.fake('git', 'import os,sys\nif "diff" in sys.argv:\n    print(' +
                          repr(patch) + ')\nelse:\n    os.execv(' + repr(real_git) + ', ["git", *sys.argv[1:]])')
                self.call('prompts', self.iter, code=1)
                self.assertIn(reason, self.last_stderr)
                self.assertFalse((self.iter / 'prompts.json').exists())
        for patch in ['diff --git a/a.txt b/a.txt\nGIT binary patch\n',
                      'diff --git a/a.txt b/a.txt\nBinary files /dev/null and b/a.txt differ\n',
                      'diff --git a/a.txt b/a.txt\nBinary files a/a.txt and /dev/null differ\n',
                      'diff --git a/old b/a.txt\nBinary files a/old and b/a.txt differ\n',
                      'diff --git "a/old\\t" "b/a.txt"\nBinary files "a/old\\t" and "b/a.txt" differ\n']:
            with self.subTest(patch=patch):
                self.fake('git', 'import os,sys\nif "diff" in sys.argv:\n    print(' +
                          repr(patch) + ')\nelse:\n    os.execv(' + repr(real_git) + ', ["git", *sys.argv[1:]])')
                self.assertFalse(any(j['cli'] == 'agy' for j in self.call('prompts', self.iter)['reviewers']))

    def test_invalid_fixdiff_state_is_rejected_by_all_consumers(self):
        self.ready_diff()
        valid = self.read_json(self.iter / 'fixdiff.json')
        task = self.root / 'task'
        task.write_text('fix')
        cases = [([], 'invalid fixdiff state'), ({}, 'invalid fixdiff state'),
                 (dict(valid, state=[]), 'invalid fixdiff state'),
                 (dict(valid, mode='unknown'), 'invalid fixdiff state'),
                 (dict(valid, diffHash=''), 'invalid fixdiff state'),
                 (dict(valid, mode='copy', state={'files': [], 'diffHash': 'hash'}), 'invalid fixdiff state'),
                 (dict(valid, mode='copy', state={'files': {}, 'diffHash': ''}), 'invalid fixdiff state'),
                 (dict(valid, state={'tree': ''}), 'invalid fixdiff tree')]
        for key in ('empty', 'afterCheck', 'changed'):
            cases.append((dict(valid, **{key: 1}), 'invalid fixdiff state'))
        for missing in valid:
            cases.append(({key: value for key, value in valid.items() if key != missing}, 'invalid fixdiff state'))
        for state, reason in cases:
            for command in ('fixdiff', 'task', 'gate'):
                with self.subTest(state=state, command=command):
                    self.write_json(self.iter / 'fixdiff.json', state)
                    options = ['--input', task] if command == 'task' else []
                    result = self.call(command, self.iter, *options, code=0 if command == 'gate' else 1)
                    if command == 'gate':
                        self.assertEqual(result['status'], 'not_performed')
                        self.assertEqual(result['reason'], reason)
                        self.assertEqual(result['next'], 'abort')
                    else:
                        self.assertIn(reason, self.last_stderr)

    def test_copy_rereview_keeps_saved_outside_fixes_across_iterations(self):
        self.repository()
        (self.repo / 'outside').write_text('before\n')
        (self.repo / 'empty').touch()
        self.initialize('--path', 'a.txt', '--roles', '')
        self.add(file='outside')
        self.add(file='empty')
        self.fail_tree_generation()
        self.call('snapshot', self.iter)
        (self.repo / 'outside').write_text('first fix\n')
        (self.repo / 'empty').unlink()
        self.call('fixdiff', self.iter)
        self.call('ledger', self.run, 'fix', 'F1', '--iteration', 1, '--summary', 'fix')
        second = self.run / 'iter-2'
        original = (self.iter / 'fix.diff').read_bytes()
        (self.iter / 'fix.diff').write_text('tampered')
        self.call('prompts', second, code=1)
        self.assertIn('diff changed after generation', self.last_stderr)
        (self.iter / 'fix.diff').write_bytes(original)
        self.call('prompts', second)
        self.assertIn('outside', self.read_json(second / 'target.json')['files'])
        self.assertIn('empty', self.read_json(second / 'target.json')['files'])
        self.assertIn('+first fix', (second / 'claude.prompt.md').read_text())
        self.call('snapshot', second)
        self.assertIn(b'outside\0', (second / 'snapshot.files').read_bytes())
        (self.repo / 'outside').write_text('second fix\n')
        self.call('fixdiff', second)
        third = self.run / 'iter-3'
        self.call('prompts', third)
        self.assertIn('+second fix', (third / 'claude.prompt.md').read_text())
        self.assertNotIn('+first fix', (third / 'claude.prompt.md').read_text())
        self.assertIn('outside', self.read_json(third / 'target.json')['files'])

    def test_copy_rereview_non_git_and_unborn_keep_outside_fixes(self):
        skill = self.ja_skill()
        for is_git in (False, True):
            with self.subTest(git=is_git):
                if is_git:
                    self.repository(commit=False)
                    shutil.rmtree(self.iter)
                    self.fail_tree_generation()
                (self.repo / 'a.txt').write_text('範囲内\n')
                (self.repo / 'outside').write_text('変更なし\nbefore\n')
                self.initialize('--path', 'a.txt', '--roles', '', '--ja-skill', skill)
                self.add(file='outside')
                self.assertEqual(self.call('snapshot', self.iter)['mode'], 'copy')
                (self.repo / 'outside').write_text('変更なし\n範囲外修正\n')
                self.call('fixdiff', self.iter)
                second = self.run / 'iter-2'
                self.call('prompts', second, '--ja-review')
                self.assertIn('outside', self.read_json(second / 'target.json')['files'])
                self.assertIn('+範囲外修正', (second / 'claude.prompt.md').read_text())
                text = (second / 'ja-review/batch-1.txt').read_text()
                self.assertIn('=== FILE: outside\n=== 指摘対象の行: 2-2', text)
                self.assertIn('=== FILE: a.txt\n=== 指摘対象の行: 全行', text)

    def test_ja_path_rereview_selects_changed_japanese_and_skips_large_context(self):
        self.repository()
        (self.repo / 'a.txt').write_text('未変更\n' + 'x' * mr.JA_BATCH_LIMIT + '\nold\n')
        self.git('commit', '-am', 'baseline')
        skill = self.ja_skill()
        self.initialize('--path', 'a.txt', '--ja-review', '--ja-skill', skill, '--roles', '')
        (self.repo / 'a.txt').write_text('未変更\n' + 'x' * mr.JA_BATCH_LIMIT + '\nnew\n')
        second = self.run / 'iter-2'
        self.assertFalse(any(j['cli'] == 'agy' for j in self.call('prompts', second)['reviewers']))
        (self.repo / 'a.txt').write_text('未変更\n' + 'x' * mr.JA_BATCH_LIMIT + '\n変更行\n')
        self.call('prompts', second)
        text = (second / 'ja-review/batch-1.txt').read_text()
        self.assertIn('=== 指摘対象の行: 3-3\n3: 変更行', text)
        self.assertNotIn('=== 指摘対象の行: 全行', text)

    def test_review_parallel_limit_runs_all_jobs(self):
        jobs = [self.job('claude', f'claude-{n}', 10) for n in range(mr.MAX_PARALLEL + 1)]
        self.review_plan(jobs)
        self.fake('claude', '''import fcntl,time
from pathlib import Path
with Path('counts').open('a+') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    stream.seek(0)
    active, peak = map(int, (stream.read() or '0 0').split())
    stream.seek(0); stream.truncate(); stream.write(f'{active+1} {max(peak, active+1)}'); stream.flush()
time.sleep(.2)
with Path('counts').open('r+') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    active, peak = map(int, stream.read().split())
    stream.seek(0); stream.truncate(); stream.write(f'{active-1} {peak}')
print('問題なし')
''')
        result = self.call('review', self.iter)
        self.assertEqual(len(result['reviewers']), len(jobs))
        self.assertTrue(all(j['status'] == 'success' for j in result['reviewers']))
        active, peak = map(int, (self.repo / 'counts').read_text().split())
        self.assertEqual(active, 0)
        self.assertLessEqual(peak, mr.MAX_PARALLEL)

    def test_review_job_io_failure_preserves_other_results(self):
        jobs = [self.job('claude', 'claude-general'), self.job('codex', 'codex-general')]
        self.review_plan(jobs)
        for fault in ('prompt', 'log', 'output'):
            with self.subTest(fault=fault):
                prompt = Path(jobs[0]['prompt'])
                log = mr.attempt_path(Path(jobs[0]['output']), 1, 'stdout.log')
                output = Path(jobs[0]['output'])
                if fault == 'prompt':
                    prompt.unlink()
                elif fault == 'log':
                    log.unlink(missing_ok=True)
                    log.mkdir()
                else:
                    output.unlink(missing_ok=True)
                    output.mkdir()
                try:
                    result = self.call('review', self.iter)
                    self.assertEqual([j['status'] for j in result['reviewers']], ['absent', 'success'])
                    self.assertIn('No such file' if fault == 'prompt' else 'Is a directory', result['reviewers'][0]['reason'])
                finally:
                    if fault == 'prompt':
                        prompt.write_text('review')
                    elif fault == 'log':
                        log.rmdir()
                    else:
                        output.rmdir()

    def test_guard_timeouts_leave_diagnostics_and_allow_rereview(self):
        self.ready_diff()
        task = self.root / 'task'
        task.write_text('fix')
        self.call('task', self.iter, '--input', task)
        self.call('fixdiff', self.iter)
        self.fake('jev-guard', 'import subprocess,sys,time\nfrom pathlib import Path\n'
                  'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n'
                  'Path("guard-child").write_text(str(child.pid))\nprint("started",flush=True)\ntime.sleep(60)')
        launcher = 'import sys\nsys.path.insert(0,sys.argv.pop(1))\nimport mr\nmr.GUARD_TIMEOUT=.5\nsys.exit(mr.main())'
        for command, directory, options, code in [('gate', self.iter, [], 0),
                                                  ('rules', self.iter, [], 0),
                                                  ('ledger', self.run, ['render'], 1)]:
            with self.subTest(command=command):
                (self.repo / 'guard-child').unlink(missing_ok=True)
                (self.run / 'guard-child').unlink(missing_ok=True)
                result = subprocess.run([sys.executable, '-c', launcher, str(CLI.parent), command,
                                         str(directory), *options], env=self.env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                if command == 'ledger':
                    self.assertIn('timed out', result.stderr)
                else:
                    output = json.loads(result.stdout)
                    self.assertEqual(output['status'], 'not_performed')
                    self.assertIn('timed out', output['reason'])
                    if command == 'gate':
                        self.assertEqual(output['next'], 'rereview')
                    self.assertEqual((self.iter / (command + '.stdout.log')).read_text(), 'started\n')
                marker = (self.run if command == 'ledger' else self.repo) / 'guard-child'
                pid = marker.read_text()
                self.assert_process_stopped(pid)
