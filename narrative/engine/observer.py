"""Single-snapshot observations: state, tile provenance and one frame image.

An :class:`Observation` is built inside one SQLite read transaction. The
exposure row, the tile manifest rows and the frame row therefore describe
the same point in time; the frame PNG is decoded only from tile bytes that
this same snapshot marks complete. Pending tiles are rendered as a hatch and
can never be mistaken for a reviewed plate.
"""
import hashlib
import json

from facetproof.tile_protocol import decode

from .frames import render_observation_frame


class ObservationError(RuntimeError):
    """Raised when a rendered frame would disagree with the SQL snapshot."""


class Observer:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.blobs = app.blobs

    def _records(self, conn, exposure_id):
        rows = conn.execute(
            "SELECT * FROM facet_tiles WHERE exposure_id=? ORDER BY ordinal",
            (exposure_id,)).fetchall()
        records = []
        for row in rows:
            item = dict(row)
            item["bounds"] = json.loads(row["bounds"])
            item["payload"] = json.loads(row["payload"]) if row["payload"] is not None else None
            records.append(item)
        return records

    def observe(self, exposure_id, *, chapter, stage, token_labels, frame_index,
                note, extra=None):
        """Open one read transaction and capture SQL state + frame together."""
        conn = self.db.connect()
        try:
            conn.execute("BEGIN")
            exp = dict(conn.execute(
                "SELECT * FROM exposures WHERE id=?", (exposure_id,)).fetchone())
            exp["request"] = json.loads(exp["request"])
            frame_row = conn.execute(
                "SELECT * FROM facet_tile_frames WHERE exposure_id=?",
                (exposure_id,)).fetchone()
            planned = frame_row is not None
            records = self._records(conn, exposure_id) if planned else []

            # Independent readability check of every committed tile, using the
            # CAS bytes reachable from this snapshot. A missing/corrupt blob
            # fails the observation rather than hiding the gap.
            tile_views = []
            verified_channel_hashes = set()
            for tile in records:
                view = {
                    "ordinal": tile["ordinal"],
                    "bounds": tile["bounds"],
                    "state": tile["state"],
                    "producer_token": tile["producer_token"],
                    "producer_label": token_labels.get(tile["producer_token"], "OTHER"),
                }
                if tile["state"] == "complete":
                    if tile["payload"] is None:
                        raise ObservationError(f"tile {tile['ordinal']} complete without payload")
                    decoded = decode(tile["payload"], tile["bounds"],
                                     tile["fingerprint"], self.blobs, objects=None)
                    view["triangles"] = tile["payload"]["triangles"]
                    view["fragments"] = tile["payload"]["fragments"]
                    view["channels"] = {name: desc["sha256"]
                                        for name, desc in tile["payload"]["channels"].items()}
                    verified_channel_hashes.update(view["channels"].values())
                else:
                    if tile["payload"] is not None or tile["producer_token"] is not None:
                        raise ObservationError(
                            f"pending tile {tile['ordinal']} carries payload/producer data")
                    view["triangles"] = None
                    view["fragments"] = None
                    view["channels"] = None
                tile_views.append(view)

            width = frame_row["width"] if planned else exp["request"]["width"] * exp["request"]["samples"]
            height = frame_row["height"] if planned else exp["request"]["height"] * exp["request"]["samples"]
            fingerprint = frame_row["fingerprint"] if planned else None
            # Even before a manifest exists, render an honest empty canvas so
            # the playback never shows a stale or invented image.
            frame_png = render_observation_frame(
                width, height, records, self.blobs, token_labels, planned=planned)
            complete = [t for t in tile_views if t["state"] == "complete"]
            observation = {
                "frame_index": frame_index,
                "chapter": chapter,
                "stage": stage,
                "note": note,
                "exposure": {
                    "id": exp["id"],
                    "state": exp["state"],
                    "attempt": exp["attempt"],
                    "lease_owner": token_labels.get(exp["token"]) if exp["token"] else None,
                    "lease_active": bool(exp["token"] and exp["lease_until"]),
                    "cancel_requested": bool(exp["cancel_requested"]),
                    "error": exp["error"],
                    "plate_id": exp["plate_id"],
                    "request": exp["request"],
                },
                "frame": {
                    "planned": planned,
                    "width": width,
                    "height": height,
                    "tile_edge": frame_row["tile_size"] if planned else None,
                    "total_tiles": frame_row["total_tiles"] if planned else 0,
                    "fingerprint": fingerprint,
                },
                "tiles": tile_views,
                "counts": {
                    "complete": len(complete),
                    "pending": len(tile_views) - len(complete),
                    "committed_pixels": sum(
                        (t["bounds"][2] - t["bounds"][0]) *
                        (t["bounds"][3] - t["bounds"][1]) for t in complete),
                    "total_pixels": width * height,
                },
                "verified_channel_blob_hashes": sorted(verified_channel_hashes),
                "consistency": {
                    "single_sql_snapshot": True,
                    "committed_tiles_readable": True,
                    "pending_tiles_have_no_payload": True,
                },
                "extra": extra or {},
            }
            return observation, frame_png
        finally:
            conn.rollback()
            conn.close()

    def events(self, exposure_id):
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT seq,action,payload,created FROM events WHERE subject=? ORDER BY seq",
                (exposure_id,)).fetchall()
            return [{"seq": r["seq"], "action": r["action"],
                     "payload": json.loads(r["payload"])} for r in rows]
        finally:
            conn.close()

    def tile_table_fingerprint(self, exposure_id):
        """Hash the authoritative manifest rows for cross-check statements."""
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT ordinal,state,producer_token,payload,fingerprint FROM facet_tiles "
                "WHERE exposure_id=? ORDER BY ordinal", (exposure_id,)).fetchall()
            material = json.dumps([dict(r) for r in rows], sort_keys=True)
            return hashlib.sha256(material.encode()).hexdigest()
        finally:
            conn.close()
