# CRAWLER_HANDOVER.md

> 交接范围：`backend/` 下高校新闻获奖采集、赛事基础数据采集、**生产库赛事批量补全**、**赛氪网数据源预检**、**候选表与审核 CLI**、**AI 攻略/灵感生成链路**、**云端定时任务链**、测试库联调与数据库迁移。
> 生成时间：2026-11-07（Asia/Shanghai）｜最后更新：2026-11-08 13:40（Git 安全存档 + 爬虫边界测试 + 《云端部署手册》交付）
> 原则：后续接手请先读本文件，再按需读代码。

---

## 零、接手必读（30 秒版）

**当前阶段：爬虫闭环 + AI 内容生成闭环已完成；2026-11-08 已完成阿里云部署（原地升级 `/root/KuAo` + systemd + crontab，见 12.5），下一步 = 80 条候选审核 + 新闻 `LIST_URL` 确定后补挂 cron。**

> **【紧急 TODO｜2026-11-08】详情页数据无法正常展示**：电脑/手机访问 `/competition/10` 时，「时间安排、参赛对象」显示通用套话，官网链接、具体比赛月份、已抓取攻略均未展示。排查顺序：① 生产库 `id=10` 的真实字段（`official_url` / `reg_*` / `contest_*` / `full_process_guide`）；② `GET /api/competitions/10` 的 JSON 返回是否包含这些字段；③ 前端 `frontend/src/views/CompetitionDetailView.vue` 的兜底与渲染逻辑（`signUpTime` / `contestTime` 拼接、`resolveImage`）。

| 项目 | 现状 |
|---|---|
| 生产库 `competitions.db` | **33** 条赛事（含 id=36）/ **87** 条获奖；有封面 **6** 条（id=6/8/18/22/27/34）；有比赛月份 **8** 条；**候选 82 条（pending 80 / approved 1 / rejected 1）**；**有攻略 7 条（id=1–6、10）**；**ai_jobs 0 条**；孤儿 insights/ideas **已清零**；SHA-256 `3af3af9c2d8fb4f278bff012f96aa4bd9bd878697443d910359f015066f54d73` |
| 测试库 `test_crawl.db` | **33** 条赛事 / **88** 条获奖 / **82** 候选；**有攻略 9 条**（本轮真机生成 id=21、12）；**ai_jobs 2 条（均已 done）**；SHA-256 `b001887393e2103c2ad22e584de21970528de99063d33e7fc7754aaa6721b0b9` |
| `competition_candidates` | 2026-11-08 落地并部署生产；`db.py` 建表 + 插入/去重/统计/提拔函数；生产/测试各 **82** 条 |
| 审核 CLI | `review_candidates.py --list / --approve <id> / --reject <id>`；approve 重新过 `check_image()`、同事务写主表 + 标记 approved |
| 前端封面兜底 | 已完成；本轮 `npm.cmd run build` 再次通过（7.36s / 115 modules） |
| 赛氪映射 + 预检 | 已完成：32 条映射（22 精确 / 7 挂主赛 / 1 子赛道占位 / 2 无对应）、16 条报名冲突 |
| 生产库写入 | 2026-11-08 12:05：5 封面 + 2 条比赛时间 + id=34 报名起始月；备份 `competitions.db.bak-20261108-120553` |
| 生产候选部署 | 2026-11-08 12:34：62 条目录 + 20 条赛氪；备份 `…bak-20261108-123438` / `…123446` |
| **AI 攻略链路** | **已完成（2026-11-08 13:15）**：`ai_jobs` 表 + 4 个 `guide_*` 列已上生产；`POST/GET /api/competitions/{id}/generate_guide`（登录 + 异步 + 幂等）；前端详情页「✨ 生成 AI 攻略」按钮 + 轮询；列表接口已剔除长文 |
| **每日慢刷** | **已完成**：`daily_ai_worker.py`（4 篇攻略 + 2 条灵感/天，预算熔断、3 天失败冷却、自动备份）；测试库真机跑通 2 篇（3378 + 2945 tokens） |
| **孤儿数据清理** | **已执行（2026-11-08 13:09）**：删除 insights 10 条 / ideas 12 条；备份 `competitions.db.bak-20261108-130926` |

**本轮新决策（不要擅自推翻）：**

1. **写生产库时，报名时间只填补 NULL，禁止覆盖已有值**。赛氪给的是平台报名窗口，与库内官方口径可能不同（16 条冲突清单见第六节第 15 条），已有值一律保留。
2. **封面仍执行方案 C**：宽高比 >2.6 一律拒收；无图交给前端渐变首字兜底，详情页也不放宽 banner。
3. **本轮赛氪抓取只做存量 32 条**：低频、小批量、测试级；不扩到 5172 条全量、不商用；`robots.txt` 明确 `Disallow: /api/`，合规风险已知。**新赛事发现另立方案**，方案评审通过前不写生产库。
4. **往年届次护栏**：赛氪页面报名结束超过 400 天视为陈旧，整条跳过（id=2/4/17/26/30 已跳过）。
5. **新赛事发现一律先进候选/审核**：发现源只负责产出 `competition_candidates`，人工审核后才 INSERT `competitions`，不允许自动写生产库（见第九节）。
6. **候选表 POC 已定稿（2026-11-08 12:35）**：`discover_university.py`（84 项目录，剔除 2 条非比赛，写入 62 条）+ `discover_saikr.py`（前 2 页 × 10 = 20 条/天，3~5 秒延迟）均已跑通；赛氪当天重复 `--apply` 会被每日守卫拦截，需 `--force`。
7. **审核机制已落地（2026-11-08 12:36）**：`review_candidates.py` 已就绪；approve 会**重新过 `check_image()`**，在同一事务里 INSERT `competitions`（`sort_order=9999`）+ 候选标记 `approved` + 回填 `competition_id`；reject 只改候选状态。
8. **AI 生成必须登录 + 预算闸门（2026-11-08 定）**：攻略接口依赖 `get_current_user`，不允许匿名；全站每日 8 个任务、队列 5、失败冷却 30 分钟；`ai_jobs` 唯一部分索引保证同赛事同时只有 1 个活跃任务。**不要绕过它直接调模型写库**。
9. **攻略只在详情接口返回**：列表接口已移除 `full_process_guide`（30+ 条长文会让列表响应多出上百 KB）；前端列表只展示卡片信息。
10. **孤儿 insights/ideas 直接删除（已执行）**：`competition_id` 为空/悬空的数据在按赛事隔离的接口下永远读不到，不回填、不保留。

**下一步安排（按优先级）：**

1. **《云端部署手册》已执行（2026-11-08）**：见 `CLOUD_DEPLOYMENT_GUIDE.md`；实际采用原地升级，完整记录见 12.5。后续按 12.5 的回滚路径维护。
2. **Crontab 已挂（2026-11-08）**：5 条任务（02:30 备份 / 04:10 候选 / 04:40 AI / 周日 05:20 补全 / 05:40+05:50 清理）；**新闻采集暂缓**，`LIST_URL` 确定后单独加一条。
3. **并行人工审核**：生产库 80 条 pending 继续用 `review_candidates.py` 审核；服务/广告类（「辅助报名」「考试入口」）直接 `--reject`。

> ⚠️ 生产库现在**允许写**（用户 2026-11-07 明确授权），但每次写库前必须自动备份，且所有脚本默认 dry-run —— 见第四节红线。

---

## 一、架构说明

### 1. 高校新闻获奖采集器

**文件：`crawler_news.py`**（由 `crawler_demo.py` 重命名而来，旧文件名已不存在）

核心作用：

- 单页新闻抓取：`requests` + `BeautifulSoup` 提取正文，调用 `llm.py` 抽取获奖信息；
- 列表页批量：解析列表页详情链接、自动识别“下一页”、支持显式分页规则；
- 逐条写入 `awards` 表，`source_url` 保存原文链接；
- 去重：入库前按 `source_url` 查重，入库时由 `content_hash` 唯一索引兜底；
- 非获奖新闻过滤：列表标题粗筛 + 正文关键词粗筛 + 大模型 `is_award_news` 判定；
- 孤立记录处理：赛事匹配不到 `competitions` 时自动创建占位赛事，保证 `competition_id` 不为 NULL；
- 生产库红线：真实写库必须传 `--db-path`，生产库默认拒绝，需 `--allow-production` 手动解除。

关键函数：

| 函数 | 作用 |
|---|---|
| `fetch_detail_links(list_url)` | 传入列表页 URL，返回详情页 URL 列表 |
| `extract_detail_links(html, list_url)` | 纯解析版，传入 HTML + 列表页 URL |
| `find_next_page_url(html, page_url)` | 自动判断下一页 |
| `build_page_urls(list_url, pages, page_url)` | 生成显式翻页 URL |
| `_run_batch(args)` | 批量采集主控，逐条异常隔离 |
| `ensure_competition()` | 未知赛事自动建占位 |
| `save_award()` | 调 `db.create_award` 写库 |

### 2. 赛事基础数据采集器

**文件：`crawler_competitions.py`**

核心作用：

- 抓取单个赛事详情/通知页；
- 提取 `<head>` 的 `og:image`（没有则回退正文首图，只存 URL，不下载图片）；
- 调用 `llm.py` 抽取赛事字段；
- 按名称匹配 `competitions`：命中则 UPDATE，未命中则 INSERT；
- 更新/插入后回读目标库并打印关键字段；
- 支持 `--competition-id` 强制更新占位赛事，支持 `--force-insert` 测插入路径；
- 生产库红线同 `crawler_news.py`。

### 3. 共享依赖

| 文件 | 作用 |
|---|---|
| `llm.py` | 大模型 JSON 抽取封装（`chat_json` / `safe_json` / 重试） |
| `db.py` | SQLite 数据访问层、建表/迁移、awards/competitions CRUD |
| `config.py` | 读取 `.env`（DeepSeek、JWT、CORS 等） |
| `verify_crawl.py` | 读回并校验 awards 写入结果，拒绝查询生产库 |

### 4. 生产库数据工具（本轮新增）

| 文件 | 作用 |
|---|---|
| `merge_test_competitions.py` | 把测试库里生产库没有的赛事/获奖并入生产库（name + content_hash 去重，写前备份） |
| `batch_update_competitions.py` | 遍历缺失字段的赛事 -> 抓入口页 -> 大模型抽取 -> **只填空**；限流/重试/失败跳过/封面质量闸门 |
| `migrate_production.py` | 生产库体检 + 备份 + `db.init_db()` 补列（默认只读体检，`--apply` 才动） |
| `url-map.json` | 覆盖采集入口页的映射 `{"赛事id": "通知页URL"}`，用于官网首页抓不到内容时改指通知页 |

**`batch_update_competitions.py` 的关键行为（改它之前务必先读这段）：**

- **只填空**：`FILLABLE = (image_url, contest_start_month, contest_end_month, official_url, intro, summary, category, level_label)`，只有库里当前为空才写；`name` 默认不动，`reg_*_month` 也不动（人工/种子数据优先）。
- **采集溯源**：无论是否命中，都会写 `discover_source` + `last_checked_at`，但这两个字段不算「有新信息」，不会让一条无产出记录被计为 updated。
- **封面质量闸门** `check_image()`：HTTP 200 + `Content-Type: image/*` + 从文件头解析宽高（PNG/JPEG/GIF/WebP，无三方依赖）→ 宽 ≥300、高 ≥150、宽高比 ≤2.6、反向 ≤2.2；不合格**宁可不写**。逐张试候选（og:image 优先，正文图其次，最多 4 张）。
- **入口**：优先 `official_url`，可用 `--url-map` 覆盖；`--ids` 指定时忽略 `--filter`。

### 5. 赛氪数据源预检（2026-11-08 新增，只读）

**文件：`pipeline_update.py` + `saikr_map.json`**

- 赛氪是 Vue SPA，HTML 只有 4.6 KB 空壳，必须走内部 JSON API：
  - 列表：`GET https://apiv4buffer.saikr.com/api/pc/contest/lists?page=&limit=&sort=&class_id=&level=`
  - 详情：`GET https://apiv4buffer.saikr.com/api/pc/contest/info?contest_id=<id>`
  - 匿名可调，无 Token/签名/强制 Cookie；`acw_tc` 是阿里云 WAF 被动标记。
- `pipeline_update.py` 链路：赛氪详情 → 封面过 `check_image()` + 报名月份 → 通知页/官网抓正文 → 大模型抽比赛月份 → 逐条汇总（**只读，不写库**）。
- 护栏：往年届次（报名结束 >400 天）整条跳过；正文 <300 字不调模型；赛氪 3~5 秒、通知页 5~10 秒限速。
- `saikr_map.json`：32 条赛事 ↔ 赛氪 contest_id 映射（22 精确 / 7 省级或分赛区挂全国主赛 / 2 无对应 / 9 条标了 needs_review）。
- **`--apply` 已实现（2026-11-08）**：生产库必须 `--allow-production`；写前用 sqlite backup API 自动备份并校验；`build_ops()` 只补 NULL（逐字段），`apply_ops()` 的 UPDATE 再带一次 `IS NULL` 闸门；封面写前重新 `check_image()` 复检；`--from-report` 可复用预检报告，避免重复联网/烧模型额度。

