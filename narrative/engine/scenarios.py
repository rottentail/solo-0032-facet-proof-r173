"""Real, re-runnable Facet Proof scenarios rendered into observations.

Three independent data roots are produced, each with its own full transcript:

* ``interrupted``  - worker A commits real tiles, its lease dies, a late write
  is rejected, worker B recovers the lease, reuses the frozen tiles and only
  pays for the remaining windows, then the complete plate is delivered.
* ``cancelled``    - a queued cancellation and a running-cancellation pair,
  retained honestly with their terminal states and released capacity.
* ``failed-retry`` - a worker fails mid exposure; the explicit retry reuses
  the complete tiles under the same frozen fingerprint.

Nothing here fakes timing: leases really expire against the wall clock via
the same ``now()`` the production code uses, and every rejected call is the
service returning its real Fault.
"""
import time

from facetproof.errors import Fault
from facetproof.passes import serialize_passes
from facetproof.planner import build_plan
from facetproof.raster import rasterize
from facetproof.tile_protocol import encode
from facetproof.values import digest, now

from .observer import Observer
from .scene import RENDER_REQUEST, SEAL_REVISION, TILE_EDGE, scene_fingerprint
from .transcript import ApiClient

# How many of the 36 tiles worker A finishes before its process is killed.
A_TILES_BEFORE_CRASH = 9
# Short lease (seconds) so the interruption is real but the replay stays fast.
SHORT_LEASE = 1
LEASE_MARGIN = 1.25


