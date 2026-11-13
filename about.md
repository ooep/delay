# jr.suki.ing 晚点烘焙器

GitHub Actions 每 5 分钟从各家铁道运营商的**实时运行信息源**烘焙当前晚点列车快照，
并把每一班映射到 **jr.suki.ing 的 tmaps 车次键**，产出一个小 JSON。
jr.suki.ing 只需读这一个文件，用 `by_suki_key` 直查表给自己的列车打晚点标签。

```
上游实时源 ────────┐
上游时刻表服务 ────├─→ baker (Actions) ──→ data/delays.json ──→ jr.suki.ing
主站车次快照 ──────┘   (只下载有晚点列车的线路)  (Pages / jsDelivr)
```

> 各运营商的具体端点、参数与逆向过程**不在本文档内**，全部只存在于加密源码
> （`baker/` 下的密文）中。本文档只描述公开契约与设计取舍。

## 产物 `data/delays.json`

```jsonc
{
  "generated_at": "2026-10-25T19:28:54+09:00",
  "delayed_count": 107,
  "trains": [
    {
      "op": "JR-West", "line": "紀勢本線", "number": "346M", "type": "普通",
      "delay_min": 61, "delay_s": 3660, "from": "宮前",
      "scheduled": { "dep": "08:42", "arr": "09:56", "origin": "紀伊内原",
                     "destination": "和歌山", "stops": ["紀伊内原", "..."] },
      "suki": [ { "key": "10780", "subseq": true, "overlap": 1.0, "extra": 1 }, "..." ]
    }
  ],
  "by_suki_key": {
    "10780": { "delay_min": 61, "train": "346M", "line": "紀勢本線", "dep": "08:42",
               "ambiguous": false, "alternatives": [] }
  }
}
```

- `by_suki_key` 只收强匹配（站序完全包含），未匹配/弱匹配的列车仍出现在
  `trains[]` 里（带 `suki` 候选或不带），用于面板列表展示。
- `ambiguous: true` 表示同一线路同一停站 pattern 有多班晚点列车打到了同一个
  key（或一个 key 被多班命中）。**用时刻决胜**：`web/delay-tag.js` 的
  `for(key, {depMin})` 会按发车时刻就近选择；时刻逻辑在站点侧，烘焙侧不猜。

## 映射规则（全部实测验证）

两侧车号是不同命名空间（上游用 `346M`，tmaps 用自家键），因此按语义 join：

1. **线路名**：上游标题去括号变体、去 `JR` 前缀，叠加 `lines_map.json`
   别名和 `baker/suki.py` 里的两张别名表：
   `TITLE_ALIASES`（无歧义标题，如改名、都営/横浜市営命名、JR-East
   运行名→物理线名）与 `RAILWAY_ALIASES`（跨公司撞名按线路 ID 决胜，如
   日光線/宇都宮線/大師線/空港線）。全量审计：119 条 realtime 线路里
   116 条桥接成功且站名重合 ≥50%；3 条例外为主站侧缺口（一条 JR 线）
   和两条无名貨物支线。新增命名差异往这两张表加一行即可。
2. **站序**：上游时刻表腿的有序停站与 tmaps `s_jas` 做有序包含匹配
   （允许主站侧两端更长——直通腿从中途起算）。归一化统一 `ヶ/ケ` 字形
   （霞ヶ関↔霞ケ関）并剥去都道府县消歧括号（追分(北海道)）；反方向列车
   自然被站序排除。
3. **评分与置信**：站序完全包含 = `exact`；仅差 1 站（两侧数据源的停站
   模式漂移）= `fuzzy: 1`，进入 `by_suki_key` 时带 `fuzzy` 字段供 UI 降级。
   实测某晚 133 班晚点：125 exact + 5 fuzzy-1 = **98% 映射**。

## 时刻表的两层日历

上游时刻表索引里分两层，**互不镜像**：

1. **命名日历层** `railways[rid].bundle.calendars`：`Weekday` / `SaturdayHoliday` /
   `Dated`（部分私铁用 `Saturday`/`Holiday` 方言）。绝大多数线路走这一层。
2. **按日期层** `dateBundles`：顶层 `calendars[YYYY-MM-DD] -> sha256` 指针，覆盖
   257 天，每天一份合并 bundle，内含当天需要特殊表的线路 → 该线路自己的
   sha256。`railways[rid].calendars` 里出现日期，才表示这条线在当天有独立时刻表。

**两条新干线只有按日期层**：它们的 `bundle.calendars` 只有一个 96 字节的空壳
`Weekday`（`trains: []`），全部班次躺在当日 bundle 里。旧版 `calendar_candidates()`
只看第一层，于是 `find_leg()` 对这两条线永远返回 None，只能靠 `suki-direct`
兜底——这就是"上游有延误、主站没标注"的一个来源。

取表顺序（`LegStore.calendar_candidates`）：**当日日期 bundle → Dated →
Weekday/周末方言 → 其余**。日期层只在"索引有该日期 且 该线路的 calendars
含该日期"时启用；拉取失败降级到下一候选，不中断烘焙。

配套维护：
- `data/tt/tt-index.json` 每周随 refresh 落盘，CDN 抖动时 `timetable_index()`
  回退到该快照（丢索引 = 丢全部 leg，新干线直接失效）。落盘前会剥掉每个日历项
  的 `url` 与 `dateBundles.baseUrl`，只留 sha256——索引是公开产物，没有理由
  顺带发布一份上游地址清单，主机名在运行时由 `common.blob_url()` 拼回。
