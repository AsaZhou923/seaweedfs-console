"""Consistent SQLite backup / isolated restore; source S3 bytes are out of scope."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def verify(path):
    with sqlite3.connect(path) as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Database integrity check failed')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Foreign key check failed')
        return {row[0]: conn.execute('SELECT COUNT(*) FROM "' + row[0].replace('"', '""') + '"').fetchone()[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup', 'restore', 'verify'])
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path, nargs='?')
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.is_file():
        raise RuntimeError('Source database not found')
    if args.action == 'verify':
        print(json.dumps({'integrity': 'ok', 'tables': verify(source)}))
        return
    if args.destination is None:
        parser.error('destination is required')
    destination = args.destination.resolve()
    if destination.exists() or destination == source:
        raise RuntimeError('Destination must be a new isolated path; stop API/worker before activating restored data')
    verify(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as target:
            src.backup(target)
    tables = verify(destination)
    result = {'action': args.action, 'integrity': 'ok', 'tables': tables,
              'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
              'scope': 'console SQLite only; credentials/config backed up separately; S3 objects excluded'}
    destination.with_suffix(destination.suffix + '.manifest.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
