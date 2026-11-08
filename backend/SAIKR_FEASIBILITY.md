# 赛氪网（saikr.com）数据获取可行性评估与落地方案

> 调研时间：2026-11-07 23:50 ~ 2026-11-08 00:30（Asia/Shanghai）
> 调研方式：`backend/venv` 内 requests 只读探针；**未写数据库、未改业务代码、未下载图片落盘**
> 结论：**接口可直接用 requests 调，不需要 Playwright**；但「图片/比赛时间」只能解决一部分，且 robots.txt 存在合规冲突，需要产品拍板。

---

## 零、一页结论

| 问题 | 结论 | 证据 |
|---|---|---|
| 数据走什么接口 | 前端 SPA 统一请求 `https://apiv4buffer.saikr.com` 下的 JSON API（Lumen 后端） | 首页 HTML 注释 `api: https://apiv4buffer.saikr.com`；JS 里 axios 实例 `baseURL=bC`，`bC="https://apiv4buffer.saikr.com"` |
| 是否需要复杂鉴权 | **不需要**。无 Token、无签名、无 nonce、无强制 Cookie；匿名可直接拿全量数据 | 请求拦截器只在本地有登录态时才加 `Authorization: saikrapi <token>`；无 UA、无 Referer 的裸请求同样 200 |
| 反爬强度 | 有阿里云 WAF（`acw_tc` Cookie），但**对这两个接口没有触发验证/滑块/429** | 连续 52 次目录请求（66 秒）0 失败；5 条详情连续调用 5/5 成功 |
| 图片能不能拿到 | **27/32 有封面**（`web_pic_big`），但其中 **14 张是 2.8~5.5 的宽 banner**，过不了现有 2.6 闸门；能过闸门的 **13/32**；另有 12/32 正文里有配图 | 逐张读文件头量尺寸（未落盘） |
| 比赛时间能不能拿到 | **不能完全替代**。报名时间结构化且 32/32 齐全；**比赛时间只有 4/32** | 目录 + 详情字段统计 |
| 官网能不能拿到 | 没有独立字段，但 **24/32 正文里有官网外链**（夹着百科/B站/公众号图等噪声，需白名单）；我们库里 32 条 official_url 本来就全有 | 正文 `<a href>` 提取 |
| 要不要上 Playwright | **现阶段不需要**。接口直连 0.07~0.27 秒/条；Playwright 只作接口失效后的兜底 | 见第四节成本表 |
| 最大风险 | `robots.txt` 明确 `Disallow: /api/`、`Disallow: /*?*`、`Crawl-delay: 10`，技术可行 ≠ 授权可行 | 见 2.4 |

**一句话给产品**：赛氪能显著改善「封面图」和「报名时间」，但**解决不了榜单赛事「比赛时间缺失」这个老问题**；而且它的目录 90% 是自家商业赛/校级赛，真正对我们有价值的榜单赛事页面反而"信息少、图是 banner"——和高校通知页的毛病同源。建议**先做 32 条存量映射的小范围补全（P0/P1），不要一上来全量抓 5172 条**。

---

## 一、调研方法（可复现）

1. 抓首页 HTML（4.6 KB）→ 发现是 SPA，只有 3 个 JS bundle，无 SSR 数据。
2. 从 `newstatic.saikr.com/contest_index/assets/index-*.js`（1.16 MB）里提取：
   - 构建注释：`build-mode: production | api: https://apiv4buffer.saikr.com`
   - axios 实例与拦截器（`baseURL`、`Authorization` 逻辑）
   - 各页面 chunk 里的 `$.get("api/pc/...")` 调用
3. 用 requests 按 JS 里的真实路径直接请求，验证返回结构与鉴权。
4. 拉全量目录（5172 条）做关键词/模糊匹配，评估与现有 32 条赛事的覆盖度。
5. 对匹配到的 32 条逐条调详情接口，统计字段覆盖率、封面尺寸、正文外链。

> 说明：没有用浏览器 DevTools 抓包，而是**从前端代码反推 + 真实请求验证**——结果比抓包更完整（能看到全部参数和分支），成本也更低。

---

## 二、接口拆解

### 2.1 站点形态

