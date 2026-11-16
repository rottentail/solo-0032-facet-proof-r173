"""Isolated full-render baseline used to quantify what resume saved.

The baseline runs in its own temporary data root against an identical frozen
scene. It measures the real rasterization work for the whole 384x384 canvas so
the narrative can state, with measured numbers, how many windows and how much
rasterizer time the takeover worker did not pay for.
"""
import tempfile
import time

from facetproof.application import Application
from facetproof.routing import PREFIX, route_request
from facetproof.raster import rasterize
from facetproof.tile_protocol import encode

from .scene import (KEEL_MESH, PIGMENTS, PLACEMENTS, RENDER_REQUEST, SEAL_REVISION,
                    TILE_EDGE, WING_MESH, ASSEMBLY)


def _call(app, method, route, data=None):
    value = route_request(app, method, PREFIX + route, data or {})
    return value[0] if isinstance(value, tuple) else value


def measure_full_render():
    """Return measured work for a from-scratch render of the frozen scene."""
    root = tempfile.mkdtemp(prefix="facet-narrative-baseline-")
    app = Application(root)
    wing = _call(app, "POST", "meshes", WING_MESH)
    keel = _call(app, "POST", "meshes", KEEL_MESH)
    p1 = _call(app, "POST", "pigments", PIGMENTS[0])
    p2 = _call(app, "POST", "pigments", PIGMENTS[1])
    assembly = _call(app, "POST", "assemblies",
                     {"name": ASSEMBLY["name"], "lens": ASSEMBLY["lens"],
                      "illumination": ASSEMBLY["illumination"]})
    _call(app, "PUT", "assemblies/" + assembly["id"],
          {"revision": 1, "placements": [
              {"key": "wing", "mesh_id": wing["id"], "pigment_id": p1["id"],
               "transform": PLACEMENTS[0]["transform"]},
              {"key": "keel", "mesh_id": keel["id"], "pigment_id": p2["id"],
               "transform": PLACEMENTS[1]["transform"]}]})
    seal = _call(app, "POST", "assemblies/" + assembly["id"] + "/seal",
                 {"revision": SEAL_REVISION})
    exposure = _call(app, "POST", "seals/" + seal["id"] + "/exposures",
                     {**RENDER_REQUEST, "key": "baseline"})
    claim = app.exposures.claim(lease_seconds=120)
    from facetproof.planner import build_plan
    plan = build_plan(seal["payload"], claim["request"])
    frame, records = app.worker.tiles.store.prepare(claim, plan, TILE_EDGE)

    def checkpoint():
        pass

    tile_timings = []
    total_fragments = 0
    started = time.monotonic()
    channel_hashes = {}
    for record in records:
        t0 = time.monotonic()
        rendered = rasterize(plan, checkpoint, tuple(record["bounds"]))
        payload = encode(rendered, record["bounds"], frame["fingerprint"], app.blobs)
        dt = time.monotonic() - t0
        app.worker.tiles.store.commit(claim, record["ordinal"], payload)
        tile_timings.append({"ordinal": record["ordinal"], "seconds": round(dt, 6),
                             "fragments": rendered["fragments"]})
        total_fragments += rendered["fragments"]
        channel_hashes[record["ordinal"]] = {
            name: desc["sha256"] for name, desc in payload["channels"].items()}
    raster_seconds = time.monotonic() - started
    return {
        "baseline_root": root,
        "frame_fingerprint": frame["fingerprint"],
        "total_tiles": len(records),
        "rasterize_seconds": round(raster_seconds, 6),
        "total_fragments": total_fragments,
        "tile_timings": tile_timings,
        "channel_hashes": channel_hashes,
    }
