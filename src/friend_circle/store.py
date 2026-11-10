from contextlib import contextmanager
import json
import os
import sqlite3
import time

from .config import Config
from .errors import FederationError
from .migrations import LATEST, statements, migrate

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actors(
 id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
 controller TEXT REFERENCES actors(id), public_pem TEXT NOT NULL, key_name TEXT NOT NULL,
 created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS remote_actors(
 id TEXT PRIMARY KEY, document TEXT NOT NULL, fetched REAL NOT NULL);
CREATE TABLE IF NOT EXISTS blocks(kind TEXT NOT NULL, target TEXT NOT NULL,
 PRIMARY KEY(kind,target));
CREATE TABLE IF NOT EXISTS nonces(
 key_id TEXT NOT NULL, nonce TEXT NOT NULL, expires REAL NOT NULL,
 PRIMARY KEY(key_id,nonce));
CREATE INDEX IF NOT EXISTS nonce_expiry ON nonces(expires);
CREATE TABLE IF NOT EXISTS inbox(
 actor TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL,
 received REAL NOT NULL, status TEXT NOT NULL DEFAULT 'received', error TEXT,
 PRIMARY KEY(actor,id));
CREATE INDEX IF NOT EXISTS inbox_processing ON inbox(status,received);
CREATE TABLE IF NOT EXISTS outbox(
 id TEXT PRIMARY KEY, actor TEXT NOT NULL REFERENCES actors(id), body TEXT NOT NULL,
 created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS outbox_group ON outbox(json_extract(body,'$."rb:group"'));
CREATE TABLE IF NOT EXISTS deliveries(
 id INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES outbox(id),
 recipient TEXT NOT NULL, origin TEXT NOT NULL, endpoint TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL, lease_until REAL, lease_token TEXT,
 last_status INTEGER, error TEXT, UNIQUE(event_id,recipient));
CREATE INDEX IF NOT EXISTS delivery_ready ON deliveries(status,next_at,origin);
CREATE TABLE IF NOT EXISTS peers(
 origin TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
 circuit_until REAL NOT NULL DEFAULT 0, last_dispatched REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS rate_limits(
 bucket TEXT NOT NULL, window INTEGER NOT NULL, count INTEGER NOT NULL,
 PRIMARY KEY(bucket,window));
CREATE TABLE IF NOT EXISTS contact_notes(
 owner TEXT NOT NULL REFERENCES actors(id), remote TEXT NOT NULL, note TEXT NOT NULL,
 PRIMARY KEY(owner,remote));
CREATE TABLE IF NOT EXISTS contact_blocks(
 owner TEXT NOT NULL REFERENCES actors(id), remote TEXT NOT NULL,
 PRIMARY KEY(owner,remote));
CREATE TABLE IF NOT EXISTS friendship_groups(
 id TEXT PRIMARY KEY, local_human TEXT NOT NULL REFERENCES actors(id), remote_human TEXT NOT NULL,
 local_ai TEXT NOT NULL, remote_ai TEXT NOT NULL, status TEXT NOT NULL,
 initiated INTEGER NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS groups_contact ON friendship_groups(local_human,remote_human,status);
CREATE TABLE IF NOT EXISTS follows(
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES friendship_groups(id),
 actor TEXT NOT NULL, target TEXT NOT NULL, direction TEXT NOT NULL, status TEXT NOT NULL,
 UNIQUE(group_id,actor,target));
CREATE INDEX IF NOT EXISTS follows_group ON follows(group_id,status,direction);
CREATE TABLE IF NOT EXISTS account_visibility(
 actor TEXT PRIMARY KEY REFERENCES actors(id), visible_days INTEGER, revision INTEGER NOT NULL);
"""


class Store:
    def __init__(self, config: Config):
        self.config = config
        marker = config.state_dir / ".friend-circle-state"
        if config.state_dir.exists() and any(config.state_dir.iterdir()) and not marker.is_file():
            raise ValueError("use a dedicated empty state directory; refusing unrelated data")
        config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not marker.exists():
            with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                f.write("friend-circle-development-state\n")
        os.chmod(config.state_dir, 0o700)
        self.path = config.state_dir / "federation.sqlite3"
        if self.path.is_symlink():
            raise ValueError("database symlinks not allowed")
        with self.connect(write=True) as db:
            previous = 2
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                version = db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
                if not version or version[0] not in {str(v) for v in range(1, LATEST + 1)}:
                    raise ValueError("unsupported state schema; no automatic downgrade")
                previous = int(version[0])
                bound_origin = db.execute(
                    "SELECT value FROM metadata WHERE key='origin'"
                ).fetchone()
                if not bound_origin or bound_origin[0] != config.origin:
                    raise ValueError("state directory belongs to a different origin")
            statements(db, SCHEMA)
            existing = db.execute("SELECT value FROM metadata WHERE key='origin'").fetchone()
            if existing and existing[0] != config.origin:
                raise ValueError("state directory belongs to a different origin")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('origin',?)", (config.origin,))
            migrate(db, previous)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def one(self, sql, values=()):
        with self.connect() as db:
            return db.execute(sql, values).fetchone()

    def all(self, sql, values=()):
        with self.connect() as db:
            return db.execute(sql, values).fetchall()

    def blocked(self, actor, origin):
        return bool(
            self.one(
                "SELECT 1 FROM blocks WHERE (kind='actor' AND target=?) "
                "OR (kind='origin' AND target=?)",
                (actor, origin),
            )
        )

    def rate_limit(self, bucket, limit, now=None):
        now = time.time() if now is None else now
        window = int(now // 60)
        with self.connect(write=True) as db:
            db.execute("DELETE FROM rate_limits WHERE window < ?", (window - 2,))
            row = db.execute(
                "SELECT count FROM rate_limits WHERE bucket=? AND window=?", (bucket, window)
            ).fetchone()
            if row and row[0] >= limit:
                raise FederationError("rate_limited", 429)
            db.execute(
                "INSERT INTO rate_limits VALUES (?,?,1) ON CONFLICT(bucket,window) "
                "DO UPDATE SET count=count+1",
                (bucket, window),
            )

    def consume_nonce(self, key_id, nonce, expires, now=None):
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            db.execute("DELETE FROM nonces WHERE expires < ?", (now,))
            try:
                db.execute("INSERT INTO nonces VALUES (?,?,?)", (key_id, nonce, expires + 60))
            except sqlite3.IntegrityError as exc:
                raise FederationError("replayed_signature", 409) from exc


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def decode(raw):
    def unique(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("duplicate JSON key")
            out[k] = v
        return out

    try:
        obj = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )

        def check(v, depth=0):
            if depth > 16:
                raise ValueError("JSON nesting")
            if isinstance(v, dict):
                for child in v.values():
                    check(child, depth + 1)
            elif isinstance(v, list):
                if len(v) > 100:
                    raise ValueError("array too large")
                for child in v:
                    check(child, depth + 1)

        check(obj)
        if not isinstance(obj, dict):
            raise ValueError("JSON object required")
        return obj
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise FederationError("invalid_json") from exc