- `https://www.saikr.com/` 是纯前端渲染（Vue 3 + Vite），HTML 只有 4.6 KB 骨架，**没有 `__INITIAL_STATE__`/`__NEXT_DATA__`**。
- 直接 requests 抓详情页只能拿到同一份 4.6 KB 空壳（`<title>` 还是站点通用标题），**所以必须走 API，抓 HTML 没有意义**。
- 静态资源在 `newstatic.saikr.com`，图片在 `publicqn.saikr.com`（七牛 CDN）。

### 2.2 三个核心接口（实测全部 200）

| 用途 | 方法与 URL | 关键参数 | 返回 |
|---|---|---|---|
| 筛选项字典 | `GET https://apiv4buffer.saikr.com/api/pc/contest/listConfig` | 无 | 学科分类树（工科/文体/理科/商科…，含 class_id） |
| 赛事列表 | `GET https://apiv4buffer.saikr.com/api/pc/contest/lists` | `page`、`limit`（实测 200 可用）、`sort`（1 开赛时间 / 2 最近更新 / 3 最多浏览）、`class_id`（可 `|` 分隔多选）、`level`、`univs_id` | `{code:200, data:{list:[...], total:5172}}` |
| 赛事详情 | `GET https://apiv4buffer.saikr.com/api/pc/contest/info` | `contest_id=59755` 或 `contest_url=<slug>`（**不要带 `vse/` 前缀**，带了返回 4110「竞赛不存在」） | 75 个字段，含正文 HTML、封面、时间、附件 |

请求示例：

```bash
# 列表（匿名）
curl "https://apiv4buffer.saikr.com/api/pc/contest/lists?page=1&limit=200&sort=2"

# 详情（匿名）
curl "https://apiv4buffer.saikr.com/api/pc/contest/info?contest_id=58069"
```

列表单条字段（够做卡片）：`contest_id / contest_name / contest_url / level_name / organiser / regist_start_time / regist_end_time / contest_start_time / contest_end_time / time_name / thumb_pic / labels / contest_class_second`

详情额外字段（够做详情页）：`content`（正文 HTML，2k~31k 字符）、`web_pic_big`（封面大图）、`contest_stage`（赛程阶段）、`attachment`（通知 PDF，键是文件名）、`sup_organizer`（组织单位）、`look_count / focus_num`（热度）、`is_valid`

时间字段是 **Unix 秒级时间戳**（列表）或 **`2026/11/08 10:00:00` 字符串**（详情）；无比赛时间时详情返回 `-0001/11/30 00:00:00`（即时间戳 0），列表返回 `0`，需要判空。

### 2.3 鉴权结论：**无鉴权**

JS 里的请求拦截器原文（压缩后）：

```js
lt.interceptors.request.use(e => {
  const t = Qt();                       // 读本地登录 token
  return e.authToken = t,
         t && e.headers && (e.headers.Authorization = "saikrapi ".concat(t)),
         e
})
```

即：**只有本地存在登录 token 时才加 `Authorization: saikrapi <token>`**，匿名访问不校验。实测：

- 带完整浏览器头（UA + Origin + Referer）→ 200
- 只带 UA、不带 Referer → 200
- 完全裸 `python-requests/2.x`（连 UA 都不改）→ 200

响应里会下发 `acw_tc=...; path=/; HttpOnly; Max-Age=1800`（阿里云 WAF 的流量标记），但**不回校验**；没有 `X-RateLimit-*`、没有签名参数、没有时间戳/nonce 校验。

### 2.4 反爬与 robots.txt（**必须让产品知道**）

`https://www.saikr.com/robots.txt` 原文关键段：

```
User-agent: *
Allow: /
Disallow: /search
Disallow: /admin/
Disallow: /api/
Disallow: /login
Disallow: /*?*
Crawl-delay: 10

User-agent: bingbot
Disallow: /
```

含义与影响：

1. **`Disallow: /api/` 直接禁掉了我们要用的接口**；`Disallow: /*?*` 连所有带查询串的 URL 都禁（我们的接口全是 query 参数）。
2. `Crawl-delay: 10` 要求单爬虫 10 秒一次；`/search` 禁止；Bing 系全禁（说明站点在主动做爬虫治理）。
3. 实测**没有**触发滑块/验证码/429，但这是"没被发现"，不是"被允许"。**技术可行 ≠ 合规可行**。