---

## 二、文件清单与改动点

### 新增文件

| 文件 | 改动点 |
|---|---|
| `crawler_news.py` | 原 `crawler_demo.py` 重命名；新增 `--allow-production`；真实写库必须先指定测试库；批量/单页能力保留 |
| `crawler_competitions.py` | 新增赛事采集器：正文/og:image 提取、AI 抽取、competitions upsert、回读验证 |
| `verify_crawl.py` | 新增 awards 读回校验脚本：按 `source_url/award_grade/competition_id/year` 做断言，拒绝生产库 |
| `test_crawler_units.py` | 新增单测（pytest）：覆盖 `extract_image_url`（og:image/首图回退/logo 过滤）与 `_normalize_level`，19 例全绿，不连网不写库 |
| `migrate_production.py` | 新增生产迁移脚本：默认只读体检，`--apply` 才先备份 `competitions.db.bak`、校验备份、再 `db.init_db()` 补列；未在真实生产库执行过 |
| `batch_update_competitions.py` | 新增批量补全脚本：遍历 `image_url` 为空的赛事 -> 抓官网 -> 大模型抽取 -> **只填空**；5~10 秒随机延迟、单条重试、失败跳过、写前备份、封面图质量闸门（可达性 + 尺寸/宽高比，挡 banner 与死链）；支持 `--ids / --limit / --filter / --url-map` |
| `merge_test_competitions.py` | 新增合并脚本：把 `test_crawl.db` 里生产库没有的赛事/获奖按 name + content_hash 去重后并入生产库，写前自动备份 |
| `url-map.json` | 新增：把 5 条老赛事的入口页改指到高校通知页（id=1/2/10/12/13），供 `--url-map` 使用 |
| `pipeline_update.py` | 新增（2026-11-08）：赛氪 + 通知页统一清洗脚本；默认只读预检，`--apply` 才写库；`--from-report` 复用预检报告；`--ids/--limit/--skip-saikr/--skip-notice/--saikr-delay/--notice-delay` |
| `saikr_map.json` | 新增（2026-11-08）：32 条赛事 ↔ 赛氪 `contest_id` 映射草案（含 `match_type / needs_review / note`），写库前需人工过一遍 9 条待裁决 |
| `discover_common.py` | 新增（2026-11-08）：候选表发现公共层——目标库保护/生产库备份、`ensure_candidates_table`、入库循环、去重统计、类别猜测 |
| `discover_university.py` | 新增（2026-11-08）：高校 84 项目录页发现器；支持文本行/表格、序号变体、无 scheme 官网、过滤非比赛词，默认 dry-run |
| `discover_saikr.py` | 新增（2026-11-08）：赛氪列表前 2 页（≤20 条/天）增量发现；3~5 秒延迟、往年届次跳过、每日守卫 |
| `review_candidates.py` | 新增（2026-11-08）：候选审核 CLI；`--list/--approve/--reject`，approve 重新过封面闸门并在同一事务写主表 |
| `ai_content.py` | 新增（2026-11-08）：AI 生成核心——v2 攻略 Prompt（注入月份 + 获奖作品，禁止编造）、`generate_guide_for()`、`generate_inspiration()`、输入指纹 `guide_input_hash()` |
| `ai_jobs_runner.py` | 新增（2026-11-08）：任务编排层——单线程池、预算/队列/冷却、`run_guide_job` / `run_inspiration_job`、按需 `request_guide()` |
| `daily_ai_worker.py` | 新增（2026-11-08）：每日慢刷（默认 dry-run）；选片、预算熔断、3 天冷却、自动备份、JSON 报告 |
| `cleanup_orphan_ai.py` | 新增（2026-11-08）：孤儿 insights/ideas 清理（默认 dry-run，写生产需 `--allow-production` + 自动备份） |
| `test_ai_guide.py` | 新增（2026-11-08）：16 例单测/接口测试（ai_jobs、幂等、409/429、列表瘦身、Prompt、孤儿清理），全部 mock 大模型 |
| `AI_AUTOMATION_PLAN.md` | 新增（2026-11-08）：AI 生成与自动化方案评审稿（调研 + 设计 + crontab 链） |
| `backup_db.py` | 新增（2026-11-08）：SQLite 在线备份（backup API，WAL 安全，单文件 DELETE 模式，integrity + 行数校验，按天/份数保留）；供 crontab 02:30 调用 |
| `test_crawler_boundaries.py` | 新增（2026-11-08）：上云边界测试 20 例——Timeout/ConnectionError 不中断批量、残缺 JSON `safe_json` 兜底、空网页 `extract_page` 抛可预期 `CrawlError` 且批量继续 |
| `CLOUD_DEPLOYMENT_GUIDE.md` | 新增（2026-11-08）：《云端部署手册》——阿里云 Ubuntu 依赖、代码拉取、`.env`、DB 快照/WAL、systemd、Nginx、crontab 链、验收与回滚 |
| `CRAWLER_HANDOVER.md` | 本交接文档 |

### 修改文件

| 文件 | 改动点 |
|---|---|
| `crawler_competitions.py` | ① `SYSTEM_PROMPT` 增加「比赛时间 ≠ 报名/提交截止」红线，无依据必须 null（2026-11-07，影响单页与批量两个入口）；② `SCHEMA_HINT` 同步加注；③ `fetch_competition_page()` 额外把原始 HTML 挂到 `page["html"]`，供批量脚本在封面不合格时重挑候选图而不必二次抓取 |
| `db.py` | `_AWARD_WRITABLE_FIELDS` 补 `competition_raw`/`award_grade`；`_AWARD_EMBEDDING_FIELDS` 同步；`_MIGRATION_COLUMNS` 新增 `image_url / contest_start_month / contest_end_month / discover_source / last_checked_at / is_active`；`update_competition` 白名单补这些字段；`list_preparations` 的 JOIN 补出 `image_url / contest_*_month` 并对缺列老库降级为 NULL；**2026-11-08 新增**：`CANDIDATES_DDL`、`init_db(db_path=None)` 建候选表、`normalize_competition_name / find_duplicate_candidate / add_candidate / query_candidates / candidate_stats / update_candidate_status / ensure_candidates_table / promote_candidate`；`find_duplicate_candidate` 支持 `include_candidates=False`（审核时只查主表） |
| `schema_v2.sql` | `competitions` 建表新增图片、比赛时间、采集溯源、上下架字段（新库直接带列） |
| `models/schemas.py` | `Competition` 新增 `image`、`contest_start_month`、`contest_end_month` |
| `routers/competitions.py` | `_row_to_competition` 把 DB 的 `image_url` 映射为 API 的 `image`，并输出比赛月份 |
| `requirements.txt` | 新增爬虫依赖 `requests==2.34.2`、`beautifulsoup4==4.13.4`、`lxml==5.4.0`；末尾新增「开发/测试专用」段：`pytest==9.2.0`、`ruff==0.17.4`、`pyflakes==4.1.0`（部署镜像可不装） |
| `db.py`（2026-11-08 13:00 追加） | 新增 `AI_JOBS_DDL` + `ensure_ai_jobs_table`；`_MIGRATION_COLUMNS` 补 `guide_generated_at / guide_model / guide_prompt_ver / guide_input_hash`；`save_generated_guide`；`claim_ai_job`（`BEGIN IMMEDIATE` 原子抢占）/`start/finish/fail/skip`/`get_guide_state`/`select_guide_candidates`/`select_inspiration_candidates`/`count_orphan_ai_content`/`delete_orphan_ai_content`/`guide_coverage` |
| `routers/competitions.py`（2026-11-08 13:00 追加） | 新增 `POST/GET /api/competitions/{id}/generate_guide`（登录、202/409/429、幂等）；详情返回 `guide_status/guide_generated_at`；列表不再返回长文 |
| `models/schemas.py`（2026-11-08 13:00 追加） | `Competition` 移除 `full_process_guide`；`CompetitionDetail` 新增 `full_process_guide / guide_status / guide_generated_at` |
| `config.py`（2026-11-08 13:00 追加） | 新增 `AI_GUIDE_DAILY_LIMIT=8` / `AI_GUIDE_QUEUE_MAX=5` / `AI_GUIDE_FAIL_COOLDOWN_MIN=30` |
| `generate_guides.py`（2026-11-08 13:00 重写） | 改为调用 `ai_content`（v2 Prompt）；新增 `--db-path / --allow-production / --max-tokens`；`--dry-run` 不再执行 `init_db()`；写生产前自动备份 |
| `migrate_production.py`（2026-11-08 13:00 追加） | `TARGET_COLUMNS` 补 4 个 `guide_*`；体检/复检新增 `ai_jobs` 表检查 |
| `frontend/src/services/api.js` | 新增 `requestGuide / getGuideStatus`；错误对象补 `status`，供按钮区分 401/409/429 |
| `frontend/src/views/CompetitionDetailView.vue` | 攻略为空时显示「✨ 生成 AI 攻略」按钮 + 登录引导 + 轮询（3s→10s）+ 生成时间展示 |

### 前端封面兜底（2026-11-08 完成）

| 文件 | 改动点 |
|---|---|
| `frontend/src/utils/coverFallback.js` | 新增：赛事名哈希 → 稳定色相（0-359）+ 首字提取（跳过引号/括号） |
| `frontend/src/assets/main.css` | 新增 `.cover-fallback` / `.cover-fallback__glyph` / `.cover-fallback--hero`，含 `.dark` 暗色适配 |
| `frontend/src/components/CompetitionCard.vue` | 无图或 `@error` 加载失败 → 渐变首字；容器仍 `aspect-[4/3]`，**布局尺寸不变**；换图自动重试 |
| `frontend/src/views/CompetitionDetailView.vue` | 详情页 `h-64` 大图位同样兜底，替换原灰色虚线空盒 |
| `frontend/dist` | 已重新 `npm.cmd run build` 通过（2026-11-08 复验 8.37s / 115 modules）；PowerShell 里必须用 `npm.cmd`，`npm.ps1` 会被执行策略拦截 |

验证截图：`backend/workspace/cover-light.png`、`cover-dark.png`、`cover-detail.png`。

### 测试库与产物（非源码）

| 路径 | 说明 |
|---|---|
| `test_crawl.db` | 测试库；`*.db` 已被 `.gitignore` 忽略 |
| `workspace/crawl_demo_result.json` | 默认新闻页真实抽取记录 |
| `workspace/crawl_demo_math.json` | 数学建模页真实抽取记录 |
| `workspace/batch_report.json` | 列表批量测试报告 |
| `workspace/competition_insert_edu_robot.json` | 中国教育机器人大赛插入记录 |
| `workspace/competition_update_robot.json` | 中国机器人大赛占位更新记录 |
| `workspace/batch_competitions_*.json` | 批量补全每次运行的完整报告（含大模型原始 JSON、封面候选判定、写入前后）；按时间戳命名 |
| `workspace/pipeline_report.json` | 2026-11-08 赛氪 + 通知页 32 条预检原始记录（测试库基线） |
| `workspace/pipeline_report_prod.json` | 同一批结果按生产库只读基线重算的预期写入结果 |
| `workspace/pipeline_report.md` | 可读版预检报告（总览 + 32 条明细 + 16 条报名冲突） |
| `workspace/pipeline_apply_20261108-120553.json` | 写生产库完整记录：10 个字段的将写/跳过清单、备份路径、写入前后覆盖、SHA-256 |
| `workspace/saikr_catalog.json` | 赛氪 5172 条目录缓存（列表 API 原始字段，3.9 MB；新赛事发现可直接复用，勿重复拉取） |
| `workspace/cover-light.png` / `cover-dark.png` / `cover-detail.png` | 前端封面兜底验证截图 |
| `url-map.json` | 赛事 id -> 通知页 URL 的覆盖映射（当前 5 条） |
| `competitions.db.bak-*` | 每次写生产库前的自动备份（见下方快照） |

### 当前快照（2026-11-08 13:15，AI 链路落地 + 生产库迁移后）

| 库 | 记录数 | 说明 |
|---|---|---|
| `competitions.db`（生产） | competitions=**33**，awards=**87**，candidates=**82**，insights=**3**，ideas=**4**，ai_jobs=**0** | 已迁移 `ai_jobs` + 4 个 `guide_*` 列；id=36 由赛氪候选 82 审核通过（`category='待确认'`、无官网、`sort_order=9999`）；候选 **pending 80 / approved 1 / rejected 1**；**有攻略 7 条**；孤儿数据已清零；`integrity_check=ok`；SHA-256 `3af3af9c2d8fb4f278bff012f96aa4bd9bd878697443d910359f015066f54d73` |
| `test_crawl.db`（测试） | competitions=**33**，awards=**88**，candidates=**82**，ai_jobs=**2** | 已迁移新表/新列；**有攻略 9 条**（本轮真机生成 id=21、12）；两条 ai_jobs 均 `done`；SHA-256 `b001887393e2103c2ad22e584de21970528de99063d33e7fc7754aaa6721b0b9` |
| `competition_candidates` | 生产 **82** / 测试 **82** | 两边均零重复（`norm_name`/`source_key` 双查）；生产 `approved 1 / pending 80 / rejected 1` |

