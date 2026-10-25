# jr.suki.ing 晚点烘焙器

GitHub Actions 每 10 分钟从 **dokorail**（我们自己的站）烘焙当前晚点列车快照，
并把每一班映射到 **jr.suki.ing 的 tmaps 车次键**，产出一个小 JSON。
jr.suki.ing 只需读这一个文件，用 `by_suki_key` 直查表给自己的列车打晚点标签。

```
dokorail /api/feeds ──┐
dokorail 时刻表 bundle ├─→ baker (Actions */10) ──→ data/delays.json ──→ jr.suki.ing
jr-static tmaps.json ─┘        (只下载有晚点列车的线路)      (Pages / jsDelivr)
```

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
  `for(key, {depMin})` 会按发车时刻就近选择；时刻逻辑在你侧，烘焙侧不猜。

## 映射规则（全部实测验证）

两侧车号是不同命名空间（dokorail/ODPT 用 `346M`，tmaps 用自家键），因此按语义 join：

1. **线路名**：dokorail 标题去括号变体、去 `JR` 前缀，叠加 `lines_map.json`
   别名和 `baker/suki.py` 里的两张别名表：
   `TITLE_ALIASES`（无歧义标题，如東武改名、都営/横浜市営命名、JR-East
   运行名→物理线名）与 `RAILWAY_ALIASES`（跨公司撞名按线路 ID 决胜，如
   日光線/宇都宮線/大師線/空港線）。全量审计：119 条 realtime 线路里
   116 条桥接成功且站名重合 ≥50%；3 条例外为 suki 侧缺口（JR鹿島線）
   和两条无名貨物支线。新增命名差异往这两张表加一行即可。
2. **站序**：dokorail 时刻表腿的有序停站与 tmaps `s_jas` 做有序包含匹配
   （允许 suki 侧两端更长——直通腿从中途起算，如 346M.3 起于紀伊内原而完整
   交路起于御坊）。归一化统一 `ヶ/ケ` 字形（霞ヶ関↔霞ケ関）并剥去
   都道府县消歧括号（追分(北海道)）；反方向列车自然被站序排除。
3. **评分与置信**：站序完全包含 = `exact`；仅差 1 站（两侧数据源的停站
   模式漂移，如 dokorail 含信号场/部分停车站）= `fuzzy: 1`，进入
   `by_suki_key` 时带 `fuzzy` 字段供 UI 降级显示。当晚实测 133 班晚点：
   125 exact + 5 fuzzy-1 = **98% 映射**；剩 3 班是上游数据缺口（2 班
   dokorail bundle 当日缺该班次、1 班 suki 无该特急停站模式）。

## 文件

- `baker/common.py` — dokorail 抓取 + v3 bundle 解码（站序/时刻/dated 位掩码）
- `baker/refresh.py` — 每周刷新慢变缓存：`data/lines.json`（线路桥 + 实时运营商表）和 suki 侧 `tmaps.json`/`lines_map.json` 快照
- `baker/suki.py` — tmaps 匹配器
- `baker/delays.py` — 主流程：feeds → 当日相关 bundle → 语义匹配 → delays.json
- `web/delay-tag.js` — 站点接入（`loadDelays().for(key, {depMin})`）
- `.github/workflows/bake-delays.yml` — 唯一 workflow：*/10 采晚点，周日刷新缓存

## 部署

1. 推到 GitHub（public 仓库 Actions 分钟数无限；private 2000 分钟/月也够用
   `*/10` × ~40s）。无需任何 secret——dokorail 是自己的站。
2. jr.suki.ing 侧：
   `const delays = await loadDelays("https://cdn.jsdelivr.net/gh/<user>/<repo>@main/data/delays.json")`
   然后对每班渲染中的列车调 `delays.for(tmapsKey, {depMin: depMin})`。
3. 面板可直接渲染 `trains[]`（含未映射的车）。

## 运维注记

- GitHub cron 有 5–20 分钟抖动，`generated_at` 在文件里，UI 建议展示数据年龄。
  若要真·实时：在 dokorail 的 `/api/feeds` 加一行 suki.ing 的 ACAO，前端直连，
  本烘焙留作静态兜底。
- feed 目前放行 JR-West / Keikyu / Tobu / Toei / YokohamaMunicipal；JR-East
  现在答 403，烘焙器逐运营商容错——dokorail 一放开它自动进板。