> 这一条不解决，不建议直接上生产定时任务。合规路径见第七节。

---

## 三、数据质量实测（对我们 32 条赛事）

### 3.1 目录覆盖

- 全量目录 **5172 条**，翻 26 页（`limit=200`）/ 52 页（`limit=100`）拉完，耗时 66 秒，0 失败。
- 用品牌词 + 名称相似度对我们 32 条做粗匹配：**32/32 都能在赛氪找到对应赛事**（蓝桥杯 12 条、挑战杯 22 条、互联网+ 20 条、计算机设计 15 条、软件杯 8 条、学创杯 6 条、iCAN 7 条、百度之星 7 条、计算机系统 8 条、服务外包 8 条、嵌入式 13 条、物联网 25 条、机器人大赛 113 条、信息安全 18 条、中美青年 4 条、ICPC 15 条）。
- 但**粗匹配有 6 条是错的**，必须人工建映射表：
  - id=24 中国高校计算机大赛·省级选拔赛 → 匹到「2024第三届全国高校计算机技能竞赛」（不相关）
  - id=25 全国大学生软件测试大赛·省级赛 → 匹到一条叫「测试」的记录
  - id=35 中国教育机器人大赛 → 匹到「全国大学生机器人大赛」（不同赛事）
  - id=11 中国高校计算机大赛 → 匹到「中国高校计算机大赛-AIGC创新赛」（是子赛道，不是主赛）
  - id=23/26/27/28/29/30 是"省级赛/分赛区"，会匹到全国主赛——**这可以接受**（用主赛信息兜底），但要在映射表里标注 `match_type=parent`。
- 同一赛事存在**多届重复**（如蓝桥杯 12 条、挑战杯 22 条），必须按"报名开始时间最新 / 年份最新"选版，不能取第一条。

### 3.2 字段可用性（32 条逐条实测）

> 口径：按 3.1 的**粗匹配**逐条调详情接口统计。其中 6 条映射待人工裁决（id=11/24/25/35 及 2 条子赛道），最终数字预计偏差 ±3 条，以 P0 人工映射表为准。

| 字段 | 覆盖 | 说明 |
|---|---|---|
| 报名开始/结束时间 | **32/32** | 结构化、可信，比 LLM 从正文猜稳 |
| 比赛开始/结束时间 | **4/32** | 其余是 0 / `-0001`；**这个字段救不了榜单赛事的"比赛月份"** |
| 封面 `web_pic_big` | **27/32** | 5 条完全没有（id=1、14、17、26、34） |
| 封面能过现有 2.6 闸门 | **13/32** | 比例 1.5~2.4，卡片可用 |
| 封面是宽 banner（2.79~5.49） | **14/32** | 会被 `check_image()` 判 banner 拒绝 |
| 正文配图 ≥1 张 | **12/32** | 集中在赛氪自家商业赛；榜单赛事基本 0 张 |
| 正文有外部链接 | **24/32** | 噪声多：百度百科、B站、`mmbiz.qpic.cn`、`52jingsai.com`；需域名白名单 |
| 通知 PDF 附件 | 部分 | 如蓝桥杯、挑战杯有官方通知 PDF URL |
| 正文长度 | 2k~3.3k 字符（榜单赛事） | 是**官方通知原文**，比高校转载页干净，适合喂 LLM |

### 3.3 关键发现：赛氪有两类页面，质量完全相反

| | 赛氪自家/商业赛（占绝大多数） | 榜单/官方赛事（我们真正要的） |
|---|---|---|
| 正文配图 | **7~16 张**，含海报/证书/流程图 | **0 张** |
| 比赛时间 | 结构化有值 | 大多为 0 |
| 封面 | 列表缩略图 760×506（1.5:1），卡片友好 | 多为 `web_pic_big` banner（997×263、1875×404、4:1） |
| 正文 | 营销文案 + 报名引导 | **官方通知原文**（蓝桥杯 1.9k 字、挑战杯 3.3k 字），常带官网链接 |
| 标题 | 带「【最后3天】」「【可领证书】」营销前缀 | 相对规范 |

