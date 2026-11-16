# Facet Proof 离线渲染进度解说素材

这套素材用**真实分块渲染**展示同一次冻结场景下的三种结局：工作器中断后被接管续做、
取消（如实保留失败/取消终态）、失败后显式重试复用。全部为 Python 标准库实现，
**不需要网络、GPU 或任何第三方依赖**，最终交付一个可断网打开的自包含 HTML 播放器。

## 复跑方式

```sh
# 在仓库根目录（含 facetproof/ 与 narrative/）
python3 -m narrative --output narrative-output
```

脚本对三个独立数据根各跑一遍真实业务流程，再用独立的 `facetreader` 核验导出包。
退出码 `0` 表示 `cross-check.json` 全部通过；任一核验失败退出码为 `2`。

可复跑输入是 `inputs/frozen-scene.json`：两份网格、两种材质、镜头/光照、一次布置修订、
一次 seal，以及固定的渲染请求（192×192、samples=2 → 384×384 高采样画布，64px 分 36 块）。
三个场景回放的是同一份输入；跨数据根重新生成的对象 id 不同，因此用**结构指纹**
（去除生成 id 后的几何/材质/镜头/光照摘要）证明冻结场景一致，而通道字节哈希逐块相同。

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `player.html` | 单一自包含离线播放器，帧 PNG 与解说数据全部内嵌（data URI），可 `file://` 断网播放 |
| `frames/` | 每个观察点的原始帧 PNG（按 `场景-帧号` 命名），与 `player.html` 内嵌字节一致 |
| `inputs/frozen-scene.json` | 可复跑的冻结场景输入（含逐步 API 说明） |
| `narrative-run/<场景>/transcript.jsonl` | 每次调用的请求/响应流水（JSONL）。`channel:"http"` 是真实路由 JSON；`channel:"internal-control"` 是为制造中断而做的内部租约/分块动作，显式标注、不会被当作生产 HTTP |
| `narrative-run/<场景>/observations.json` | 每个观察点：同一 SQLite 只读快照下的任务状态、36 块状态与 producer 标签、通道哈希 |
| `narrative-run/<场景>/data-root/` | 该场景真实的 SQLite 与内容寻址 blob，可直接用 `facetproof` 服务打开复查 |
| `narrative-run/baseline-full-render.json` | 独立数据根对同场景从零渲染 36 块的逐块计时与片段数 |
| `narrative-run/compute-savings.json` | 接管/重试实际未支付的光栅化时间与片段测试（由基线逐块计时汇总） |
| `delivery/plate-bundle.zip` | 最终导出包（plate + seal + 六个通道） |
| `delivery/color.png` `depth.png` `object.png` | 最终图版三个通道，与 ZIP 内字节同源同哈希 |
| `delivery/facetreader-report.json` | 独立读取器（不导入服务代码）的核验通过报告与信任边界 |
| `delivery/facetreader-truncated-rejection.json` | 截断包被独立读取器拒绝的记录 |
| `cross-check.json` | 11 项自动核验，全部为 `true` 才整体通过 |
| `MANIFEST.sha256` / `manifest.json` | 全部产物的 SHA-256 清单 |

## 三个章节讲清的区别

1. **中断与接管（主叙事，9 帧）**
   - 工作器 A 领取短租约，真实光栅化并持久化前 9 块后进程被杀死；
   - 租约到期后，迟到的 A 又算出第 10 块的字节，但 `tile.commit` 被服务以
     `lease_lost (409)` 拒绝——这些 CAS 字节不被任何清单引用，**不会冒充接管者认可的进度**；
   - `worker/recover` 把过期租约回收为排队，工作器 B 以新 token 接管，在**同一分块指纹**下
     跳过 9 块、只补算 27 块，且 A 的 9 块 producer token 原样保留；
   - 仅当 36 块完整、可读、租约有效且未取消时才整幅下采样、生成图版——
     **块完成是持久进度，不等于图版成功**。
2. **取消（3 帧）**：排队取消直接终态、运行中取消在工作器确认前不释放额度；
   已算的块作为历史保留，但没有 plate，重试被 `state (409)` 拒绝。
3. **失败与重试（3 帧）**：工作器 D 失败留下 12 块，显式 retry 后工作器 E 复用这 12 块、
   补算 24 块成功；成功图版与首次失败属于同一冻结场景。

## “同一次观察”是如何保证的

每个观察点在**一个 SQLite 只读事务**内同时读取曝光行、分块清单行与帧清单行，
帧 PNG 只解码该快照标记为 `complete` 的块；pending 块统一画成深色斜纹，绝不补造像素。
观察 JSON 记录每块的 `producer_label`、状态与通道哈希，并独立读取 CAS 字节验证可读性，
任何“complete 却无 payload / pending 却带 payload / blob 不可读”的不一致都会令构建失败。

## 最终交付的同源核验

`cross-check.json` 自动断言：被中断块与独立全新渲染逐块字节相同；迟到写入的三个通道 blob
无任何清单引用；图版六通道哈希与导出 ZIP 完全一致；独立读取器接受正式包、拒绝截断包；
颜色/深度/物体通道指向同一 plate、曝光与 seal；取消运行无 plate；重试图版与主叙事同冻结场景。

注意 `facetreader` 的信任边界：包内一致只能证明内部字节与元数据自洽，
不能证明外部来源真实，也不能证明服务端当前审阅意见仍有效。