- **JR-Shikoku（四国）走独立源**：`baker/jrshikoku.py` 直接拉四国自己的走行位置
  服务 `train.jr-shikoku.co.jp/data/train/train.json`（端点与字段
  `Line/TrainNum/Type/Direction/delay/Pos` 系从其混淆前端 traindisp_ob.js 逆向），
  线路代码映射到 dokorail 的 JR-Shikoku 时刻表 bundle 解腿，再按同样的
  站序规则映射到 suki 键。注意：**该服务于 2026-10-22 起被官方暂停**
  （[官方 X 公告](https://x.com/jr_shikoku_info/status/2113221330715947440)，
  端点 403、无恢复时间表）——烘焙器按"暂停即跳过、恢复自动接入"处理，
  恢复后 `delays.json` 会自动出现 JR-Shikoku 运营商条目。
- **JR-Hokkaido（北海道）走独立源**：`baker/jrhokkaido.py` 直连
  `www3.jrhokkaido.co.jp` 的公开 JSON（`json/location/now/location_NN_now.json`
  + `webunkou/json/daiya/daiya_NN.json` 时刻表 + `eki_master.json` 站名，全部
  UTF-8 带 BOM，缺失线区答 302 跳过）。其 `senku` 线区码已逐一锚定
  （00=特急、02=根室、05=千歳、06=函館、07=日高、09=富良野、10=釧網、
  11=花咲、12=学園都市/札沼、13=石北、14=宗谷、15=石勝、17=室蘭、
  19=新幹線），晚点车的完整停站时刻直接取自 JR 北海道自家 daiya 时刻表
  （不经 dokorail），车号去重（同一列车会在相邻区间的分组里重复出现），
  再按站序映射到 suki 键。花咲线 suki 侧暂无数据，只上板不映射。
- **JR-East（JR東日本 东北/新潟/千葉/横浜エリア）走独立源**：`baker/jreast_dokotrain.py`
  直连どこトレ `doko-train.jp` 的公开 JSON（`/json/trainstatus/lines_status.json`
  汇总 + `/json/trainstatus/<線区ID>.json` 每线班次 + 线路/车站 master，无鉴权
  无 WAF 问题）。`LATENCY`(分) 即晚点、车号自带 M/D 后缀与 dokorail bundle
  一致；线路桥是通用实现（delays.py 的 title_index）：线区名剥括号/前缀后
  对 dokorail railway 标题（如 外房線→JR-East.Sotobo、常磐線→JR-East.Joban），
  匹配到腿再走统一站序映射。覆盖 78 线区；dokorail 没有的线（越後線、仙山線
  等）只要 dokorail 侧有时刻表 bundle 就能全程映射，两边都没有的线路
  只上板不映射。这一路正好补上 dokorail 端点被 Cloudflare 挡掉的 JR-East
  实时数据。
- **JR-Kyushu（九州）走独立源**：`baker/jrkyushu.py` 直连 goto-mirai web app
  的 `api/common/trainPosition?drivingRouteCode=C&upperLowerKbn=1|2&stationCode=S
  &isVisibleAllTrain=true`（站位槽制，字段 `trainNumber/trainKindInformationName/
  delayMinutes/orMoreDelayMinutes(+N分以上)/roundedDelayMinutes(約N分)`）。
  已接入：600/601=鹿児島本線。**扩充新线路需要 (routeCode, 该线上任一stationCode)
  对**：routeCode 用宽松的 `api/station/headerData` 串行扫描发现
  （已知 500=福北ゆたか線 501=若松線 502=原田線 503=あまくさみすみ線
  600/601=鹿児島本線 700=長崎本線），stationCode 从该线 trainPosition
  响应的 stationInfo 里取（或经浏览器抓 app 车站检索 master）。
  车号是纯数字（3222），dokorail bundle 带字母后缀（3222M/520D），LegStore
  走数字前缀匹配；八代等区段跨线重复记录按车号去重。
  ⚠️ 该站有 Akamai WAF：**必须串行+限速+带 Referer**，并发扫描会触发
  全站封禁（GitHub Actions 每次跑批 IP 全新，无此顾虑）。
- 晚点语义：`odpt:delay` 秒、按分钟量化、JR-West 封顶 3660s（显示 +61），
  字段缺失 = 未知（当准点显示），阈值 ≥60s。
- baker 的 commit 会保住仓库活跃度，scheduled workflow 不会被 60 天规则停用。
- 每次快照都是一次 commit，git 历史天然就是晚点档案。
