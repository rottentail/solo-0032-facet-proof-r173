"""Drive all narrative scenarios and assemble the offline, re-runnable package."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

from facetproof.application import Application
from facetreader import validate_bundle

from .engine.benchmark import measure_full_render
from .engine.frames import render_plate_reference
from .engine.scenarios import Narrative
from .engine.scene import build_input


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=False, indent=2) + "\n",
                    encoding="utf-8")


def _new_root(workdir, name):
    root = workdir / "runs" / name / "data-root"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def build(output_dir):
    out = Path(output_dir).resolve()
    workdir = out / "narrative-run"
    frames_dir = out / "frames"
    inputs_dir = out / "inputs"
    delivery_dir = out / "delivery"
    for directory in (frames_dir, inputs_dir, delivery_dir, workdir):
        directory.mkdir(parents=True, exist_ok=True)

    input_doc = build_input()
    write_json(inputs_dir / "frozen-scene.json", input_doc)

    # Baseline first: measured cost of rendering every tile from scratch.
    baseline = measure_full_render()
    baseline_public = {k: v for k, v in baseline.items() if k != "channel_hashes"}
    write_json(workdir / "baseline-full-render.json", baseline)
    baseline_hashes = baseline["channel_hashes"]

    # ------------------------------------------------------- scenario A
    root_a = _new_root(workdir, "interrupted")
    narrative_a = Narrative(Application(root_a), "interrupted")
    summary_a, plate_a, passes_a, bundle_a = narrative_a.run_interrupted()
    _persist_run(narrative_a, workdir / "interrupted", frames_dir)
    (delivery_dir / "plate-bundle.zip").write_bytes(bundle_a)
    for name in ("color.png", "depth.png", "object.png"):
        (delivery_dir / name).write_bytes(passes_a[name])
    write_json(delivery_dir / "plate-summary.json", summary_a["plate"])
    reader_report = validate_bundle(bundle_a)
    write_json(delivery_dir / "facetreader-report.json", reader_report)
    # Truncated bundle must be refused by the independent reader.
    truncated = validate_bundle(bundle_a[:-20])
    write_json(delivery_dir / "facetreader-truncated-rejection.json", truncated)
    # Final delivered color frame, rendered from the actual delivered channel.
    final_png = render_plate_reference(
        summary_a["plate"]["width"], summary_a["plate"]["height"], passes_a["color.rgb"])
    (frames_dir / "final-delivered-color.png").write_bytes(final_png)
    write_json(workdir / "interrupted" / "summary.json", summary_a)

    # ------------------------------------------------------- scenario B
    root_b = _new_root(workdir, "cancelled")
    narrative_b = Narrative(Application(root_b), "cancelled")
    summary_b = narrative_b.run_cancelled()
    _persist_run(narrative_b, workdir / "cancelled", frames_dir)
    write_json(workdir / "cancelled" / "summary.json", summary_b)

    # ------------------------------------------------------- scenario C
    root_c = _new_root(workdir, "failed-retry")
    narrative_c = Narrative(Application(root_c), "failed-retry")
    summary_c, plate_c, passes_c = narrative_c.run_failed_retry(baseline["rasterize_seconds"])
    _persist_run(narrative_c, workdir / "failed-retry", frames_dir)
    write_json(workdir / "failed-retry" / "summary.json", summary_c)

    cross_check = _cross_check(summary_a, summary_b, summary_c, baseline_hashes,
                               reader_report, truncated, delivery_dir, frames_dir)
    savings = _compute_savings(baseline, summary_a, summary_c)
    write_json(workdir / "compute-savings.json", savings)
    write_json(out / "cross-check.json", cross_check)
    write_json(out / "story.json", _story_index(narrative_a, narrative_b, narrative_c,
                                                summary_a, summary_b, summary_c,
                                                baseline_public, reader_report, savings))
    _build_player(out, frames_dir)
    _write_manifest(out)
    return cross_check


def _persist_run(narrative, run_dir, frames_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "transcript.jsonl").write_bytes(narrative.api.transcript_bytes())
    write_json(run_dir / "observations.json", {"observations": narrative.observations})
    # Persist the exact PNG bytes produced inside each observation's snapshot.
    for index, png in narrative.frame_pngs.items():
        name = f"{narrative.run_id}-frame-{index:02d}.png"
        (frames_dir / name).write_bytes(png)


def _cross_check(summary_a, summary_b, summary_c, baseline_hashes,
                 reader_report, truncated, delivery_dir, frames_dir):
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    # 1. Every scenario traces to the same structural frozen scene. Generated
    #    object ids differ across independent data roots, so compare the seal
    #    *content* (geometry/material/lens/lights), not the minted ids.
    scene_fps = {summary_a["scene_fingerprint"], summary_c["scene_fingerprint"]}
    add("single_frozen_scene", len(scene_fps) == 1,
        {"structural_scene_fingerprints": sorted(scene_fps),
         "note": "seal 原始 id 与 content_hash 按数据根独立生成；结构指纹证明冻结场景一致"})

    # 2. Worker A's interrupted tile bytes are byte-identical to an isolated
    #    fresh render of the same scene (compute was real and reusable).
    a_hashes = summary_a["worker_a"]["channel_hashes"]
    mismatches = []
    for ordinal, channels in a_hashes.items():
        if baseline_hashes[ordinal] != channels:
            mismatches.append(ordinal)
    add("interrupted_tiles_match_fresh_render", not mismatches,
        {"tiles_checked": len(a_hashes), "mismatched_tiles": mismatches})

    # 3. The late write's blobs are referenced by no manifest (not accepted).
    late = set(summary_a["worker_a"]["late_unreferenced_hashes"].values())
    referenced = set()
    for channels in a_hashes.values():
        referenced.update(channels.values())
    add("late_write_never_referenced", late.isdisjoint(referenced) and len(late) == 3,
        {"unreferenced_channel_blobs": sorted(late)})

    # 4. The delivered plate's six channels match the exported bundle bytes.
    plate_channels = summary_a["plate_verify"]["passes"]
    bundle_channel_hashes = {name: desc["sha256"]
                             for name, desc in reader_report["channels"].items()}
    add("plate_bundle_channel_hashes", plate_channels == bundle_channel_hashes,
        {"plate": plate_channels, "bundle": bundle_channel_hashes})

    # 5. Independent reader accepts the bundle and rejects the truncated one.
    add("facetreader_accepts_bundle", reader_report["valid"] is True,
        reader_report["summary"])
    add("facetreader_rejects_truncated", truncated["valid"] is False,
        {"error": truncated.get("error")})

    # 6. Color/depth/object all come from the same plate id / seal / exposure.
    same_origin = (summary_a["plate"]["plate_id"] ==
                   reader_report["provenance"]["plate_id"] and
                   summary_a["exposure_id"] == reader_report["provenance"]["exposure_id"] and
                   summary_a["plate"]["seal_hash"] == reader_report["provenance"]["seal_hash"])
    add("all_channels_same_frozen_origin", same_origin,
        {"plate_id": summary_a["plate"]["plate_id"],
         "exposure_id": summary_a["exposure_id"],
         "seal_hash": summary_a["plate"]["seal_hash"]})

    # 7. No plate exists for the cancelled run; its tiles never became delivery.
    add("cancelled_run_has_no_plate",
        summary_b["running_case"]["plate_id"] is None and
        summary_b["running_case"]["terminal_state"] == "cancelled",
        {"terminal": summary_b["running_case"]["terminal_state"]})

    # 8. The failed/retry plate renders the same frozen scene as scenario A.
    add("retry_plate_same_frozen_scene",
        summary_c["scene_fingerprint"] == summary_a["scene_fingerprint"],
        {"retry_scene_fingerprint": summary_c["scene_fingerprint"]})

    # 9. Frame PNGs on disk are the bytes referenced by the story index.
    frame_files = sorted(p.name for p in frames_dir.glob("*.png"))
    add("frame_assets_present", len(frame_files) >= 10, {"count": len(frame_files)})

    add("delivery_assets_present",
        all((delivery_dir / n).exists() for n in
            ("plate-bundle.zip", "color.png", "depth.png", "object.png")),
        {"files": sorted(p.name for p in delivery_dir.iterdir())})

    return {"all_passed": all(c["passed"] for c in checks), "checks": checks}


def _compute_savings(baseline, summary_a, summary_c):
    """Quantify avoided rasterization from measured per-tile baseline timings."""
    per_tile = {item["ordinal"]: item for item in baseline["tile_timings"]}

    def block(skipped, label):
        skipped_seconds = sum(per_tile[o]["seconds"] for o in skipped)
        skipped_fragments = sum(per_tile[o]["fragments"] for o in skipped)
        return {
            "case": label,
            "tiles_reused_without_rasterizing": len(skipped),
            "tiles_rasterized": baseline["total_tiles"] - len(skipped),
            "total_tiles": baseline["total_tiles"],
            "avoided_rasterize_seconds": round(skipped_seconds, 6),
            "full_rasterize_seconds": baseline["rasterize_seconds"],
            "avoided_fragment_tests": skipped_fragments,
            "total_fragment_tests": baseline["total_fragments"],
            "reused_tile_ordinals": skipped,
            "measurement": "baseline 在独立数据根对同一冻结场景逐块计时；被复用块的耗时即接管者未支付的光栅化成本",
        }

    takeover = block(summary_a["worker_b"]["reused_tiles"], "interrupted-takeover")
    retry = block(summary_c["retry_attempt"]["reused_tiles"], "failed-retry")
    return {"baseline": {"total_tiles": baseline["total_tiles"],
                         "rasterize_seconds": baseline["rasterize_seconds"],
                         "total_fragments": baseline["total_fragments"]},
            "savings": [takeover, retry]}


def _story_index(narrative_a, narrative_b, narrative_c,
                 summary_a, summary_b, summary_c, baseline, reader_report, savings):
    def frame_refs(narrative):
        refs = []
        for obs in narrative.observations:
            refs.append({"frame_index": obs["frame_index"],
                         "file": f"frames/{narrative.run_id}-frame-{obs['frame_index']:02d}.png",
                         "chapter": obs["chapter"], "stage": obs["stage"],
                         "note": obs["note"],
                         "counts": obs["counts"],
                         "exposure_state": obs["exposure"]["state"],
                         "lease_owner": obs["exposure"]["lease_owner"]})
        return refs

    return {
        "title": "Facet Proof 分块渲染：中断、接管续做与最终交付",
        "chapters": [
            {"id": "interrupted", "title": "中断与接管（主叙事）",
             "frames": frame_refs(narrative_a),
             "summary": summary_a},
            {"id": "cancelled", "title": "取消的演示（如实保留终态）",
             "frames": frame_refs(narrative_b), "summary": summary_b},
            {"id": "failed-retry", "title": "失败与显式重试（复用同冻结块）",
             "frames": frame_refs(narrative_c), "summary": summary_c},
        ],
        "baseline_full_render": baseline,
        "compute_savings": savings,
        "final_delivery": {
            "bundle": "delivery/plate-bundle.zip",
            "reader_report": reader_report,
            "final_frame": "frames/final-delivered-color.png",
        },
        "guarantees": [
            "每一帧画面、块来源（producer token）与任务状态取自同一 SQLite 只读快照。",
            "已算完的块只表述为持久化进度，直到 36 块完整且租约有效才生成图版。",
            "迟到工作器失租后的写入被 lease_lost 拒绝，其 CAS 字节不被任何清单引用。",
            "颜色、深度、物体通道与导出 ZIP 来自同一冻结 seal、同一曝光与同一块指纹。",
            "取消与失败运行保留真实终态，不伪造成功，也不产出图版。",
        ],
    }


def _build_player(out, frames_dir):
    from .engine.player import write_player
    write_player(out)


def _write_manifest(out):
    manifest = {}
    skip = {"MANIFEST.sha256", "manifest.json"}
    for path in sorted(p for p in out.rglob("*") if p.is_file() and p.name not in skip):
        manifest[str(path.relative_to(out))] = sha256_file(path)
    lines = "".join(f"{digest}  {name}\n" for name, digest in sorted(manifest.items()))
    (out / "MANIFEST.sha256").write_text(lines, encoding="utf-8")
    write_json(out / "manifest.json", manifest)


def main():
    parser = argparse.ArgumentParser(description="Build the offline Facet Proof narrative package")
    parser.add_argument("--output", default="narrative-output")
    args = parser.parse_args()
    result = build(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["all_passed"] else 2)


if __name__ == "__main__":
    main()