class Narrative:
    def __init__(self, app, run_id):
        self.app = app
        self.api = ApiClient(app, run_id)
        self.observer = Observer(app)
        self.run_id = run_id
        self.ids = {}
        self.token_labels = {}
        self.observations = []
        self.frame_pngs = {}
        self._frame_no = 0

    # ------------------------------------------------------------------ setup
    def commission_scene(self):
        from .scene import WING_MESH, KEEL_MESH, PIGMENTS, ASSEMBLY, PLACEMENTS
        self.ids["wing_mesh"] = self.api.call("POST", "meshes", WING_MESH)["id"]
        self.ids["keel_mesh"] = self.api.call("POST", "meshes", KEEL_MESH)["id"]
        self.ids["wing_pigment"] = self.api.call("POST", "pigments", PIGMENTS[0])["id"]
        self.ids["keel_pigment"] = self.api.call("POST", "pigments", PIGMENTS[1])["id"]
        assembly = self.api.call("POST", "assemblies",
                                 {"name": ASSEMBLY["name"], "lens": ASSEMBLY["lens"],
                                  "illumination": ASSEMBLY["illumination"]})
        self.ids["assembly"] = assembly["id"]
        self.api.call("PUT", "assemblies/" + assembly["id"], {
            "revision": 1,
            "placements": [
                {"key": "wing", "mesh_id": self.ids["wing_mesh"],
                 "pigment_id": self.ids["wing_pigment"], "transform": PLACEMENTS[0]["transform"]},
                {"key": "keel", "mesh_id": self.ids["keel_mesh"],
                 "pigment_id": self.ids["keel_pigment"], "transform": PLACEMENTS[1]["transform"]},
            ]})
        seal = self.api.call("POST", "assemblies/" + assembly["id"] + "/seal",
                             {"revision": SEAL_REVISION})
        self.ids["seal"] = seal
        return seal

    def submit_exposure(self, key):
        exposure = self.api.call(
            "POST", "seals/" + self.ids["seal"]["id"] + "/exposures",
            {**RENDER_REQUEST, "key": key})
        self.ids["exposure"] = exposure["id"]
        return exposure

    # ------------------------------------------------------------- primitives
    def _plan(self, claim):
        with self.app.db.unit_of_work() as conn:
            seal = self.app.db.get(conn, claim["seal_id"], "seal")["payload"]
        return build_plan(seal, claim["request"])

    def _render_tile(self, plan, frame_fingerprint, record):
        def checkpoint():
            pass
        rendered = rasterize(plan, checkpoint, tuple(record["bounds"]))
        return encode(rendered, record["bounds"], frame_fingerprint, self.app.blobs)

    def observe(self, chapter, stage, note, *, extra=None):
        self._frame_no += 1
        observation, frame_png = self.observer.observe(
            self.ids["exposure"], chapter=chapter, stage=stage,
            token_labels=self.token_labels, frame_index=self._frame_no,
            note=note, extra=extra)
        self.observations.append(observation)
        self.frame_pngs[self._frame_no] = frame_png
        return observation, frame_png

    def _expire_lease(self, claim):
        """Wait out a real short lease; record the wall-clock boundary."""
        time.sleep(max(0.0, claim["lease_until"] - now()) + 0.05)

    # ============================================================ scenario A
    def run_interrupted(self):
        self.commission_scene()
        self.submit_exposure("frozen-narrative")
        initial_resources = self.api.call("GET", "resources")
        self.observe("interrupted", "queued",
                     "曝光已接受并预留高采样像素额度；尚未生成任何分块清单，画布为全空。",
                     extra={"resources": initial_resources})

        # Worker A takes a real (short) lease and prepares the tile frame.
        claim_a = self.app.exposures.claim(lease_seconds=SHORT_LEASE)
        self.token_labels[claim_a["token"]] = "A"
        self.api.control("worker.claim",
                         {"lease_seconds": SHORT_LEASE, "exposure_id": claim_a["id"]},
                         {"token": claim_a["token"], "attempt": claim_a["attempt"],
                          "state": claim_a["state"]},
                         note="工作器 A 领取任务（内部步骤，等价于 worker/step 的领取动作）")
        plan = self._plan(claim_a)
        frame, records = self.app.worker.tiles.store.prepare(claim_a, plan, TILE_EDGE)
        self.api.control("tiles.prepare",
                         {"exposure_id": claim_a["id"], "tile_size": TILE_EDGE},
                         {"fingerprint": frame["fingerprint"], "total_tiles": frame["total_tiles"]},
                         note="按冻结请求建立 36 块清单")
        self.observe("interrupted", "frame-prepared",
                     "分块清单已建立，全部 36 块处于 pending；还没有任何可审阅像素。")

        # A renders and durably commits the first tiles, one observation per
        # tile so the playback shows real incremental progress.
        a_channel_hashes = {}
        a_observations = []
        for ordinal in range(A_TILES_BEFORE_CRASH):
            record = records[ordinal]
            payload = self._render_tile(plan, frame["fingerprint"], record)
            committed = self.app.worker.tiles.store.commit(claim_a, ordinal, payload)
            assert committed is True
            a_channel_hashes[ordinal] = {k: d["sha256"] for k, d in payload["channels"].items()}
            self.api.control(
                "tile.commit",
                {"exposure_id": claim_a["id"], "ordinal": ordinal,
                 "bounds": record["bounds"], "producer": "A"},
                {"committed": True, "fragments": payload["fragments"],
                 "triangles": payload["triangles"],
                 "channels": {k: d["sha256"] for k, d in payload["channels"].items()}},
                note=f"工作器 A 完成并持久化第 {ordinal} 块")
            if ordinal in (0, 4, A_TILES_BEFORE_CRASH - 1):
                obs, _ = self.observe(
                    "interrupted", f"worker-a-tile-{ordinal}",
                    f"工作器 A 已提交 {ordinal + 1}/36 块；这些块是已持久化的计算进度，不是图版。")
                a_observations.append(obs["frame_index"])

        # The A process is killed; its lease keeps ticking and then expires.
        crash = {"reason": "worker_process_killed", "tiles_committed": A_TILES_BEFORE_CRASH,
                 "plate_published": False, "lease_seconds": SHORT_LEASE}
        self.api.control("worker.crash", {"exposure_id": claim_a["id"]}, crash,
                         note="工作器 A 进程被杀死；已提交块保留，租约开始倒计时")
        self._expire_lease(claim_a)

        # Late worker A wakes up and tries to publish one more tile. The bytes
        # are fully rendered, but publication must be refused (lease_lost).
        late_record = records[A_TILES_BEFORE_CRASH]
        late_payload = self._render_tile(plan, frame["fingerprint"], late_record)
        late_blob_hashes = {k: d["sha256"] for k, d in late_payload["channels"].items()}
        late_error = None
        try:
            self.app.worker.tiles.store.commit(claim_a, late_record["ordinal"], late_payload)
        except Fault as exc:
            late_error = exc.as_dict()
            late_error["status"] = exc.status
        assert late_error is not None and late_error["error"] == "lease_lost"
        self.api.control(
            "tile.commit-rejected",
            {"exposure_id": claim_a["id"], "ordinal": late_record["ordinal"],
             "producer": "A", "lease_until": claim_a["lease_until"], "now": now()},
            {"rejected": True, "fault": late_error,
             "rendered_but_uncommitted_channel_hashes": late_blob_hashes},
            note="迟到的工作器 A 试图在失租后提交第 9 块，服务拒绝；字节存在于 CAS 但不被任何清单引用")
        self.observe(
            "interrupted", "late-write-rejected",
            "失租后的迟到写入被拒绝：第 9 块仍是 pending，A 的产物没有冒充接管者认可的进度。",
            extra={"late_attempt": {"ordinal": late_record["ordinal"],
                                    "fault": late_error,
                                    "unreferenced_blob_hashes": late_blob_hashes}})

        # Recovery moves the expired lease back to the queue (real HTTP route).
        recovered = self.api.call("POST", "worker/recover")
        assert self.ids["exposure"] in recovered["recovered"]
        self.observe("interrupted", "recovered-to-queue",
                     "worker/recover 把过期租约回收为 queued；已提交的 9 块原样保留，等待同一冻结定义下的接管。")

        # Worker B claims and renders only what is missing.
        claim_b = self.app.exposures.claim(lease_seconds=120)
        self.token_labels[claim_b["token"]] = "B"
        self.api.control("worker.claim",
                         {"lease_seconds": 120, "exposure_id": claim_b["id"]},
                         {"token": claim_b["token"], "attempt": claim_b["attempt"],
                          "state": claim_b["state"]},
                         note="工作器 B 接管（attempt 增加，新租约 token）")
        frame_b, records_b = self.app.worker.tiles.store.prepare(claim_b, plan, TILE_EDGE)
        assert frame_b["fingerprint"] == frame["fingerprint"]
        self.api.control("tiles.prepare-resume",
                         {"exposure_id": claim_b["id"]},
                         {"fingerprint": frame_b["fingerprint"],
                          "reused_complete": sum(r["state"] == "complete" for r in records_b)},
                         note="接管者复用同一指纹的清单，已完成块直接跳过")

        # Instrument the resume: which tiles are skipped vs rasterized.
        skip = [r["ordinal"] for r in records_b if r["state"] == "complete"]
        todo = [r["ordinal"] for r in records_b if r["state"] != "complete"]
        started = time.monotonic()
        rendered = self.app.worker.tiles.render(claim_b, plan, lambda: None)
        resume_seconds = time.monotonic() - started
        for ordinal in todo:
            self.api.control(
                "tile.commit",
                {"exposure_id": claim_b["id"], "ordinal": ordinal, "producer": "B"},
                {"committed": True},
                note=f"工作器 B 补算并提交第 {ordinal} 块")
        self.api.control(
            "tiles.assemble",
            {"exposure_id": claim_b["id"]},
            {"skipped_tiles": skip, "rasterized_tiles": todo,
             "resume_render_seconds": round(resume_seconds, 6)},
            note="全部 36 块齐备后才进行整幅装配；进度查询从未代替图版成功")
        self.observe(
            "interrupted", "takeover-complete",
            "B 只补算了 27 块，A 的 9 块按原 token 保留；36 块完整后才进入整幅下采样与图版提交。",
            extra={"resume": {"skipped_tiles": skip, "rasterized_tiles": todo,
                              "rasterized_count": len(todo), "reused_count": len(skip),
                              "resume_render_seconds": round(resume_seconds, 6)}})

        # Final plate via the same tail the production Worker.run_one uses.
        plate, plate_meta, passes = self._finish_plate(claim_b, plan, rendered)
        verify = self.api.call("POST", "plates/" + plate["id"] + "/verify")
        progress = self.api.call("GET", "exposures/" + self.ids["exposure"] + "/progress")
        self.observe(
            "interrupted", "plate-succeeded",
            "图版成功是独立事实：所有块完整、可读、租约有效且未取消才生成；progress.complete 与 state=succeeded 同时成立。",
            extra={"verification": verify, "progress": progress})

        bundle_raw = self.api.call("GET", "plates/" + plate["id"] + "/bundle")[0]
        self.ids["bundle_bytes"] = len(bundle_raw)
        proof = self.api.call("POST", "plates/" + plate["id"] + "/proofs",
                              {"note": "解说接收方审阅：通道与冻结来源一致"})
        decision = self.api.call("POST", "proofs/" + proof["id"] + "/decide",
                                 {"state": "accepted", "note": "接受图版"})
        final_resources = self.api.call("GET", "resources")

        events = self.observer.events(self.ids["exposure"])
        manifest_hash_after = self.observer.tile_table_fingerprint(self.ids["exposure"])
        summary = {
            "run_id": self.run_id,
            "scenario": "interrupted",
            "exposure_id": self.ids["exposure"],
            "seal_id": self.ids["seal"]["id"],
            "seal_content_hash": self.ids["seal"]["payload"]["content_hash"],
            "scene_fingerprint": scene_fingerprint(self.ids["seal"]),
            "frame_fingerprint": frame["fingerprint"],
            "worker_a": {"tiles_committed": A_TILES_BEFORE_CRASH,
                         "channel_hashes": a_channel_hashes,
                         "late_write_fault": late_error,
                         "late_unreferenced_hashes": late_blob_hashes},
            "worker_b": {"attempt": claim_b["attempt"],
                         "reused_tiles": skip, "rasterized_tiles": todo,
                         "resume_render_seconds": round(resume_seconds, 6)},
            "plate": plate_meta,
            "plate_verify": verify,
            "progress_final": progress,
            "proof_decision": decision["state"],
            "bundle_bytes": len(bundle_raw),
            "resources_before": initial_resources,
            "resources_after": final_resources,
            "tile_manifest_sha256": manifest_hash_after,
            "events": events,
        }
        return summary, plate, passes, bundle_raw

    def _finish_plate(self, claim, plan, rendered):
        passes, statistics = serialize_passes(plan, rendered, claim["request"])
        descriptors = {name: self.app.blobs.put(data) for name, data in passes.items()}
        with self.app.db.unit_of_work() as conn:
            seal = self.app.db.get(conn, claim["seal_id"], "seal")["payload"]
        payload = {
            "format": "facet-plate-1", "seal_id": claim["seal_id"],
            "seal_hash": seal["content_hash"], "request": claim["request"],
            "width": claim["request"]["width"], "height": claim["request"]["height"],
            "lens_hash": plan["lens_hash"], "objects": plan["objects"],
            "passes": descriptors, "statistics": statistics,
            "visual_hash": digest(descriptors)}
        self.app.exposures.heartbeat(claim["id"], claim["token"])
        plate = self.app.plates.finish(claim, payload)
        self.api.control("plate.finish",
                         {"exposure_id": claim["id"]},
                         {"plate_id": plate["id"], "state": plate["state"],
                          "visual_hash": payload["visual_hash"],
                          "statistics": statistics},
                         note="整幅画布下采样并提交最终图版（仅一次，整幅边界）")
        meta = {"plate_id": plate["id"], "state": plate["state"],
                "width": payload["width"], "height": payload["height"],
                "visual_hash": payload["visual_hash"],
                "seal_hash": payload["seal_hash"], "lens_hash": payload["lens_hash"],
                "passes": descriptors, "statistics": statistics,
                "objects": plan["objects"]}
        return plate, meta, passes

    # ============================================================ scenario B
    def run_cancelled(self):
        self.commission_scene()
        # B1: cancel while still queued.
        self.submit_exposure("cancel-queued")
        queued = self.api.call("POST", "exposures/" + self.ids["exposure"] + "/cancel")
        step = self.api.call("POST", "worker/step")
        self.observe("cancelled", "queued-cancel",
                     "排队中取消：状态直接为 cancelled，工作器领取返回 idle，未产生任何分块或图版。",
                     extra={"cancel_response": queued, "worker_step": step})
        retry_fault = self._expect_fault(
            "POST", "exposures/" + self.ids["exposure"] + "/retry", {})
        self.api.call("GET", "resources")
        first = {"exposure_id": self.ids["exposure"], "terminal_state": queued["state"],
                 "worker_step": step, "retry_rejected": retry_fault}

        # B2: cancel after the worker starts running (lease held, tiles made).
        self.submit_exposure("cancel-running")
        claim = self.app.exposures.claim(lease_seconds=120)
        self.token_labels[claim["token"]] = "C"
        self.api.control("worker.claim", {"lease_seconds": 120},
                         {"token": claim["token"], "attempt": claim["attempt"]},
                         note="工作器 C 领取第二个曝光")
        plan = self._plan(claim)
        frame, records = self.app.worker.tiles.store.prepare(claim, plan, TILE_EDGE)
        for ordinal in range(6):
            payload = self._render_tile(plan, frame["fingerprint"], records[ordinal])
            self.app.worker.tiles.store.commit(claim, ordinal, payload)
        self.api.control("tiles.partial", {"tiles": 6}, {"committed": 6},
                         note="取消前已持久化 6 块（真实计算，保留但永不成为图版）")
        cancel = self.api.call("POST", "exposures/" + self.ids["exposure"] + "/cancel")
        self.observe(
            "cancelled", "running-cancel-requested",
            "运行中收到取消：cancel_requested=1，但工作器确认终态前额度不释放、状态仍显示运行占有的事实。",
            extra={"cancel_response": cancel})
        # The running worker acknowledges the terminal state through the same
        # fail() path the production worker uses when the heartbeat Fault fires.
        settled = self.app.exposures.fail(claim["id"], claim["token"], "cancelled")
        self.api.control("worker.acknowledge-cancel",
                         {"token": claim["token"]},
                         {"state": settled["state"], "error": settled["error"]},
                         note="工作器在心跳处观察到取消，确认终态并释放额度")
        after = self.api.call("GET", "exposures/" + self.ids["exposure"])
        resources = self.api.call("GET", "resources")
        self.observe(
            "cancelled", "cancelled-terminal",
            "取消被接受后状态为 cancelled；已算的 6 块作为历史保留，但没有 plate，进度不等于交付。",
            extra={"exposure": {"state": after["state"], "error": after["error"],
                                "plate_id": after["plate_id"]}, "resources": resources})
        events = self.observer.events(self.ids["exposure"])
        return {"run_id": self.run_id, "scenario": "cancelled",
                "queued_case": first,
                "running_case": {"exposure_id": self.ids["exposure"],
                                 "terminal_state": after["state"],
                                 "tiles_before_cancel": 6,
                                 "plate_id": after["plate_id"],
                                 "resources": resources, "events": events}}

    # ============================================================ scenario C
    def run_failed_retry(self, baseline_seconds):
        self.commission_scene()
        self.submit_exposure("fail-retry")
        claim = self.app.exposures.claim(lease_seconds=120)
        self.token_labels[claim["token"]] = "D"
        self.api.control("worker.claim", {"lease_seconds": 120},
                         {"token": claim["token"], "attempt": claim["attempt"]},
                         note="工作器 D 领取任务")
        plan = self._plan(claim)
        frame, records = self.app.worker.tiles.store.prepare(claim, plan, TILE_EDGE)
        done = 12
        saved_hashes = {}
        for ordinal in range(done):
            payload = self._render_tile(plan, frame["fingerprint"], records[ordinal])
            self.app.worker.tiles.store.commit(claim, ordinal, payload)
            saved_hashes[ordinal] = {k: d["sha256"] for k, d in payload["channels"].items()}
        self.api.control("tiles.partial", {"tiles": done}, {"committed": done},
                         note="失败前已持久化 12 块")
        self.observe("failed-retry", "failed",
                     "工作器 D 失败（render_timeout 类故障）；12 块已落盘，曝光进入 failed，没有图版。")
        failed = self.app.exposures.fail(claim["id"], claim["token"], "render_timeout")
        self.api.control("worker.fail", {"token": claim["token"]},
                         {"state": failed["state"], "error": failed["error"]},
                         note="生产工作器捕获故障后登记 failed 终态")
        retry = self.api.call("POST", "exposures/" + self.ids["exposure"] + "/retry")
        self.observe(
            "failed-retry", "retried-queued",
            "显式 retry 仅允许 failed 曝光：重新排队、重新预留额度，原 12 块因同一冻结指纹仍可复用。",
            extra={"retry_response": {"state": retry["state"]}})
        claim2 = self.app.exposures.claim(lease_seconds=120)
        self.token_labels[claim2["token"]] = "E"
        self.api.control("worker.claim", {"lease_seconds": 120},
                         {"token": claim2["token"], "attempt": claim2["attempt"]},
                         note="工作器 E 领取重试")
        frame2, records2 = self.app.worker.tiles.store.prepare(claim2, plan, TILE_EDGE)
        assert frame2["fingerprint"] == frame["fingerprint"]
        skip = [r["ordinal"] for r in records2 if r["state"] == "complete"]
        todo = [r["ordinal"] for r in records2 if r["state"] != "complete"]
        started = time.monotonic()
        rendered = self.app.worker.tiles.render(claim2, plan, lambda: None)
        resume_seconds = time.monotonic() - started
        plate, plate_meta, passes = self._finish_plate(claim2, plan, rendered)
        verify = self.api.call("POST", "plates/" + plate["id"] + "/verify")
        self.observe(
            "failed-retry", "retry-succeeded",
            f"重试成功：复用 {len(skip)} 块、补算 {len(todo)} 块；成功图版与首次失败属于同一冻结场景。",
            extra={"resume": {"reused": skip, "rasterized": todo,
                              "resume_render_seconds": round(resume_seconds, 6)},
                   "verification": verify})
        events = self.observer.events(self.ids["exposure"])
        return {"run_id": self.run_id, "scenario": "failed-retry",
                "exposure_id": self.ids["exposure"],
                "seal_id": self.ids["seal"]["id"],
                "seal_content_hash": self.ids["seal"]["payload"]["content_hash"],
                "scene_fingerprint": scene_fingerprint(self.ids["seal"]),
                "frame_fingerprint": frame["fingerprint"],
                "first_attempt": {"worker": "D", "tiles": done,
                                  "channel_hashes": saved_hashes,
                                  "error": "render_timeout"},
                "retry_attempt": {"worker": "E", "reused_tiles": skip,
                                  "rasterized_tiles": todo,
                                  "resume_render_seconds": round(resume_seconds, 6),
                                  "full_render_baseline_seconds": baseline_seconds},
                "plate": plate_meta, "plate_verify": verify, "events": events}, plate, passes

    def _expect_fault(self, method, route, data):
        try:
            self.api.call(method, route, data)
        except Fault as exc:
            return {"error": exc.code, "status": exc.status, "detail": exc.detail}
        raise AssertionError("expected a Fault for " + route)
