import random
import time
import uuid

from .config import ACTIVITY_MAX_AGE
from .errors import FederationError
from .signatures import sign
from .store import decode


class DeliveryWorker:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def claim(self, now=None):
        now = time.time() if now is None else now
        token = uuid.uuid4().hex
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE deliveries SET status='retry',lease_token=NULL WHERE "
                "status='leased' AND lease_until<?",
                (now,),
            )
            row = db.execute(
                "SELECT d.*,o.actor,o.body,o.created FROM deliveries d "
                "JOIN outbox o ON o.id=d.event_id JOIN peers p ON p.origin=d.origin "
                "WHERE d.status IN ('pending','retry') AND d.next_at<=? AND p.circuit_until<=? "
                "AND NOT EXISTS(SELECT 1 FROM deliveries active WHERE active.origin=d.origin "
                "AND active.status='leased') "
                "ORDER BY p.last_dispatched,d.next_at,d.id LIMIT 1",
                (now, now),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE deliveries SET status='leased',lease_until=?,lease_token=?,"
                "attempts=attempts+1 WHERE id=?",
                (now + self.service.config.lease_seconds, token, row["id"]),
            )
            db.execute("UPDATE peers SET last_dispatched=? WHERE origin=?", (now, row["origin"]))
            job = dict(row)
            job.update(lease_token=token, attempts=row["attempts"] + 1)
            return job

    def finish(self, job, status, http_status=None, error=None, retry_after=0):
        now = time.time()
        if status == "retry" and job["attempts"] >= self.service.config.max_attempts:
            status = "dead"
        delay = max(
            min(retry_after, 3600), min(3600, 2 ** job["attempts"] * 5) * random.uniform(0.75, 1.25)
        )
        with self.store.connect(write=True) as db:
            result = db.execute(
                "UPDATE deliveries SET status=?,next_at=?,lease_until=NULL,lease_token=NULL,"
                "last_status=?,error=? WHERE id=? AND status='leased' AND lease_token=?",
                (status, now + delay, http_status, error, job["id"], job["lease_token"]),
            )
            if not result.rowcount:
                return False  # old worker lost lease; must not overwrite new worker's outcome
            if status == "acknowledged":
                db.execute(
                    "UPDATE peers SET failures=0,circuit_until=0 WHERE origin=?", (job["origin"],)
                )
            elif status in {"retry", "dead"}:
                db.execute(
                    "UPDATE peers SET failures=failures+1,circuit_until="
                    "CASE WHEN failures>=2 THEN ? ELSE circuit_until END WHERE origin=?",
                    (now + 60, job["origin"]),
                )
        return True

    def run_once(self):
        job = self.claim()
        if job is None:
            return False
        if self.store.blocked(job["recipient"], job["origin"]):
            self.finish(job, "cancelled", error="blocked")
            return True
        if time.time() - job["created"] > ACTIVITY_MAX_AGE:
            self.finish(job, "dead", error="event_expired")
            return True
        event = decode(job["body"])
        if event["type"] in {"Follow", "Accept"}:
            group = self.store.one(
                "SELECT status FROM friendship_groups WHERE id=?", (event.get("rb:group"),)
            )
            if not group or group[0] not in {"pending", "active"}:
                self.finish(job, "cancelled", error="relationship_revoked")
                return True
        lease = self.store.one("SELECT status,lease_token FROM deliveries WHERE id=?", (job["id"],))
        if lease["status"] != "leased" or lease["lease_token"] != job["lease_token"]:
            return True
        try:
            # Recheck identity/capabilities; refreshing is bounded by the identity cache TTL.
            doc = self.service.identity.remote(job["recipient"])
            endpoint = doc["endpoints"]["sharedInbox"]
            msg = sign(self.service.identity, job["actor"], "POST", endpoint, job["body"].encode())
            response = self.service.http.send("POST", endpoint, dict(msg.headers), msg.body)
            if 200 <= response.status < 300:
                self.finish(job, "acknowledged", response.status)
            elif response.status == 429 or response.status >= 500:
                try:
                    retry_after = int(response.headers.get("Retry-After", 0))
                except ValueError:
                    retry_after = 0
                self.finish(job, "retry", response.status, "remote_retryable", retry_after)
            else:
                self.finish(job, "dead", response.status, "remote_rejected")
        except FederationError as exc:
            self.finish(
                job, "retry" if exc.status >= 500 or exc.status == 429 else "dead", error=exc.code
            )
        return True