> 这解释了为什么"赛氪图多"的直觉对**商业赛**成立，对**我们收录的榜单赛事**基本不成立。

### 3.4 封面尺寸实例（读文件头，未落盘）

| 赛事 | 尺寸 | 比例 | 现有闸门 |
|---|---|---|---|
| 挑战杯·创业计划 | 997×263 | 3.79 | ❌ banner |
| 计算机设计大赛 | 1875×404 | 4.64 | ❌ banner |
| 蓝桥杯（第16届） | 实测宽高比 2.88 | 2.88 | ❌ 略超 |
| 中美青年创客 | —（比例 1.94） | 1.94 | ✅ 通过 |
| 嵌入式芯片赛 | —（比例 1.54） | 1.54 | ✅ 通过 |
| iCAN | 1920×800 | 2.40 | ✅ 通过 |
| 服务外包（thumb 样例） | 760×506 | 1.50 | ✅ 通过 |

图片体积 60 KB~750 KB，`publicqn.saikr.com` **无 Referer 防盗链**（裸请求 200），前端可直接 `<img src>` 引用（仍建议后续加图片代理，防 CDN 策略变化）。

### 3.5 结论：赛氪能解决什么

| 我们的痛点 | 赛氪能否解决 | 说明 |
|---|---|---|
| 封面图 | **部分解决** | 27/32 有图；13 张可直接用，14 张是 banner（需产品定策略） |
| 报名时间 | **能** | 结构化 32/32，可替代 LLM 猜报名月份 |
| 比赛时间 | **不能** | 只有 4/32；仍需 LLM 读正文/PDF，且继续执行「截止日不算比赛时间」红线 |
| 官网 | **边际改善** | 我们 32 条 official_url 本来都有；对**新发现赛事**有用（24/32 正文有外链） |
| 简介/摘要 | **能** | 官方通知原文干净，LLM 摘要质量会比高校转载页高 |
| 新赛事发现 | **能，但噪声大** | 5172 条里大部分是商业赛/校级赛，需按 level + 品牌白名单过滤 |

---

## 四、Playwright 可行性（兜底方案，不是首选）

**结论：现阶段不需要。** 只有当赛氪给 API 加签名/风控、或接口改版时，才启用。

| 维度 | requests 直连（当前） | Playwright 无头浏览器 |
|---|---|---|
| 单条耗时 | 0.07~0.27 秒 | 2~5 秒（SPA 渲染 + 等 XHR） |
| 32 条总耗时 | ~10 秒 | 2~4 分钟 |
| 依赖体积 | 0（venv 已有 requests） | `pip install playwright` 约 40 MB；浏览器可复用本机 **Edge 157 / Chrome 157**（`channel="msedge"`），**无需再下 150 MB Chromium** |
| 内存 | 忽略 | 每 context 200~400 MB |
| 稳定性 | 高（接口不变） | 中（DOM 改版、WAF JS 挑战、懒加载） |
| 维护成本 | 低（字段缺失告警） | 中高（选择器、等待策略、无头特征） |
| 能否绕 WAF | 不适用 | 能执行 JS 挑战，但仍可能被设备指纹识别 |

如果将来要上，建议：`playwright` + `channel="msedge"` + 拦截 `**/api/pc/contest/**` 的响应直接取 JSON（**不要解析 DOM**），这样页面结构改版也不影响数据，成本最低。

---

## 五、落地方案

### 5.1 总体链路

```text
赛氪目录 API（26 页 / limit=200）
        │  一次性/每周全量
        ▼
品牌+名称匹配 ──► 人工确认映射表（competition_sources）
        │
        ▼（只对我们关心的 ~32 条）
赛氪详情 API（contest_id）
        │
        ├─ 结构化字段：报名时间、封面、主办方、级别、类别
        ├─ content HTML ──► 复用 crawler_competitions 的 LLM 抽取（简介/摘要/比赛月份/官网）
        └─ 图片 ──► 复用 batch_update_competitions.check_image() 闸门（只存 URL）
        │
        ▼
字段合并策略（只填空 / 可覆盖白名单）──► test_crawl.db 演练 ──► 人工抽查 ──► 生产库
```

