"""In-process JSON HTTP client with a faithful request/response transcript.

Every public API action goes through :class:`ApiClient.call`, which invokes
the exact routing entrypoint used by ``facetproof.server`` and appends one
JSONL record. Internal control actions (manual lease work used to stage an
interruption) are recorded too, but with ``channel: "internal-control"`` so
the transcript can never be mistaken for a production HTTP exchange.
"""
import json
import time

from facetproof.errors import Fault
from facetproof.routing import PREFIX, route_request
from facetproof.values import canonical


class ApiClient:
    def __init__(self, app, run_id):
        self.app = app
        self.run_id = run_id
        self.records = []
        self._seq = 0

    def call(self, method, route, data=None, *, channel="http", note=""):
        """Call a documented route; return the decoded JSON value.

        Faults are caught and recorded (with the real HTTP status) then
        re-raised so scenarios can assert on the rejection honestly.
        """
        self._seq += 1
        target = PREFIX + route
        record = {
            "seq": self._seq,
            "run_id": self.run_id,
            "channel": channel,
            "method": method,
            "path": target,
            "request": data if data is not None else {},
            "note": note,
            "wallclock": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        }
        try:
            value = route_request(self.app, method, target, data if data is not None else {})
            if isinstance(value, tuple):
                payload, mime = value
                if mime == "application/zip":
                    record["response"] = {"content_type": "application/zip", "bytes": len(payload),
                                          "sha256": _sha256(payload)}
                    record["status"] = 200
                    self.records.append(record)
                    return value
                record["response"] = json.loads(payload.decode("utf-8"))
            else:
                record["response"] = value
            record["status"] = 200
            self.records.append(record)
            return value
        except Fault as exc:
            record["status"] = exc.status
            record["response"] = exc.as_dict()
            record["fault"] = True
            self.records.append(record)
            raise

    def control(self, action, detail, result, *, note=""):
        """Record a non-HTTP internal control step used to stage the demo."""
        self._seq += 1
        self.records.append({
            "seq": self._seq,
            "run_id": self.run_id,
            "channel": "internal-control",
            "method": "INTERNAL",
            "path": "control/" + action,
            "request": detail,
            "response": result,
            "status": 200,
            "note": note,
            "wallclock": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        })

    def transcript_bytes(self):
        return ("\n".join(canonical(record) for record in self.records) + "\n").encode("utf-8")


def _sha256(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()