生产库覆盖率：**有图 6/33｜有比赛月份 8/33｜报名月份完整 31/33｜攻略 7/33**。

当前**有比赛月份**的 8 条（生产库，格式 `id: 起始-结束`）：

```
1: 3-7   （中国国际大学生创新大赛，来源：长沙理工校赛通知）
2: 9-     （挑战杯，来源：华中师大院赛通知“暂定9月中旬”）
9: 6-11  （学创杯，来源：官网/通知页，模型置信度仅 0.45，待复核）
10: 7-8  （中国大学生计算机设计大赛，来源：官网首页推断，置信度较低）
12: 4-4  （蓝桥杯，来源：北科大通知“4月11/12/25日省赛”）
23: 7-8  （计算机设计大赛省级赛，来源：通知页）
34: 8-8  （中国机器人大赛，来源：重庆交大新闻页）
35: 11-11（中国教育机器人大赛，来源：搜狐新闻页）
```

生产库 2026-11-08 12:05 写入（备份 `competitions.db.bak-20261108-120553`，32 行、integrity ok）：

- 封面 5 条：id=6（1920×800）、8（670×346）、18（1680×715）、22（995×416）、27（995×416，与 22 同图）；
- 比赛时间 2 条：id=9（6-11）、id=23（7-8）；
- 报名：id=34 仅补 `reg_start_month=3`；`reg_end_month=7` 原值保留 → **3-7 为混合来源，待人工复核**；
- 16 条赛氪报名冲突**全部保留库内值**；写入后 diff 校验：仅上述 10 个字段变化，awards 仍 87 条。

测试库 2026-11-08 的赛氪试跑写入（**仅测试库**，备份 `test_crawl.db.bak-20261108-000326`）：

- 报名月份：id=3（4-6）、6（4-9）、8（4-6）、10（4-4）、12（10-4，cross=1）、22（4-6）；
- 封面：id=6（1920×800）、8（670×346）、22（995×416）；id=10/12/3 因 banner/无图未写；
- `contest_*` 全部未动。

备份链（写库前自动生成，**永不覆盖**；按时间顺序）：

| 备份 | 对应节点 |
|---|---|
| `competitions.db.bak-20261106-115044` | 最原始快照（未迁移列，`d007a8f7…422fcb`） |
| `competitions.db.bak-20261107-232917` | 合并 id=34/35 **之前**（30 条赛事 / 84 获奖） |
| `competitions.db.bak-20261107-233310` | 第一轮首页抓取试水**之前**（该轮坏数据已整体回滚） |
| `competitions.db.bak-20261107-233523` | 第二轮（带闸门）试水**之前** |
| `competitions.db.bak-20261107-233909` | 清空 id=35 死链封面**之前** |
| `competitions.db.bak-20261107-234010` | 5 条通知页批量补全**之前** |
| `competitions.db.bak-20261107-234143` | 回滚 id=13 误读月份**之前** |
| `competitions.db.bak-20261108-120553` | **本轮写赛氪补全之前**（32 赛事 / 87 获奖，SHA `45ccfea1…`） |
| `competitions.db.bak-20261108-123438` | **候选表建表 + 84 项目录导入之前**（32 赛事 / 87 获奖，SHA `cdab55f5…`） |
| `competitions.db.bak-20261108-123446` | **赛氪 20 条候选导入之前**（32 赛事 / 87 获奖 / 62 候选） |
| `competitions.db.bak` | **AI 迁移之前**（33 赛事 / 87 获奖，SHA `db77ac…`；`migrate_production.py --apply` 生成） |
| `competitions.db.bak-20261108-130926` | **清理孤儿 insights/ideas 之前**（insights 13 / ideas 16） |

注意：
- 生产库**已经**具备 `image_url / contest_start_month / contest_end_month` 列（2026-11-07 23:23 你启动后端时 `startup_init_db()` 已自动补列并回填 `summary`）。
- 33 条里仍有 **26 条无攻略**、**26 条无封面**、**24 条无比赛月份**——这是待办，不是 bug；无图走前端兜底，无攻略可点按钮生成。
- **2026-11-08 状态**：赛氪补全已写入生产库，写库策略为「image_url 只补空、reg_* 逐字段只补 NULL、contest_* 只填空」，详见第四节 13-15 条。
- **AI 迁移（13:09）后**：新增 `ai_jobs` 表 + 4 个 `guide_*` 列；孤儿数据清零；生产 SHA `3af3af9c…`。
- 恢复某个节点：先删 `competitions.db-wal` / `-shm`，再 `Copy-Item <备份> .\competitions.db -Force`。

---

## 三、运行命令（backend 目录）

### 0. 从生产库复制测试库

```powershell
cd D:\agent-lab\KuAo-Platform\backend

# 没有后端服务在写库时可直接复制
Copy-Item .\competitions.db .\test_crawl.db -Force
```

若后端正在运行（WAL 可能未 checkpoint），建议用 SQLite backup API 生成测试库，避免拿到不一致快照。

### 1. 新闻单页：dry-run（不写库）

```powershell
.\venv\Scripts\python.exe crawler_news.py `
  --db-path .\test_crawl.db `
  --dry-run `
  --show-raw `
  --url "https://cise.hunnu.edu.cn/info/1165/7066.htm"
```

### 2. 新闻单页：真实写测试库

```powershell
.\venv\Scripts\python.exe crawler_news.py `
  --db-path .\test_crawl.db `
  --url "https://cise.hunnu.edu.cn/info/1165/7066.htm"
```

### 3. 新闻列表：批量抓取 + 自动翻页

```powershell
.\venv\Scripts\python.exe crawler_news.py `
  --list-url "https://cise.hunnu.edu.cn/news_more_list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1165&a1070851t=43&a1070851c=10&a1070851p=12" `
  --db-path .\test_crawl.db `
  --max-pages 4 `
  --max-items 10 `
  --report .\workspace\batch_report.json
```

显式翻页模板：

```powershell
.\venv\Scripts\python.exe crawler_news.py `
  --list-url "https://x.edu/list" `
  --page-url "https://x.edu/list?p={page}" `
  --pages 1-3 `
  --db-path .\test_crawl.db `
  --max-items 8
```

### 4. 赛事采集：插入新赛事

```powershell
.\venv\Scripts\python.exe crawler_competitions.py `
  --url "https://www.sohu.com/a/1091484812_122595727" `
  --db-path .\test_crawl.db `
  --save-json .\workspace\competition_insert_edu_robot.json
```

### 5. 赛事采集：更新指定占位赛事

```powershell
.\venv\Scripts\python.exe crawler_competitions.py `
  --url "http://smve.cqjtu.edu.cn/info/1037/6028.htm" `
  --db-path .\test_crawl.db `
  --competition-id 34 `
  --save-json .\workspace\competition_update_robot.json
```

### 6. 赛事采集：只抽取不写库

```powershell
.\venv\Scripts\python.exe crawler_competitions.py `
  --url "https://www.sohu.com/a/1091484812_122595727" `
  --db-path .\test_crawl.db `
  --dry-run `
  --show-raw
```

### 7. awards 读回校验

```powershell
.\venv\Scripts\python.exe verify_crawl.py `
  --db-path .\test_crawl.db `
  --award-id 86 87 88 89
```

### 8. 语法/编译检查（不连网、不写库）

```powershell
.\venv\Scripts\python.exe -m py_compile crawler_news.py crawler_competitions.py db.py models\schemas.py routers\competitions.py verify_crawl.py
.\venv\Scripts\python.exe -m pytest test_crawler_units.py -q     # 19 passed
.\venv\Scripts\python.exe -m pyflakes crawler_news.py crawler_competitions.py db.py
```

### 9. 批量补全：演练（不写库，但会花大模型额度）

```powershell
# 单条看效果
.\venv\Scripts\python.exe batch_update_competitions.py --ids 10 --allow-production

# 5 条通知页（用 url-map 覆盖入口）
.\venv\Scripts\python.exe batch_update_competitions.py `
  --ids 1,2,10,12,13 --url-map .\url-map.json --allow-production --show-raw
```

### 10. 批量补全：真实写库（自动备份）

```powershell
# 验证 Prompt 修复后重跑这 5 条
.\venv\Scripts\python.exe batch_update_competitions.py `
  --ids 1,2,10,12,13 --url-map .\url-map.json --apply --allow-production

# 扫尾：只跑"比赛月份为空"的赛事
.\venv\Scripts\python.exe batch_update_competitions.py `
  --filter months --limit 22 --apply --allow-production

# 只跑封面为空的（注意：通知页几乎无图，多数会因闸门不写）
.\venv\Scripts\python.exe batch_update_competitions.py --filter image --apply --allow-production
```

> 每次 `--apply` 都会先自动备份 `competitions.db.bak-<时间戳>`，并输出汇总 + 报告路径 `workspace/batch_competitions_*.json`。
> 想跑单条但库里已有值（比如想覆盖 id=13 的月份），用 `--ids` 可以绕开 filter，但**仍然只填空**——想改已有值必须手工 SQL 或加 `--update-name` 之类的开关。

### 11. 测试库 -> 生产库合并

```powershell
# 演练
.\venv\Scripts\python.exe merge_test_competitions.py

# 真正合并（自动备份）
.\venv\Scripts\python.exe merge_test_competitions.py --apply

# 连占位赛事一起合并（默认跳过 category='待确认'）
.\venv\Scripts\python.exe merge_test_competitions.py --apply --include-placeholders
```

### 12. 生产库体检 / 迁移（一般用不到，后端启动会自动 init_db）

```powershell
.\venv\Scripts\python.exe migrate_production.py            # 只读体检
.\venv\Scripts\python.exe migrate_production.py --apply    # 备份 + 补列
```

### 13. 赛氪映射 + 统一清洗（2026-11-08：默认只读，`--apply` 写库）

```powershell
# 全量 32 条预检（约 10 分钟；会花大模型额度；不写库）
.\venv\Scripts\python.exe pipeline_update.py `
  --db-path .\test_crawl.db `
  --saikr-delay 3,5 --notice-delay 5,10

# 只跑指定赛事
.\venv\Scripts\python.exe pipeline_update.py --ids 3,6,8 --db-path .\test_crawl.db

# 只跑赛氪、不抓通知页（快速看封面/报名）
.\venv\Scripts\python.exe pipeline_update.py --skip-notice --db-path .\test_crawl.db

# 复用已保存的预检报告（不联网、不调模型）先看写入计划
.\venv\Scripts\python.exe pipeline_update.py `
  --from-report .\workspace\pipeline_report_prod.json --db-path .\competitions.db

# 真正写生产库（自动备份 + 生产库拦截 + 只填 NULL + 封面复检）
.\venv\Scripts\python.exe pipeline_update.py `
  --from-report .\workspace\pipeline_report_prod.json `
  --db-path .\competitions.db --apply --allow-production
```

- 预检报告写到 `workspace/pipeline_report.json`；生产基线对比报告见 `workspace/pipeline_report_prod.json` 与 `workspace/pipeline_report.md`。
- `--apply` 写库前会打印「将写 / 将跳过」清单并自动备份；写后输出覆盖率、SHA-256 与 `workspace/pipeline_apply_<时间戳>.json`。
- 不加 `--from-report` 时 `--apply` 会重新联网+调模型；**补数量验证优先复用报告，避免重复烧额度**。

### 14. 赛氪 -> 测试库小批量试跑（2026-11-08 已执行 6 条）

```powershell
# 只写 image_url / reg_start_month / reg_end_month / reg_cross_year；自动备份 test_crawl.db
.\venv\Scripts\python.exe .\workspace\saikr_apply.py `
  --ids 3,6,8,10,12,22 --db-path .\test_crawl.db --apply
```

> 该脚本在 `workspace/` 下，属一次性工具；策略与红线一致：往年届次跳过、封面过闸门、`contest_*` 不动。生产库不要直接用它。

### 15. 新赛事发现：候选表 POC（2026-11-08，默认 dry-run）

```powershell
# ① 84 项目录（默认山东农业工程学院；不写库，只出预览）
.\venv\Scripts\python.exe discover_university.py --db-path .\test_crawl.db

# 换华南师大页面（同一套解析）
.\venv\Scripts\python.exe discover_university.py `
  --url "http://aol.scnu.edu.cn/a/20241021/899.html" `
  --preview scnu_preview.json --db-path .\test_crawl.db

# 真正写候选表（自动建 competition_candidates）
.\venv\Scripts\python.exe discover_university.py --db-path .\test_crawl.db --apply

# ② 赛氪前 2 页 / 20 条（不写库）
.\venv\Scripts\python.exe discover_saikr.py --db-path .\test_crawl.db

# 真正写候选表；当天第二次会被守卫拦截，需 --force 才重跑
.\venv\Scripts\python.exe discover_saikr.py --db-path .\test_crawl.db --apply
.\venv\Scripts\python.exe discover_saikr.py --db-path .\test_crawl.db --apply --force
```

- 预览：`workspace/roster_preview.json`、`workspace/scnu_preview.json`、`workspace/saikr_preview.json`。
- 每日守卫状态：`workspace/saikr_daily_state.json`（仅 `--apply` 成功后写）。
- 候选表统计（只读）：

