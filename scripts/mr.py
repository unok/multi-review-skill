#!/usr/bin/env python3
"""Mechanical operations for multi-review. Decisions stay with the caller."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

SKILL = Path(__file__).resolve().parents[1]
TREE_ID = r'(?:[0-9a-f]{40}|[0-9a-f]{64})'
MAX_OPTIONAL_SPECIALISTS = 4
DEFAULT_SPECIALISTS = ('pattern-consistency', 'naming-symmetry')
MAX_SPECIALISTS = len(DEFAULT_SPECIALISTS) + MAX_OPTIONAL_SPECIALISTS
MAX_ITERATIONS = 5
DIFF_BUDGET = 200000
REVIEW_TIMEOUT = 600
GUARD_TIMEOUT = 600
JA_TIMEOUT = 1800
JA_BATCH_LIMIT = 100000
NEAR_FIX_LINES = 5
STDERR_TAIL_CHARS = 4000
MAX_ATTEMPTS = 2
# Two general + six specialist jobs; ja batches may wait in the queue.
MAX_PARALLEL = 8
KILL_WAIT_SECONDS = 1
EXIT_AWAITING = 2
EXIT_CANDIDATES = 10
LEDGER_FILE = 'findings.json'
RUN_FILE = 'run.json'
FIXDIFF_FILE = 'fixdiff.json'
FIX_DIFF_FILE = 'fix.diff'
SNAPSHOT_MODE_FILE = 'snapshot.mode'
LEDGER_MD_FILE = 'ledger.md'
STATUS_AWAITING = 'awaiting_instruction'
NO_LINE_MARK = '［行なし］'
FILE_HEADER = '=== FILE: '
# Exclusion rules copied from ja-review/SKILL.md, 2026-11-10 (see JOB_MODELS).
JA_EXCLUDED_DIRS = ('dist', 'build')
JA_EXCLUDED_FILES = ('pnpm-lock.yaml', 'package-lock.json', 'npm-shrinkwrap.json', 'yarn.lock', 'bun.lockb')
JA_TEXT = re.compile(r'[\u3040-\u30ff\u4e00-\u9fff]')
SPAN_SEPARATOR = ', '
REVIEWERS = ('claude', 'codex', 'ja-review')
SEVERITIES = {'high': '高', 'medium': '中', 'low': '低'}
NEXT = {name: name for name in ('finish', 'limit', 'rereview', 'abort', 'caller_decision', 'awaiting_instruction')}
DOWNSTREAM = {'prompts': ('prompts.json', 'review.json', 'collected.json', 'collected.md',
                          'target.diff', 'target.json', 'ja-review'),
              'review': ('review.json', 'collected.json', 'collected.md'),
              'collect': ('collected.json', 'collected.md')}
# jev-guard treats section risk at or above this probability as new risk.
SECTION_RISK_THRESHOLD = .5
DIFF_OPTIONS = ('--no-ext-diff', '--no-textconv', '--no-color',
                '--src-prefix=a/', '--dst-prefix=b/')
JOB_MODELS = {
    'default': {'claude': {'general': ('claude-opus-5-5', 'medium'),
                           'specialist': ('claude-opus-5-5', 'high')},
                'codex': {'general': ('gpt-6.1-sol', 'medium')}},
    'codex': {'claude': {'general': ('claude-opus-5-5', 'high')},
              'codex': {'general': ('gpt-6.1-sol', 'medium'),
                        'specialist': ('gpt-6.1-sol', 'high')}},
    # Model copied from ~/.claude/skills/ja-review/SKILL.md, 2026-11-10.
    'ja-review': {'agy': {'general': ('gemini-3.8-flash-high', None)}},
}
STATES = ('unresolved', 'fixed', 'on_hold', 'rejected')
LABELS = ('未解決', '修正済み', '保留', '却下')
FIELDS = {'id', 'severity', 'source', 'file', 'line', 'problem', 'fixSummary',
          'fixedIteration', 'status', 'reason'}


class Failure(Exception):
    pass


EXPECTED_ERRORS = (Failure, OSError, ValueError, subprocess.TimeoutExpired)


def require(condition, message):
    if not condition:
        raise Failure(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def positive_int(value):
    return type(value) is int and value > 0


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def parse_json(text):
    return json.loads(text, object_pairs_hook=unique_object,
                      parse_constant=lambda value: require(False, 'invalid JSON number: ' + value))


def read_json(path):
    return parse_json(path.read_text(encoding='utf-8'))


def load_run(root):
    run = read_json(root / RUN_FILE)
    require(isinstance(run, dict) and
            all(nonempty(run.get(key)) for key in ('cwd', 'jaSkill', 'baseSummary')) and
            run.get('mode') in ('paths', 'uncommitted', 'base') and
            run.get('profile') in ('default', 'codex') and
            all(type(run.get(key)) is bool for key in ('git', 'jaReview')) and
            all(isinstance(run.get(key), list) and all(nonempty(v) for v in run[key])
                for key in ('paths', 'roles', 'excluded')) and
            set(run['excluded']) <= set(REVIEWERS) and
            positive_int(run.get('diffBudget')) and
            all(type(run.get(key)) in (int, float) and run[key] > 0 and math.isfinite(run[key])
                for key in ('timeout', 'jaTimeout')) and
            type(run.get('taskSequence')) is int and run['taskSequence'] >= 0 and
            isinstance(run.get('tasks'), dict) and
            (run.get('base') is None or nonempty(run['base'])) and
            (run['mode'] != 'base' or nonempty(run.get('base'))), 'invalid run state')
    require(all(nonempty(name) and isinstance(entry, dict) and positive_int(entry.get('sequence')) and
                'hash' in entry and (entry['hash'] is None or nonempty(entry['hash']))
                for name, entry in run['tasks'].items()) and
            isinstance(run.get('imports', {}), dict) and
            all(nonempty(k) and nonempty(v) for k, v in run.get('imports', {}).items()),
            'invalid run state')
    role_list(','.join(run['roles']))
    return run


def load_review_state(directory, filename):
    state = read_json(directory / filename)
    require(isinstance(state, dict) and isinstance(state.get('reviewers'), list),
            'invalid ' + filename)
    if filename == 'prompts.json':
        require(nonempty(state.get('cwd')) and positive_int(state.get('iteration')), 'invalid prompts.json')
    for job in state['reviewers']:
        require(isinstance(job, dict) and
                all(nonempty(job.get(key)) for key in ('cli', 'source', 'model', 'prompt', 'output')) and
                (nonempty(job.get('effort')) or job['cli'] == 'agy' and job.get('effort') is None) and
                type(job.get('timeout')) in (int, float) and job['timeout'] > 0 and math.isfinite(job['timeout']),
                'invalid ' + filename + ' reviewer')
        if filename == 'review.json':
            require(job.get('status') in ('success', 'absent') and
                    (job['status'] != 'success' or job['cli'] in REVIEW_CLIS), 'invalid review.json reviewer')
    return state


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = data.encode('utf-8') if isinstance(data, str) else data
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            name = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def save_json(path, value):
    write_atomic(path, json.dumps(value, indent=2) + '\n')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run_command(argv, cwd, *, data=None, env=None, allowed=(0,), timeout=None):
    if timeout is None:
        result = subprocess.run(argv, cwd=cwd, input=data, capture_output=True, env=env)
    else:
        result = completed_process(argv, timeout, *run_process_group(argv, cwd, data, timeout, env=env))
    require(result.returncode in allowed,
            f'{argv[0]} exited {result.returncode}: ' + result.stderr.decode('utf-8', 'replace'))
    return result


def completed_process(argv, timeout, code, stdout, stderr, reason):
    if reason == 'timeout':
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
    require(reason is None, reason)
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


def git(cwd, *args, **kwargs):
    return run_command(['git', *args], cwd, **kwargs).stdout


def plain_diff(cwd, subcommand, *args, paths=()):
    pathspec = ['--', *paths] if paths else []
    return git(cwd, '--literal-pathspecs', '-c', 'core.quotePath=true', '-c', 'diff.suppressBlankEmpty=false',
               subcommand, *DIFF_OPTIONS, *args, *pathspec)


def validate_ledger(value):
    require(isinstance(value, dict) and set(value) == {'reviewId', 'iteration', 'findings'},
            'invalid ledger fields')
    require(nonempty(value['reviewId']) and positive_int(value['iteration']), 'invalid ledger identity')
    require(isinstance(value['findings'], list), 'findings must be an array')
    seen = set()
    for item in value['findings']:
        require(isinstance(item, dict) and set(item) <= FIELDS, 'unknown finding fields')
        require({'id', 'severity', 'source', 'file', 'line', 'problem', 'status'} <= set(item),
                'missing finding fields')
        require(all(nonempty(item[key]) for key in ('id', 'source', 'file', 'problem')),
                'empty finding string')
        require(item['id'] not in seen, 'duplicate finding ID')
        seen.add(item['id'])
        require(item['severity'] in SEVERITIES.values() and item['status'] in STATES,
                'invalid severity or status')
        require(positive_int(item['line']), 'line must be a positive integer')
        for key in ('reason', 'fixSummary'):
            require(key not in item or isinstance(item[key], str), 'invalid ' + key)
        if 'fixedIteration' in item:
            require(item['status'] == 'fixed' and positive_int(item['fixedIteration']) and
                    item['fixedIteration'] <= value['iteration'], 'invalid fixedIteration')
        if item['status'] in ('rejected', 'on_hold'):
            require(nonempty(item.get('reason')), 'rejection/hold reason required')
        else:
            require('reason' not in item, 'reason is only for rejected/on_hold')
        if item['status'] == 'fixed':
            require(nonempty(item.get('fixSummary')) and 'fixedIteration' in item,
                    'fixed finding needs summary and iteration')
    return value


def load_ledger(root):
    return validate_ledger(read_json(root / LEDGER_FILE))


def render(root, ledger):
    output = root / LEDGER_MD_FILE
    if shutil.which('jev-guard'):
        temporary = root / 'ledger.render.md'
        temporary.unlink(missing_ok=True)
        run_command(['jev-guard', 'ledger', 'render', '--findings', str(root / LEDGER_FILE),
                     '--out', str(temporary)], root, timeout=GUARD_TIMEOUT)
        require(temporary.is_file(), 'ledger render produced no output')
        os.replace(temporary, output)
    else:
        sections = []
        for state, label in zip(STATES, LABELS):
            items = sorted((i for i in ledger['findings'] if i['status'] == state),
                           key=lambda i: tuple((1, int(p)) if p.isdecimal() else (0, p)
                                               for p in re.split(r'(\d+)', i['id'])))
            rows = []
            for item in items:
                fields = [('ID', item['id']), ('重要度', item['severity']),
                          ('出所', item['source']), ('ファイル', f"{display_path(item['file'])}:{item['line']}"),
                          ('問題', item['problem'])]
                if 'reason' in item:
                    fields.append(('理由', item['reason']))
                if item['status'] == 'fixed':
                    fields.append(('修正イテレーション', str(item['fixedIteration'])))
                rows.append('- ' + ' | '.join(key + ': ' + re.sub(
                    r'\s*(?:\r\n|[\r\n\u2028\u2029])\s*', ' ', value)
                    for key, value in fields))
            sections.append('## ' + label + '\n\n' + '\n'.join(rows or ['なし']))
        write_atomic(output, '\n\n'.join([
            '# 台帳', '<!-- jev-guard ledger render で生成。編集は findings.json に対して行う -->',
            *sections]) + '\n')


def normalize_finding(item):
    require('line' in item and nonempty(item.get('problem')), 'line and problem required')
    item = dict(item)
    if item.get('line') in (None, '-'):
        # jev-guard's ledger schema requires a positive integer, so null cannot be stored.
        item['line'] = 1
        if not item.get('problem', '').startswith(NO_LINE_MARK):
            item['problem'] = NO_LINE_MARK + item.get('problem', '')
    return item


def add_findings(source, ledger, run):
    ids = []
    incoming = read_json(source)
    incoming = incoming if isinstance(incoming, list) else [incoming]
    for raw in incoming:
        require(isinstance(raw, dict), 'finding must be an object')
        item = normalize_finding(raw)
        item.setdefault('status', 'unresolved')
        receipt = str(ledger['iteration']) + ':' + digest(
            json.dumps(item, sort_keys=True).encode())
        previous_id = run.get('imports', {}).get(receipt)
        if 'id' not in item and previous_id:
            require(any(i['id'] == previous_id for i in ledger['findings']), 'import receipt without finding')
            ids.append(previous_id)
            continue
        if 'id' not in item:
            numbers = [int(i['id'][1:]) for i in ledger['findings']
                       if re.fullmatch(r'F\d+', i['id'])]
            item['id'] = 'F' + str(max(numbers, default=0) + 1)
        ledger['findings'].append(item)
        if 'id' not in raw:
            run.setdefault('imports', {})[receipt] = item['id']
        ids.append(item['id'])
    return ids


def change_findings(args, ledger):
    action, ids = args.action, []
    require(args.ids, 'finding IDs required')
    require(set(args.ids) <= {i['id'] for i in ledger['findings']}, 'unknown finding ID')
    if action == 'update':
        patch = read_json(args.input)
        require(isinstance(patch, dict) and set(patch) <=
                {'severity', 'source', 'file', 'line', 'problem'}, 'invalid update fields')
        if 'line' in patch:
            require(patch['line'] in (None, '-') or positive_int(patch['line']),
                    'line must be a positive integer, null or "-"')
    for item in ledger['findings']:
        if item['id'] not in args.ids:
            continue
        ids.append(item['id'])
        if action == 'update':
            unknown_line = item['line'] == 1 and item['problem'].startswith(NO_LINE_MARK)
            item.update(patch)
            if positive_int(patch.get('line')):
                item['problem'] = item['problem'].removeprefix(NO_LINE_MARK)
            elif unknown_line:
                item['line'] = None
            item.update(normalize_finding(item))
            continue
        if action == 'fix':
            require(nonempty(args.summary), 'fix summary required')
            require(args.iteration == ledger['iteration'], 'fix iteration must be current')
            item.update(status='fixed', fixSummary=args.summary, fixedIteration=args.iteration)
            item.pop('reason', None)
        else:
            require(nonempty(args.reason), 'reason required')
            item.update(status='rejected' if action == 'reject' else 'on_hold', reason=args.reason)
            item.pop('fixedIteration', None)
            item.pop('fixSummary', None)
    return ids


def save_ledger_and_run(root, ledger, run):
    original = (root / LEDGER_FILE).read_bytes()
    save_json(root / LEDGER_FILE, validate_ledger(ledger))
    try:
        save_json(root / RUN_FILE, run)
    except BaseException:
        write_atomic(root / LEDGER_FILE, original)
        raise


def ledger_action(args, root, run):
    ledger = load_ledger(root)
    ids = []
    if args.action in ('add', 'update'):
        require(args.input is not None, '--input required for ' + args.action)
    if args.action == 'add':
        ids = add_findings(args.input, ledger, run)
    elif args.action != 'render':
        ids = change_findings(args, ledger)
    validate_ledger(ledger)
    if args.action != 'render':
        save_ledger_and_run(root, ledger, run)
    render(root, ledger)
    return {'ids': ids}


def safe_relative(cwd, name):
    path = Path(name)
    require(not path.is_absolute() and '..' not in path.parts and name not in ('', '.'),
            'unsafe relative file: ' + display_path(name))
    # A symlink itself is a file; do not follow directory links out of the root.
    require(path.parent == Path('.') or (cwd / path.parent).resolve().is_relative_to(cwd),
            'file parent escapes cwd: ' + display_path(name))
    return cwd / path


def head_exists(cwd):
    return run_command(['git', 'rev-parse', '--verify', 'HEAD'], cwd, allowed=(0, 128)).returncode == 0


def nul_paths(data):
    return [os.fsdecode(p) for p in data.split(b'\0') if p]


def status_records(cwd):
    return git(cwd, 'status', '--porcelain=v1', '-z', '--untracked-files=all')


def expand_path(cwd, path):
    if not path.is_dir() or path.is_symlink():
        return [str(path.relative_to(cwd))]
    return [str(p.relative_to(cwd)) for p in path.rglob('*')
            if '.git' not in p.relative_to(cwd).parts and (p.is_file() or p.is_symlink())]


def scope_files(run, *, include_changes=False):
    cwd = Path(run['cwd'])
    files = []
    if run['mode'] == 'paths':
        for name in run['paths']:
            path = cwd / name
            if run['git'] and path.is_dir() and not path.is_symlink():
                files.extend(nul_paths(git(cwd, '--literal-pathspecs', 'ls-files', '-z', '--cached', '--others',
                                           '--exclude-standard', '--', name)))
            else:
                files.append(name)
    else:
        records = status_records(cwd).split(b'\0')
        i = 0
        while i < len(records):
            record = records[i]
            i += 1
            if not record:
                continue
            files.append(os.fsdecode(record[3:]))
            if b'R' in record[:2] or b'C' in record[:2]:
                files.append(os.fsdecode(records[i]))
                i += 1
        if run['mode'] == 'base':
            files.extend(nul_paths(git(cwd, 'diff', '--name-only', '-z', run['base'] + '...HEAD')))
    if include_changes and run['mode'] == 'paths' and run['git'] and not head_exists(cwd):
        files.extend(scope_files(dict(run, mode='uncommitted')))
    return sorted({file for name in files for file in expand_path(cwd, cwd / name)})


def save_worktree_tree(cwd, directory, name):
    index = directory / (name + '.index')
    index.unlink(missing_ok=True)
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    if head_exists(cwd):
        git(cwd, 'read-tree', 'HEAD', env=env)
    else:
        git(cwd, 'read-tree', '--empty', env=env)
    git(cwd, 'add', '-A', env=env)
    require(not any(record.startswith(b'160000 ') for record in
                    git(cwd, 'ls-files', '--stage', '-z', env=env).split(b'\0')),
            'cannot snapshot gitlink: nested worktree content is not saved in a tree')
    ignored = git(cwd, 'ls-files', '-z', '-c', '-i', '--exclude-standard')
    git(cwd, 'update-index', '--add', '--remove', '-z', '--stdin', data=ignored, env=env)
    value = git(cwd, 'write-tree', env=env).strip()
    value = os.fsdecode(value)
    require(re.fullmatch(TREE_ID, value), 'invalid tree ID')
    write_atomic(directory / (name + '.tree'), value + '\n')
    return value


def snapshot(args, root, run):
    directory = args.directory.resolve()
    iteration = iteration_number(directory)
    require(not (directory / SNAPSHOT_MODE_FILE).exists(), 'snapshot already saved')
    require(not (directory / 'snapshot.json').exists(), 'snapshot already attempted')
    cwd = Path(run['cwd'])
    error = None
    if run['git']:
        try:
            save_worktree_tree(cwd, directory, 'snapshot')
        except EXPECTED_ERRORS as problem:
            error = str(problem)
            (directory / 'snapshot.tree').unlink(missing_ok=True)
        else:
            write_atomic(directory / SNAPSHOT_MODE_FILE, 'tree\n')
            return {'mode': 'tree'}
    files, _, _ = iteration_files(directory, run, iteration)
    excluded = []
    for item in load_ledger(root)['findings']:
        if item['status'] == 'unresolved':
            names = [item['file']]
            for position in other_locations(item['problem'])[1]:
                try:
                    names.append(location(position)[0])
                except Failure as problem:
                    excluded.append(str(problem))
            for name in names:
                try:
                    safe_relative(cwd, name)
                except Failure as problem:
                    excluded.append(str(problem))
                else:
                    files.extend(expand_path(cwd, cwd / name))
    copied = []
    for name in sorted(set(files)):
        source = safe_relative(cwd, name)
        if not source.exists() and not source.is_symlink():
            continue
        require(not (source.is_dir() and not source.is_symlink()), 'cannot copy directory: ' + display_path(name))
        copy_root = directory / 'snapshot'
        require(not copy_root.is_symlink(), 'snapshot directory must not be a link')
        destination = safe_relative(copy_root, name)
        require(not any(parent.is_symlink() for parent in destination.parents
                        if parent.is_relative_to(copy_root)), 'snapshot parent must not be a link: ' + display_path(name))
        destination.parent.mkdir(parents=True, exist_ok=True)
        # A retry may replace a link left by a failed copy; never write through it.
        destination.unlink(missing_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
        copied.append(name)
    write_atomic(directory / 'snapshot.files', b''.join(os.fsencode(p) + b'\0' for p in copied))
    save_json(directory / 'snapshot.json', {'treeError': error, 'excluded': excluded})
    write_atomic(directory / SNAPSHOT_MODE_FILE, 'copy\n')
    return {'mode': 'copy', 'treeError': error}


def location(text):
    match = re.fullmatch(r'(.+):(\d+|-)(?:-\d+)?', text.strip())
    require(match is not None, 'invalid location: ' + text)
    return git_path(match[1]), None if match[2] == '-' else int(match[2])


def other_locations(text):
    match = re.fullmatch(r'(.+?)（他: (.+)）', text, re.S)
    if not match:
        return text, []
    positions = ['']
    for token in re.findall(r'"(?:[^"\\]|\\.)*"|[,、]|[^",、]+|"', match[2]):
        if token in (',', '、'):
            positions.append('')
        else:
            positions[-1] += token
    return match[1], positions


def display_path(name):
    # JSON quoting preserves undecodable POSIX bytes as reversible surrogate escapes.
    return json.dumps(name) if any(0xD800 <= ord(char) <= 0xDFFF for char in name) else name


def saved_mode(directory):
    mode = (directory / SNAPSHOT_MODE_FILE).read_text(encoding='utf-8').strip()
    require(mode in ('tree', 'copy'), 'invalid snapshot.mode')
    return mode


def quote_git_path(name):
    # Keep spaces quoted: GNU diff labels have no Git-style trailing tab.
    if all(32 < byte < 127 and byte not in (ord('"'), ord('\\')) for byte in os.fsencode(name)):
        return name
    return '"' + ''.join(chr(byte) if 32 <= byte < 127 and byte not in (ord('"'), ord('\\'))
                         else f'\\{byte:03o}' for byte in os.fsencode(name)) + '"'


def copy_file_diff(directory, cwd, name):
    saved_path = directory / 'snapshot' / name
    current = safe_relative(cwd, name)
    kinds = [path.is_symlink() for path in (saved_path, current)]
    modes = ['120000' if path.is_symlink() else
             '100755' if path.exists() and path.lstat().st_mode & 0o111 else '100644'
             for path in (saved_path, current)]
    with tempfile.TemporaryDirectory() as temporary:
        paths = []
        for index, path in enumerate((saved_path, current)):
            if path.is_symlink():
                saved = Path(temporary) / str(index)
                saved.write_bytes(os.fsencode(os.readlink(path)) + b'\n')
                paths.append(saved)
            else:
                paths.append(path)
        old_label = quote_git_path('a/' + name)
        new_label = quote_git_path('b/' + name)
        result = run_command(['diff', '-uN', '--label', old_label,
                              '--label', new_label,
                              '--', *map(str, paths)], cwd, allowed=(0, 1))
        patch = result.stdout
        if modes[0] != modes[1]:
            mode_lines = ('deleted file mode ' + modes[0] + '\n' if not current.exists() and not current.is_symlink()
                          else 'old mode ' + modes[0] + '\nnew mode ' + modes[1] + '\n')
            patch = (f'diff --git {old_label} {new_label}\n' + mode_lines).encode() + patch
        content = paths[1].read_bytes() if paths[1].is_file() else b''
        return patch, {'present': current.exists() or current.is_symlink(),
                       'symlink': kinds[1], 'executable': modes[1] == '100755', 'hash': digest(content)}


def fixdiff(args, root, run):
    directory = args.directory.resolve()
    previous = load_fixdiff_state(directory) if (directory / FIXDIFF_FILE).exists() else None
    require(previous is None or not previous['afterCheck'],
            'after-check already completed')
    if args.after_check:
        require(previous is not None,
                'after-check requires one fresh fixdiff and may run only once')
    # A failed regeneration must not leave an older diff marked complete.
    (directory / FIXDIFF_FILE).unlink(missing_ok=True)
    mode = saved_mode(directory)
    cwd = Path(run['cwd'])
    if mode == 'tree':
        (directory / 'after.tree').unlink(missing_ok=True)
        before_tree = (directory / 'snapshot.tree').read_text(encoding='utf-8').strip()
        require(re.fullmatch(TREE_ID, before_tree), 'invalid snapshot tree')
        after = save_worktree_tree(cwd, directory, 'after')
        patch = plain_diff(cwd, 'diff-tree', '-p', '-r', '--no-renames', before_tree, after)
        state = {'tree': after}
    else:
        manifest = (directory / 'snapshot.files').read_bytes()
        require(not manifest or manifest.endswith(b'\0'), 'invalid snapshot.files')
        chunks, files = [], {}
        for name in nul_paths(manifest):
            patch, files[name] = copy_file_diff(directory, cwd, name)
            chunks.append(patch)
        patch = b''.join(chunks)
        state = {'diffHash': digest(patch), 'files': files}
    write_atomic(directory / FIX_DIFF_FILE, patch)
    result = {'mode': mode, 'empty': not patch, 'changed': previous is not None and
              state != previous['state'], 'state': state, 'diffHash': digest(patch),
              'afterCheck': args.after_check}
    save_json(directory / FIXDIFF_FILE, result)
    return result


def role_texts():
    text = (SKILL / 'references/roles.md').read_text(encoding='utf-8')
    matches = list(re.finditer(r'^## \d+\. .+?（role-([a-z-]+)\.md[^\n]*\n', text, re.M))
    slugs = [match[1] for match in matches]
    require(len(set(slugs)) == len(slugs) and 'general' in slugs, 'invalid role catalog')
    roles = {}
    for match in matches:
        end = text.find('\n## ', match.end())
        body = text[match.end():end if end >= 0 else len(text)].strip()
        require(body, 'empty role: ' + match[1])
        roles[match[1]] = body
    return roles


def role_list(value):
    roles = value.split(',') if value else []
    require(len(roles) == len(set(roles)) and set(roles) <= set(role_texts()) - {'general'},
            'unknown or repeated specialist role')
    require(len(roles) <= MAX_SPECIALISTS and
            len(set(roles) - set(DEFAULT_SPECIALISTS)) <= MAX_OPTIONAL_SPECIALISTS,
            'too many specialist roles')
    return roles


def target_root(args, root):
    requested = args.cwd.resolve()
    require(requested.is_dir(), 'cwd must be a directory')
    probe = run_command(['git', 'rev-parse', '--show-toplevel'], requested, allowed=(0, 128))
    is_git = probe.returncode == 0
    cwd = Path(os.fsdecode(probe.stdout).removesuffix('\n')) if is_git else requested
    require(not root.is_relative_to(cwd), 'review directory must be outside target')
    return requested, cwd, is_git


def init(args, root, run):
    require(not (root / RUN_FILE).exists() and not (root / LEDGER_FILE).exists(),
            'review already initialized')
    requested, cwd, is_git = args.target
    paths = []
    for name in args.path:
        path = Path(os.path.abspath(requested / name))
        require(path.exists() or path.is_symlink(), 'target does not exist: ' + display_path(name))
        require(path.is_relative_to(cwd), 'target spans multiple roots')
        if is_git:
            path_root = os.fsdecode(git(path if path.is_dir() and not path.is_symlink() else path.parent,
                                       'rev-parse', '--show-toplevel')).removesuffix('\n')
            require(Path(path_root) == cwd, 'target spans multiple repositories')
        paths.append(str(path.relative_to(cwd)))
    mode, base = 'paths', None
    if not paths:
        require(is_git, 'non-git target requires --path')
        if status_records(cwd):
            mode = 'uncommitted'
        else:
            candidates = []
            symbolic = run_command(['git', 'symbolic-ref', 'refs/remotes/origin/HEAD'], cwd,
                                   allowed=(0, 1, 128))
            if symbolic.returncode == 0:
                candidates.append(os.fsdecode(symbolic.stdout).strip())
            candidates.extend(['main', 'master'])
            for candidate in candidates:
                result = run_command(['git', 'rev-parse', '--verify', candidate], cwd, allowed=(0, 128))
                if result.returncode == 0:
                    base = os.fsdecode(result.stdout).strip()
                    base_name = candidate
                    break
            require(base is not None, 'default branch not found; specify target')
            require(git(cwd, 'diff', '--name-only', base + '...HEAD'), 'empty review scope')
            mode = 'base'
    roles = role_list(args.roles)
    require(args.diff_budget > 0 and args.timeout > 0 and args.ja_timeout > 0 and
            math.isfinite(args.timeout) and math.isfinite(args.ja_timeout), 'limits must be positive and finite')
    excluded = set(args.exclude)
    if args.only:
        excluded.update(set(REVIEWERS) - set(args.only))
    run = {'cwd': str(cwd), 'git': is_git, 'paths': paths, 'mode': mode, 'base': base,
           'profile': args.profile, 'roles': roles, 'excluded': sorted(excluded),
           'jaReview': args.ja_review, 'jaSkill': str(args.ja_skill.expanduser().resolve()),
           'diffBudget': args.diff_budget, 'timeout': args.timeout, 'jaTimeout': args.ja_timeout,
           'taskSequence': 0, 'tasks': {}}
    branch = 'non-git'
    if is_git:
        branch_result = run_command(['git', 'symbolic-ref', '--short', '-q', 'HEAD'], cwd, allowed=(0, 1))
        branch = os.fsdecode(branch_result.stdout).strip() or 'detached HEAD'
    scope = ', '.join(map(display_path, paths)) if paths else base_name if mode == 'base' else 'HEAD'
    run['baseSummary'] = args.base_summary or f'{mode}: {branch}, {scope}; {len(scope_files(run))} files'
    enabled = set(REVIEWERS) if args.ja_review else set(REVIEWERS) - {'ja-review'}
    require(enabled - excluded, 'all reviewers excluded')
    try:
        save_json(root / RUN_FILE, run)
        save_json(root / LEDGER_FILE, {'reviewId': root.name, 'iteration': 1, 'findings': []})
        render(root, load_ledger(root))
        iteration_dir(root, 1).mkdir(exist_ok=True)
    except EXPECTED_ERRORS:
        for name in (RUN_FILE, LEDGER_FILE, LEDGER_MD_FILE):
            (root / name).unlink(missing_ok=True)
        raise
    return run


def report_format():
    text = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
    heading = '### 共通報告フォーマット（全レビュアーに指示する）'
    require(text.count(heading) == 1, 'common report heading missing or duplicated')
    section = text.split(heading, 1)[1]
    match = re.search(r'```\n(.+?)\n```', section, re.S)
    require(match is not None, 'common report block missing')
    return match[1]


def file_body(cwd, name):
    path = safe_relative(cwd, name)
    if path.is_symlink():
        return f'{FILE_HEADER}{display_path(name)}\n[リンク] → {display_path(os.readlink(path))}\n'
    if not path.is_file():
        return ''
    data = path.read_bytes()
    if b'\0' in data:
        return f'{FILE_HEADER}{display_path(name)}\n[バイナリ]\n'
    return f'{FILE_HEADER}{display_path(name)}\n' + data.decode('utf-8', 'replace') + '\n'


def scoped_git_diff(run):
    cwd = Path(run['cwd'])
    base = os.fsdecode(git(cwd, 'merge-base', run['base'], 'HEAD')).strip() if run['mode'] == 'base' else 'HEAD'
    paths = run['paths'] if run['mode'] == 'paths' else []
    return plain_diff(cwd, 'diff', base, paths=paths)


def untracked_files(run):
    paths = ['--', *run['paths']] if run['mode'] == 'paths' else []
    return nul_paths(git(Path(run['cwd']), '--literal-pathspecs', 'ls-files', '--others',
                         '--exclude-standard', '-z', *paths))


def target_patch(run):
    cwd = Path(run['cwd'])
    if head_exists(cwd):
        parts = [scoped_git_diff(run)]
        parts.extend(file_body(cwd, name).encode('utf-8') for name in untracked_files(run))
    else:
        parts = [file_body(cwd, name).encode('utf-8') for name in scope_files(run, include_changes=True)]
    return b'\n'.join(parts)


def rereview_changes(directory, run):
    files = set()
    cwd = Path(run['cwd'])
    scoped = set(scope_files(run))
    baseline = after = None
    copy_baselines = {}
    for number in range(1, iteration_number(directory)):
        previous = iteration_dir(directory.parent, number)
        if not (previous / FIXDIFF_FILE).exists():
            continue
        state = valid_diff(previous)
        if state['mode'] != 'tree':
            for name in state['state']['files']:
                # Parent Git cannot express the contents of a nested repository.
                nested = run['git'] and any((parent / '.git').exists() for parent in (cwd / name).parents
                                            if parent != cwd and parent.is_relative_to(cwd))
                if not nested and (run['mode'] != 'paths' or name in scoped):
                    continue
                copy_baselines.setdefault(name, previous)
            continue
        if run['mode'] != 'paths':
            continue
        before = (previous / 'snapshot.tree').read_text(encoding='utf-8').strip()
        if baseline is None:
            baseline = before
        after = state['state']['tree']
        names = set(nul_paths(git(cwd, 'diff-tree', '--no-commit-id', '--name-only', '-r',
                                  '-z', before, after))) - scoped
        files.update(names)
    patches = []
    for name, previous in sorted(copy_baselines.items()):
        patch, current = copy_file_diff(previous, cwd, name)
        if patch or not current['present']:
            files.add(name)
            patches.append(patch)
    if baseline is not None and files:
        patches.append(plain_diff(cwd, 'diff-tree', '-p', '-r', '--no-renames', baseline, after,
                                  paths=sorted(files)))
    return files, patches


def iteration_files(directory, run, iteration):
    extra_files, fixes = rereview_changes(directory, run) if iteration > 1 else (set(), [])
    files = sorted(set(scope_files(run, include_changes=iteration > 1)) | extra_files)
    return files, extra_files, fixes


def git_path(text):
    if not text.startswith('"'):
        return text
    require(text.endswith('"'), 'invalid quoted git path')
    if re.search(r'(?<!\\)\\ud[c-f][0-9a-f]{2}', text):
        try:
            return parse_json(text)
        except ValueError as error:
            raise Failure('invalid JSON path: ' + str(error)) from error
    data = text[1:-1].encode('utf-8')
    result = bytearray()
    i = 0
    escapes = {ord('n'): 10, ord('r'): 13, ord('t'): 9, ord('b'): 8,
               ord('f'): 12, ord('v'): 11, ord('a'): 7, ord('\\'): ord('\\'), ord('"'): ord('"')}
    while i < len(data):
        if data[i] != ord('\\'):
            result.append(data[i])
            i += 1
            continue
        i += 1
        require(i < len(data), 'invalid git path escape')
        if ord('0') <= data[i] <= ord('7'):
            part = data[i:i + 3]
            require(len(part) == 3 and all(ord('0') <= b <= ord('7') for b in part), 'invalid octal git path')
            result.append(int(part, 8))
            i += 3
        else:
            require(data[i] in escapes, 'unknown git path escape')
            result.append(escapes[data[i]])
            i += 1
    return os.fsdecode(bytes(result))


def diff_header_path(raw, prefix):
    name = raw[4:].split('\t', 1)[0]
    name = git_path(name) if name.startswith('"') else name
    return name[2:] if name.startswith(prefix) else name


def changed_lines(patch, *, additions_only=False):
    """Track hunk counts so body lines cannot become file headers."""
    changed, binary = {}, set()
    path = old_path = None
    line, old_left, new_left = 0, 0, 0
    for raw in patch.split('\n'):
        if old_left or new_left:
            if raw.startswith('+'):
                changed[path].add(max(1, line))
                line, new_left = line + 1, new_left - 1
            elif raw.startswith('-'):
                if not additions_only:
                    changed[path].add(max(1, line))
                old_left -= 1
            elif raw.startswith(' '):
                line, old_left, new_left = line + 1, old_left - 1, new_left - 1
            continue
        if raw.startswith('diff --git '):
            path = old_path = None
            header = raw[len('diff --git '):]
            if header.startswith('"'):
                tokens = re.findall(r'"(?:[^"\\]|\\.)*"|\S+', header)
                if len(tokens) == 2:
                    path = git_path(tokens[1])[2:]
            else:
                for split in re.finditer(r' (?:b/|"b/)', header):
                    if header[:split.start()][2:] == header[split.start() + 1:][2:]:
                        path = header[split.start() + 3:]
                        break
                if path is None:
                    split = re.search(r' (?:b/|"b/)', header)
                    if split:
                        path = git_path(header[split.start() + 1:])[2:]
        elif raw.startswith('--- '):
            path = None
            old_path = diff_header_path(raw, 'a/')
        elif raw.startswith('rename to '):
            path = git_path(raw[len('rename to '):])
        elif raw.startswith('+++ '):
            path = diff_header_path(raw, 'b/')
            if path == '/dev/null':
                path = old_path
            changed.setdefault(path, set())
        elif raw.startswith(('Binary files ', 'GIT binary patch')):
            if raw.startswith('Binary files '):
                match = re.fullmatch(r'Binary files ("(?:[^"\\]|\\.)*"|\S+) and ("(?:[^"\\]|\\.)*"|\S+) differ', raw)
                if match:
                    old, new = (git_path(part) for part in match.groups())
                    if new.startswith('b/'):
                        path = new[2:]
                    elif new == '/dev/null' and old.startswith('a/'):
                        path = old[2:]
            require(path is not None, 'cannot locate binary diff')
            binary.add(path)
        elif raw.startswith('@@ '):
            match = re.fullmatch(r'@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*', raw)
            require(match is not None and path is not None, 'invalid diff hunk')
            old_left = int(match[1]) if match[1] is not None else 1
            line = int(match[2])
            new_left = int(match[3]) if match[3] is not None else 1
    return changed, binary


def append_span(spans, number):
    if spans and number == spans[-1][1] + 1:
        previous = f'{spans[-1][0]}-{spans[-1][1]}'
        spans[-1][1] = number
        return len(f'{spans[-1][0]}-{number}') - len(previous)
    spans.append([number, number])
    return len(f'{number}-{number}') + (len(SPAN_SEPARATOR) if len(spans) > 1 else 0)


def format_spans(spans):
    return SPAN_SEPARATOR.join(f'{start}-{end}' for start, end in spans)


def ja_file_text(cwd, name):
    path = safe_relative(cwd, name)
    if path.is_symlink() or not path.is_file() or any(p in JA_EXCLUDED_DIRS for p in Path(name).parts) or \
            '.min.' in path.name or path.name.endswith('.lock') or \
            path.name in JA_EXCLUDED_FILES:
        return None
    data = path.read_bytes()
    if b'\0' in data:
        return None
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return None
    return text if JA_TEXT.search(text) else None


def ja_chunk_end(numbered, start, lines, overhead):
    end, size, spans = start, overhead, []
    target_size = len('全行'.encode()) if lines is None else 0
    while end < len(numbered):
        is_target = lines is not None and end + 1 in lines
        increment = append_span([span[:] for span in spans[-1:]], end + 1) if is_target else 0
        next_size = size + len(numbered[end].encode())
        if next_size + target_size + increment > JA_BATCH_LIMIT:
            break
        if is_target:
            append_span(spans, end + 1)
        target_size += increment
        size = next_size
        end += 1
    return end, spans


def ja_file_chunks(name, text, lines, header):
    body = text.split('\n')
    if body and body[-1] == '':
        body.pop()
    numbered = [f'{n}: {line.rstrip(chr(13))}\n' for n, line in enumerate(body, 1)]
    chunks, start = [], 0
    base_prefix = f'{FILE_HEADER}{display_path(name)}\n=== 指摘対象の行: '
    overhead = len((header + base_prefix + '\n').encode())
    while start < len(numbered):
        end, spans = ja_chunk_end(numbered, start, lines, overhead)
        if end == start and lines is not None and start + 1 not in lines:
            start += 1
            continue
        require(end > start, 'single ja line exceeds batch limit: ' + display_path(name))
        if lines is None or spans:
            target = '全行' if lines is None else format_spans(spans)
            chunks.append(base_prefix + target + '\n' + ''.join(numbered[start:end]))
        start = end
    return chunks


def ja_batches(directory, run, files, ranges):
    skill = Path(run['jaSkill'])
    if skill.is_file():
        skill = skill.parent
    template = skill / 'references/prompt.md'
    if not template.is_file() or not (skill / 'SKILL.md').is_file() or not shutil.which('agy'):
        return []
    header = template.read_text(encoding='utf-8') + '\n'
    require(len(header.encode()) < JA_BATCH_LIMIT, 'ja template exceeds batch limit')
    batches, batch = [], header
    cwd = Path(run['cwd'])
    for name in files:
        text = ja_file_text(cwd, name)
        lines = ranges.get(name)
        if text is None or lines == set():
            continue
        if lines is not None and not any(JA_TEXT.search(line)
                                         for n, line in enumerate(text.split('\n'), 1) if n in lines):
            continue
        for chunk in ja_file_chunks(name, text, lines, header):
            if len((batch + chunk).encode()) > JA_BATCH_LIMIT:
                batches.append(batch)
                batch = header
            batch += chunk
    if batch != header:
        batches.append(batch)
    jobs = []
    for n, text in enumerate(batches, 1):
        prompt = directory / 'ja-review' / f'batch-{n}.txt'
        write_atomic(prompt, text)
        jobs.append(make_job('agy', 'ja-review', JOB_MODELS['ja-review']['agy']['general'],
                             prompt, prompt.parent / f'out-{n}.md', run['jaTimeout']))
    return jobs


def make_job(cli, source, model_spec, prompt, output, timeout):
    model, effort = model_spec
    return {'cli': cli, 'source': source, 'model': model, 'effort': effort,
            'prompt': str(prompt), 'output': str(output), 'timeout': timeout}


def iteration_number(directory):
    match = re.fullmatch(r'iter-(\d+)', directory.name)
    require(match is not None and 1 <= int(match[1]) <= MAX_ITERATIONS,
            f'iteration must be iter-1 through iter-{MAX_ITERATIONS}')
    return int(match[1])


def iteration_dir(root, number):
    return root / f'iter-{number}'


def prompt_target(directory, run, ledger, iteration, files, reconsider, fixes=()):
    target = 'レビュー対象（git 差分では変更行だけ）:\n' + '\n'.join(map(display_path, files)) + '\n'
    if run['mode'] == 'paths':
        target += '\n指定パス:\n' + '\n'.join(map(display_path, run['paths'])) + '\n'
    if run['mode'] != 'paths' or (iteration > 1 and (run['git'] or fixes)):
        patch = b'\n'.join([target_patch(run), *fixes])
        if len(patch) > run['diffBudget']:
            write_atomic(directory / 'target.diff', patch)
            target += '\n差分全文を必ず読む: ' + display_path(str(directory / 'target.diff')) + '\n'
        else:
            target += '\n' + patch.decode('utf-8', 'replace')
    save_json(directory / 'target.json', {'files': files, 'mode': run['mode']})
    if iteration > 1:
        target += '\n検討済みのため再報告不要（前提変更で再評価する項目を除く）:\n'
        target += '\n'.join(f"{i['id']} {display_path(i['file'])}:{i['line']} {i['problem']} 理由: {i['reason']}"
                            for i in ledger['findings'] if i['status'] in ('rejected', 'on_hold')
                            and i['id'] not in reconsider)
        target += '\n前回の修正概要:\n' + '\n'.join(
            f"{i['id']} {i['fixSummary']}" for i in ledger['findings']
            if i['status'] == 'fixed' and i.get('fixedIteration') == iteration - 1)
    return target


def review_jobs(directory, run, target):
    roles = role_texts()
    common = report_format()
    jobs = []
    specialist_cli = next(cli for cli, models in JOB_MODELS[run['profile']].items() if 'specialist' in models)
    specs = [('claude', 'general'), ('codex', 'general')]
    # SKILL.md Step 3 selects no specialist roles when their CLI is absent.
    if specialist_cli not in run['excluded'] and shutil.which(specialist_cli):
        specs.extend((specialist_cli, role) for role in run['roles'])
    for cli, role in specs:
        if cli in run['excluded']:
            continue
        stem = 'claude' if cli == 'claude' and role == 'general' else 'role-' + role
        prompt = directory / (stem + '.prompt.md')
        write_atomic(prompt, roles[role] + '\n\n' + common + '\n\n' + target)
        model_spec = JOB_MODELS[run['profile']][cli]['general' if role == 'general' else 'specialist']
        jobs.append(make_job(cli, cli + '-' + role, model_spec, prompt,
                             directory / (stem + '.md'), run['timeout']))
    return jobs


def invalidate(directory, command):
    for name in DOWNSTREAM[command]:
        path = directory / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def prompts(args, root, run):
    directory = args.directory.resolve()
    iteration = iteration_number(directory)
    ledger = load_ledger(root)
    require(iteration in (ledger['iteration'], ledger['iteration'] + 1), 'invalid iteration transition')
    if args.roles is not None:
        run['roles'] = role_list(args.roles)
    reconsider = set(args.reconsider.split(',')) if args.reconsider else set()
    require(reconsider <= {i['id'] for i in ledger['findings']}, 'unknown reconsider ID')
    invalidate(directory, 'prompts')
    ja_review = run['jaReview'] if args.ja_review is None else args.ja_review
    files, extra_files, fixes = iteration_files(directory, run, iteration)
    target = prompt_target(directory, run, ledger, iteration, files, reconsider, fixes)
    jobs = review_jobs(directory, run, target)
    if ja_review and 'ja-review' not in run['excluded']:
        cwd = Path(run['cwd'])
        ranges = {}
        extra_additions, _ = changed_lines(b'\n'.join(fixes).decode('utf-8', 'replace'), additions_only=True)
        if run['git'] and head_exists(cwd) and (run['mode'] != 'paths' or iteration > 1):
            # Only the git patch has hunk syntax; untracked bodies are whole-file inputs.
            ranges = {name: set() for name in files}
            additions, _ = changed_lines(scoped_git_diff(run).decode('utf-8', 'replace'), additions_only=True)
            ranges.update(additions)
            for name, numbers in extra_additions.items():
                ranges.setdefault(name, set()).update(numbers)
            for name in untracked_files(run):
                ranges.pop(name, None)
        elif extra_files:
            ranges = {name: extra_additions.get(name, set()) for name in extra_files}
        jobs.extend(ja_batches(directory, run, files, ranges))
    require(jobs, 'no reviewer selected')
    require(any(shutil.which(job['cli']) for job in jobs), 'no installed reviewer selected')
    ledger['iteration'] = iteration
    save_ledger_and_run(root, ledger, run)
    render(root, ledger)
    result = {'cwd': run['cwd'], 'iteration': iteration, 'reviewers': jobs}
    save_json(directory / 'prompts.json', result)
    return result


def parse_finding(heading, block, ja):
    fields = {}
    for key, value in re.findall(r'^- ([^:\n]+): (.+)$', block, re.M):
        require(key not in fields, 'duplicate report field: ' + key)
        fields[key] = value.strip()
    if ja:
        require(all(nonempty(fields.get(k)) for k in ('分類', '原文', '修正案', '理由')),
                'incomplete ja report')
        positions = [location(heading[2])]
        severity = SEVERITIES[heading[1]]
        problem = fields['原文'] + ' — ' + fields['理由']
    else:
        require(all(nonempty(fields.get(k)) for k in ('ファイル', '問題', '修正案')),
                'incomplete common report')
        first, others = other_locations(fields['ファイル'])
        positions = [location(p) for p in [first, *others]]
        severity, problem = heading[1], fields['問題']
    require(all(line is None or line > 0 for _, line in positions), 'invalid report line')
    return {'severity': severity, 'file': positions[0][0], 'line': positions[0][1],
            'locations': [{'file': p, 'line': n} for p, n in positions],
            'problem': problem, 'suggestion': fields['修正案'], 'raw': heading[0] + block}


def parse_report(text, ja=False):
    text = text.replace('\r\n', '\n').strip()
    empty = '指摘なし' if ja else '問題なし'
    pattern = (r'^### (' + '|'.join(SEVERITIES) + r') (.+)$' if ja else
               r'^### 指摘\d+（重要度: (' + '|'.join(SEVERITIES.values()) + r')）[ \t]*$')
    headings = list(re.finditer(r'^### .+$', text, re.M))
    preface = text[:headings[0].start()] if headings else text
    empty_report = re.search(r'^[ \t]*' + empty + r'[ \t]*[。.]?[ \t]*$', preface, re.M)
    finding_heading = r'^### (?:指摘\d|(?:' + '|'.join(SEVERITIES) + ')(?: |$))'
    has_findings = any(re.match(finding_heading, heading[0]) for heading in headings)
    if empty_report and not has_findings:
        return []
    require(not empty_report, 'contradictory empty report')
    findings, errors = [], []
    for index, raw_heading in enumerate(headings):
        heading = re.fullmatch(pattern, raw_heading[0])
        block = text[raw_heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(text)]
        if heading is None:
            # Summary headings carry no finding; malformed finding headings stay visible.
            if not re.match(finding_heading, raw_heading[0]):
                continue
        try:
            require(heading is not None, 'malformed report heading')
            findings.append(parse_finding(heading, block, ja))
        except Failure as error:
            errors.append({'severity': '不明', 'file': '', 'line': None, 'locations': [],
                           'problem': '解析不能: ' + str(error), 'suggestion': '元出力を確認',
                           'raw': raw_heading[0] + block, 'parseError': str(error)})
    require(findings, errors[0]['parseError'] if errors else 'no valid report')
    return findings + errors


ACTIVE = set()
STOPPING = threading.Event()


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def interrupt(signum, frame):
    STOPPING.set()
    for process in tuple(ACTIVE):
        kill_group(process)
    raise KeyboardInterrupt


# exec review cannot combine a diff selector and a role prompt; use ordinary exec.
# Claude -p with permission-mode plan can fail at ExitPlanMode and lose the report.
REVIEW_CLIS = {
    'claude': {'argv': lambda job, final, prompt: [
        'claude', '-p', '--model', job['model'], '--effort', job['effort'], '--tools', 'Read,Glob,Grep'],
        'stdin': True, 'stdout': True, 'ja_format': False, 'prompt_limit': None},
    'codex': {'argv': lambda job, final, prompt: [
        'codex', '-m', job['model'], '-c', 'model_reasoning_effort="' + job['effort'] + '"',
        'exec', '-s', 'read-only', '--skip-git-repo-check', '-o', str(final), '-'],
        'stdin': True, 'stdout': False, 'ja_format': False, 'prompt_limit': None},
    # Copied agy invocation: ~/.claude/skills/ja-review/SKILL.md, 2026-11-10.
    'agy': {'argv': lambda job, final, prompt: [
        'agy', '--model', job['model'], '--mode', 'plan', '-p', prompt.decode('utf-8')],
        'stdin': False, 'stdout': True, 'ja_format': True, 'prompt_limit': JA_BATCH_LIMIT},
}


def finish_process(process):
    try:
        return process.communicate(timeout=KILL_WAIT_SECONDS)
    except subprocess.TimeoutExpired as error:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
        process.wait(timeout=KILL_WAIT_SECONDS)
        return error.output or b'', error.stderr or b''


def run_process_group(argv, cwd, data, timeout, *, env=None):
    process = None
    code, stdout, stderr, reason = None, b'', b'', None
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, env=env)
        ACTIVE.add(process)
        if STOPPING.is_set():
            kill_group(process)
        try:
            stdout, stderr = process.communicate(input=data,
                                                 timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(process)
            stdout, stderr = finish_process(process)
            reason = 'timeout'
        code = process.returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        reason = str(error)
    finally:
        if process:
            ACTIVE.discard(process)
            kill_group(process)
            if process.poll() is None:
                try:
                    finish_process(process)
                except subprocess.TimeoutExpired as error:
                    reason = str(error)
    return code, stdout, stderr, reason


def attempt_path(output, number, suffix):
    return output.with_name(output.stem + f'.attempt-{number}.' + suffix)


def stderr_tail(stderr):
    # agy emits this startup notice even on success; keep other diagnostics.
    return '\n'.join(line for line in stderr.decode('utf-8', 'replace').splitlines()
                     if 'logging before google.Init' not in line)[-STDERR_TAIL_CHARS:]


def review_attempt(job, cwd, output, number):
    final = attempt_path(output, number, 'md')
    final.unlink(missing_ok=True)
    prompt = Path(job['prompt']).read_bytes()
    adapter = REVIEW_CLIS[job['cli']]
    if adapter['prompt_limit'] is not None:
        require(len(prompt) <= adapter['prompt_limit'], 'ja batch exceeds limit')
    argv = adapter['argv'](job, final, prompt)
    code, stdout, stderr, reason = run_process_group(argv, cwd, prompt if adapter['stdin'] else b'', job['timeout'])
    write_atomic(attempt_path(output, number, 'stdout.log'), stdout)
    stderr_path = attempt_path(output, number, 'stderr.log')
    write_atomic(stderr_path, stderr)
    if adapter['stdout']:
        write_atomic(final, stdout)
    if reason is None:
        if code != 0:
            reason = f'exit {code}'
        else:
            try:
                parse_report(final.read_text(encoding='utf-8'), adapter['ja_format'])
            except EXPECTED_ERRORS as error:
                reason = str(error)
    return {'number': number, 'exitCode': code, 'reason': reason,
            'output': str(final), 'stderr': str(stderr_path), 'stderrTail': stderr_tail(stderr)}


def absent(job, reason, attempts):
    return dict(job, status='absent', reason=reason, attempts=attempts)


def reviewer(job, cwd):
    output = Path(job['output'])
    attempts = []
    try:
        output.unlink(missing_ok=True)
    except EXPECTED_ERRORS as error:
        return absent(job, str(error), attempts)
    if not shutil.which(job['cli']):
        return absent(job, 'not installed', attempts)
    if job['cli'] not in REVIEW_CLIS:
        return absent(job, 'unsupported CLI', attempts)
    for number in range(1, MAX_ATTEMPTS + 1):
        require(not STOPPING.is_set(), 'review interrupted')
        try:
            attempt = review_attempt(job, cwd, output, number)
            attempts.append(attempt)
            reason = attempt['reason']
            final = Path(attempt['output'])
            if reason is None:
                data = final.read_bytes()
                write_atomic(output, data)
                return dict(job, status='success', attempts=attempts, outputHash=digest(data))
        except EXPECTED_ERRORS as error:
            return absent(job, str(error), attempts)
    return absent(job, reason, attempts)


def reviewer_failures(results):
    sources = {job['source'] for job in results}
    absent = sorted(source for source in sources if not any(
        job['status'] == 'success' for job in results if job['source'] == source))
    failed_batches = [{'output': job['output'], 'reason': job.get('reason'),
                       'attempts': job.get('attempts', [])} for job in results
                      if REVIEW_CLIS.get(job['cli'], {}).get('ja_format') and job['status'] != 'success']
    return {'absent': absent, 'failedBatches': failed_batches}


def review(args, root, run):
    directory = args.directory.resolve()
    plan = load_review_state(directory, 'prompts.json')
    require(plan['reviewers'], 'no reviewers')
    invalidate(directory, 'review')
    with ThreadPoolExecutor(max_workers=min(len(plan['reviewers']), MAX_PARALLEL)) as executor:
        results = list(executor.map(lambda job: reviewer(job, plan['cwd']), plan['reviewers']))
    result = dict(reviewer_failures(results), reviewers=results,
                  success=any(j['status'] == 'success' for j in results))
    save_json(directory / 'review.json', result)
    require(result['success'], 'all reviewers failed')
    return result


def aggregate_near(flags):
    if True in flags:
        return True
    if not flags or None in flags:
        return None
    return False


def collect(args, root, run):
    directory = args.directory.resolve()
    results = load_review_state(directory, 'review.json')['reviewers']
    require(any(j['status'] == 'success' for j in results), 'no successful reviews')
    invalidate(directory, 'collect')
    iteration = iteration_number(directory)
    previous_valid = False
    changes, binary = {}, set()
    if iteration > 1:
        previous = iteration_dir(directory.parent, iteration - 1)
        try:
            valid_diff(previous)
            changes, binary = changed_lines((previous / FIX_DIFF_FILE).read_text(encoding='utf-8', errors='replace'))
            previous_valid = True
        except EXPECTED_ERRORS:
            # A failed regeneration cannot supply reliable proximity information.
            pass
    items = []
    for job in results:
        if job['status'] != 'success':
            continue
        output = Path(job['output'])
        require(nonempty(job.get('outputHash')), 'missing review output hash')
        require(digest(output.read_bytes()) == job['outputHash'], 'review output changed after completion')
        for finding in parse_report(output.read_text(encoding='utf-8'), REVIEW_CLIS[job['cli']]['ja_format']):
            flags = []
            for pos in finding['locations']:
                unknown = not previous_valid or pos['line'] is None or \
                    pos['file'] in binary
                near = None if unknown else any(abs(pos['line'] - line) <= NEAR_FIX_LINES
                                                for line in changes.get(pos['file'], set()))
                pos['nearPreviousFix'] = near
                flags.append(near)
            finding.update(source=job['source'], output=str(output),
                           nearPreviousFix=aggregate_near(flags))
            items.append(finding)
    items.sort(key=lambda i: (i['file'], i['line'] is None, i['line'] or 0, i['source']))
    result = dict(reviewer_failures(results), findings=items)
    lines = []
    for item in items:
        mark = {True: '近傍', None: '判定不能', False: '近傍外'}[item['nearPreviousFix']]
        positions = ', '.join(display_path(p['file']) + ':' + (str(p['line']) if p['line'] else '-')
                              for p in item['locations'])
        lines.append(f"- [{mark}] [{item['severity']}] {positions} ({item['source']}) "
                     f"{item['problem']} → {item['suggestion']}\n  出力: {display_path(item['output'])}")
    write_atomic(directory / 'collected.md', '\n'.join(lines) + '\n')
    save_json(directory / 'collected.json', result)
    return result


def task(args, root, run):
    directory = args.directory.resolve()
    run['taskSequence'] += 1
    run['tasks'][directory.name] = {'sequence': run['taskSequence'], 'hash': None}
    save_json(root / RUN_FILE, run)
    (directory / 'task.json').unlink(missing_ok=True)
    completion = directory / FIXDIFF_FILE
    if completion.exists():
        load_fixdiff_state(directory)
        completion.unlink()
    data = args.input.read_bytes()
    require(data.strip(), 'empty delegation task')
    write_atomic(directory / 'fix-task.md', data)
    entry = {'sequence': run['taskSequence'], 'hash': digest(data)}
    run['tasks'][directory.name] = entry
    save_json(directory / 'task.json', entry)
    save_json(root / RUN_FILE, run)
    return entry


def load_fixdiff_state(directory):
    state = read_json(directory / FIXDIFF_FILE)
    require(isinstance(state, dict) and {'mode', 'diffHash', 'empty', 'state', 'afterCheck', 'changed'} <= state.keys()
            and isinstance(state['state'], dict) and state['mode'] in ('tree', 'copy')
            and all(type(state[key]) is bool for key in ('empty', 'afterCheck', 'changed'))
            and nonempty(state['diffHash']), 'invalid fixdiff state')
    if state['mode'] == 'tree':
        require(isinstance(state['state'].get('tree'), str) and
                re.fullmatch(TREE_ID, state['state']['tree']), 'invalid fixdiff tree')
    else:
        require(isinstance(state['state'].get('files'), dict) and
                nonempty(state['state'].get('diffHash')), 'invalid fixdiff state')
    return state


def valid_diff(directory):
    state = load_fixdiff_state(directory)
    require(state['mode'] == saved_mode(directory), 'diff mode changed')
    require(digest((directory / FIX_DIFF_FILE).read_bytes()) == state['diffHash'], 'diff changed after generation')
    if state['mode'] == 'tree':
        require((directory / 'after.tree').read_text(encoding='utf-8').strip() == state['state']['tree'], 'after tree changed')
    return state


def not_performed(reason, **extra):
    return dict(status='not_performed', reason=reason, **extra)


def process_fields(result):
    return {'exitCode': result.returncode, 'stdout': result.stdout.decode('utf-8', 'replace'),
            'stderr': result.stderr.decode('utf-8', 'replace')}


def guard_command(argv, cwd, directory, name):
    code, stdout, stderr, reason = run_process_group(argv, cwd, None, GUARD_TIMEOUT)
    write_atomic(directory / (name + '.stdout.log'), stdout)
    write_atomic(directory / (name + '.stderr.log'), stderr)
    return completed_process(argv, GUARD_TIMEOUT, code, stdout, stderr, reason)


def rules(args, root, run):
    directory = args.directory.resolve()
    (directory / 'rules.json').unlink(missing_ok=True)
    try:
        state = valid_diff(directory)
        require(state['mode'] == 'tree', 'copy mode')
        require(not state['empty'], 'empty fix diff')
        require(shutil.which('jev-guard'), 'jev-guard not installed')
        entry = read_json(directory / 'task.json')
        require(isinstance(entry, dict) and {'sequence', 'hash'} <= entry.keys(),
                'invalid delegation task')
        require(entry == run['tasks'].get(directory.name) and
                entry['sequence'] == run['taskSequence'] and entry['hash'] and
                digest((directory / 'fix-task.md').read_bytes()) == entry['hash'],
                'stale delegation task')
        result = guard_command(['jev-guard', 'rules', '--diff', str(directory / FIX_DIFF_FILE),
                                '--task', str(directory / 'fix-task.md')], run['cwd'], directory, 'rules')
        output = {'status': 'clear' if result.returncode == 0 else 'candidates' if result.returncode == EXIT_CANDIDATES
                  else 'error', **process_fields(result)}
    except EXPECTED_ERRORS as error:
        output = not_performed(str(error))
    save_json(directory / 'rules.json', output)
    return output


def is_probability(number):
    return type(number) in (int, float) and 0 <= number <= 1


def validate_probabilities(value):
    for key in ('confidence', 'probability'):
        if key in value:
            number = value[key]
            require(number is None or is_probability(number),
                    'invalid ' + key)


def optional_str(obj, key, label):
    require(key not in obj or isinstance(obj[key], str), 'invalid ' + label)


def validate_verdict(value):
    require(isinstance(value, dict), 'gate JSON must be object')
    require(value.get('verdict') in ('needed', 'skippable') and nonempty(value.get('verdictId')),
            'invalid gate verdict or ID')
    require(isinstance(value.get('findings'), list) and isinstance(value.get('sections'), list)
            and isinstance(value.get('newRisk'), dict), 'invalid gate arrays or newRisk')
    for finding in value['findings']:
        require(isinstance(finding, dict) and nonempty(finding.get('id')) and 'resolved' in finding
                and (finding['resolved'] is None or type(finding['resolved']) is bool),
                'invalid finding verdict')
        validate_probabilities(finding)
        optional_str(finding, 'rationale', 'finding rationale')
    risk = value['newRisk']
    validate_probabilities(risk)
    require('value' in risk and (risk['value'] is None or type(risk['value']) is bool), 'invalid newRisk')
    optional_str(risk, 'rationale', 'newRisk rationale')
    for section in value['sections']:
        require(isinstance(section, dict) and nonempty(section.get('path')), 'invalid section')
        optional_str(section, 'error', 'section error')
        if 'newRiskProbability' in section:
            probability = section['newRiskProbability']
            require(is_probability(probability),
                    'invalid section risk probability')
        optional_str(section, 'newRiskRationale', 'section rationale')
    if 'rechecks' in value:
        require(isinstance(value['rechecks'], list), 'invalid rechecks')
        for recheck in value['rechecks']:
            require(isinstance(recheck, dict) and isinstance(recheck.get('findingIds'), list)
                    and all(nonempty(i) for i in recheck['findingIds']), 'invalid recheck evidence')
    key = 'reason' if value['verdict'] == 'needed' else 'reviewPrompt'
    require(nonempty(value.get(key)), 'missing gate ' + key)
    return value


def evaluate_verdict(verdict):
    eligible = verdict['verdict'] == 'needed' and \
        verdict['reason'] in ('fix_not_resolved', 'new_risk') and \
        all('error' not in s for s in verdict['sections']) and \
        all(type(f['resolved']) is bool for f in verdict['findings'])
    grounds = [{'kind': 'finding', 'id': f['id'], 'rationale': f.get('rationale'),
                'rechecks': [r for r in verdict.get('rechecks', []) if f['id'] in r.get('findingIds', [])]}
               for f in verdict['findings'] if f['resolved'] is False]
    if verdict['newRisk']['value'] is True:
        grounds.append({'kind': 'newRisk', 'rationale': verdict['newRisk'].get('rationale')})
    grounds.extend({'kind': 'section', 'path': s['path'], 'rationale': s.get('newRiskRationale')}
                   for s in verdict['sections'] if s.get('newRiskProbability', 0) >= SECTION_RISK_THRESHOLD)
    return {'status': 'evaluated', 'verdict': verdict, 'overruleEligible': eligible,
            'grounds': grounds, 'next': NEXT['caller_decision']}


def gate(args, root, run):
    directory = args.directory.resolve()
    (directory / 'gate.json').unlink(missing_ok=True)
    try:
        state = valid_diff(directory)
    except EXPECTED_ERRORS as error:
        output = not_performed(str(error), next=NEXT['abort'])
        save_json(directory / 'gate.json', output)
        return output
    try:
        ledger = load_ledger(root)
        if state['mode'] == 'tree' and state['empty']:
            output = not_performed('empty fix diff', next=NEXT['finish'])
        elif ledger['iteration'] >= MAX_ITERATIONS:
            output = not_performed('iteration limit', next=NEXT['limit'])
        elif state['mode'] == 'copy':
            output = not_performed('copy mode', next=NEXT['rereview'])
        elif not shutil.which('jev-guard'):
            output = not_performed('jev-guard not installed', next=NEXT['rereview'])
        else:
            result = guard_command(['jev-guard', 'rereview', '--provider', 'claude', '--findings',
                                    str(root / LEDGER_FILE), '--diff', str(directory / FIX_DIFF_FILE),
                                    '--base-summary', run['baseSummary']],
                                   run['cwd'], directory, 'gate')
            if result.returncode == EXIT_AWAITING:
                output = {'status': STATUS_AWAITING, 'exitCode': EXIT_AWAITING,
                          'reason': 'api key missing', 'next': NEXT['awaiting_instruction'],
                          'stderr': result.stderr.decode('utf-8', 'replace')}
            elif result.returncode != 0:
                output = {'status': 'error', 'reason': 'jev-guard failed',
                          'next': NEXT['rereview'], **process_fields(result)}
            else:
                verdict = validate_verdict(parse_json(result.stdout.decode('utf-8')))
                output = evaluate_verdict(verdict)
    except EXPECTED_ERRORS as error:
        output = not_performed(str(error), next=NEXT['rereview'])
    save_json(directory / 'gate.json', output)
    return output


class ProfileAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        previous = getattr(namespace, '_profile_seen', None)
        if previous is not None and previous != values:
            parser.error('conflicting profiles')
        namespace._profile_seen = values
        setattr(namespace, self.dest, values)


def always_zero(result):
    return 0


COMMANDS = {
    'init': (init, 'root', always_zero),
    'prompts': (prompts, 'iteration', always_zero),
    'review': (review, 'iteration', always_zero),
    'collect': (collect, 'iteration', always_zero),
    'snapshot': (snapshot, 'iteration', always_zero),
    'task': (task, 'iteration', always_zero),
    'fixdiff': (fixdiff, 'iteration', always_zero),
    'ledger': (ledger_action, 'root', always_zero),
    'rules': (rules, 'iteration', lambda result: EXIT_CANDIDATES if result.get('exitCode') == EXIT_CANDIDATES else 0),
    'gate': (gate, 'iteration', lambda result: EXIT_AWAITING if result.get('status') == STATUS_AWAITING else 0),
}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    parsers = {name: sub.add_parser(name) for name in COMMANDS}
    for item in parsers.values():
        item.add_argument('directory', type=Path)
    task_parser = parsers['task']
    task_parser.add_argument('--input', type=Path, required=True)
    init_parser = parsers['init']
    init_parser.add_argument('--cwd', type=Path, required=True)
    init_parser.add_argument('--profile', action=ProfileAction, choices=('default', 'codex'), default='default')
    init_parser.add_argument('--base-summary')
    init_parser.add_argument('--path', action='append', default=[])
    init_parser.add_argument('--roles', default=','.join(DEFAULT_SPECIALISTS))
    init_parser.add_argument('--exclude', choices=REVIEWERS, action='append', default=[])
    init_parser.add_argument('--only', choices=REVIEWERS, action='append', default=[])
    init_parser.add_argument('--ja-review', action='store_true')
    init_parser.add_argument('--ja-skill', type=Path, default=Path('~/.claude/skills/ja-review'))
    init_parser.add_argument('--diff-budget', type=int, default=DIFF_BUDGET)
    init_parser.add_argument('--timeout', type=float, default=REVIEW_TIMEOUT)
    init_parser.add_argument('--ja-timeout', type=float, default=JA_TIMEOUT)
    prompts_parser = parsers['prompts']
    prompts_parser.add_argument('--roles')
    prompts_parser.add_argument('--reconsider', default='')
    prompts_parser.add_argument('--ja-review', action=argparse.BooleanOptionalAction, default=None)
    fixdiff_parser = parsers['fixdiff']
    fixdiff_parser.add_argument('--after-check', action='store_true')
    ledger_parser = parsers['ledger']
    ledger_parser.add_argument('action', choices=('add', 'update', 'fix', 'reject', 'hold', 'render'))
    ledger_parser.add_argument('ids', nargs='*')
    ledger_parser.add_argument('--input', type=Path)
    ledger_parser.add_argument('--iteration', type=int)
    ledger_parser.add_argument('--summary')
    ledger_parser.add_argument('--reason')
    return p


@contextmanager
def run_lock(root):
    require(root.is_dir(), 'review directory not found')
    with (root / '.mr.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('another command is updating this review')
        yield


def main():
    args = parser().parse_args()
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        root = args.directory.resolve()
        handler, root_kind, exit_code = COMMANDS[args.command]
        if root_kind == 'iteration':
            iteration_number(root)
            root = root.parent
        if args.command == 'init':
            args.target = target_root(args, root)
            root.mkdir(parents=True, exist_ok=True)
        with run_lock(root):
            run = None if args.command == 'init' else load_run(root)
            result = handler(args, root, run)
        print(json.dumps(result))
        return exit_code(result)
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return 1
    except EXPECTED_ERRORS as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
