"""The single frozen scene used by every narrative scenario.

All scenarios (interrupted takeover, cancellation, failure/retry, final
delivery) replay these exact inputs, so every tile fingerprint, seal hash
and plate channel traces back to one frozen scene definition. The file is
both readable documentation and the re-runnable input for build_narrative.
"""
import copy

# Two objects so the object-id channel and the frozen object mapping have
# something real to show. Both meshes are closed two-sided solids.
WING_MESH = {
    "name": "斜翼",
    "vertices": [
        [-1.0, -0.8, -0.6], [1.0, -0.8, -0.6], [1.0, 0.8, -0.6], [-1.0, 0.8, -0.6],
        [-0.7, -0.5, 0.9], [0.7, -0.5, 0.9], [0.7, 0.5, 0.9], [-0.7, 0.5, 0.9],
    ],
    "faces": [
        [0, 1, 2], [0, 2, 3],
        [4, 6, 5], [4, 7, 6],
        [0, 4, 5], [0, 5, 1],
        [2, 6, 7], [2, 7, 3],
        [1, 5, 6], [1, 6, 2],
        [3, 7, 4], [3, 4, 0],
    ],
}

KEEL_MESH = {
    "name": "下腹鳍",
    "vertices": [
        [-0.35, -0.85, -0.2], [0.35, -0.85, -0.2],
        [0.35, -0.85, 0.45], [-0.35, -0.85, 0.45],
        [0.0, -1.55, 0.1],
    ],
    "faces": [
        [0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4],
        [0, 3, 2], [0, 2, 1],
    ],
}

PIGMENTS = [
    {"name": "釉青", "color": [0.12, 0.62, 0.61], "roughness": 0.55, "specular": 0.25, "two_sided": True},
    {"name": "赭金", "color": [0.78, 0.45, 0.16], "roughness": 0.7, "specular": 0.15, "two_sided": True},
]

ASSEMBLY = {
    "name": "斜翼结构说明图",
    "lens": {"eye": [3.0, 2.0, 4.0], "target": [0.0, -0.2, 0.0], "projection": "perspective", "fov": 42},
    "illumination": {
        "ambient": 0.18,
        "background": [0.93, 0.95, 0.97],
        "lights": [
            {"direction": [1.0, 2.0, 3.0], "intensity": 0.8, "color": [1.0, 1.0, 1.0]},
            {"direction": [-2.0, -1.0, 1.0], "intensity": 0.35, "color": [0.85, 0.9, 1.0]},
        ],
    },
}

# Placement revision 1 is the only arrangement ever sealed.
PLACEMENTS_REVISION = 1
PLACEMENTS = [
    {"key": "wing", "transform": {"euler": [0.0, 20.0, 0.0]}},
    {"key": "keel", "transform": {"euler": [0.0, -10.0, 0.0]}},
]
SEAL_REVISION = 2

# One render definition shared by all scenarios: 192x192 at 2x supersamples
# renders on a 384x384 high-sample canvas, which the 64px protocol divides
# into exactly 36 (6x6) tiles.
RENDER_REQUEST = {"key": "frozen-narrative", "width": 192, "height": 192, "samples": 2}
TILE_EDGE = 64


def build_input():
    """Return a fresh, self-contained re-runnable input document."""
    return {
        "schema": "facet-narrative-input-1",
        "description": "Facet Proof 分块渲染中断/接管/交付解说的可复跑输入（纯标准库，离线）。",
        "steps": [
            {"api": "POST /facet/v1/meshes", "body": WING_MESH, "bind": "wing_mesh"},
            {"api": "POST /facet/v1/meshes", "body": KEEL_MESH, "bind": "keel_mesh"},
            {"api": "POST /facet/v1/pigments", "body": PIGMENTS[0], "bind": "wing_pigment"},
            {"api": "POST /facet/v1/pigments", "body": PIGMENTS[1], "bind": "keel_pigment"},
            {"api": "POST /facet/v1/assemblies",
             "body": {"name": ASSEMBLY["name"], "lens": ASSEMBLY["lens"],
                      "illumination": ASSEMBLY["illumination"]},
             "bind": "assembly"},
            {"api": "PUT /facet/v1/assemblies/{assembly}",
             "body": {"revision": PLACEMENTS_REVISION, "placements": [
                 {"key": "wing", "mesh_id": "{wing_mesh.id}", "pigment_id": "{wing_pigment.id}",
                  "transform": PLACEMENTS[0]["transform"]},
                 {"key": "keel", "mesh_id": "{keel_mesh.id}", "pigment_id": "{keel_pigment.id}",
                  "transform": PLACEMENTS[1]["transform"]}]},
             "bind": "assembly_revised"},
            {"api": "POST /facet/v1/assemblies/{assembly}/seal",
             "body": {"revision": SEAL_REVISION}, "bind": "seal"},
        ],
        "render_request": RENDER_REQUEST,
        "tile_edge": TILE_EDGE,
        "notes": [
            "所有场景使用同一份网格、材质、镜头与光照；seal 之后上游再修改不影响该冻结闭包。",
            "渲染请求固定为 192x192、samples=2，高采样画布 384x384，按 64px 分为 36 块。",
            "复跑命令：python3 -m narrative.build_narrative --output narrative-output",
        ],
    }


def frozen_input_json():
    return copy.deepcopy(build_input())


def scene_fingerprint(seal):
    """Structural fingerprint of the frozen geometry/material/lens content.

    Independent data roots mint different generated ids, so raw seal hashes
    differ across runs; this digest removes the identity fields and keeps the
    actual frozen scene (vertices, faces, pigments, transforms, lens, lights),
    letting the narrative prove that separate runs rendered one identical scene.
    """
    from facetproof.values import digest

    closure = []
    for item in seal["payload"]["closure"]:
        closure.append({
            "key": item["placement"]["key"],
            "transform": item["placement"]["transform"],
            "mesh": {"name": item["mesh"]["name"], "vertices": item["mesh"]["vertices"],
                     "faces": item["mesh"]["faces"], "bounds": item["mesh"]["bounds"]},
            "pigment": {k: v for k, v in item["pigment"].items()},
        })
    structural = {
        "name": seal["payload"]["name"],
        "lens": seal["payload"]["lens"],
        "illumination": seal["payload"]["illumination"],
        "closure": sorted(closure, key=lambda entry: entry["key"]),
    }
    return digest(structural)
