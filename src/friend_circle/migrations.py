"""Additive migrations. Executed statement-by-statement inside one write transaction."""

MIGRATIONS = {
    3: """
CREATE TABLE invitation_tokens(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES actors(id), token_hash TEXT NOT NULL,
 created REAL NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
 max_uses INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0);
CREATE INDEX invitation_owner ON invitation_tokens(owner,created);
CREATE INDEX invitation_expiry ON invitation_tokens(expires);
CREATE TABLE invitation_uses(
 invitation_id TEXT NOT NULL REFERENCES invitation_tokens(id), requester TEXT NOT NULL,
 event_id TEXT NOT NULL, used REAL NOT NULL, PRIMARY KEY(invitation_id,requester));
""",
}
MIGRATIONS[4] = """
CREATE TABLE actor_profiles(actor TEXT PRIMARY KEY REFERENCES actors(id), body TEXT NOT NULL,
 revision INTEGER NOT NULL, lease_seconds INTEGER NOT NULL DEFAULT 300);
CREATE TABLE social_objects(id TEXT PRIMARY KEY, actor TEXT NOT NULL REFERENCES actors(id),
 kind TEXT NOT NULL, root TEXT NOT NULL, parent TEXT, body TEXT NOT NULL, published REAL NOT NULL,
 revision INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, visibility TEXT NOT NULL);
CREATE INDEX objects_actor ON social_objects(actor,kind,published);
CREATE INDEX objects_root ON social_objects(root,kind);
CREATE UNIQUE INDEX single_live_like ON social_objects(actor,root) WHERE kind='like' AND deleted=0;
CREATE TABLE object_audience(object_id TEXT NOT NULL REFERENCES social_objects(id), reader TEXT NOT NULL,
 PRIMARY KEY(object_id,reader));
CREATE INDEX audience_reader ON object_audience(reader,object_id);
CREATE TABLE object_cache(reader TEXT NOT NULL REFERENCES actors(id), id TEXT NOT NULL,
 actor TEXT NOT NULL, root TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
 expires REAL NOT NULL, synced REAL NOT NULL, revision INTEGER NOT NULL, source_seq INTEGER NOT NULL,
 PRIMARY KEY(reader,id));
CREATE INDEX cache_profile ON object_cache(reader,actor,kind,source_seq);
CREATE INDEX cache_root ON object_cache(reader,root,kind);
CREATE INDEX cache_expiry ON object_cache(expires);
CREATE TABLE profile_cache(reader TEXT NOT NULL REFERENCES actors(id), actor TEXT NOT NULL,
 body TEXT NOT NULL, expires REAL NOT NULL, synced REAL NOT NULL, next_cursor INTEGER,
 PRIMARY KEY(reader,actor));
CREATE TABLE social_references(root TEXT NOT NULL, id TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(root,id));
CREATE TABLE cache_tombstones(reader TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
 PRIMARY KEY(reader,id));
"""
MIGRATIONS[5] = """
CREATE TABLE mcp_tokens(
 id TEXT PRIMARY KEY, actor TEXT NOT NULL REFERENCES actors(id), token_hash TEXT NOT NULL,
 scopes TEXT NOT NULL, label TEXT NOT NULL, created REAL NOT NULL, expires REAL,
 last_used REAL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE INDEX mcp_tokens_actor ON mcp_tokens(actor,revoked)
"""
MIGRATIONS[6] = """
CREATE TABLE audience_groups(id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES actors(id),
 name TEXT NOT NULL, revision INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX audience_groups_owner ON audience_groups(owner,deleted);
CREATE TABLE audience_group_members(group_id TEXT NOT NULL REFERENCES audience_groups(id),
 actor TEXT NOT NULL, PRIMARY KEY(group_id,actor));
CREATE TABLE post_scopes(object_id TEXT PRIMARY KEY REFERENCES social_objects(id), body TEXT NOT NULL);
CREATE TABLE post_groups(object_id TEXT NOT NULL REFERENCES social_objects(id),
 group_id TEXT NOT NULL REFERENCES audience_groups(id), PRIMARY KEY(object_id,group_id));
CREATE INDEX post_groups_group ON post_groups(group_id,object_id);
"""
LATEST = max(MIGRATIONS)


def statements(db, script):
    # These migrations deliberately contain no triggers or semicolons in literals.
    for statement in script.split(";"):
        if statement.strip():
            db.execute(statement)


def migrate(db, previous):
    for version in range(max(2, previous) + 1, LATEST + 1):
        statements(db, MIGRATIONS[version])
    db.execute(
        "INSERT INTO metadata VALUES ('schema',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(LATEST),),
    )