**复用现有资产，不另起炉灶**：

- 抓取/正文清洗：`crawler_competitions.fetch_competition_page()` 可复用其请求头/编码处理逻辑（但赛氪走 API，不走 HTML）。
- LLM 抽取：`crawler_competitions.extract_competition()` 直接喂 `content` 纯文本（去标签），保留 SYSTEM_PROMPT 的「截止日不算比赛时间」红线。
- 封面闸门：`batch_update_competitions.check_image()` / `_image_dimensions()` 原样复用。
- 写库：`batch_update_competitions.build_patch()` 的「只填空」策略 + 自动备份 + `--allow-production` 三件套。
- 新增文件建议：`crawler_saikr.py`（只读采集 + 归一化），**不要**把赛氪逻辑塞进现有两个大文件。

### 5.2 分阶段（建议排期）

| 阶段 | 内容 | 工作量 | 产出 | 是否写库 |
|---|---|---|---|---|
| **P0 映射表** | 人工确认 32 条 ↔ 赛氪 contest_id（含 parent/子赛道标注、选最新届） | 0.5 天 | `saikr_map.json` | 否 |
| **P1 只读采集器** | `crawler_saikr.py`：目录同步 + 详情抓取 + 归一化 + dry-run 打印 | 1~1.5 天 | 命令行工具 | 否 |
| **P2 试点 5 条** | 选蓝桥杯/挑战杯/计算机设计/软件杯/服务外包，对比现有库字段，人工验收 | 0.5 天 | 对比报告 | 只写 test 库 |
| **P3 存量补全** | 32 条批量补图/补报名月份/补简介，自动备份 | 0.5 天 | 生产库更新 | 是（需授权） |
| **P4 增量任务** | 夜间增量：`sort=2` 前 3 页 + 映射赛事详情，变更检测 | 1 天 | 定时任务 | 是 |

**P0/P1 之前不要动生产库。**

### 5.3 字段映射

| 赛氪字段 | 我们字段 | 规则 |
|---|---|---|
| `contest_name` | `name` | 去掉「【…】」营销前缀、年份、届次；**不覆盖**人工维护的名称 |
| `level_name` | `level` | 全国性/全球性 → `national`；省级 → `provincial`；其他/校级/自由 → 不入库 |
| `contest_class_second` | `category` | 建映射表（computer→计算机综合与软件、acm→程序设计、tiaozhanbei→创新创业…），映射不上的保留原 category |
| `regist_start_time` / `regist_end_time` | `reg_start_month` / `reg_end_month` + `reg_cross_year` | 取月份；跨年时 `reg_cross_year=1` |
| `contest_start_time` / `contest_end_time` | `contest_start_month` / `contest_end_month` | **仅当值有效**（非 0、非 `-0001`）才写；否则保持 NULL，交给 LLM |
| `web_pic_big` | `image_url` | 过 `check_image()` 才写；banner 按产品决策处理（见第七节） |
| `content` | 喂 LLM | 去标签后取前 6000 字；产出 `intro/summary/official_url/contest_*_month` |
| 正文 `<a href>` | `official_url` | 仅当现有为空时，用域名白名单（lanqiao.cn、tiaozhanbei.net、cnsoftbei.com 等）提取 |
| `contest_id` / `contest_url` | 新表 `competition_sources` | 溯源用，不改 `competitions` 表结构 |

### 5.4 匹配与选版规则

1. 先用品牌词（蓝桥杯/挑战杯/互联网+/计算机设计/软件杯/学创杯/iCAN/百度之星/计算机系统/服务外包/嵌入式/物联网/机器人大赛/信息安全/中美青年/ICPC）圈定候选。
2. 候选里按 `level_name` 优先「全国性」，再按 `regist_start_time` 取最新一届。
3. 名称相似度 < 0.8 或候选歧义（同品牌多届）→ **进人工确认队列**，不自动写。
4. 映射表存 `match_type`：`exact` / `parent`（省级赛挂全国主赛）/ `manual`，后续可审计。

### 5.5 限速与稳定性

