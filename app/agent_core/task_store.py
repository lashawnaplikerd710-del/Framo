"""SQLite-backed IDs, states, artifacts and replayable events for local jobs."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from .file_io import io_path
import re
import sqlite3
import uuid


ID = re.compile(r'^(asset|job|artifact|batch)_[0-9a-f]{16,64}$')


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with io_path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def _id(value: str, prefix: str):
    if not isinstance(value, str) or not ID.fullmatch(value) or not value.startswith(prefix + '_'):
        raise ValueError('Invalid ' + prefix + ' ID')
    return value


class TaskStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'agent.sqlite3'
        with self._connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, state TEXT NOT NULL, spec_json TEXT NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                job_id TEXT NOT NULL, timestamp TEXT NOT NULL, stage TEXT NOT NULL,
                type TEXT NOT NULL, payload_json TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS events_job_sequence ON events(job_id, sequence);
              CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL,
                revision_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                verified INTEGER NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(job_id, kind, revision_id));
              CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, stage TEXT NOT NULL,
                input_sha256 TEXT NOT NULL, status TEXT NOT NULL, report_path TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(job_id, stage, input_sha256));
              CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY, state TEXT NOT NULL, spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT);
              CREATE TABLE IF NOT EXISTS batch_children (
                batch_id TEXT NOT NULL, output_id TEXT NOT NULL, job_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(batch_id, output_id), UNIQUE(job_id));
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def job_dir(self, job_id):
        return self.root / 'jobs' / _id(job_id, 'job')

    def put_asset(self, record):
        asset_id = _id(record['asset_id'], 'asset')
        with self._connect() as db:
            db.execute('INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?)',
                (asset_id, record['path'], record['sha256'],
                 json.dumps(record, ensure_ascii=False), stamp()))
        return asset_id

    def get_asset(self, asset_id):
        with self._connect() as db:
            row = db.execute('SELECT metadata_json FROM assets WHERE asset_id=?',
                             (_id(asset_id, 'asset'),)).fetchone()
        if row is None:
            raise KeyError('Unknown asset_id')
        return json.loads(row['metadata_json'])

    def put_job(self, spec):
        job_id = 'job_' + uuid.uuid4().hex
        now = stamp()
        folder = self.job_dir(job_id)
        folder.mkdir(parents=True, exist_ok=False)
        atomic_json(folder / 'task.json', spec)
        with self._connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                (job_id, 'CREATED', json.dumps(spec, ensure_ascii=False), 0, None, now, now))
        self.event(job_id, 'CREATED', 'progress', {'message': '任务已建立'})
        return job_id

    def get_job(self, job_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE job_id=?', (_id(job_id, 'job'),)).fetchone()
        if row is None:
            raise KeyError('Unknown job_id')
        result = dict(row)
        result['task_spec'] = json.loads(result.pop('spec_json'))
        result['cancel_requested'] = bool(result['cancel_requested'])
        result['schema_version'] = '1.0'
        return result

    def set_state(self, job_id, state, *, error=None, event_type='progress', message=None):
        self.get_job(job_id)
        with self._connect() as db:
            db.execute('UPDATE jobs SET state=?, error=?, updated_at=? WHERE job_id=?',
                       (state, error, stamp(), job_id))
        self.event(job_id, state, event_type, {'message': message or state,
                                               **({'error': error} if error else {})})

    def request_cancel(self, job_id):
        job = self.get_job(job_id)
        if job['state'] in ('COMPLETED', 'READY_FOR_REVIEW', 'CANCELLED'):
            return job
        with self._connect() as db:
            db.execute('UPDATE jobs SET cancel_requested=1, updated_at=? WHERE job_id=?', (stamp(), job_id))
        self.event(job_id, job['state'], 'progress', {'message': '已请求取消；正在等待当前操作安全停止'})
        return self.get_job(job_id)

    def clear_cancel(self, job_id):
        self.get_job(job_id)
        with self._connect() as db:
            db.execute('UPDATE jobs SET cancel_requested=0, updated_at=? WHERE job_id=?', (stamp(), job_id))

    def event(self, job_id, stage, kind, payload):
        if kind not in ('progress', 'error', 'waiting_input', 'artifact_complete', 'task_finished'):
            raise ValueError('Unsupported event type')
        now = stamp(); event_id = uuid.uuid4().hex
        with self._connect() as db:
            cur = db.execute('INSERT INTO events(event_id,job_id,timestamp,stage,type,payload_json) VALUES(?,?,?,?,?,?)',
                             (event_id, job_id, now, stage, kind, json.dumps(payload, ensure_ascii=False)))
            sequence = cur.lastrowid
        return {'event_id': event_id, 'sequence': sequence, 'job_id': job_id,
                'timestamp': now, 'stage': stage, 'type': kind, 'payload': payload,
                'schema_version': '1.0'}

    def read_events(self, job_id, after_sequence=0):
        self.get_job(job_id)
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError('after_sequence must be a nonnegative integer')
        with self._connect() as db:
            rows = db.execute('SELECT * FROM events WHERE job_id=? AND sequence>? ORDER BY sequence',
                              (job_id, after_sequence)).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event['payload'] = json.loads(event.pop('payload_json'))
            event['schema_version'] = '1.0'
            result.append(event)
        return result

    def put_artifact(self, job_id, kind, revision_id, path, *, verified=False):
        self.get_job(job_id)
        path = Path(path).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(self.job_dir(job_id)):
            raise ValueError('Artifacts must be files inside their job directory')
        sha = digest(path)
        with self._connect() as db:
            row = db.execute('SELECT * FROM artifacts WHERE job_id=? AND kind=? AND revision_id=?',
                             (job_id, kind, revision_id)).fetchone()
            if row is not None:
                if row['path'] != str(path) or row['sha256'] != sha:
                    raise ValueError('Existing artifact revision changed; create a new revision')
                return dict(row)
            artifact_id = 'artifact_' + uuid.uuid4().hex
            db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)',
                       (artifact_id, job_id, kind, revision_id, str(path), sha, int(verified), stamp()))
        row = self.get_artifact(artifact_id)
        self.event(job_id, self.get_job(job_id)['state'], 'artifact_complete',
                   {'artifact_id': artifact_id, 'kind': kind, 'revision_id': revision_id,
                    'verified': verified})
        return row

    def get_artifact(self, artifact_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM artifacts WHERE artifact_id=?',
                             (_id(artifact_id, 'artifact'),)).fetchone()
        if row is None:
            raise KeyError('Unknown artifact_id')
        return self._validate_artifact_row(row)

    def _validate_artifact_row(self, row):
        """Validate fetched metadata without reopening SQLite for every file."""
        value = dict(row)
        path = Path(value['path']).resolve(strict=True)
        if not path.is_relative_to(self.job_dir(value['job_id'])) or digest(path) != value['sha256']:
            raise ValueError('Artifact path or content changed')
        value['verified'] = bool(value['verified'])
        value['schema_version'] = '1.0'
        return value

    def list_artifacts(self, job_id):
        self.get_job(job_id)
        with self._connect() as db:
            rows = db.execute('SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at',
                              (job_id,)).fetchall()
        return [self._validate_artifact_row(row) for row in rows]

    def list_artifacts_safe(self, job_id):
        """Return valid artifacts plus isolated integrity failures.

        Strict callers keep using :meth:`list_artifacts`; the workbench uses
        this read-only view so one historical overwrite cannot hide newer
        checkpoints or crash the task details page.
        """
        self.get_job(job_id)
        with self._connect() as db:
            rows = db.execute('SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at',
                              (job_id,)).fetchall()
        valid, invalid = [], []
        for row in rows:
            try:
                valid.append(self._validate_artifact_row(row))
            except (OSError, ValueError, KeyError) as exc:
                invalid.append({'artifact_id': row['artifact_id'], 'kind': row['kind'],
                                'revision_id': row['revision_id'], 'path': row['path'],
                                'error': str(exc)})
        return valid, invalid

    def artifact(self, job_id, kind, revision_id='revision_001'):
        for row in self.list_artifacts(job_id):
            if row['kind'] == kind and row['revision_id'] == revision_id:
                return row
        return None

    def operation(self, job_id, stage, input_sha256, status, report_path=None):
        self.get_job(job_id)
        with self._connect() as db:
            row = db.execute('SELECT * FROM operations WHERE job_id=? AND stage=? AND input_sha256=?',
                             (job_id, stage, input_sha256)).fetchone()
            if row and row['status'] == 'completed' and status == 'running':
                raise ValueError('Completed operation must not be repeated')
            operation_id = row['operation_id'] if row else uuid.uuid4().hex
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET status=excluded.status,report_path=excluded.report_path,updated_at=excluded.updated_at',
                       (operation_id, job_id, stage, input_sha256, status,
                        str(report_path) if report_path else None, stamp()))
        return operation_id

    def get_operation(self, job_id, stage, input_sha256):
        self.get_job(job_id)
        with self._connect() as db:
            row = db.execute('SELECT * FROM operations WHERE job_id=? AND stage=? AND input_sha256=?',
                             (job_id, stage, input_sha256)).fetchone()
        return dict(row) if row else None

    # Batch persistence is additive. Each child remains an ordinary job so the
    # existing worker, checkpoint, quality and registration code is reused.
    def batch_dir(self, batch_id):
        return self.root / 'batches' / _id(batch_id, 'batch')

    def put_batch(self, spec):
        batch_id = 'batch_' + uuid.uuid4().hex
        now = stamp()
        folder = self.batch_dir(batch_id)
        folder.mkdir(parents=True, exist_ok=False)
        atomic_json(folder / 'batch.json', {**spec, 'batch_id': batch_id,
                                             'state': 'CREATED', 'created_at': now,
                                             'updated_at': now})
        with self._connect() as db:
            db.execute('INSERT INTO batches VALUES (?,?,?,?,?,?)',
                       (batch_id, 'CREATED', json.dumps(spec, ensure_ascii=False), now, now, None))
        return batch_id

    def get_batch(self, batch_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM batches WHERE batch_id=?',
                             (_id(batch_id, 'batch'),)).fetchone()
            children = db.execute('SELECT * FROM batch_children WHERE batch_id=? ORDER BY ordinal',
                                  (batch_id,)).fetchall()
        if row is None:
            raise KeyError('Unknown batch_id')
        result = dict(row)
        result['batch_spec'] = json.loads(result.pop('spec_json'))
        result['children'] = [dict(child) for child in children]
        result['schema_version'] = '1.0'
        return result

    def list_batches(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be an integer in 1..1000')
        with self._connect() as db:
            rows = db.execute('SELECT batch_id FROM batches ORDER BY created_at DESC LIMIT ?',
                              (limit,)).fetchall()
        return [self.get_batch(row['batch_id']) for row in rows]

    def add_batch_child(self, batch_id, output_id, job_id, ordinal):
        self.get_batch(batch_id)
        self.get_job(job_id)
        if not isinstance(output_id, str) or not output_id.strip() or type(ordinal) is not int or ordinal < 1:
            raise ValueError('Invalid batch child identity')
        with self._connect() as db:
            db.execute('INSERT INTO batch_children VALUES (?,?,?,?,?,?)',
                       (batch_id, output_id, job_id, ordinal, 'CREATED', stamp()))
        self.event(job_id, 'BATCH', 'progress', {'message': '已加入批次',
                                                   'batch_id': batch_id,
                                                   'output_id': output_id})

    def set_batch_state(self, batch_id, state, *, error=None, children=None):
        current = self.get_batch(batch_id)
        now = stamp()
        child_updates = {child['output_id']: child for child in (children or [])
                         if isinstance(child, dict) and child.get('output_id')}
        with self._connect() as db:
            db.execute('UPDATE batches SET state=?, error=?, updated_at=? WHERE batch_id=?',
                       (state, error, now, batch_id))
            for output_id, child in child_updates.items():
                db.execute('UPDATE batch_children SET state=?, updated_at=? WHERE batch_id=? AND output_id=?',
                           (child.get('state', 'UNKNOWN'), now, batch_id, output_id))
        persisted_children = [dict(child, state=child_updates.get(child['output_id'], {}).get(
            'state', child['state']), updated_at=now if child['output_id'] in child_updates else child['updated_at'])
                              for child in current['children']]
        atomic_json(self.batch_dir(batch_id) / 'batch.json', {
            **current['batch_spec'], 'batch_id': batch_id, 'state': state,
            'error': error, 'created_at': current['created_at'], 'updated_at': now,
            'children': persisted_children
        })
        return {**current, 'state': state, 'error': error, 'updated_at': now,
                'children': persisted_children}

    def batch_for_job(self, job_id):
        self.get_job(job_id)
        with self._connect() as db:
            row = db.execute('SELECT * FROM batch_children WHERE job_id=?', (job_id,)).fetchone()
        return dict(row) if row else None
