#!/usr/bin/env python3
"""Replace one exact plugin hint in a saved Jibo prompt, retaining its history.

Preview is the default. Stop the gateway before --apply so its cached agent
cannot write the old prompt back. Supply the old and new plugin __init__.py;
the tool parses their literal hints without importing or executing either.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile


def platform_hint(path):
    matches = []
    for node in ast.walk(ast.parse(Path(path).read_text())):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != 'register_platform':
            continue
        keywords = {item.arg: item.value for item in node.keywords}
        if ast.literal_eval(keywords.get('name')) == 'jibo':
            matches.append(ast.literal_eval(keywords['platform_hint']))
    if len(matches) != 1 or not isinstance(matches[0], str) or not matches[0]:
        raise ValueError('Expected one literal Jibo platform hint in ' + str(path))
    return matches[0]


def prepare(conn, session_id, previous, current):
    row = conn.execute('SELECT s.source, s.system_prompt_hash, COALESCE(p.prompt, s.system_prompt) '
        'FROM sessions s LEFT JOIN system_prompts p ON p.hash=s.system_prompt_hash WHERE s.id=?', (session_id,)).fetchone()
    if row is None or row[0] != 'jibo':
        raise ValueError('Session must exist and belong to Jibo')
    prompt = row[2] or ''
    if prompt.count(current) == 1 and previous not in prompt:
        return row[1], prompt, prompt
    if not previous or previous == current or prompt.count(previous) != 1 or current in prompt:
        raise ValueError('Stored prompt must contain exactly one old hint and no new hint; refusing a broad rewrite')
    return row[1], prompt, prompt.replace(previous, current, 1)


def refresh(conn, session_id, previous, current, *, backup=None):
    old_hash, old, new = prepare(conn, session_id, previous, current)
    new_hash = hashlib.sha256(new.encode('utf8')).hexdigest()
    report = {'session_id': session_id, 'old_prompt_hash': old_hash, 'new_prompt_hash': new_hash,
              'old_chars': len(old), 'new_chars': len(new), 'changed': old != new, 'applied': False}
    if backup is None or old == new:
        return report
    conn.execute('BEGIN IMMEDIATE')
    try:
        # Recheck after acquiring the writer lock; never race another prompt update.
        if prepare(conn, session_id, previous, current) != (old_hash, old, new):
            raise ValueError('Session prompt changed during preview')
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'session_id': session_id, 'system_prompt_hash': old_hash, 'system_prompt': old}, f)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        # Preserve the old immutable prompt for rollback; only this session's
        # reference changes. Messages, model settings, and other sessions stay intact.
        conn.execute('INSERT OR IGNORE INTO system_prompts(hash,prompt) VALUES (?,?)', (new_hash, new))
        conn.execute('UPDATE sessions SET system_prompt_hash=?,system_prompt=NULL WHERE id=?', (new_hash, session_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    report.update(applied=True, backup=str(backup))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True)
    parser.add_argument('--previous-plugin', type=Path, required=True)
    parser.add_argument('--current-plugin', type=Path, default=Path(__file__).resolve().parents[1] / '__init__.py')
    selected_home = Path(os.environ.get('HERMES_HOME') or (Path.home() / '.hermes')).expanduser()
    parser.add_argument('--database', type=Path, default=selected_home / 'state.db')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        state = subprocess.check_output(['systemctl', '--user', 'show', 'hermes-gateway', '-p', 'ActiveState', '--value'], text=True).strip()
        if state not in ('inactive', 'failed'):
            parser.error('Stop hermes-gateway before applying: its cached agent could overwrite this migration')
    previous, current = platform_hint(args.previous_plugin), platform_hint(args.current_plugin)
    uri = args.database.resolve().as_uri() + ('?mode=rw' if args.apply else '?mode=ro')
    with sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10) as conn:
        backup = Path(tempfile.mkdtemp(prefix='jibo-prompt-before-')) / 'prompt.json' if args.apply else None
        report = refresh(conn, args.session, previous, current, backup=backup)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
