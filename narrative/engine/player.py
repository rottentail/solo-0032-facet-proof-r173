"""Emit a fully self-contained offline HTML player.

All frame PNGs and the story index are embedded as data URIs / JSON inside one
HTML file, so the deck plays from ``file://`` with no network access and no
external assets. A non-embedded ``player-assets`` copy is kept alongside for
receivers who want to inspect the raw JSON and PNGs directly.
"""
import base64
import json
from pathlib import Path

HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: "Noto Sans CJK SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
         background:#101216; color:#e8eaed; }
  header { padding:18px 24px; border-bottom:1px solid #262a31; background:#15181d; }
  header h1 { font-size:20px; margin:0 0 6px; }
  header p { margin:0; color:#9aa0a8; font-size:13px; line-height:1.6; }
  .layout { display:grid; grid-template-columns: 240px 1fr 340px; gap:0; min-height:calc(100vh - 86px); }
  nav { border-right:1px solid #262a31; padding:14px; background:#14171c; overflow:auto; }
  nav .chapter { font-size:12px; letter-spacing:.08em; color:#7c838d; text-transform:uppercase; margin:14px 0 6px; }
  nav button { display:block; width:100%; text-align:left; background:none; border:1px solid transparent;
               color:#cfd4da; padding:7px 9px; border-radius:7px; cursor:pointer; font-size:13px; margin:2px 0; }
  nav button:hover { background:#1d2229; }
  nav button.active { background:#22303f; border-color:#34506b; color:#fff; }
  main { padding:20px 24px; display:flex; flex-direction:column; gap:14px; }
  .stage { background:#0b0d10; border:1px solid #262a31; border-radius:12px; padding:16px; text-align:center; }
  .stage img { max-width:100%; max-height:62vh; image-rendering:pixelated; border-radius:6px; }
  .caption { background:#15181d; border:1px solid #262a31; border-radius:10px; padding:14px 16px; }
  .caption h2 { margin:0 0 8px; font-size:16px; }
  .caption p { margin:0; color:#d6dae0; line-height:1.7; font-size:14px; }
  .controls { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .controls button { background:#24303d; color:#e8eaed; border:1px solid #35475a; border-radius:8px;
                     padding:8px 14px; cursor:pointer; font-size:13px; }
  .controls button:disabled { opacity:.4; cursor:default; }
  .progress { flex:1; min-width:160px; height:6px; background:#222831; border-radius:99px; overflow:hidden; }
  .progress i { display:block; height:100%; background:#4f86c6; width:0; }
  aside { border-left:1px solid #262a31; padding:16px; background:#14171c; overflow:auto; font-size:13px; }
  aside h3 { font-size:13px; margin:16px 0 8px; color:#9aa0a8; }
  aside h3:first-child { margin-top:0; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th,td { text-align:left; padding:5px 6px; border-bottom:1px solid #232830; }
  th { color:#8b929c; font-weight:600; }
  .tag { display:inline-block; padding:2px 8px; border-radius:99px; font-size:11px; }
  .tag.queued { background:#3a3522; color:#e3c873; }
  .tag.running { background:#1d3349; color:#7db4ea; }
  .tag.failed { background:#43261f; color:#e89a86; }
  .tag.cancelled { background:#3a2a2a; color:#d9a0a0; }
  .tag.succeeded { background:#1f3d2c; color:#8fd6a4; }
  .dotA { color:#e08a52; font-weight:700; } .dotB { color:#6fa8e6; font-weight:700; }
  .dotC { color:#d9a0a0; font-weight:700; } .dotD { color:#e0b85a; font-weight:700; }
  .dotE { color:#9bc98f; font-weight:700; }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:3px 12px; font-size:12px; }
  .kv b { color:#8b929c; font-weight:600; }
  .warn { background:#2a2016; border:1px solid #5a4022; color:#f0c894; border-radius:8px; padding:10px 12px; font-size:12px; line-height:1.6; }
  .mono { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:11px; word-break:break-all; color:#aab2bc; }
  footer { padding:12px 24px; border-top:1px solid #262a31; color:#7c838d; font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <p>离线解说：真实分块渲染下的中断、接管续做与最终交付。每帧画面、块来源（producer）与任务状态取自同一次 SQLite 只读快照；
     已提交块只是持久进度，迟到工作器失租后的写入不会成为接管者认可的进度。</p>
</header>
<div class="layout">
  <nav id="nav"></nav>
  <main>
    <div class="stage"><img id="frame" alt="observation frame"></div>
    <div class="controls">
      <button id="prev">◀ 上一帧</button>
      <button id="play">▶ 自动播放</button>
      <button id="next">下一帧 ▶</button>
      <div class="progress"><i id="bar"></i></div>
      <span id="counter" class="mono"></span>
    </div>
    <div class="caption"><h2 id="caption-title"></h2><p id="caption-text"></p></div>
  </main>
  <aside id="panel"></aside>
</div>
<footer>本页面为单一自包含 HTML，所有图片与数据已内嵌，可断网以 file:// 打开。原始记录见 narrative-run/ 与 delivery/。</footer>
<div id="savings" style="padding:18px 24px;border-top:1px solid #262a31;background:#14171c"></div>
<script>
const STORY = __STORY__;
const FRAMES = __FRAMES__;
const flat = [];
STORY.chapters.forEach(ch => ch.frames.forEach(f => flat.push({...f, chapterTitle: ch.title})));
let idx = 0, timer = null;
const stateClass = s => ["queued","running","failed","cancelled","succeeded"].includes(s) ? s : "";
function render() {
  const f = flat[idx];
  document.getElementById("frame").src = FRAMES[f.file] || FRAMES[f.file.split("/").pop()];
  document.getElementById("caption-title").textContent = f.chapterTitle + " · " + f.stage;
  document.getElementById("caption-text").textContent = f.note;
  document.getElementById("counter").textContent = (idx+1) + " / " + flat.length;
  document.getElementById("bar").style.width = ((idx+1)/flat.length*100) + "%";
  document.querySelectorAll("nav button").forEach((b,i)=>b.classList.toggle("active", i===idx));
  const panel = document.getElementById("panel");
  panel.innerHTML = panelHtml(f);
  document.getElementById("prev").disabled = idx===0;
  document.getElementById("next").disabled = idx===flat.length-1;
}
function panelHtml(f){
  const rows = [];
  rows.push("<h3>同一次观察</h3>");
  rows.push('<div class="kv"><b>章节</b><span>'+f.chapterTitle+'</span><b>阶段</b><span>'+f.stage+'</span>');
  rows.push('<b>任务状态</b><span class="tag '+stateClass(f.exposure_state)+'">'+f.exposure_state+'</span>');
  rows.push('<b>租约持有者</b><span class="dot'+(f.lease_owner||"")+'">'+(f.lease_owner||"—（无有效租约）")+'</span></div>');
  rows.push("<h3>分块进度（同一快照）</h3>");
  rows.push("<table><tr><th>已完成</th><th>待处理</th><th>已提交高采样像素</th></tr>");
  rows.push("<tr><td>"+f.counts.complete+" / "+(f.counts.complete+f.counts.pending)+"</td><td>"+
            f.counts.pending+'</td><td class="mono">'+f.counts.committed_pixels+" / "+f.counts.total_pixels+"</td></tr></table>");
  if(f.tiles){
    rows.push("<h3>块来源（producer token）</h3><table><tr><th>块</th><th>状态</th><th>来源</th></tr>");
    f.tiles.forEach(t=>{
      rows.push("<tr><td>"+t.ordinal+"</td><td>"+t.state+"</td><td class='dot"+(t.producer_label||"")+"'>"+
        (t.producer_label?("工作器 "+t.producer_label):"—")+"</td></tr>");
    });
    rows.push("</table>");
  }
  if(f.extra){
    rows.push("<h3>关键证据</h3><pre class='mono' style='white-space:pre-wrap'>"+
      JSON.stringify(f.extra, null, 1).replace(/[<>&]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]))+"</pre>");
  }
  rows.push('<div class="warn">块完成 ≠ 图版成功：仅当全部块完整、内容可读、租约有效且未取消时才生成最终图版；进度查询不能代替图版成功事实。</div>');
  return rows.join("");
}
function buildNav(){
  const nav = document.getElementById("nav");
  let html = "";
  STORY.chapters.forEach(ch=>{
    html += '<div class="chapter">'+ch.title+'</div>';
    ch.frames.forEach((f,i)=>{
      const globalIndex = flat.indexOf(f);
      html += '<button data-i="'+globalIndex+'">'+f.stage+'</button>';
    });
  });
  nav.innerHTML = html;
  nav.querySelectorAll("button").forEach(b=>b.onclick=()=>{stop(); idx=+b.dataset.i; render();});
}
function stop(){ if(timer){clearInterval(timer); timer=null; document.getElementById("play").textContent="▶ 自动播放";} }
document.getElementById("prev").onclick=()=>{stop(); if(idx>0){idx--;render();}};
document.getElementById("next").onclick=()=>{stop(); if(idx<flat.length-1){idx++;render();}};
document.getElementById("play").onclick=()=>{
  if(timer){stop();return;}
  document.getElementById("play").textContent="⏸ 暂停";
  timer=setInterval(()=>{ if(idx<flat.length-1){idx++;render();} else {stop();} }, 2600);
};
buildNav(); render(); renderSavings();
function renderSavings(){
  const s = STORY.compute_savings;
  if(!s){ return; }
  let html = '<h2 style="margin:0 0 10px;font-size:16px">恢复节省了哪些计算（独立数据根实测）</h2>';
  html += '<p style="color:#9aa0a8;font-size:12px;line-height:1.6;margin:0 0 12px">基线对同一冻结场景从零渲染全部 '+
          s.baseline.total_tiles+' 块，光栅化耗时 '+s.baseline.rasterize_seconds+
          ' 秒、片段测试 '+s.baseline.total_fragments+' 次。下列被复用块的耗时与片段测试，正是接管/重试者没有重新支付的成本。</p>';
  html += '<table style="width:100%;border-collapse:collapse;font-size:13px;max-width:900px"><tr style="color:#8b929c">'+
          '<th style="text-align:left;padding:6px">场景</th><th style="text-align:right;padding:6px">复用块</th>'+
          '<th style="text-align:right;padding:6px">补算块</th><th style="text-align:right;padding:6px">避免光栅化(秒)</th>'+
          '<th style="text-align:right;padding:6px">避免片段测试</th></tr>';
  s.savings.forEach(row=>{
    html += '<tr><td style="padding:6px;border-top:1px solid #232830">'+row.case+'</td>'+
            '<td style="text-align:right;padding:6px;border-top:1px solid #232830">'+row.tiles_reused_without_rasterizing+' / '+row.total_tiles+'</td>'+
            '<td style="text-align:right;padding:6px;border-top:1px solid #232830">'+row.tiles_rasterized+'</td>'+
            '<td style="text-align:right;padding:6px;border-top:1px solid #232830">'+row.avoided_rasterize_seconds+'</td>'+
            '<td style="text-align:right;padding:6px;border-top:1px solid #232830">'+row.avoided_fragment_tests+'</td></tr>';
  });
  html += '</table>';
  document.getElementById("savings").innerHTML = html;
}
</script>
</body>
</html>
"""


def write_player(out):
    out = Path(out)
    story = json.loads((out / "story.json").read_text(encoding="utf-8"))
    frames_dir = out / "frames"
    embedded = {}
    for png in sorted(frames_dir.glob("*.png")):
        embedded[png.name] = "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode("ascii")

    # Merge per-tile detail and extra evidence into the story frame refs so the
    # side panel can render provenance without any extra fetch.
    run_obs = {}
    for run in ("interrupted", "cancelled", "failed-retry"):
        doc = json.loads((out / "narrative-run" / run / "observations.json").read_text(encoding="utf-8"))
        for obs in doc["observations"]:
            run_obs[(run, obs["frame_index"])] = obs
    for chapter in story["chapters"]:
        for ref in chapter["frames"]:
            obs = run_obs[(chapter["id"], ref["frame_index"])]
            ref["tiles"] = [{"ordinal": t["ordinal"], "state": t["state"],
                             "producer_label": t["producer_label"]} for t in obs["tiles"]]
            ref["extra"] = obs.get("extra")

    html = (HTML_TEMPLATE
            .replace("__TITLE__", story["title"])
            .replace("__STORY__", json.dumps(story, ensure_ascii=False))
            .replace("__FRAMES__", json.dumps(embedded, ensure_ascii=False)))
    (out / "player.html").write_text(html, encoding="utf-8")

    # Also keep a non-embedded asset bundle for direct file inspection.
    assets = out / "player-assets"
    assets.mkdir(exist_ok=True)
    (assets / "story.json").write_text(json.dumps(story, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
