#!/usr/bin/env python3
"""Run the unittest suite in parallel groups of separate processes.

Usage: python3 tests/parallel.py [-j N]
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PATTERN = 'test*.py'
STOP_WAIT_SECONDS = 5
# 受けたら子のプロセスグループを止めて、128＋シグナル番号で終わるシグナル。
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# unittest の要約に出る件数の名前。成功扱いにしない内訳を含めて、この順で表示する。
COUNT_KEYS = ('failures', 'errors', 'skipped', 'expected failures', 'unexpected successes')
# 列挙は別プロセスで行い、テストモジュールの import がランナーの状態に残らないようにする。
LIST_SCRIPT = '''import json, sys, unittest
root, pattern, output = sys.argv[1:]
loader = unittest.TestLoader()
suite = loader.discover(root, pattern=pattern, top_level_dir=root)
def walk(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from walk(test)
        else:
            yield test
ids = [test.id() for test in walk(suite)]
with open(output, 'w', encoding='utf-8') as stream:
    json.dump({'ids': ids, 'errors': list(loader.errors)}, stream)
'''
# 組の結果は出力の文字列ではなく、TestResult の件数を結果ファイルに書いて渡す。
# 実行が最後まで終わったときだけファイルができるよう、一時名に書いてから置き換える。
WORKER_SCRIPT = '''import json, os, sys, unittest
output, names = sys.argv[1], sys.argv[2:]
suite = unittest.defaultTestLoader.loadTestsFromNames(names)
runner = unittest.TextTestRunner(verbosity=2, warnings=None if sys.warnoptions else 'default')
result = runner.run(suite)
counts = {'ran': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
          'skipped': len(result.skipped), 'expected failures': len(result.expectedFailures),
          'unexpected successes': len(result.unexpectedSuccesses)}
with open(output + '.tmp', 'w', encoding='utf-8') as stream:
    json.dump(counts, stream)
os.replace(output + '.tmp', output)
sys.exit(0 if result.wasSuccessful() else 1)
'''


class Stopped(BaseException):
    """Raised when the runner receives one of STOP_SIGNALS."""

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


class StopRequests:
    """Turn STOP_SIGNALS into Stopped, holding them while workers are being registered."""

    def __init__(self):
        self.holding = False
        self.pending = None

    def handle(self, signum, frame):
        if self.pending is None:
            self.pending = signum
        if not self.holding:
            # 2 回目以降のシグナルで停止処理や後始末を飛ばさないよう、投げる前に保留へ切り替える。
            self.holding = True
            raise Stopped(signum)

    def hold(self):
        self.holding = True

    def release(self):
        # 確認と解除の間に届いたシグナルを取りこぼさないよう、先に解除してから確かめる。
        self.holding = False
        if self.pending is not None:
            self.holding = True
            raise Stopped(self.pending)


def default_jobs():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a positive integer') from None
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('-j', dest='jobs', type=positive_int, default=None,
                   help='number of parallel processes (default: usable CPUs)')
    return p


def split(ids, jobs):
    count = min(jobs, len(ids))
    return [ids[index::count] for index in range(count)]


def collect(root, requests):
    processes = []
    with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryFile() as stdout, \
            tempfile.TemporaryFile() as stderr:
        output = Path(temporary) / 'ids.json'
        try:
            # ワーカーと同じく別のプロセスグループで起動し、中断時は import 中に起動された子孫ごと止める。
            requests.hold()
            processes.append(subprocess.Popen([sys.executable, '-c', LIST_SCRIPT, str(root), PATTERN, str(output)],
                                              cwd=root, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                              start_new_session=True))
            requests.release()
            returncode = processes[0].wait()
            if returncode != 0 or not output.is_file():
                stdout.seek(0)
                stderr.seek(0)
                text = (stdout.read() + stderr.read()).decode('utf-8', 'replace')
                return [], [f'listing exited with code {returncode}\n' + text]
            value = json.loads(output.read_text(encoding='utf-8'))
        except BaseException:
            requests.hold()
            stop_groups(processes)
            raise
    return value['ids'], value['errors']


def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def stop_groups(processes):
    """Interrupt each process group, then kill groups that outlive the grace period."""
    for process in processes:
        signal_group(process, signal.SIGINT)
    end = time.monotonic() + STOP_WAIT_SECONDS
    for process in processes:
        try:
            process.wait(timeout=max(0, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for process in processes:
        # 子が先に終わっても孫が残ることがあるので、グループ全体に送る。
        signal_group(process, signal.SIGKILL)
        process.wait()


def read_result(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    keys = ('ran',) + COUNT_KEYS
    if not isinstance(value, dict) or set(value) != set(keys) or not all(
            type(value[key]) is int and value[key] >= 0 for key in keys):
        return None
    return value


def run_groups(root, groups, requests):
    processes, outputs = [], []
    with tempfile.TemporaryDirectory() as temporary:
        results = [Path(temporary) / f'group-{number}.json' for number in range(1, len(groups) + 1)]
        try:
            # 起動から一覧への登録までに受けたシグナルは保留し、全部を登録してから止める。
            requests.hold()
            for ids, result in zip(groups, results):
                # 出力はパイプで詰まらないよう、OS の一時ディレクトリの無名ファイルで受ける。
                output = tempfile.TemporaryFile()
                outputs.append(output)
                processes.append(subprocess.Popen(
                    [sys.executable, '-u', '-c', WORKER_SCRIPT, str(result), *ids], cwd=root,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True))
            requests.release()
            for process in processes:
                process.wait()
            finished = []
            for process, output, result in zip(processes, outputs, results):
                output.seek(0)
                value = read_result(result)
                if process.returncode != 0 or value is None:
                    # 失敗・未完了の組は、テストが起動した子孫が残りうるのでグループごと止める。
                    # 成功した組には送らない（回収済みの PID の再利用で無関係なプロセスを止めないため）。
                    signal_group(process, signal.SIGKILL)
                finished.append((process.returncode, output.read().decode('utf-8', 'replace'), value))
            return finished
        except BaseException:
            # 止めている最中のシグナルで子を取り残さないよう、保留したまま止める。
            requests.hold()
            stop_groups(processes)
            raise
        finally:
            for output in outputs:
                output.close()


def group_problems(ids, returncode, result):
    problems = []
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = str(-returncode)
        problems.append('terminated by signal ' + name)
    if result is None:
        problems.append('no valid result file')
    if returncode > 0 and (result is None or returncode != expected_returncode(result)):
        problems.append('exited with code ' + str(returncode))
    elif returncode == 0 and result is not None and expected_returncode(result) != 0:
        problems.append('exited with code 0')
    if result is not None and result['ran'] != len(ids):
        problems.append(f'ran {result["ran"]} tests, expected {len(ids)}')
    return problems


def expected_returncode(result):
    # TestResult.wasSuccessful と同じ条件で、ワーカーの終了コードと食い違わないかを確かめる。
    return 1 if result['failures'] or result['errors'] or result['unexpected successes'] else 0


def report(groups, results, expected, elapsed):
    ran, totals, problems = 0, dict.fromkeys(COUNT_KEYS, 0), []
    for number, (ids, (returncode, text, result)) in enumerate(zip(groups, results), 1):
        own = group_problems(ids, returncode, result)
        counted = result is not None and any(result[key] for key in COUNT_KEYS)
        if result is not None:
            ran += result['ran']
            for key in COUNT_KEYS:
                totals[key] += result[key]
        if own or counted:
            print(f'===== group {number}/{len(groups)} (exit {returncode}) =====')
            sys.stdout.write(text if text.endswith('\n') or not text else text + '\n')
            problems.extend(f'group {number}: {problem}' for problem in own)
    if ran != expected:
        problems.append(f'test count mismatch: ran {ran}, expected {expected}')
    for problem in problems:
        print(problem)
    jobs = len(groups)
    print(f'Ran {ran} test{"" if ran == 1 else "s"} in {elapsed:.1f}s ({jobs} job{"" if jobs == 1 else "s"})')
    counts = ', '.join(f'{key}={value}' for key, value in totals.items() if value)
    if problems or counts:
        print('FAILED' + (f' ({counts})' if counts else ''))
        return 1
    print('OK')
    return 0


def main(argv=None, root=ROOT):
    args = parser().parse_args(argv)
    jobs = default_jobs() if args.jobs is None else args.jobs
    requests = StopRequests()
    previous = {signum: signal.signal(signum, requests.handle) for signum in STOP_SIGNALS}
    try:
        started = time.monotonic()
        ids, errors = collect(root, requests)
        if errors:
            print('test collection failed:', file=sys.stderr)
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        if not ids:
            print('no tests found under ' + str(root), file=sys.stderr)
            return 1
        groups = split(ids, jobs)
        results = run_groups(root, groups, requests)
        return report(groups, results, len(ids), time.monotonic() - started)
    except Stopped as stopped:
        print('interrupted by ' + signal.Signals(stopped.signum).name, file=sys.stderr)
        return 128 + stopped.signum
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    sys.exit(main())
