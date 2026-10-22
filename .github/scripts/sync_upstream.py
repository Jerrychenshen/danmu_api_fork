#!/usr/bin/env python3
"""Bounded upstream snapshot sync. No credentials, network writes, or worktree edits."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

REPOSITORY = 'Jerrychenshen/danmu_api_fork'
UPSTREAM = 'https://github.com/huangxd-/danmu_api.git'
STATE = '.github/upstream-sync.json'
OWNER_ENV = {
    'GIT_AUTHOR_NAME': 'Jerrychenshen',
    'GIT_AUTHOR_EMAIL': '37981711+Jerrychenshen@users.noreply.github.com',
    'GIT_COMMITTER_NAME': 'Jerrychenshen',
    'GIT_COMMITTER_EMAIL': '37981711+Jerrychenshen@users.noreply.github.com',
}


def git(*args, data=None, env=None):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args],
                            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={**os.environ, **(env or {})})
    if result.returncode:
        raise ValueError('git ' + args[0] + ' failed: ' + result.stderr.decode(errors='replace'))
    return result.stdout


def protected(path):
    return path == '.github' or path.startswith('.github/') or path == '.pr_agent.toml'


def entries(ref):
    result = {}
    for item in git('ls-tree', '-rz', ref).split(b'\0'):
        if item:
            meta, raw_path = item.split(b'\t', 1)
            mode, kind, sha = meta.decode().split()
            path = raw_path.decode('utf-8')
            if kind != 'blob':
                raise ValueError('Submodules/unsupported tree entry require review: ' + path)
            result[path] = (mode, sha)
    return result


def read_json(ref, path):
    return json.loads(git('show', ref + ':' + path))


def check_context():
    if (os.environ.get('GITHUB_REPOSITORY') != REPOSITORY
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_EVENT_NAME') not in ('schedule', 'workflow_dispatch')):
        raise ValueError('Only scheduled/manual runs on ' + REPOSITORY + ' main are allowed')


def plan(upstream, verify=False):
    if git('status', '--porcelain', '--untracked-files=all').strip():
        raise ValueError('Working tree must be clean, including untracked files')
    head = git('rev-parse', 'HEAD').decode().strip()
    if head != git('rev-parse', 'refs/remotes/origin/main').decode().strip():
        raise ValueError('HEAD must equal fetched origin/main; concurrent or unsafe base')
    state = read_json(head, STATE)
    baseline = state['upstream_sha']
    if state.get('upstream_repository') != UPSTREAM or state.get('upstream_branch') != 'main':
        raise ValueError('Unexpected upstream state configuration')
    if not re.fullmatch('[0-9a-f]{40}', baseline):
        raise ValueError('Invalid baseline SHA')
    tip = git('rev-parse', '--verify', upstream + '^{commit}').decode().strip()
    git('merge-base', '--is-ancestor', baseline, tip)
    current, old, new = entries(head), entries(baseline), entries(tip)
    source = lambda tree: {p: v for p, v in tree.items() if not protected(p)}
    if source(current) != source(old):
        raise ValueError('Custom source changes detected; manual review required')
    changed = {p for p in old.keys() | new.keys() if old.get(p) != new.get(p)
               and not protected(p)}
    sensitive = {p for p in changed if Path(p).name in (
        'vercel.json', 'vercel.ts', 'vercel.js', '.vercelignore', '.nvmrc',
        '.node-version', '.npmrc', 'Dockerfile') or p.startswith('.vercel/')}
    if 'package.json' in changed:
        before, after = read_json(baseline, 'package.json'), read_json(tip, 'package.json')
        keys = ('engines', 'type', 'main', 'packageManager', 'os', 'cpu', 'volta')
        if (any(before.get(k) != after.get(k) for k in keys)
                or any(before.get('scripts', {}).get(k) != after.get('scripts', {}).get(k)
                       for k in ('start', 'build', 'vercel-build', 'test'))):
            sensitive.add('package.json runtime/build/test contract')
    if sensitive:
        raise ValueError('Vercel/runtime migration requires manual review: ' + ', '.join(sorted(sensitive)))
    mode = 'update' if baseline != tip else ('verify' if verify else 'noop')
    # Even a controls-only upstream commit advances the recorded SHA. npm runs only
    # when application source differs; upstream .github never enters our tree.
    merged = {**source(new), **{p: v for p, v in current.items() if protected(p)}}
    if mode == 'update':
        state['upstream_sha'] = tip
        blob = git('hash-object', '-w', '--stdin', data=(json.dumps(state, indent=2) + '\n').encode()).decode().strip()
        merged[STATE] = ('100644', blob)
    with tempfile.TemporaryDirectory() as temp:
        index_env = {'GIT_INDEX_FILE': str(Path(temp) / 'index')}
        git('read-tree', '--empty', env=index_env)
        records = b''.join(f'{mode_} {sha}\t{p}\0'.encode() for p, (mode_, sha) in sorted(merged.items()))
        git('update-index', '-z', '--index-info', data=records, env=index_env)
        tree = git('write-tree', env=index_env).decode().strip()
    return dict(mode=mode, head=head, upstream=tip, tree=tree, source_changed=bool(changed))


def finalize(saved):
    # Recompute every decision from immutable Git objects; no dependency output is staged.
    if plan(saved['upstream'], saved['mode'] == 'verify') != saved:
        raise ValueError('Plan changed or was tampered with; refusing commit')
    if saved['mode'] == 'noop':
        return ''
    if saved['mode'] == 'verify':
        message = 'chore: verify native Vercel Git integration (empty commit)\n\nNo application source changes. Automated on behalf of repository owner.\n'
    else:
        message = (f"chore: sync upstream {saved['upstream']}\n\n"
                   f"Upstream: {UPSTREAM}/commit/{saved['upstream']}\n"
                   'Snapshot retains upstream source attribution and license.\n'
                   'Automated on behalf of repository owner Jerrychenshen.\n')
    return git('commit-tree', saved['tree'], '-p', saved['head'],
               data=message.encode(), env=OWNER_ENV).decode().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'finalize'))
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    try:
        check_context()
        if args.command == 'prepare':
            # Exact public upstream only; never use an editable remote URL.
            git('fetch', '--no-tags', UPSTREAM, 'refs/heads/main')
            result = plan('FETCH_HEAD', args.verify)
            args.plan.write_text(json.dumps(result, indent=2) + '\n')
            outputs = {k: str(v).lower() if isinstance(v, bool) else v for k, v in result.items()}
        else:
            result = json.loads(args.plan.read_text())
            outputs = {'commit': finalize(result)}
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
                for key, value in outputs.items():
                    out.write(f'{key}={value}\n')
        print(json.dumps(outputs, indent=2))
    except (ValueError, KeyError, OSError, UnicodeError) as exc:
        # Escape annotation control characters (including unusual Git filenames).
        message = str(exc).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print('::error::' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
