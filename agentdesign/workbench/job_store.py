"""SQLite task, version and call records for the local workbench."""

from contextlib import contextmanager
import json

from localization_agent.memory import MemoryStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS p2_workspace (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS p2_jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS p2_job_queue ON p2_jobs(status);
CREATE TABLE IF NOT EXISTS p2_items (
    job TEXT NOT NULL, step INTEGER NOT NULL, item TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (job, step, item));
CREATE TABLE IF NOT EXISTS p2_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, kind TEXT NOT NULL,
    public INTEGER NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS p2_job_events ON p2_events(job, public, seq);
CREATE TABLE IF NOT EXISTS p2_actions (
    scope TEXT NOT NULL, key TEXT NOT NULL, signature TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (scope, key));
CREATE TABLE IF NOT EXISTS p2_heads (
    item TEXT PRIMARY KEY, version INTEGER NOT NULL, locked INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS p2_versions (
    item TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY (item, version));
CREATE TABLE IF NOT EXISTS p2_calls (
    job TEXT NOT NULL, lane TEXT NOT NULL, slot INTEGER NOT NULL, attempt INTEGER NOT NULL,
    signature TEXT NOT NULL, status TEXT NOT NULL, units INTEGER NOT NULL, data TEXT,
    PRIMARY KEY(job, lane, slot, attempt));
"""


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class JobError(ValueError):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


class Jobs:
    def __init__(self, path):
        self.memory = MemoryStore(path)
        self.db = self.memory.db
        self.db.execute("PRAGMA busy_timeout=10000")

    def close(self):
        self.memory.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            yield

    def get(self, job_id):
        row = self.db.execute("SELECT data FROM p2_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobError(404, "job_not_found")
        return json.loads(row["data"])

    def put(self, job, *, event=None):
        self.db.execute(
            "INSERT INTO p2_jobs VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data",
            (job["job_id"], job["status"], encode(job)),
        )
        if event:
            self.event(job["job_id"], event, status=job["status"], reason=job.get("reason"), step=job["cursor"])

    def item(self, job_id, step, item_id):
        row = self.db.execute("SELECT data FROM p2_items WHERE job=? AND step=? AND item=?",
                              (job_id, step, item_id)).fetchone()
        if row is None:
            raise JobError(404, "job_item_not_found")
        return json.loads(row["data"])

    def items(self, job_id, step=None):
        sql = "SELECT data FROM p2_items WHERE job=?"
        params = (job_id,)
        if step is not None:
            sql += " AND step=?"
            params += (step,)
        return [json.loads(row["data"]) for row in self.db.execute(sql + " ORDER BY step,rowid", params)]

    def put_item(self, item):
        self.db.execute(
            "INSERT INTO p2_items VALUES (?,?,?,?) ON CONFLICT(job,step,item) DO UPDATE SET data=excluded.data",
            (item["job_id"], item["step"], item["occurrence_id"], encode(item)),
        )
        self.event(item["job_id"], "item", occurrence_id=item["occurrence_id"], item_status=item["status"],
                   reason=item.get("reason"), step=item["step"])

    def event(self, job_id, kind, *, public=True, **data):
        self.db.execute("INSERT INTO p2_events(job,kind,public,data) VALUES (?,?,?,?)",
                        (job_id, kind, public, encode(data)))

    def events(self, job_id, after):
        return [
            {"id": row["seq"], "event": row["kind"], **json.loads(row["data"])}
            for row in self.db.execute(
                "SELECT seq,kind,data FROM p2_events WHERE job=? AND public=1 AND seq>? ORDER BY seq LIMIT 100",
                (job_id, after),
            )
        ]

    def audit(self, job_id, name, item_id, *, lane=None):
        return [
            json.loads(row[0]) for row in self.db.execute(
                """SELECT data FROM p2_events WHERE job=? AND public=0 AND kind=?
                   AND json_extract(data,'$.item')=?
                   AND (? IS NULL OR json_extract(data,'$.lane')=?) ORDER BY seq""",
                (job_id, name, item_id, lane, lane),
            )
        ]

    def action(self, scope, key, payload):
        if not key:
            raise JobError(400, "idempotency_key_required")
        row = self.db.execute("SELECT signature,data FROM p2_actions WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row is None:
            return None
        if row["signature"] != encode(payload):
            raise JobError(409, "idempotency_key_reused_with_different_payload")
        return json.loads(row["data"])

    def save_action(self, scope, key, payload, result):
        self.db.execute("INSERT INTO p2_actions VALUES (?,?,?,?)", (scope, key, encode(payload), encode(result)))

    def head(self, item_id):
        row = self.db.execute("SELECT version,locked FROM p2_heads WHERE item=?", (item_id,)).fetchone()
        return (row["version"], bool(row["locked"])) if row else (0, False)

    def version(self, item_id, version):
        row = self.db.execute("SELECT data FROM p2_versions WHERE item=? AND version=?", (item_id, version)).fetchone()
        return json.loads(row["data"]) if row else None

    def set_head(self, item_id, version, locked):
        self.db.execute(
            """INSERT INTO p2_heads VALUES (?,?,?)
               ON CONFLICT(item) DO UPDATE SET version=excluded.version,locked=excluded.locked""",
            (item_id, version, locked),
        )

    def append_version(self, item_id, version, data):
        self.db.execute("INSERT INTO p2_versions VALUES (?,?,?)", (item_id, version, encode(data)))
        self.set_head(item_id, version, False)