- refresh 打印体检：日期层覆盖区间、今天是否覆盖、哪些线路"只有日期层"
  （`date-only railways`），以及各 JR 公司日期层的最远覆盖日。
- 烘焙日志 `[leg] date bundles: N days, today ... covered=yes` 与
  `[leg] date bundle <date> used for N railways` 可直接确认当日走了日期层。
- 站表一致性：bundle 的停站索引指向**打包时**的站序，`data/lines.json`
  只在周日刷新；若上游新增车站，`decode_stops` 会退化成 `#idx`，
  烘焙结束会打 `[leg] WARN station list shorter than bundle indexes`。

### 时刻表本地缓存（`data/tt/cache/`）

上游把时刻表当静态资源：blob 走内容寻址路径并声明 `max-age=31536000, immutable`，
索引声明 `max-age=86400`。烘焙是 5 分钟一次，等于同一份几十 MiB 每天重复拉
近 300 遍——这是全流程唯一无视上游缓存语义的地方。

现在的做法是不改时效、只按对方的约定办事：

- blob 按 URL 里的 sha256 存成 `<hash>.json`，命中直接读盘；换了时刻表就是换了
  hash，**天然 miss**，不存在读到旧版的可能
- 索引在 `TT_INDEX_TTL`（默认 3600s）内直接用 `cache/idx.json`；过期才带
  `If-None-Match` 重新校验，304 只更新 `fetched_at`（515 KiB → 几百字节）
- 两次写盘都是"临时文件 + `os.replace`"的原子替换，runner 被 kill 也不会留下
  半截文件被下一次当成内容真值
- 目录由 `actions/cache` 按天携带（key `tt-v3-<JST 日期>`），当天首次烘焙落一份，
  后续全部命中；缓存恢复失败不影响烘焙
- `Source.trim_cache()` 在收尾把目录压到 `TT_CACHE_MAX`（默认 200 MiB），
  按 mtime 淘汰旧 blob；`cache_report()` 打一行体检：

```
[tt] cache 420 hit / 48 fetch (90%), 6.2 MiB pulled, 0 corrupt;
     index 1 ttl/0 304/0 pull; disk 516 files 62.8 MiB
```

实测收益：同一天冷/热两轮，`235 blobs 54.8s / 41.1 MiB` → `4.4s / 0 MiB`。

**稳定性检测**（任一失败都只降级，不中断）：
- blob 读出来不是合法 JSON → 删除、计 `corrupt`、当场重拉
- 索引快照用 sha256 + 字节数双重校验，截断/改坏则丢弃重拉
- 目录不可写 → 关闭缓存，退回纯网络
- 索引 200 但响应体不是 JSON（代理错误页）→ 退到提交快照
- 索引彻底不可达 → 先退运行缓存、再退提交快照

`TT_INDEX_TTL` / `TT_CACHE_MAX` 都可用环境变量覆盖，便于排查"缓存没生效"。

## 文件

- `baker/common.py` — 上游 feed 抓取 + v3 bundle 解码（站序/时刻/dated 位掩码）
- `baker/refresh.py` — 每周刷新慢变缓存：线路桥、时刻表索引、主站车次快照
- `baker/suki.py` — tmaps 匹配器
- `baker/delays.py` — 主流程：feeds → 当日相关 bundle → 语义匹配 → delays.json
- `baker/` 下另有若干 `*_*.py` —— 各运营商的独立接入模块，端点、参数与
  逆向过程只写在密文内部注释里，不在此展开
- `web/delay-tag.js` — 站点接入（`loadDelays().for(key, {depMin})`）
- `.github/workflows/bake-delays.yml` — 主 workflow

## 部署

1. 推到 GitHub（public 仓库 Actions 分钟数无限）。
2. jr.suki.ing 侧：
   `const delays = await loadDelays("https://cdn.jsdelivr.net/gh/<user>/<repo>@main/data/delays.json")`
   然后对每班渲染中的列车调 `delays.for(tmapsKey, {depMin: depMin})`。
3. 面板可直接渲染 `trains[]`（含未映射的车）。

## 调度与运维

- **节奏**：主驱动是外部调度器，每 5 分钟 dispatch 一次；workflow 里的 cron
  （`*/10`）只是兜底，GitHub 自身对高频 schedule 有节流，实测仅少量触发。
  两者共用同一 concurrency 组，重叠时后者被取消。
- **耗时**：单轮中位 95s，p95 约 123s（16 核 runner、87 路 feed 并发）。
- **部署**：数据提交（step 5）与静态资产部署（step 7）分离，资产上传是内容
  寻址的幂等操作，失败重试 3 次；即便这一步失败，数据也已入库，下一轮补齐。
- 晚点语义：秒级源按分钟量化，部分运营商封顶 61 分（显示 `+61`），
  字段缺失 = 未知（当准点显示），阈值 ≥60s。
- feed 侧逐运营商容错：某家 403/超时/停服只影响该家，其余照常出板；
  上游一放开就自动进板。
- 每次快照都是一次 commit，git 历史天然就是晚点档案（另有
  `data/delay-history/` 月度归档）。