```powershell
.\venv\Scripts\python.exe -c "import db; print(db.candidate_stats('test_crawl.db'))"
```

> 2026-11-08 实测：84 项页解析 84 条 / 过滤 2 条 / 5 条缺官网 → 82 条候选，其中 62 条新增、20 条命中主表；赛氪 20 条全部新增。两个脚本重复跑均为 0 新增。

### 16. 审核 CLI：候选 -> 主表（2026-11-08）

```powershell
# 列出待审（ID / 来源 / 级别 / 名称）
.\venv\Scripts\python.exe review_candidates.py --list --db-path .\test_crawl.db
.\venv\Scripts\python.exe review_candidates.py --list --db-path .\competitions.db --limit 50

# 通过：先查主表重名 -> 封面重新过 check_image -> 同事务写主表 + 标记 approved
.\venv\Scripts\python.exe review_candidates.py --approve 1 --db-path .\test_crawl.db

# 拒绝：只改候选状态，不动主表
.\venv\Scripts\python.exe review_candidates.py --reject 2 --db-path .\test_crawl.db

# 生产库（写前自动备份 + integrity_check；必须 --allow-production）
.\venv\Scripts\python.exe review_candidates.py `
  --approve 1 --db-path .\competitions.db --allow-production
```

- `--approve` 行为：候选必须 `pending`；主表已有同名/高相似 -> 拒绝；封面不合格 -> 仍入库但 `image_url=NULL`；新赛事固定 `sort_order=9999`，首页不曝光。
- `--reject` 行为：候选必须 `pending`；只写 `status='rejected'`、`reviewed_at`，不碰主表。
- 已在 `workspace/review_test.db` 副本实测：approve -> competitions id=36、候选标记 approved；reject -> 候选标记 rejected；重复 approve 被拒。
- **注意（2026-11-08 13:00 核对）**：生产库实际已有 `id=36`（候选 82 approve）+ 1 条 reject，候选状态为 `80 pending / 1 approved / 1 rejected`；交接文档此前记录「主表未动」已过期。

### 17. AI 攻略 / 灵感（2026-11-08 新增）

```powershell
# ① 每日慢刷演练（不写库、不调模型，只看候选与预估 token）
.\venv\Scripts\python.exe daily_ai_worker.py --db-path .\competitions.db

# ② 测试库真跑（2 篇攻略 + 1 条灵感；会消耗模型额度，约 3~30K tokens）
.\venv\Scripts\python.exe daily_ai_worker.py `
  --db-path .\test_crawl.db --limit 2 --idea-limit 1 --apply

# ③ 生产库（cron 用；自动备份 + 生产库闸门）
.\venv\Scripts\python.exe daily_ai_worker.py `
  --db-path .\competitions.db --apply --allow-production `
  --limit 4 --idea-limit 2 --budget-tokens 300000

# ④ 人工单条补跑 / 重刷（兼容旧 CLI，内部已是 v2 Prompt）
.\venv\Scripts\python.exe generate_guides.py --only-missing --db-path .\test_crawl.db
.\venv\Scripts\python.exe generate_guides.py --id 21 --db-path .\test_crawl.db --dry-run
.\venv\Scripts\python.exe generate_guides.py --id 21 --db-path .\competitions.db --allow-production
```

- 按需接口（必须登录，`Authorization: Bearer <token>`）：
  - `POST /api/competitions/{id}/generate_guide` → 202 `{status: queued, job_id, poll_after_ms}`；已有攻略 200；失败冷却 409；预算/队列 429。
  - `GET  /api/competitions/{id}/generate_guide` → `{status, job_id, attempts, guide_length, generated_at}`。
- 详情接口返回 `full_process_guide` + `guide_status`；**列表接口不再返回长文**。
- 数据表：`ai_jobs`（kind/status/attempts/tokens/error）+ `competitions.guide_generated_at / guide_model / guide_prompt_ver / guide_input_hash`。
- 报告：`workspace/ai_worker_<时间戳>.json`；失败任务状态与错误在 `ai_jobs.error`（不回传前端）。

### 18. 孤儿 AI 数据清理（一次性，2026-11-08 已执行）

```powershell
# 只统计（默认 dry-run）
.\venv\Scripts\python.exe cleanup_orphan_ai.py --db-path .\competitions.db

# 真正删除（自动备份；生产库必须 --allow-production）
.\venv\Scripts\python.exe cleanup_orphan_ai.py `
  --db-path .\competitions.db --apply --allow-production