- 列表：26 次请求，间隔 1 秒（一次性）；增量只拉前 3 页，间隔 2 秒。
- 详情：每条间隔 **3~5 秒**（不低于现有赛事补全的 5~10 秒红线精神），32 条约 2~3 分钟。
- 失败重试：3 次指数退避；仍失败记 `skipped` 继续，不中断批次。
- 监控：每日记录 `code!=200` 比例、字段缺失率、`content` 长度突变（接口改版信号）。

### 5.6 新表（不动 `competitions`）

```sql
CREATE TABLE IF NOT EXISTS competition_sources (
    source        TEXT NOT NULL,              -- 'saikr'
    source_id     TEXT NOT NULL,              -- contest_id
    source_url    TEXT,                       -- https://www.saikr.com/vse/<slug>
    competition_id INTEGER,
    match_type    TEXT,                       -- exact / parent / manual
    confidence    REAL,
    updated_at    TEXT,
    PRIMARY KEY (source, source_id)
);
```

理由：生产库现在**不能**随便加列（红线 5 只允许精确 `UPDATE`），独立表可随时回滚、也方便以后接第二个数据源。

---

## 六、风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| robots.txt 禁 `/api/` 与 query 串 | **高（合规）** | 先走商务/官方授权；未授权前只做小批量人工核验，不做定时全量；页面署名「数据来源：赛氪」并回链 |
| 接口是内部接口，随时可能改 | 中 | 字段缺失/`code!=200` 告警；保留 Playwright 拦截 XHR 的兜底分支 |
| banner 封面裁进 4:3 卡片观感差 | 中 | 产品决策（放宽/仅详情页用/占位图） |
| 目录 90% 是商业赛/校级赛 | 中 | 只按映射表取数，不做"全量入库" |
| 多届重复、营销标题 | 中 | 选最新届 + 名称清洗 + 人工确认 |
| 图片版权/热链 | 中 | 只存 URL（红线 8）、署名回链、后续加图片代理 |
| 被 WAF 限流/封 IP | 低~中 | 3~5 秒延迟、夜间跑、单 IP 小流量、失败即停 |
| 生产库误写 | 低 | 复用现有 `--dry-run` 默认 + `--allow-production` + 自动备份三件套 |

---

## 七、需要产品拍板的三件事

1. **banner 封面怎么处理？**
   - A. 保持 2.6 闸门：**13/32** 有封面，其余占位图（最保守，和现行红线一致）
   - B. 放宽到 5.5 + `object-cover` 居中裁切：约 **27/32** 有图，但 4:1 海报裁进卡片会切掉两侧文字，观感风险高
   - C. **卡片仍用 2.6 闸门，详情页顶部允许 banner 大图**（推荐：卡片干净，详情页信息量足）
2. **范围：只补现有 32 条，还是扩到赛氪全量发现新赛事？**
   - 建议先做 32 条（P0~P3），验证字段质量后再评估"发现"能力；全量 5172 条里大部分是商业赛，直接入库会污染列表。
3. **合规：是否先联系赛氪商务/BD 拿授权？**
   - 建议发一封合作邮件（数据引用 + 回链 + 低频率），拿到书面许可再上定时任务；否则只做一次性人工核验。

---

## 八、本次产出与下一步

**本次已完成（只读）**：

- 打通 3 个接口，验证匿名可调、无签名、无 Cookie 依赖；
- 全量目录 5172 条落盘 `workspace/saikr_catalog.json`（供映射复用）；
- 32 条赛事字段覆盖率、封面尺寸、外链质量实测；
- 单条详情样例 `workspace/saikr_detail_59755.json`（75 字段，可对照）。

**下一步（等产品确认第七节）**：

1. P0：人工确认 32 条映射表（我可以先出一版 `saikr_map.json` 草案，标出 6 条需人工裁决的）。
2. P1：写 `crawler_saikr.py`（只读 + dry-run），先在 5 条上跑通。
3. P2：对比现有库字段，出「补什么/不补什么」清单，再决定是否写生产库。

> 备注：本次调研**没有**修改 `crawler_competitions.py` / `batch_update_competitions.py`，也没有写任何数据库；任务 1 的 Prompt 验证是在生产库 `--dry-run` 下完成的，未落库。
