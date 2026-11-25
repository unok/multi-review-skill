import importlib.util
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

RUNNER = Path(__file__).resolve().parent / 'parallel.py'
# 子や孫の起動・停止を待つ上限と、その間の確認間隔。
WAIT_SECONDS = 30
POLL_SECONDS = .05
# パイプの容量（Linux で 64KiB）を大きく超え、出力の詰まりと欠けを確かめられる大きさ。
LARGE_OUTPUT_CHARS = 4000000
# 待機の上限（WAIT_SECONDS）より十分長く、止めなければ自然には終わらない時間。
LONG_SLEEP_SECONDS = 600
spec = importlib.util.spec_from_file_location('parallel', RUNNER)
parallel = importlib.util.module_from_spec(spec)
with patch.object(sys, 'dont_write_bytecode', True):
    spec.loader.exec_module(parallel)


class ParallelRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        # 偽のテストパッケージに .pyc を残さず、ランナーが root に書かないことを確かめられるようにする。
        environment = patch.dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        environment.start()
        self.addCleanup(environment.stop)

    def package(self, **modules):
        package = self.root / 'fakepkg'
        package.mkdir(exist_ok=True)
        (package / '__init__.py').write_text('')
        for name, body in modules.items():
            (package / (name + '.py')).write_text(textwrap.dedent(body), encoding='utf-8')
        return package

    def passing(self, count):
        return 'import unittest\nclass Case(unittest.TestCase):\n' + ''.join(
            f'    def test_{n}(self):\n        pass\n' for n in range(count))

    def run_main(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = parallel.main(list(argv), root=self.root)
        return code, stdout.getvalue() + stderr.getvalue()

    def assert_process_stopped(self, pid):
        end = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < end:
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
            if not state or state.startswith('Z'):
                return
            time.sleep(POLL_SECONDS)
        self.fail('process is still running: ' + str(pid))

    def test_default_jobs_use_affinity_then_cpu_count(self):
        self.package(test_a=self.passing(4))
        for affinity, cpus, jobs in (({0, 1, 2}, 7, '(3 jobs)'), (AttributeError, 2, '(2 jobs)'),
                                     (AttributeError, None, '(1 job)')):
            with self.subTest(affinity=affinity, cpus=cpus):
                option = {'side_effect': affinity} if affinity is AttributeError else {'return_value': affinity}
                with patch.object(os, 'sched_getaffinity', create=True, **option), \
                        patch.object(os, 'cpu_count', return_value=cpus):
                    code, output = self.run_main()
                self.assertEqual(code, 0, output)
                self.assertIn('Ran 4 tests', output)
                self.assertIn(jobs, output)

    def test_invalid_jobs_are_usage_errors(self):
        self.package(test_a=self.passing(1))
        for value in ('0', '-3', 'abc', '', '1.5'):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as raised, redirect_stderr(io.StringIO()):
                    parallel.main(['-j', value], root=self.root)
                self.assertEqual(raised.exception.code, 2)
        for argv in (['-k', 'x'], ['fakepkg.test_a'], ['-j']):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as raised, redirect_stderr(io.StringIO()):
                    parallel.main(argv, root=self.root)
                self.assertEqual(raised.exception.code, 2)

    def test_all_pass_reports_total_and_jobs(self):
        self.package(test_a=self.passing(3), test_b=self.passing(2))
        code, output = self.run_main('-j', '2')
        self.assertEqual(code, 0, output)
        self.assertRegex(output, r'^Ran 5 tests in [0-9.]+s \(2 jobs\)\nOK\n$')

    def test_single_job_runs_everything_in_one_group(self):
        self.package(test_a=self.passing(3))
        code, output = self.run_main('-j', '1')
        self.assertEqual(code, 0, output)
        self.assertIn('Ran 3 tests in', output)
        self.assertIn('(1 job)', output)

    def test_jobs_beyond_test_count_are_capped(self):
        self.package(test_a=self.passing(3))
        code, output = self.run_main('-j', '500')
        self.assertEqual(code, 0, output)
        self.assertIn('(3 jobs)', output)

    def test_failure_and_error_show_failed_group_output_only(self):
        self.package(test_a='''
            import unittest
            class Case(unittest.TestCase):
                def test_fail(self):
                    self.assertEqual(1, 2, 'FAIL-MARK')
                def test_error(self):
                    raise RuntimeError('ERROR-MARK')
                def test_ok(self):
                    pass
            ''')
        code, output = self.run_main('-j', '3')
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL-MARK', output)
        self.assertIn('ERROR-MARK', output)
        self.assertNotIn('test_ok', output)
        self.assertNotRegex(output, r'(?m)^group \d+: ')
        self.assertRegex(output, r'Ran 3 tests in [0-9.]+s \(3 jobs\)\nFAILED \(failures=1, errors=1\)\n$')

    def test_skip_expected_failure_and_unexpected_success_fail(self):
        bodies = {'skipped': "    def test_x(self):\n        self.skipTest('SKIP-MARK')\n",
                  'expected failures': '    @unittest.expectedFailure\n    def test_x(self):\n        self.fail()\n',
                  'unexpected successes': '    @unittest.expectedFailure\n    def test_x(self):\n        pass\n'}
        for key, body in bodies.items():
            with self.subTest(key=key):
                self.package(test_a='import unittest\nclass Case(unittest.TestCase):\n' + body
                             + '    def test_ok(self):\n        pass\n')
                code, output = self.run_main('-j', '2')
                self.assertEqual(code, 1, output)
                self.assertIn('test_x', output)
                self.assertNotRegex(output, r'(?m)^group \d+: ')
                self.assertRegex(output, r'\nFAILED \(' + key + r'=1\)\n$')

    def test_collection_errors_fail_without_running(self):
        for name, body, reason in (('test_b', 'def broken(:\n', 'SyntaxError'),
                                   ('test_b', 'import missing_module_for_parallel_test\n', 'missing_module_for_parallel_test'),
                                   ('test_b', 'import os\nos._exit(3)\n', 'listing exited with code 3'),
                                   ('test_b', "import os, sys\nsys.stdout.buffer.write(b'\\xff')\nsys.stdout.flush()\nos._exit(3)\n",
                                    'listing exited with code 3\n\ufffd')):
            with self.subTest(reason=reason):
                marker = self.root / 'ran'
                self.package(test_a='import unittest\nfrom pathlib import Path\nclass Case(unittest.TestCase):\n'
                             '    def test_ok(self):\n        Path(' + repr(str(marker)) + ').touch()\n',
                             **{name: body})
                code, output = self.run_main('-j', '2')
                self.assertEqual(code, 1, output)
                self.assertIn('test collection failed', output)
                self.assertIn(reason, output)
                self.assertFalse(marker.exists())

    def test_no_tests_fail(self):
        for files in ({}, {'helper': 'X = 1\n'}, {'test_a': 'import unittest\nclass Case(unittest.TestCase):\n    pass\n'}):
            with self.subTest(files=sorted(files)):
                for path in self.root.rglob('*.py'):
                    path.unlink()
                if files:
                    self.package(**files)
                code, output = self.run_main('-j', '2')
                self.assertEqual(code, 1, output)
                self.assertIn('no tests found', output)

    def test_group_killed_by_signal_fails(self):
        self.package(test_a='''
            import os, signal, unittest
            class Case(unittest.TestCase):
                def test_killed(self):
                    os.kill(os.getpid(), signal.SIGKILL)
                def test_ok(self):
                    pass
            ''')
        code, output = self.run_main('-j', '2')
        self.assertEqual(code, 1, output)
        self.assertIn('group 1: terminated by signal SIGKILL', output)
        self.assertIn('group 1: no valid result file', output)
        self.assertIn('test count mismatch: ran 1, expected 2', output)

    def test_unfinished_group_fails_even_after_summary_like_output(self):
        summary = "sys.stderr.write('Ran 1 test in 0.000s\\n\\nOK\\n'); sys.stderr.flush(); "
        for body, reasons in (('os._exit(0)', ('group 1: no valid result file',)),
                              (summary + 'os._exit(0)', ('group 1: no valid result file',)),
                              (summary + 'os._exit(3)', ('group 1: no valid result file', 'group 1: exited with code 3'))):
            with self.subTest(body=body):
                self.package(test_a='import os, sys, unittest\nclass Case(unittest.TestCase):\n'
                             '    def test_exit(self):\n        ' + body + '\n'
                             '    def test_ok(self):\n        pass\n')
                code, output = self.run_main('-j', '2')
                self.assertEqual(code, 1, output)
                for reason in reasons:
                    self.assertIn(reason, output)
                self.assertIn('test count mismatch: ran 1, expected 2', output)
                self.assertTrue(output.endswith('(2 jobs)\nFAILED\n'), output)

    def test_summary_like_output_does_not_change_result(self):
        self.package(test_a='''
            import unittest
            class Case(unittest.TestCase):
                def test_noise(self):
                    print('Ran 99 tests in 0.000s\\n\\nFAILED (errors=5)\\nOK (custom)\\nOK (skipped=x)')
                def test_ok(self):
                    pass
            ''')
        code, output = self.run_main('-j', '1')
        self.assertEqual(code, 0, output)
        self.assertRegex(output, r'^Ran 2 tests in [0-9.]+s \(1 job\)\nOK\n$')

    def test_exit_code_disagreeing_with_result_fails(self):
        # 結果ファイルを書いた後に終了コードだけが変わる場合を、atexit で作る。
        for body, reason in (("atexit.register(os._exit, 0)\n        self.fail('FAIL-MARK')", 'group 1: exited with code 0'),
                             ('atexit.register(os._exit, 3)', 'group 1: exited with code 3')):
            with self.subTest(reason=reason):
                self.package(test_a='import atexit, os, unittest\nclass Case(unittest.TestCase):\n'
                             '    def test_exit(self):\n        ' + body + '\n')
                code, output = self.run_main('-j', '1')
                self.assertEqual(code, 1, output)
                self.assertIn(reason, output)
                self.assertNotIn('no valid result file', output)

    def test_count_is_checked_per_group(self):
        # 1 組目は 2 件、2 組目は 0 件を流し、合計だけなら一致してしまう割り当てを作る。
        self.package(test_a='''
            import unittest
            class Empty(unittest.TestCase):
                pass
            class Multi(unittest.TestCase):
                def id(self):
                    name = unittest.TestCase.id(self)
                    return __name__ + ('.Multi' if name.endswith('test_a') else '.Empty')
                def test_a(self):
                    pass
                def test_b(self):
                    pass
            ''')
        code, output = self.run_main('-j', '2')
        self.assertEqual(code, 1, output)
        self.assertIn('group 1: ran 2 tests, expected 1', output)
        self.assertIn('group 2: ran 0 tests, expected 1', output)
        self.assertNotIn('test count mismatch', output)
        self.assertIn('(2 jobs)\nFAILED\n', output)

    def test_groups_run_at_the_same_time(self):
        markers = self.root / 'markers'
        markers.mkdir()
        self.package(test_a='''
            import os, time, unittest
            from pathlib import Path
            MARKERS = Path(%r)
            class Case(unittest.TestCase):
                def meet(self, mine, other):
                    (MARKERS / mine).write_text(str(os.getpid()))
                    end = time.monotonic() + %r
                    while not (MARKERS / other).exists() and time.monotonic() < end:
                        time.sleep(%r)
                    self.assertTrue((MARKERS / other).exists(), 'other group did not start')
                def test_one(self):
                    self.meet('one', 'two')
                def test_two(self):
                    self.meet('two', 'one')
            ''' % (str(markers), WAIT_SECONDS, POLL_SECONDS))
        code, output = self.run_main('-j', '2')
        self.assertEqual(code, 0, output)
        self.assertNotEqual((markers / 'one').read_text(), (markers / 'two').read_text())

    def test_start_failure_stops_started_groups_and_restores_handlers(self):
        self.package(test_a='''
            import time, unittest
            class Case(unittest.TestCase):
                def test_0(self):
                    time.sleep(%r)
                def test_1(self):
                    pass
            ''' % LONG_SLEEP_SECONDS)
        real = subprocess.Popen
        started = []

        def popen(*args, **options):
            # 引数の中身に頼らず、先に起動した組が動いている間の次の起動（2 組目）を失敗させる。
            if any(process.poll() is None for process in started):
                raise OSError('injected start failure')
            process = real(*args, **options)
            started.append(process)
            self.addCleanup(self.kill_running, process)
            return process
        handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        with patch.object(subprocess, 'Popen', popen), self.assertRaisesRegex(OSError, 'injected start failure'):
            self.run_main('-j', '2')
        first = started[-1]
        self.assertIsNotNone(first.returncode)
        self.assert_process_stopped(first.pid)
        self.assertEqual({signum: signal.getsignal(signum) for signum in handlers}, handlers)

    def test_undecodable_listing_output_is_accepted(self):
        self.package(test_a="import sys, unittest\nsys.stdout.buffer.write(b'\\xff')\nsys.stdout.flush()\n"
                            "class Case(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        code, output = self.run_main('-j', '1')
        self.assertEqual(code, 0, output)
        self.assertTrue(output.endswith('(1 job)\nOK\n'), output)

    def test_readme_command_runs_the_runner(self):
        # README と同じく、ファイルのパスを渡してスクリプトとして起動する。ROOT はコピー先の親になる。
        runner = self.root / 'tests/parallel.py'
        runner.parent.mkdir()
        shutil.copyfile(RUNNER, runner)
        for body, code, tail in (('pass', 0, r'\(2 jobs\)\nOK\n$'), ("self.fail('FAIL-MARK')", 1, r'\(2 jobs\)\nFAILED \(failures=1\)\n$')):
            with self.subTest(code=code):
                self.package(test_a='import unittest\nclass Case(unittest.TestCase):\n'
                             '    def test_a(self):\n        ' + body + '\n    def test_b(self):\n        pass\n')
                result = subprocess.run([sys.executable, str(runner), '-j', '2'], cwd=self.root,
                                        capture_output=True, text=True, timeout=WAIT_SECONDS)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertRegex(result.stdout, r'Ran 2 tests in [0-9.]+s ' + tail)

    def test_large_output_is_complete(self):
        self.package(test_a='''
            import sys, unittest
            class Case(unittest.TestCase):
                def test_large(self):
                    sys.stdout.write('x' * %d + 'END-MARK\\n')
                    sys.stdout.flush()
                    self.fail('LARGE-FAIL')
                def test_ok(self):
                    pass
            ''' % LARGE_OUTPUT_CHARS)
        code, output = self.run_main('-j', '2')
        self.assertEqual(code, 1, output[-2000:])
        self.assertIn('x' * LARGE_OUTPUT_CHARS + 'END-MARK', output)
        self.assertIn('LARGE-FAIL', output)

    def test_environment_is_passed_to_children(self):
        self.package(test_a="import os, unittest\nclass Case(unittest.TestCase):\n"
                            "    def test_env(self):\n        self.assertEqual(os.environ.get('MR_PARALLEL_PROBE'), 'value')\n")
        for value, expected in (('value', 0), (None, 1)):
            with self.subTest(value=value), patch.dict(os.environ):
                # 変数あり・なしの環境をそれぞれ明示的に作り、成功が子への受け渡しによることを確かめる。
                os.environ.pop('MR_PARALLEL_PROBE', None)
                if value is not None:
                    os.environ['MR_PARALLEL_PROBE'] = value
                code, output = self.run_main('-j', '1')
                self.assertEqual(code, expected, output)

    def test_runner_writes_nothing_into_root(self):
        self.package(test_a=self.passing(2), test_b=self.passing(1))
        before = sorted(p.relative_to(self.root) for p in self.root.rglob('*'))
        code, output = self.run_main('-j', '2')
        self.assertEqual(code, 0, output)
        self.assertEqual(sorted(p.relative_to(self.root) for p in self.root.rglob('*')), before)

    def kill_running(self, process):
        # 後始末では、まだ回収していないプロセスにだけ SIGKILL を送る（PID の再利用で無関係なものを殺さない）。
        if process.poll() is None:
            for kill in (os.killpg, os.kill):
                try:
                    kill(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            process.wait()

    def kill_remaining(self, markers):
        # テストが失敗しても、別セッションで動く子と孫を残さない。動いていて、コマンドラインが
        # このテストのものと一致する PID にだけ送る。
        for prefix, token, kill in (('worker-', 'fakepkg.test_a', os.killpg), ('grandchild-', str(markers), os.kill),
                                    ('listing-', str(self.root), os.kill)):
            for marker in markers.glob(prefix + '*'):
                pid = int(marker.read_text())
                state = subprocess.run(['ps', '-o', 'stat=,args=', '-p', str(pid)],
                                       capture_output=True, text=True).stdout.strip()
                if state and not state.startswith('Z') and token in state:
                    try:
                        kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def start_runner(self, stop_wait=parallel.STOP_WAIT_SECONDS):
        launcher = ('import sys\nfrom pathlib import Path\nimport importlib.util\n'
                    'spec = importlib.util.spec_from_file_location("parallel", sys.argv[1])\n'
                    'parallel = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(parallel)\n'
                    'parallel.STOP_WAIT_SECONDS = float(sys.argv[3])\n'
                    'sys.exit(parallel.main(["-j", "2"], root=Path(sys.argv[2])))\n')
        process = subprocess.Popen([sys.executable, '-c', launcher, str(RUNNER), str(self.root), str(stop_wait)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.kill_running, process)
        return process

    def interrupt(self, signum, ignore_sigint=False, grandchild_ignores_sigint=False,
                  stop_wait=parallel.STOP_WAIT_SECONDS):
        markers = Path(tempfile.mkdtemp(dir=self.root))
        self.addCleanup(self.kill_remaining, markers)
        grandchild = ('import os, signal, time\n'
                      + ('signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
                         if ignore_sigint or grandchild_ignores_sigint else '')
                      # シグナルの設定を終えてから、準備完了を記録する。
                      + 'open(os.path.join(%r, "ready-%%d" %% os.getpid()), "w").close()\n' % str(markers)
                      + 'time.sleep(%r)  # %s' % (LONG_SLEEP_SECONDS, markers))
        self.package(test_a='''
            import os, signal, subprocess, sys, time, unittest
            from pathlib import Path
            MARKERS = Path(%r)
            def setUpModule():
                # ワーカーの PID をテストの開始前に残し、テストが失敗しても後始末で止められるようにする。
                (MARKERS / ('worker-%%d' %% os.getpid())).write_text(str(os.getpid()))
            class Case(unittest.TestCase):
                def wait(self, name):
                    if %r:
                        signal.signal(signal.SIGINT, signal.SIG_IGN)
                    child = subprocess.Popen([sys.executable, '-c', %r])
                    (MARKERS / ('grandchild-%%d' %% child.pid)).write_text(str(child.pid))
                    ready = MARKERS / ('ready-%%d' %% child.pid)
                    end = time.monotonic() + %r
                    while not ready.exists() and time.monotonic() < end:
                        time.sleep(%r)
                    try:
                        # 孫の準備完了を見て、try の内側に入ってから、親が割り込みを送る目印を書く。
                        if ready.exists():
                            (MARKERS / (name + '.tmp')).write_text('%%d %%d' %% (os.getpid(), child.pid))
                            (MARKERS / (name + '.tmp')).rename(MARKERS / name)
                        time.sleep(%r)
                    except KeyboardInterrupt:
                        # 先に SIGINT を受けて、猶予の間に後始末できたことを残す。
                        (MARKERS / ('cleaned-' + name)).write_text('')
                        raise
                def test_one(self):
                    self.wait('one')
                def test_two(self):
                    self.wait('two')
            ''' % (str(markers), ignore_sigint, grandchild, WAIT_SECONDS, POLL_SECONDS, LONG_SLEEP_SECONDS))
        process = self.start_runner(stop_wait)
        end = time.monotonic() + WAIT_SECONDS
        while not all((markers / name).exists() for name in ('one', 'two')) and time.monotonic() < end:
            time.sleep(POLL_SECONDS)
        pids = [int(pid) for name in ('one', 'two') for pid in (markers / name).read_text().split()]
        process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=WAIT_SECONDS)
        # 強制終了は後始末に回し、ここではランナーが止めたかどうかだけを呼び出し元で確かめる。
        return process.returncode, stdout + stderr, pids, markers

    def test_stop_signals_stop_children_and_grandchildren(self):
        for signum, code in ((signal.SIGINT, 130), (signal.SIGTERM, 128 + signal.SIGTERM),
                             (signal.SIGHUP, 128 + signal.SIGHUP)):
            with self.subTest(signal=signum.name):
                returncode, output, pids, markers = self.interrupt(signum)
                self.assertEqual(returncode, code, output)
                self.assertIn('interrupted by ' + signum.name, output)
                self.assertEqual(len(pids), 4)
                for pid in pids:
                    self.assert_process_stopped(pid)
                for name in ('one', 'two'):
                    self.assertTrue((markers / ('cleaned-' + name)).exists(), name)

    def test_groups_ignoring_sigint_are_killed_after_grace_period(self):
        returncode, output, pids, markers = self.interrupt(signal.SIGINT, ignore_sigint=True, stop_wait=.5)
        self.assertEqual(returncode, 130, output)
        self.assertEqual(len(pids), 4)
        for pid in pids:
            self.assert_process_stopped(pid)

    def test_grandchildren_left_after_workers_exit_are_killed(self):
        returncode, output, pids, markers = self.interrupt(signal.SIGINT, grandchild_ignores_sigint=True)
        self.assertEqual(returncode, 130, output)
        for name in ('one', 'two'):
            self.assertTrue((markers / ('cleaned-' + name)).exists(), name)
        for pid in pids:
            self.assert_process_stopped(pid)

    def test_stop_signal_during_listing_stops_listing_and_descendants(self):
        markers = Path(tempfile.mkdtemp(dir=self.root))
        self.addCleanup(self.kill_remaining, markers)
        grandchild = 'import time\ntime.sleep(%r)  # %s' % (LONG_SLEEP_SECONDS, markers)
        # import の最中に孫を起動して止まるモジュールで、列挙の途中に割り込む。
        self.package(test_a='''
            import os, subprocess, sys, time
            from pathlib import Path
            MARKERS = Path(%r)
            (MARKERS / ('listing-%%d' %% os.getpid())).write_text(str(os.getpid()))
            child = subprocess.Popen([sys.executable, '-c', %r])
            (MARKERS / ('grandchild-%%d' %% child.pid)).write_text(str(child.pid))
            (MARKERS / 'ready.tmp').write_text('%%d %%d' %% (os.getpid(), child.pid))
            (MARKERS / 'ready.tmp').rename(MARKERS / 'ready')
            time.sleep(%r)
            ''' % (str(markers), grandchild, LONG_SLEEP_SECONDS))
        process = self.start_runner()
        end = time.monotonic() + WAIT_SECONDS
        while not (markers / 'ready').exists() and time.monotonic() < end:
            time.sleep(POLL_SECONDS)
        pids = [int(pid) for pid in (markers / 'ready').read_text().split()]
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=WAIT_SECONDS)
        self.assertEqual(process.returncode, 128 + signal.SIGTERM, stdout + stderr)
        self.assertIn('interrupted by SIGTERM', stdout + stderr)
        for pid in pids:
            self.assert_process_stopped(pid)

    def test_descendants_of_unfinished_group_are_killed(self):
        markers = Path(tempfile.mkdtemp(dir=self.root))
        self.addCleanup(self.kill_remaining, markers)
        grandchild = 'import time\ntime.sleep(%r)  # %s' % (LONG_SLEEP_SECONDS, markers)
        self.package(test_a='''
            import os, subprocess, sys, unittest
            from pathlib import Path
            MARKERS = Path(%r)
            class Case(unittest.TestCase):
                def test_exit(self):
                    child = subprocess.Popen([sys.executable, '-c', %r])
                    (MARKERS / ('grandchild-%%d' %% child.pid)).write_text(str(child.pid))
                    os._exit(3)
            ''' % (str(markers), grandchild))
        code, output = self.run_main('-j', '1')
        self.assertEqual(code, 1, output)
        self.assertIn('group 1: no valid result file', output)
        self.assertIn('group 1: exited with code 3', output)
        pids = [int(marker.read_text()) for marker in markers.glob('grandchild-*')]
        self.assertEqual(len(pids), 1)
        self.assert_process_stopped(pids[0])


if __name__ == '__main__':
    unittest.main()