```

- 2026-11-08 13:09 实测：生产库 `insights` 删 10 条 / `ideas` 删 12 条，剩余孤儿 0；主表 33/87 不变，备份 `competitions.db.bak-20261108-130926`。
- 只删 `competition_id IS NULL` 或指向不存在赛事的行；有归属的一律保留（见 `test_ai_guide.py`）。

---

## 四、开发红线

> **状态变更（2026-11-07 23:27）**：用户已明确授权本阶段可以写生产库 `competitions.db`。
> 但「可以写」不等于「随便写」——下面 1~4 条是**写库前置条件**，一条都不能省。
>
> **2026-11-08 补充**：赛氪补全已写入生产库（12:05）；生产候选表已部署（12:34，SHA `80092384…`）；写库策略（报名只补 NULL）见 13-15 条。

1. **测试仍优先 `--db-path .\test_crawl.db`**；只有批量补全/合并这类明确针对生产库的任务才写 `competitions.db`。
2. **写生产库必须同时满足**：脚本支持 `--apply`（默认 dry-run）+ 显式 `--allow-production` + **写前自动备份成功**。三者缺一，脚本必须拒绝执行。
3. **批量模式在 `db.init_db()` 之前就拦截生产库**；`migrate_production.py` 的只读体检模式不得改成写连接，不要绕过。
4. **不要为了"看效果"随手启动后端**：`main.py` 启动会执行 `db.init_db()`（幂等补列 + 回填 summary/tech_stack）。生产库已迁移过，现在启动是安全的，但它会改库文件字节（SHA 变），排查问题时容易误判。
5. **禁止对生产库手工 `DROP` / `DELETE` / 重建表**。需要改数据时先备份，再用 `UPDATE` 精确改字段。
6. **每次写库前先看目标路径**：脚本都会打印 `[目标库] ...`，先确认这一行再往下看。
7. `verify_crawl.py` 会拒绝查询生产库；要校验 awards 请指向测试副本。
8. **图片只保存 URL，严禁下载图片到本地**（批量脚本会为校验封面而"读取"图片字节做尺寸判断，但不落盘、只读前 3MB）。
9. **抓取遵守目标站点 robots.txt 与转载规定**；批量补全已有 **5~10 秒**随机延迟，**严禁调小**（早期新闻批量是 2~5 秒，赛事补全更慢）。
10. **封面宁缺毋滥**：`check_image()` 判定不合格就不要写，绝不允许为了"有图"而放宽宽高比阈值塞 banner。
11. **比赛时间宁缺毋滥**：Prompt 已规定「报名/材料提交截止不算比赛时间」，若发现模型仍误读，**回滚该字段**并记录，不要将错就错。
12. 生产库当前 SHA-256 见第二节「当前快照」；每次写库后更新它。历史快照 `d007a8f779c16c7a06fb5847fc1bebec9c690847bcea762e53799da066422fcb` 仅用于识别最老版本备份。
13. **赛氪写入策略（2026-11-08 定，重要）**：写生产库时 `reg_start_month / reg_end_month` **只填 NULL，禁止覆盖已有值**；`image_url` 只补空且必须过 `check_image()`（宽高比 ≤2.6）；`contest_*` 仍只填空。16 条报名冲突见第六节第 15 条。
14. **赛氪抓取只做低频小批量**：`robots.txt` 明确 `Disallow: /api/`、`Disallow: /*?*`、`Crawl-delay: 10`；只对存量 32 条做测试级抓取，**不扩全量、不商用**；请求间隔不低于 3 秒。新赛事发现另立方案评审，不在本轮范围。
15. **生产库已于 2026-11-08 12:05 写入赛氪补全**（备份 `competitions.db.bak-20261108-120553`，SHA `cdab55f5…`）。后续任何写库仍必须：自动备份 + `--allow-production` + 先打印「将写 / 将跳过」清单；`pipeline_update.py --apply` 已内置这三件套，**不要绕过脚本手写 UPDATE**。
16. **候选表红线（2026-11-08 新增）**：发现脚本只写 `competition_candidates`（`status=pending`），**禁止自动 INSERT/UPDATE `competitions`**；审核入库前人工核对名称/官网/类别，赛氪封面必须重新过 `check_image()`。赛氪每日上限 **2 页 / 20 条**，请求间隔 **3~5 秒**（robots `Crawl-delay: 10`，扩量/商用前必须重新做合规评估）。
17. **生产库候选表已部署（2026-11-08 12:34，13:00 复核）**：`competitions.db` 候选 **82 条（pending 80 / approved 1 / rejected 1）**；主表 **33 赛事 / 87 获奖**（id=36 由候选 82 approve 入表，`category='待确认'`、无官网，需人工补）；SHA 以第二节快照为准（`3af3af9c…`）。后续提拔/拒绝**必须走 `review_candidates.py`**，禁止手写 INSERT/UPDATE；approve 会自动备份、查主表重名、复检封面。
18. **AI 生成只能走统一链路（2026-11-08 新增）**：攻略生成必须经 `ai_content` + `ai_jobs` 抢占；**禁止绕过 `ai_jobs` 直接写 `full_process_guide`**（否则并发会重复烧钱、失败无冷却、预算失控）。按需接口必须保留 `Depends(get_current_user)`，不允许改回匿名。
19. **AI 写入不做“覆盖式重刷”**：`claim_ai_job` 在拿写锁后会再查一次攻略是否为空，非空直接返回 `exists`；`--force` 只允许人工单条使用，**cron 永不带 `--force`**。
20. **攻略长文只在详情接口返回**：不要把 `full_process_guide` 加回 `Competition`（列表）模型；前端列表不依赖它。
21. **孤儿数据清理只删无归属行**：`cleanup_orphan_ai.py` 固定条件 `competition_id IS NULL OR NOT IN (SELECT id FROM competitions)`；不要扩大成“按日期删”或手工 DELETE。

---

## 五、省 Token 军规（后续 AI 必须遵守）

1. **严禁全量 print 长文本 HTML**。  
   需要看页面时，只打印 `<title>`、目标容器文本前 500~1500 字符、meta 的 key/content；HTML 原文落盘后按行读。

2. **严禁全量重写大文件**。  
   `db.py`、`crawler_news.py`、`crawler_competitions.py` 都很大；改动必须先 `read` 指定 offset/limit，再用 `edit` 做局部替换，不要整体覆盖。

3. **看代码只看指定行数**。  
   先用 `grep` 定位函数/关键字，再 `read` 只读该函数前后 40~120 行；不要从头读到尾。

4. **所有 JSON 输出必须截断**。  
   大模型原始 JSON 只打印关键字段，或 `--save-json` 落盘后读摘要；批量报告只看 `inserted/failed/skipped` 计数和前 1~2 条示例。

5. **禁止 `SELECT *` 全量打印 `competitions`**。  
   `full_process_guide` 字段非常长，打印整行会爆上下文；查询必须显式列字段，例如：
   `SELECT id,name,category,level,official_url,image_url,contest_start_month,contest_end_month FROM competitions WHERE id=?`。

6. **不要为验证去重复调用大模型**。  
   优先读取 `workspace/*.json` 中已保存的 `raw_llm_json`；只有代码/数据源变化时才重新调用。

7. **命令输出先过滤再展示**。  
   PowerShell 用 `Select-String`、`Select-Object -First N`、`Tee-Object` 落盘；不要把完整日志贴回对话。

8. **改完先 `py_compile`**，再跑最小化测试；不要一上来跑全量批量。

9. **一次只验证一个假设**：先 dry-run，再写测试库，再读回；不要同一步混入多个远端页面/多个写库动作。

10. **上下文接近上限时**，先更新本文件，再开新窗口；新窗口先读本文件，不要重读全部历史。

---

## 六、当前已知问题 / 前端接入提示

1. 前端卡片封面读取的是 API 的 `image` 字段，后端已映射 `image_url`，封面可直接显示。
2. 【已完成 2026-11-07】`HomeView.vue` / `CompetitionsView.vue` / `PreparationView.vue` 已接入 `image` 与 `contest_start_month / contest_end_month`：
   - 新增 `formatContest(item)`：同月 -> `11 月`；跨月 -> `10 月 - 11 月`；跨年 -> `12 月 - 次年 1 月`；缺值 -> `待定`；
   - 新增 `resolveImage(item)`：只接受 `http(s)` 绝对地址，其余回落卡片占位图；
   - 后端 `db.list_preparations()` 的 JOIN 同步补出 `image_url / contest_start_month / contest_end_month`（老库缺列时以 NULL 占位，不会清空备赛清单）。
3. 【已完成 2026-11-07】`CompetitionDetailView.vue` 的 `signUpTime / contestTime` 改为由 `reg_*_month`、`contest_*_month` 前端拼接（老的 `data.contest_time / sign_up_time` 仅作兼容兜底，后端并不返回这两个字段）；`image` 统一走 `resolveImage()`。
4. 【已完成 2026-11-07】`PreparationWorkspaceView.applyDetail()` 的「比赛时间」同样改为月份拼接（保留旧字段兜底），封面走 `resolveImage()`；级别显示本来就由 `zhLevel()` 处理，无需改。
5. 【已删除 2026-11-07】`frontend/src/services/competitionMapper.js` 整份文件无任何引用（`mapToCard` 只是其中一部分），已整体删除；其 `toMonth / formatSignUp` 与各视图内代码重复，历史版本可用 `git checkout -- frontend/src/services/competitionMapper.js` 取回。
6. 遗留的重复代码（未处理）：`toMonth / formatSignUp / formatContest / resolveImage` 目前在 5 个视图里各有一份，建议生产迁移完成后再抽成 `services/competitionFormat.js`，冻结期不做。
7. **封面策略 = C（2026-11-08 前端落地）**：接受大部分赛事没有封面，无图/破图交前端优雅兜底，不再为封面死磕通知页。
   - **已完成**：`frontend/src/utils/coverFallback.js` + `assets/main.css` 的 `.cover-fallback`，无图或 `@error` 时显示「名称哈希色相渐变 + 赛事首字」，深色模式同步；详情页 `h-64` 大图位同样兜底。
   - 验证：`npm.cmd run build` 通过；截图 `workspace/cover-light.png` / `cover-dark.png` / `cover-detail.png`。
   - 仍不建议放宽 `check_image()` 阈值去接受 banner（4:3 裁切后观感更差）。
8. **采集入口的经验结论（血泪）**：
   - ✅ **新闻/宣传详情页**（高校新闻、搜狐等）：有 og:image 或正文配图，封面能过闸门，时间也常写在正文里 —— 34/35 就是这么采到的。
   - ✅ **高校教务处通知页**（`/info/xxxx/xxxxx.htm` 这类 VSB CMS 页）：正文够长、时间准确（蓝桥杯 4 月、挑战杯 9 月都验证过），但**基本没有配图**。
   - ❌ **赛事官网首页**：多是门户/SPA，正文只有几百到一千字导航文案，封面是 banner（855×200 那种），时间靠模型硬猜 —— 已弃用。
   - ❌ **PDF 型通知**（"如果您无法在线浏览此 PDF"）：正文只有 236 字，直接跳过。
   - ❌ **证书域名不匹配的站点**（如 `jwc.hncu.net`）：HTTPS 校验失败且 http 也强制跳转，抓不了。
9. **模型误读模式已记录**：通知页里"作品提交截止 6月30日"曾被模型当成比赛时间（id=13，已回滚）。Prompt 已加红线；重跑时请重点看这类页面。
10. 占位赛事更新默认保留原 `name`，只替换官网/时间/图片/类别/简介；如需改名用 `--update-name`。
11. 重庆交大页没有 `og:image`，id=34 的封面走正文首图回退，且是 `http://`（328×470 竖图，`object-cover` 裁切后可接受）；上云/HTTPS 后需处理混合内容。
12. 新插入赛事 `id=35` 的 `full_process_guide` 为空；攻略生成是后续独立任务。
13. crawled awards 的 `review_status='pending'`、`embedding_state='stale'`；语义检索只索引 `approved + ok`，需要审核/同步。
14. 前端 `HomeView` 首页只取前 8 条（按"计算机类优先 -> 国家级优先 -> sort_order -> id"排序），**新增赛事（sort_order=9999）排在最后，首页看不见**，要去"竞赛列表"页或直接访问 `/competition/34`。
15. **赛氪预检结论（2026-11-08，生产基线重算）**：
    - 封面：**5 条可新增**（id=6/8/18/22/27），1 条已有（id=34），**14 条 banner/过小被拒**（方案 C），26 条仍无图。
    - 比赛时间：**2 条可新增**（id=9 学创杯 6-11 月、id=23 计算机设计省赛 7-8 月），8 条已有，24 条仍缺。
    - 赛氪状态：25 条成功、5 条往年届次（2/4/17/26/30）、2 条无对应（25/35）。
    - **16 条报名时间冲突**（赛氪窗口 vs 库内值），完整清单见 `workspace/pipeline_report.md` 第三节；**写库时一律保留库内值，只填 NULL**。
16. **赛氪接口与合规**：`apiv4buffer.saikr.com/api/pc/contest/{lists,info}` 匿名可调（无 Token/签名/强制 Cookie），但 `robots.txt` 明确 `Disallow: /api/`、`Disallow: /*?*`、`Crawl-delay: 10`；只做存量 32 条低频测试，不做全量抓取/商用。
17. **低置信/需复核**：id=9 比赛时间 6-11 月（置信度 0.45）；id=2 只给出「9 月起」；id=27 与 id=22 共用同一张封面（同赛事家族，会出现重复图）。
18. **赛氪页面两类质量**：赛氪自家商业赛图多、时间全；榜单赛事（我们要的）多为官方通知原文 + banner 封面 + 比赛时间为 0，和高校通知页同源问题，**所以 `crawler_competitions.py` 仍需保留**。
19. **2026-11-08 12:05 写库结果**：成功 10/10 字段，0 失败；`--from-report` 复用预检报告（未重复联网/调模型）。写后 diff 只有计划内字段变化，`integrity_check=ok`，awards 未动。
20. **id=34 报名月份为混合来源**：仅补了 `reg_start_month=3`（赛氪），`reg_end_month=7`（原值）保留，赛氪原窗口是 3-4 → 需人工确认到底取 3-7 还是 3-4；这是「只补 NULL」策略的已知边界。
21. **16 条报名冲突全部保留库内值**，清单在 `workspace/pipeline_report.md` 第三节；后续想采用赛氪值必须单条人工确认，不允许批量覆盖。
22. **前端 build 复验通过（2026-11-08 12:04）**：`npm.cmd run build` 8.37s / 115 modules，无编译错误；PowerShell 必须用 `npm.cmd`。
23. **候选表 POC 结果（2026-11-08 12:35）**：`test_crawl.db` 新增 **82** 条 pending（edu-roster 62 / saikr 20），`norm_name` 与 `source_key` 均零重复；主表仍 33 赛事 / 88 获奖，**未写主表**。12:34 生产库同批部署 **82** 条 pending，主表仍 32/87。
24. **去重口径已实测**：归一化名相同 -> 跳过；相似度 ≥0.9 且官网同域（或任一方无官网）-> 跳过；地域标记（全国/省/赛区/选拔赛…）不同 -> 不自动合并。84 项目录有 20 条命中现有主表（挑战杯/ACM/服外包等）被正确跳过；重复跑脚本 0 新增。
25. **赛氪列表特征（2026-11-08 实测）**：`sort=2` 前 2 页多为英语/翻译/才艺类商业赛，名称常带 `【最后14天】` 等营销前缀（归一化时已剥离）；20 条中 19 条有 `contest_*` 月份、3 条跨年；`image_url` 存的是 `thumb_pic` 原图 URL，**审核入库前必须重跑 `check_image()`**。
26. **审核 CLI 实测（2026-11-08，临时副本）**：approve 候选 1 -> `competitions id=36`、候选 `approved` 且回填 `competition_id=36`；reject 候选 2 -> `rejected`；再次 approve 同一候选被拒（状态守卫）。临时库 `workspace/review_test.db` 已删除，测试库未污染。
27. **生产候选内容需要人工过一遍**：赛氪前 2 页会混入「辅助报名」「考试入口」「作品征集」等服务/广告类条目（例如 `2027年美赛（MCM/ICM）辅助报名`），审核时直接 `--reject`，不要因为“能抓到”就提拔。
28. **生产库已不是 32 条（2026-11-08 13:00 核对）**：`id=36` 已由赛氪候选 82 approve 入主表（`category='待确认'`、无官网、`sort_order=9999`，首页不曝光）；候选状态 `80 pending / 1 approved / 1 rejected`。旧的「主表未动」记录已过期。
29. **攻略生成是“安全但通用”的**：v2 Prompt 已注入报名/比赛月份与获奖作品；实测两篇（id=21、12）分别 2104 / 2543 字，会明确写“未公布”，不编造日期。但**模型仍可能把“报名截止”误读成比赛时间**，抽检时优先看这类句子。
30. **前端按钮的登录态**：`CompetitionDetailView` 点击按钮前用 `isLoggedIn()` 判断，未登录直接 `openAuthModal('login')`；token 失效时接口 401，拦截器会清本地凭证并广播 `auth:unauthorized`。**不要改成匿名接口**。
31. **生成耗时可接受但需轮询**：单篇 15–60s（deepseek-flash 含 reasoning）；前端 3s×20 → 10s×18，总上限约 4 分钟；超时会提示刷新，不会重复触发。
32. **`ai_jobs` 的 15 分钟租约**：进程被 kill 后，下一次抢占会把 `running` 超过 15 分钟的任务标 failed，不会永久卡死；排查生成问题先看 `ai_jobs.error`。
33. **测试库已有真机生成的攻略**：`test_crawl.db` 的 id=21、12 是本轮真机产物（2 条 ai_jobs done）；不要再把它们当“空攻略”用例，需要空攻略请另建临时库。
34. **列表响应瘦身是破坏性变更**：`GET /api/competitions` 不再返回 `full_process_guide`；前端列表本来就不读该字段（已 grep 确认），若后续有调用方依赖，必须改用详情接口。
35. **已知小 bug（上云前未修，待评估）**：`frontend/src/services/api.js` 的 `submitFeedback()` 调的是 `api.post('/api/feedback')`，而 axios 实例 `baseURL` 已是 `/api`，实际请求会变成 `/api/api/feedback`。正确写法应为 `api.post('/feedback')`。不影响爬虫/AI/部署主链路，反馈功能抽检时若 404 可先按此修。

---

## 七、下一步 TODO

### 本轮已完成（2026-11-08）

- [x] **Prompt 修复验证**：id=13（作品提交截止页）单页 `--dry-run`，`contest_start_month/contest_end_month` 均保持 `NULL`。
- [x] **赛氪映射 + 预检**：`saikr_map.json` 32 条；`pipeline_update.py` 全量 dry-run（测试库），并用生产库只读基线重算；报告 `workspace/pipeline_report.md`。
- [x] **前端封面兜底**：`coverFallback.js` + `.cover-fallback`，`npm.cmd run build` 通过；截图 `workspace/cover-*.png`。
- [x] **赛氪试跑**：6 条（3/6/8/10/12/22）写入 `test_crawl.db`，仅 image_url + reg_*，自动备份 `test_crawl.db.bak-20261108-000326`。
- [x] **生产库写入**：`pipeline_update.py --from-report … --apply --allow-production`，10/10 字段成功，自动备份 `competitions.db.bak-20261108-120553`；覆盖率 18.8% / 25.0%，SHA `cdab55f5…`。
- [x] **前端 build 复验**：`npm.cmd run build` 8.37s / 115 modules，无编译错误。
- [x] **新赛事发现调研**：赛氪列表 API 字段核对 + 2 个高校 84 项目录页实测（结论见第九节）。
- [x] **候选表落地**：`db.py` 建表 + 插入/去重/统计函数；`discover_common.py` 公共层；`test_crawl.db` 建表成功。
- [x] **84 项目录 POC**：`discover_university.py` 解析 84 条 / 过滤 2 条 / 5 条缺官网，写入 62 条，20 条命中主表跳过；华南师大页零改动复用。
- [x] **赛氪每日 POC**：`discover_saikr.py` 前 2 页 / 20 条，3~5 秒延迟，写入 20 条；每日守卫生效；重复跑 0 新增。
- [x] **审核 CLI**：`review_candidates.py --list/--approve/--reject`；临时副本实测通过（approve -> 主表 id=36 + 候选 approved；reject 正常；重复 approve 被拒）。
- [x] **生产库候选部署**：候选表建表 + 84 项目录 62 条 + 赛氪 20 条，共 **82 条 pending**；主表仍 32/87；备份 `…123438` / `…123446`；SHA `80092384…`。
- [x] **AI 生成链路（2026-11-08 13:15）**：`ai_content.py`（v2 Prompt + 指纹）、`ai_jobs_runner.py`（线程池 + 预算 + 冷却）、`daily_ai_worker.py`（慢刷）、`cleanup_orphan_ai.py`（孤儿清理）。
- [x] **按需攻略接口**：`POST/GET /api/competitions/{id}/generate_guide`（登录、202/409/429、幂等）；前端详情页按钮 + 轮询；列表接口瘦身。
- [x] **生产库迁移**：`migrate_production.py --apply` 建 `ai_jobs` + 4 个 `guide_*` 列；备份 `competitions.db.bak`（SHA `db77ac…`），迁移后 SHA `ced3268…`。
- [x] **孤儿数据清理**：删 insights 10 / ideas 12，剩余 0；备份 `…130926`。
- [x] **测试**：`test_ai_guide.py` 16 例 + 既有 91 例（共 107 passed / 2 skipped）；测试库真机生成 2 篇攻略（3378 + 2945 tokens）。
- [x] **前端 build**：`npm.cmd run build` 7.36s / 115 modules 通过。

### 下一阶段重点：上云部署 + Crontab（按顺序，新 Agent 直接照做）

#### ① 上云部署（先做，阻塞定时任务）

- [ ] **运行环境**：Linux + Python 3.11 venv，`pip install -r requirements.txt`；`.env` 只放服务器（DeepSeek/JWT/CORS），不进仓库。
- [ ] **数据迁移**：停服务后 `sqlite3 competitions.db ".backup '/tmp/competitions.db'"`（或复制时一并处理 `-wal/-shm`）→ 上传 → `PRAGMA integrity_check` + 行数校验（**33 赛事 / 87 获奖 / 82 候选 / 7 攻略 / ai_jobs 0**）→ 记录 SHA-256 `3af3af9c…`。
- [ ] **服务化**：FastAPI 用 `uvicorn/gunicorn + systemd` 常驻，Nginx 反代 + HTTPS；`frontend/dist` 静态托管；仅开放必要端口。**建议 `--workers 1`**（SQLite + 单机足够；多 worker 时 ai_jobs 仍安全，但线程池会按进程放大并发）。
- [ ] **权限与路径**：DB 目录/日志目录归服务用户可写；`competitions.db` 与 `-wal/-shm` 同目录；禁止放临时盘。
- [ ] **备份上云**：每日 `sqlite3 .backup` 到对象存储（保留 30 天）；写库脚本的本地 `.bak-*` 同步一份；每月恢复演练。
- [ ] **验收**：健康检查 / 首页 / 列表 / 详情；候选 `review_candidates.py --list` 只读；一次登录态下的攻略按钮端到端（生成 1 条 + 轮询到 ready）；测试库完成一次 approve + reject 演练。

#### ② 服务器定时任务（Crontab，上云后配置）

> 时区统一 `Asia/Shanghai`（**Debian/Ubuntu 的 cron 不支持 `CRON_TZ`，必须设系统时区**）；所有任务加 `flock -n`；日志按天切分；非 0 退出码触发告警。

```cron
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
MAILTO=""

# 0) 每天 02:30 生产库备份（本地 + 对象存储；保留 30 天）
30 2 * * * cd /opt/kuao/backend && flock -n /var/lock/kuao_backup.lock ./venv/bin/python backup_db.py --db competitions.db --keep-days 30 >> logs/backup-$(date +\%F).log 2>&1

# 1) 每周一/周四 03:00 高校新闻获奖批量（写 awards / 占位赛事）
0 3 * * 1,4 cd /opt/kuao/backend && flock -n /var/lock/kuao_news.lock ./venv/bin/python crawler_news.py --list-url "<LIST_URL>" --db-path competitions.db --max-pages 4 --allow-production >> logs/news-$(date +\%F).log 2>&1

# 2) 每天 04:10 新赛事发现（只写候选表；不加 --force，每日守卫防重复）
10 4 * * * cd /opt/kuao/backend && flock -n /var/lock/kuao_saikr.lock ./venv/bin/python discover_saikr.py --db-path competitions.db --apply --allow-production >> logs/discover-$(date +\%F).log 2>&1

# 3) 每天 04:40 AI 慢刷（攻略 4 篇 + 灵感 2 条；token 预算熔断）
40 4 * * * cd /opt/kuao/backend && flock -n /var/lock/kuao_ai.lock ./venv/bin/python daily_ai_worker.py --db-path competitions.db --apply --allow-production --limit 4 --idea-limit 2 --budget-tokens 300000 >> logs/ai-$(date +\%F).log 2>&1

# 4) 每周日 05:10 存量补全（会调模型，限 20 条）
10 5 * * 0 cd /opt/kuao/backend && flock -n /var/lock/kuao_pipeline.lock ./venv/bin/python pipeline_update.py --db-path competitions.db --apply --allow-production --limit 20 >> logs/pipeline-$(date +\%F).log 2>&1

# 5) 每天 05:30 日志清理（保留 7 天；只清 logs/，绝不碰 backups/）
30 5 * * * find /opt/kuao/backend/logs -type f -name '*.log*' -mtime +7 -delete >> /opt/kuao/backend/logs/cleanup.log 2>&1

# 6) 每周日 05:40 本地备份清理（对象存储成功后再删本地 30 天前备份）
40 5 * * 0 find /opt/kuao/backend/backups -type f -name 'competitions.db.bak-*' -mtime +30 -delete >> /opt/kuao/backend/logs/cleanup.log 2>&1
```

- [ ] **先手动逐条执行**，确认退出码 0、日志无异常，再挂 cron；`crontab -l` 留档。
- [ ] **顺序不能反**：备份（02:30）在所有写库之前 → 新闻（03:00）可能建占位赛事 → 发现（04:10）只写候选 → AI（04:40）吃当天结果 → 周度补全（05:10）与 AI 错开 → 清理（05:30）最后。
- [ ] `discover_university.py` **不挂日任务**：84 项名单年度更新时人工跑一次（`--apply --allow-production`）。
- [ ] 监控：失败 webhook/邮件；连续失败 2 次暂停任务并告警；每周核对候选增量与审核积压。
- [ ] 限速不变：赛氪 3~5 秒 / 2 页 20 条；`pipeline` 5~10 秒；新闻 2~5 秒。
- [ ] **日志与备份分目录**：现有 `.bak-*` 与源码混在 `backend/` 根目录，上云时统一挪到 `backups/`，否则 7 天日志清理的 `find` 容易误伤。

#### ③ 审核收尾（可与上云并行）

- [ ] **处理 16 条报名冲突**：默认保留库内值，按 `workspace/pipeline_report.md` 第三节逐条裁决。
- [ ] **复核低置信条目**：id=9（学创杯 6-11 月，置信度 0.45）；id=34（报名 3-7 混合来源）；id=27/id=22 封面重复。
- [ ] **生产候选 80 条待审**：按 `review_candidates.py --list` 逐条 approve/reject；服务/广告类直接拒绝；id=36 已 approve 但 `category='待确认'`，需人工补类别/官网。

### 后续（不急）

- [ ] **后台审核列表**：把 `review_candidates.py` 的 CLI 能力接到管理后台（列表/筛选/批量 approve/reject/审核备注）；当前 CLI 已覆盖人工操作。
- [ ] **占位赛事治理**：`test_crawl.db` 里 id=33「华为杯」仍是 `category='待确认'` 的占位记录（用户明确要求**保留在测试库当用例**，不要合并进生产）；生产侧后续需要人工审核/合并入口。
- [ ] **反爬与限速**：统一 UA、重试、随机延迟、robots 检查；目前赛事补全 5~10 秒、新闻批量 2~5 秒、赛氪 3~5 秒（robots 建议 10，扩量前复审）。
- [ ] **数据质量**：`official_url` 有效性校验（HEAD/域名白名单）、赛事名归一化与人工复核；补充 `build_page_urls`、dry-run 不写库、生产库拦截、competitions upsert 幂等 的单测。
- [ ] **图片 CDN/代理**：目前只存 URL；上云后评估是否接 CDN/代理，避免混合内容与源站防盗链。
- [ ] **清理 workspace 产物**：`workspace/*.json` 与 `competitions.db.bak-*` 可按日期归档，避免仓库/磁盘膨胀（当前已有 10 个备份文件、`saikr_catalog.json` 3.9 MB）。

---

## 八、快速定位

```text
新闻采集入口：      crawler_news.py  (main / _run / _run_batch)
赛事采集入口：      crawler_competitions.py (main / _run / save_competition / extract_image_url / SYSTEM_PROMPT)
赛事批量补全：      batch_update_competitions.py (main / process_one / build_patch / check_image / pick_checked_cover)
测试库->生产库合并： merge_test_competitions.py (main / build_plan / apply_plan / make_backup)
生产库体检/迁移：   migrate_production.py (inspect / make_backup / apply_migration)
入口页覆盖映射：    url-map.json
赛氪映射：          saikr_map.json（32 条；含 match_type / needs_review）
赛氪统一预检：      pipeline_update.py（默认只读；--apply / --from-report）
预检报告：          workspace/pipeline_report.md / pipeline_report_prod.json
写库记录：          workspace/pipeline_apply_20261108-120553.json
赛氪目录缓存：      workspace/saikr_catalog.json（5172 条）
新赛事发现（公共）： discover_common.py (prepare_target / backup_db / ingest / guess_category)
高校目录发现：      discover_university.py (main / extract_roster / extract_line_entries / build_candidates)
赛氪每日发现：      discover_saikr.py (main / fetch_list_page / build_candidate / check_daily_guard)
候选审核 CLI：      review_candidates.py (main / cmd_list / cmd_approve / cmd_reject / verify_cover)
候选表 DDL/CRUD：   db.py (CANDIDATES_DDL / ensure_candidates_table / add_candidate / find_duplicate_candidate / candidate_stats / promote_candidate)
AI 生成核心：        ai_content.py (build_guide_prompt / generate_guide / generate_guide_for / generate_inspiration / guide_input_hash)
AI 任务编排：        ai_jobs_runner.py (request_guide / run_guide_job / run_inspiration_job / _EXECUTOR)
AI 任务表/查询：     db.py (AI_JOBS_DDL / claim_ai_job / start|finish|fail|skip_ai_job / get_guide_state / select_guide_candidates / select_inspiration_candidates)
每日慢刷：          daily_ai_worker.py (build_parser / run / save_report)
兼容攻略 CLI：      generate_guides.py（调用 ai_content；--db-path / --allow-production）
孤儿清理：          cleanup_orphan_ai.py（count/delete_orphan_ai_content）
每日备份：          backup_db.py（make_backup / _prune / main；WAL 安全，单文件）
上云边界测试：      test_crawler_boundaries.py（20 例：网络异常 / 残缺 JSON / 空网页）
部署手册：          CLOUD_DEPLOYMENT_GUIDE.md（依赖 / DB 迁移 / systemd / Nginx / crontab）
AI 测试：            test_ai_guide.py（16 例；ai_jobs / 幂等 / 409 / 429 / 列表瘦身 / Prompt）
攻略接口：          routers/competitions.py (POST/GET /api/competitions/{id}/generate_guide)
攻略模型：          models/schemas.py (CompetitionDetail.full_process_guide / guide_status / guide_generated_at)
前端攻略按钮：      frontend/src/views/CompetitionDetailView.vue（guideBusy / onGenerateGuide / pollGuideStatus）
前端接口封装：      frontend/src/services/api.js（requestGuide / getGuideStatus）
数据库连接/迁移：   db.py (get_conn / init_db / _MIGRATION_COLUMNS / update_competition / list_preparations)
赛事 API 映射：     routers/competitions.py (_row_to_competition)
赛事 API 模型：     models/schemas.py (Competition)
读回校验：          verify_crawl.py
单测：              test_crawler_units.py (extract_image_url / _normalize_level，19 例)
测试库：            test_crawl.db（33 赛事 / 88 获奖 / 9 攻略 / 2 个已完成的 ai_jobs / 82 候选）
生产库：            competitions.db（33 赛事 / 87 获奖 / 7 攻略 / ai_jobs 0 / 82 候选，写前必须备份）
前端卡片：          frontend/src/components/CompetitionCard.vue（无图/破图 -> 渐变首字兜底）
前端兜底工具：      frontend/src/utils/coverFallback.js + assets/main.css (.cover-fallback)
前端详情页：        frontend/src/views/CompetitionDetailView.vue（h-64 大图位同样兜底）
前端时间映射：      frontend/src/views/{HomeView,CompetitionsView,PreparationView,CompetitionDetailView,PreparationWorkspaceView}.vue
```

---

## 九、新赛事批量发现方案（2026-11-08 调研 + POC 落地）

### 9.1 结论

| 路线 | 产出量 | 字段质量 | 合规/成本 | 建议 |
|---|---|---|---|---|
| ① 赛氪列表（每日 2 页 = 20 条） | 20/天，可持续 | 名称/级别/报名窗口/封面 URL 结构化；比赛时间大多为 0 | robots 禁 `/api/`、Crawl-delay 10；低频不商用风险可控但≠授权 | **做增量候选源，不直接入库** |
| ② 高校 84 项榜单目录（一次性） | 84 条种子 | 名称 + 官网 URL；无级别/报名时间 | 公开名单，风险低 | **先做，解决「库里只有 32 条」** |
| ③ 教务处通知/新闻列表（已有爬虫） | 每周几条 | 正文最准（时间/级别），图少 | 已合规运行 | **主源，继续用** |

**推荐组合**：② 一次性铺 84 条底表 → ③ 持续补详情 → ① 每日 20 条做增量发现；全部先进 `competition_candidates`，人工审核后入 `competitions`。

> **2026-11-08 用户已拍板**：以**高校目录页为主源**（84 项种子 + 教务处通知/新闻），**赛氪极低频（≤20 条/天）仅作补充**；所有发现先进候选表，人工审核后才入生产库。

### 9.2 赛氪路线：每日 20 条是否可控

接口（匿名、无 Token）：`GET https://apiv4buffer.saikr.com/api/pc/contest/lists?page=1&limit=10&sort=2`（`sort=2` = 最近更新）。
列表直接可用字段（已从 5172 条缓存核对）：

| 字段 | 说明 |
|---|---|
| `contest_id` / `contest_name` | 赛氪 ID + 赛事名 |
| `contest_url` | slug，详情页 `https://www.saikr.com/vse/<slug>`；调详情 API 时**不要带 `vse/`** |
| `level_name` | 国家级/省级…（原文） |
| `organiser` / `enter_range` | 主办方 / 参赛范围 |
| `regist_start_time` / `regist_end_time` | **Unix 秒**；0 表示无 |
| `contest_start_time` / `contest_end_time` | Unix 秒；多数为 0（拿不到比赛时间） |
| `time_status` / `time_name` | 报名中 / 未开始… |
| `thumb_pic` | 封面小图 CDN；详情 `web_pic_big` 才是大图 |
| `contest_class_first` / `contest_class_second(_id)` | 学科大类 / 子类 |
| `labels` / `rank` / `is_new` | 标签、热度、是否新上架 |

- 20 条/天 = 每天 **2 次列表请求**，间隔 ≥10 秒，符合 `Crawl-delay: 10`；不抓详情/正文时成本几乎为零。
- **风险判断**：robots 明确 `Disallow: /api/`、`Disallow: /*?*` → 技术上违反 robots，**不是「合规」而是「低风险」**；民事/刑事风险低（公开数据、不绕过反爬、不破坏系统），主要风险是 IP 封禁 + ToS 违约 + 商用转载版权。
- **可控前提**：不商用/不转售、署名并回链、只存 ID+名称+URL（正文仍回官方源）、1 次/10 秒、每日 ≤2 页、触发 429/验证码即 kill switch、优先发邮件或找商务拿授权。

### 9.3 高校目录路线：实测两个真实页面

| 页面 | 形态 | 实测结果 |
|---|---|---|
| [山东农业工程学院教务处《全国普通高校学科竞赛排行榜（2024）》](https://www.sdaeu.edu.cn/jwc/info/1047/2528.htm) | 纯 HTML 文本列表（`<p>` 分行，无 `<table>`） | requests+bs4 **81/84 自动配对名称+官网**；缺 3 条因名称不含「大赛/竞赛」（如「年会展示」「作品展」「演讲比赛」） |
| [华南师范大学美术学院《最新84项赛事…（附链接）》](http://aol.scnu.edu.cn/a/20241021/899.html) | 同上 | 同样 81/84，代码零改动可复用 |
| 反例：湖北二师 / 内江师范 / 河海大学 | 正文是**图片或 PDF**（`<table>` 数为 0） | requests+bs4 提不到结构化数据，需 PDF 解析/OCR |

关键代码（两页都适用，实测 3 秒内跑完）：

```python
import re, requests
from bs4 import BeautifulSoup

def extract_roster(url):
    r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0 ..."})
    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "lxml")
    for t in soup(["script", "style"]):
        t.decompose()
    lines = [x.strip() for x in soup.get_text("\n").split("\n") if x.strip()]
    # 从「…竞赛目录…网址」标题之后开始，避免把备注/观察目录算进来
    start = next(i for i, x in enumerate(lines)
                 if "竞赛目录" in x and ("网址" in x or "赛事" in x))
    num = re.compile(r"^(\d{1,2})\s*[、.．]\s*(.+)$")
    out = {}
    for i in range(start, len(lines)):
        m = num.match(lines[i])
        if not m:
            continue
        idx, name = int(m.group(1)), m.group(2).strip()
        if idx in out:          # 后半段「观察目录」会重新编号，取第一次出现
            continue
        url_ = next((lines[j] for j in range(i + 1, min(i + 4, len(lines)))
                     if lines[j].startswith("http")), "")
        out[idx] = (name, url_)
    return out          # 84 条，约 81 条带官网；剩余用搜索/人工补
```

> 难度评估：**84 项榜单页 = 简单**（1 次请求 + 30 行代码）；**各校认定目录 = 中等**（多为 PDF/Excel/图片，要 PDF 解析或人工导入）。所以先做「84 项种子」，不要逐个学校爬。

### 9.4 入库映射（原始信息 → `competitions`）

先落候选表，再审核入库（现有表没有审核字段，直接 INSERT 会污染线上列表）：

> **权威实现**：`db.py` 的 `CANDIDATES_DDL`；`init_db(db_path)` 与 `ensure_candidates_table(db_path)` 都可幂等建表。下面是与代码等价的结构。

```sql
CREATE TABLE IF NOT EXISTS competition_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,                 -- saikr | edu-roster | edu-notice
  source_key TEXT NOT NULL,             -- saikr:59755 / edu-roster:<name-hash>
  source_url TEXT,
  raw_name TEXT NOT NULL,
  norm_name TEXT NOT NULL,
  official_url TEXT,
  image_url TEXT,
  level_label TEXT,
  category_hint TEXT,
  reg_start_month INTEGER, reg_end_month INTEGER,
  reg_cross_year INTEGER DEFAULT 0,
  contest_start_month INTEGER, contest_end_month INTEGER,
  raw_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending/approved/rejected/duplicate
  competition_id INTEGER,                   -- 审核通过后回填 competitions.id
  created_at TEXT DEFAULT (datetime('now','localtime')),
  reviewed_at TEXT,
  UNIQUE(source, source_key)
);

CREATE INDEX IF NOT EXISTS idx_candidates_status
  ON competition_candidates (status, id);
CREATE INDEX IF NOT EXISTS idx_candidates_norm_name
  ON competition_candidates (norm_name);
CREATE INDEX IF NOT EXISTS idx_candidates_source
  ON competition_candidates (source);
```

| `competitions` 字段 | 映射来源 | 规则 |
|---|---|---|
| `name` | `contest_name` / 目录名称 | 去序号、去届次年份（2024/2025）、全角转半角、压缩空白；保留官方全称 |
| `category` | `contest_class_second` / 目录分组 | 映射到现有 7 类（创新创业、程序设计、数学建模…），未知填 `待确认` |
| `level` / `level_label` | `level_name` | 国家级→`national`、省级→`provincial`；国际级现有枚举没有，先落 `national` + label 原文 |
| `official_url` | 详情正文外链 / 目录 URL | 只收 http(s) 且非 saikr/百科/公众号域名；无合格外链留空 |
| `image_url` | `thumb_pic` / `web_pic_big` | 候选表可先存原图 URL；**审核 approve 前必须重过 `check_image()`**，banner（比例 >2.6）/过小不写 |
| `reg_start_month` / `reg_end_month` / `reg_cross_year` | `regist_*` Unix 秒 | 只取月份；跨年 `cross=1`；0 视为空 |
| `contest_start_month` / `contest_end_month` | `contest_*` / 通知页 LLM | `0`、`-0001` 一律 NULL；**不猜** |
| `intro` / `summary` | 详情 `content` 正文 | 复用 `crawler_competitions.py` 的抽取，写候选表不直接入库 |
| `discover_source` | 固定 | `saikr:list` / `edu:roster` / `edu:notice` |
| `last_checked_at` | 运行时间 | 每次扫描都更新 |
| `is_active` | `time_status` | 报名截止 >400 天视为陈旧，不新增 |

**审核入库（`review_candidates.py --approve`）**：`name` 去营销标签/年份/届次后写主表；`level` 映射 national/provincial；`sort_order=9999`（首页不曝光）；`discover_source=saikr:list / edu:roster`；封面不合格时仍入库但 `image_url=NULL`。主表 INSERT 与候选 `approved` 标记在 `db.promote_candidate()` 的同一事务内完成。

**去重（已实现）**：`norm_name` 去序号/年份/届次/营销标签/标点 + 官网域名双键；归一化名相同 -> 跳过；相似度 ≥0.9 且官网同域（或任一方无官网）-> 跳过；名称里的地域/赛区标记（全国/省/赛区/选拔赛…）不同时**不自动合并**，避免把「挑战杯」与「挑战杯省赛」、「世界技能大赛」与「…中国选拔赛」合并。

### 9.5 实施顺序（2026-11-08 更新）

1. ✅ 建候选表 + 84 项目录导入脚本（dry-run 输出 `workspace/roster_preview.json`）；
2. ⬜ 人工抽 10 条审核 → 入 `competitions`（`sort_order=9999`，首页不曝光）；
3. ✅ 接赛氪每日 20 条增量（只写候选表）+ 失败/限速监控（每日守卫生效）；
4. ✅ 审核入口先用 CLI（`review_candidates.py --list/--approve/--reject`），后台待审列表可后续再做；
5. ⬜ 观察两周：新增有效率、重复率、字段完整率，再决定是否扩量。

### 9.6 已落地实现（2026-11-08 12:36，生产 + 测试各 82 条）

| 项 | 实现 |
|---|---|
| 建表 | `db.CANDIDATES_DDL`；`init_db(db_path=None)` 自动建表；`ensure_candidates_table()` 只建候选表（不跑全量 schema） |
| 写入 | `db.add_candidate()`：先查重，再 INSERT；`UNIQUE(source, source_key)` 兜底 |
| 去重 | `db.normalize_competition_name()` + `db.find_duplicate_candidate()`（候选表 + 主表；审核时可只查主表） |
| 查询 | `query_candidates(status/source/limit/offset)`、`get_candidate(id)`、`candidate_stats()`、`count_candidates()` |
| 审核 | `review_candidates.py --list/--approve/--reject`；`db.promote_candidate()` 同事务 INSERT 主表 + 标记 approved |
| 84 项目录 | `discover_university.py`：文本行/表格、`15、` 换行、`41“大唐杯”` 无分隔符、裸域名补 `http://`、过滤 `年会展示/作品展` |
| 赛氪 | `discover_saikr.py`：`page=1..2&limit=10&sort=2`，3~5 秒延迟，每日一次守卫，往年届次（>400 天）跳过 |
| 生产保护 | 默认 `test_crawl.db`；写生产必须 `--apply --allow-production`（审核 CLI 为 `--allow-production`），自动 SQLite backup + `integrity_check` |

**实测计数**：84 项页 -> 解析 84 / 过滤 2 / 缺官网 5 -> 候选 82 -> 新增 62、命中主表 20；赛氪 -> 20 条全部新增。生产/测试各 **82 条 pending**（edu-roster 62 / saikr 20），`norm_name` 与 `source_key` 零重复；主表生产 32/87、测试 33/88 均未动。生产部署后 SHA-256 `8009238456d1304991c698e3773184a63398cf2c74938d34312a16cce9204c02`。**（以上为 12:36 时点；13:00 后生产已 approve 1 / reject 1，新增 id=36，候选变为 80 pending，见第二节快照）**

---

## 十、AI 内容生成与自动化（2026-11-08 落地）

### 10.1 旧脚本的真实定位（调研结论，勿再误解）

| 脚本 | 触发 | 写入 | 结论 |
|---|---|---|---|
| `analyzer.py` | **单条手动**，`--competition-id` 必填，无批量 | `insights`（experience/trend/gap）+ `ideas`，**不碰** `competitions` | 仍是灵感生成的唯一实现；`ai_content.generate_inspiration()` 复用它的 `generate()` |
| `generate_guides.py` | 旧版全表扫描；**已重写**为 `ai_content` 的批量 CLI | `competitions.full_process_guide`，**不碰** `ideas` | 老命令可用；**日常自动生成改用 `daily_ai_worker.py`** |

两者的旧版都写死 `db.DB_PATH`、无备份/生产库闸门/预算/任务状态，**不能直接挂 cron**；现在已由新链路接管。

### 10.2 数据模型

`ai_jobs`（2026-11-08 上生产，`db.AI_JOBS_DDL`）：

| 列 | 说明 |
|---|---|
| kind / competition_id | `guide` / `inspiration` + 赛事 ID |
| trigger | `on_demand` / `daily` / `manual` |
| status | `queued → running → done / failed / skipped` |
| attempts / tokens / error | 重试次数 / token 记账 / 失败原因（≤500 字，不回传前端） |
| requested_by / request_ip | 触发用户（匿名预留） |

关键索引：`UNIQUE(kind, competition_id) WHERE status IN ('queued','running')` —— 同一赛事同类任务同时只能有 1 个活跃任务，**跨进程/跨 worker 都安全**。

`competitions` 新增 4 列：`guide_generated_at / guide_model / guide_prompt_ver / guide_input_hash`（写入在 `db.save_generated_guide()`）。

### 10.3 按需接口

| 场景 | 状态码 | 返回 |
|---|---|---|
| 未登录 | 401 | 由 `get_current_user` 统一处理 |
| 已有攻略 | 200 | `{status: ready, guide_length}` |
| 首次触发 / 已在跑 | 202 | `{status: queued|running, job_id, poll_after_ms}`（幂等，同一 job） |
| 失败冷却（30 分钟） | 409 | `{status: failed, reason: cooldown}` + `Retry-After` |
| 日预算 / 队列满 | 429 | `{status: throttled, reason: daily_budget|queue_full}` + `Retry-After` |
| 赛事不存在 | 404 | — |

- 执行体：`ai_jobs_runner._EXECUTOR`（单线程）→ `ai_content.generate_guide_for()`；进程内串行，避免一次点开多个详情页同时烧钱。
- **竞态修复（实测踩过）**：`claim_ai_job` 用 `BEGIN IMMEDIATE` 拿写锁后再判“攻略是否为空”，否则「读空 → 另一线程写完 → 插入新任务」会产生一个注定跳过的任务。
- 僵尸回收：`running` 超过 15 分钟 → 下一次抢占标 `failed`（`stale`）。
- 前端：`CompetitionDetailView` 空攻略 → 「✨ 生成 AI 攻略」；未登录先弹登录；生成中骨架屏；3s×20 → 10s×18 轮询；成功自动刷新详情。

### 10.4 定时慢刷

```bash
python daily_ai_worker.py --db-path competitions.db --apply --allow-production \
  --limit 4 --idea-limit 2 --budget-tokens 300000 --cooldown-days 3
```

- 选片口径（`db.select_guide_candidates`）：上架 + `level='national'` + 有官网 + 攻略为空 + 无活跃任务 + 失败冷却 3 天；排序 `获奖数 DESC → sort_order（NULL 最后）→ id`。
- 灵感（`db.select_inspiration_candidates`）：同口径 + 获奖 ≥ `--min-awards`(3) + insights<3 或 ideas<4。
- 每条先 `claim` 再执行；token 达到 `--budget-tokens` 立即停；报告写 `workspace/ai_worker_<时间戳>.json`。
- 退出码：`0` 全绿 / `2` 有失败 / `3` 预算在开始前耗尽 / `4` 参数或目标库错误。
- 一次性补齐 17 条空攻略约 7 万 tokens；5 条 ≥3 获奖的灵感约 8–13.5 万；跑完自动空转（0 成本）。

### 10.5 实测成本（deepseek-flash，含 reasoning）

| 任务 | 实测 |
|---|---|
| 攻略 id=21 | 2104 字，3378 tokens，约 19s |
| 攻略 id=12 | 2543 字，2945 tokens，约 16s |
| 灵感（未真机跑） | 预估 2.5–3 万 tokens/条（3 insight + 2 idea 批） |

### 10.6 已执行的生产操作（可回溯）

| 时间 | 操作 | 备份 / 结果 |
|---|---|---|
| 13:09 前 | `migrate_production.py --apply` | 备份 `competitions.db.bak`（SHA `db77ac…`）；建 `ai_jobs` + 4 列；迁移后 SHA `ced3268…` |
| 13:09 | `cleanup_orphan_ai.py --apply --allow-production` | 备份 `competitions.db.bak-20261108-130926`；删 insights 10 / ideas 12；主表 33/87 不变 |
| 13:15 | 复核 | SHA `3af3af9c…`；`integrity_check=ok`；候选 80/1/1；攻略 7；ai_jobs 0 |

### 10.7 验证清单（本轮已跑）

```powershell
.\venv\Scripts\python.exe -m pytest test_ai_guide.py test_api_boundaries.py test_crawler_units.py -q
# 107 passed, 2 skipped
.\venv\Scripts\python.exe -m pyflakes ai_content.py ai_jobs_runner.py daily_ai_worker.py generate_guides.py cleanup_orphan_ai.py db.py routers\competitions.py
# 无输出（干净）
cd ..\frontend; npm.cmd run build   # 7.36s / 115 modules
```

### 10.8 故障排查顺序

1. `SELECT id,kind,status,attempts,error,created_at FROM ai_jobs ORDER BY id DESC LIMIT 10;` —— 先看状态与错误。
2. 前端一直“生成中”：查 `get_guide_state()` 的 `status`；若 `running` 且 `started_at` 很旧，等 15 分钟租约回收或手工 `UPDATE ai_jobs SET status='failed'`。
3. 429 `daily_budget`：当天任务数已到 `AI_GUIDE_DAILY_LIMIT`（默认 8），第二天零点自动恢复；`queue_full` 等任务跑完即可。
4. 生成质量：抽检 `full_process_guide` 是否出现库里没有的具体日期；若有，回滚该条并按 `CRAWLER_HANDOVER.md` 第四节第 11 条记录，必要时调 Prompt。
5. 接口 401：`Authorization: Bearer <token>` 是否带上；token 失效会被拦截器清掉。

---

## 十一、本轮交付清单（2026-11-08 13:15）

| 类型 | 文件 |
|---|---|
| 新增 | `ai_content.py`、`ai_jobs_runner.py`、`daily_ai_worker.py`、`cleanup_orphan_ai.py`、`test_ai_guide.py`、`AI_AUTOMATION_PLAN.md` |
| 修改 | `db.py`、`routers/competitions.py`、`models/schemas.py`、`config.py`、`generate_guides.py`、`migrate_production.py`、`frontend/src/services/api.js`、`frontend/src/views/CompetitionDetailView.vue` |
| 数据 | 生产库 `ai_jobs` + 4 列；孤儿 insights/ideas 清零；测试库真机 2 篇攻略 |
| 未做（下一步） | 服务器实机部署、crontab 实机挂载、17 条空攻略的正式回填（等上云后由 worker 分批跑） |

---

## 十二、上云前安全检查 / 边界测试 / 部署手册（2026-11-08）

### 12.1 Git 安全存档

- `.gitignore` 已补：`*.db.bak` / `*.db.bak-*` / `*.db-wal` / `*.db-shm` / `workspace/` / `.pytest_cache/` / `.ruff_cache/`。
- 已执行 `git rm --cached backend/competitions.db.bak-20261106-115044`，停止跟踪遗留的本地备份库。
  - 注意：该文件仍存在于旧提交历史（`41dde0b`）中；本轮只停止跟踪，**未重写历史**。若确认历史里含敏感数据，再用 `git filter-repo` 清理并强推。
- 敏感信息扫描：
  - 源码/文档无 `sk-*` 硬编码（`git grep` 只命中 README 占位符与 `config.py` 的 dev JWT 占位常量）；
  - `.env` 未被跟踪；
  - `config.py` 的 `_DEV_JWT_SECRET` 只是 fail-fast 哨兵，不是真实密钥。
- 本地存档 commit：**`c64cb8ae6caa14364326ba8e4ca8b308aba50afa`**（`chore: 上云前安全存档…`）。打完后 `git status` 干净。
- 遗留（未动，避免影响 RAG 首启）：`chroma_db/` 仍被 Git 跟踪（约 5 MB，可重建索引）。后续可 `git rm -r --cached chroma_db` + 加入 `.gitignore`，并在部署时用 `retriever.sync_awards_to_chroma(rebuild=True)` 重建。

### 12.2 爬虫上云边界测试（20 例全绿）

新增 `test_crawler_boundaries.py`，全部离线（不连网、不写真实库、不调模型），覆盖：

| 场景 | 用例 | 断言 |
|---|---|---|
| `requests.Timeout` / `ConnectionError` | `test_fetch_html_wraps_network_error_as_crawl_error`（2 参数） | 按 `retries` 重试；最终包装成 `CrawlError`，信息含底层原因 |
| 单条网络失败不中断批量 | `test_batch_loop_survives_timeout_and_connection_error` | 3 条候选（2 失败 / 1 正常）→ `rc=0`、`attempted=3`、`failed=2`，正常条继续处理 |
| 残缺 / 非标准 JSON | `test_safe_json_truncated_json_falls_back_without_raising`、`test_safe_json_repairs_truncated_and_nonstandard_json` | 修不了返回 fallback（不抛异常）；截断对象/数组/代码围栏能修复 |
| 任意垃圾输入 | `test_safe_json_never_raises_for_garbage`（8 参数） | 默认模式永不抛异常；`strict=True` 才抛 `LLMError` |
| 空网页 | `test_extract_page_empty_html_raises_crawl_error_not_crash`（4 参数） | 空串/空 HTML/空白 body/空容器 → 可预期 `CrawlError`，不是 `AttributeError` |
| 清洗边界 | `test_clean_text_handles_none_and_blank` | `None` / 空白 / 零宽字符 → `""` |
| 空网页不中断批量 | `test_batch_loop_survives_empty_page` | 空页记 `crawl/llm` 失败，后续正常条继续 |

运行结果：

```powershell
.\venv\Scripts\python.exe -m pytest test_crawler_boundaries.py -q
# 20 passed in 1.84s

.\venv\Scripts\python.exe -m pytest -q
# 127 passed, 2 skipped, 3 warnings in 12.19s
```

### 12.3 每日备份脚本 `backup_db.py`

- 用 `sqlite3.Connection.backup()`（不是 `cp`），把一个**包含已提交 WAL** 的一致性快照写成单文件；备份文件 `PRAGMA journal_mode=DELETE`，恢复时不依赖 `-wal/-shm`。
- 备份后自动 `PRAGMA integrity_check` + 统计 `competitions / awards / competition_candidates / ai_jobs` 行数；支持 `--keep-days` / `--keep-count` / `--label` / `--json`。
- 实测（WAL 未 checkpoint 写入一条新数据后立刻备份）：备份内行数包含该条，`integrity=ok`，备份旁无 `-wal`。
- 对生产库只读冒烟：`competitions=33 / awards=87 / competition_candidates=82 / ai_jobs=0`，`integrity=ok`。

```bash
./venv/bin/python backup_db.py --db competitions.db --backup-dir backups --keep-days 30 --label daily
```

> 备份文件 SHA 与线上库 SHA 不同是正常的（页布局/日志模式被重写）；验收看 `integrity_check` + 行数。

### 12.4 《云端部署手册》

文件：**`CLOUD_DEPLOYMENT_GUIDE.md`**（约 1410 行），包含：

1. 部署蓝图、目录/端口规划、维护窗口；
2. 服务器初始化（时区、用户、Swap、UFW + 阿里云安全组）；
3. 依赖安装（Ubuntu 22.04 deadsnakes / 24.04 默认 Python、venv、Node 可选）；
4. 代码拉取（HTTPS / Deploy Key）与 `.env` 模板、JWT 生成、权限；
5. **数据库迁移**：本地 `sqlite3.backup()` 快照 → SHA/行数校验 → scp → 服务器落库 → `migrate_production.py` 体检/迁移 → WAL 问答 → 恢复与回滚；
6. systemd 服务（`--workers 1`、`127.0.0.1:8000`、加固项）；
7. 前端 `dist` 上传 + Nginx（`/api` 反代、SPA fallback、HTTPS/certbot、混合内容提醒）；
8. **crontab 链**：02:30 备份 → 03:00 新闻（周一/四）→ 04:10 赛氪候选 → 04:40 AI 慢刷 → 05:20 周度补全 → 05:40 日志清理 → 05:50 备份清理；含 `flock`、`%` 转义、时区、失败告警、常见坑；另附可选 OSS 异地备份。
9. 上线验收清单、回滚预案、故障排查表；
10. 附录：`.env`、校验 SQL、crontab、systemd/Nginx 路径、本地快照命令。

**已于 2026-11-08 在服务器执行（原地升级，见 12.5）**；首次部署按第 1~8 节顺序完成，回滚与后续维护以 12.5 记录为准。

### 12.5 上云记录（2026-11-08 已完成）

| 项 | 值 |
|---|---|
| 发布 commit | `81e768633e420163982bda692239e8dd590760af`（`test+docs: 上云边界测试、WAL 安全备份脚本与云端部署手册`） |
| 部署时间 | 2026-11-08 14:33（切换完成）/ 15:29（cron + 验收完成，Asia/Shanghai） |
| 服务器 | 阿里云 Ubuntu 24.04.2 LTS · `112.126.90.252` · 1.6 GiB RAM（+2 GiB Swap） |
| 部署方式 | **原地升级** `/root/KuAo`：未按手册新建 `/opt/kuao` + `kuao` 用户；systemd 以 `root` 运行（与既有部署一致） |
| 迁移后 DB SHA-256 | `297e17df05ffda4f63ba14e0136599a7f9575ea6c38816471699b4e2b859b486` |
| 迁移后行数 | competitions **33** / awards **87** / candidates **82** / guides **7** / ai_jobs **0**（`integrity_check=ok`） |
| 服务 | systemd `kuao-backend`（`127.0.0.1:8000`，`--workers 1`）+ Nginx `:80` 反代 `/api/`；前端静态目录 `/var/www/html` |
| 每日备份路径 | `/root/KuAo/backend/backups/`（`backup_db.py`，保留 30 天） |
| 切换前备份 | `/root/kuao-backups/20261108-143324/`（旧库 / `.env` / nginx 配置 / 旧前端） |
| crontab 生效时间 | 2026-11-08 15:29 CST；5 条任务：02:30 备份 / 04:10 候选 / 04:40 AI / 周日 05:20 补全 / 05:40+05:50 清理 |
| 新闻采集 | **未挂**：`LIST_URL` 待定；确定后单独加一条（模板见手册 7.4） |
| 首次验收 | 用户人工验收：crontab / systemd / 端口 / 健康 / 行数 / 权限 / 时区全部通过 |
| 回滚 | `bash /root/kuao-rollback.sh 20261108-143324`；代码回退 `git checkout 3ee9dd5`；库回退用上述备份目录的 `competitions.db` |

> 与手册 0.2 的差异：主库仍在 `/root/KuAo/backend/competitions.db`，未迁到 `/opt/kuao`；因此所有 cron、systemd、备份路径都按 `/root/KuAo` 配置。后续若迁移到 `/opt/kuao`，必须同步改 systemd `WorkingDirectory`、cron 的 `cd`、日志/备份路径，并先停服务、删 `-wal/-shm` 再移库。
