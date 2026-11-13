# CRAWLER_HANDOVER.md
> 交接范围：`backend/` 的新闻获奖采集、赛事基础数据采集与补全、赛氪详情补充、候选表与审核、AI 攻略/灵感链路、云端定时任务、管理后台、前端展示规范。
> 最后更新：2026-11-11（新增第十节「JS 渲染站点抓取 SOP」，接入 playwright-mcp 浏览器抓包）。本文件是**操作手册**，不记变更史；单条历史用 `git log` 与 `workspace/*.json` 回溯。
> 读法：第零节 30 秒 → 第二节命令 → 第五节后台 → 第七节边界。
## 文档导航
零、接手必读（30 秒版）
一、架构、数据契约与文件清单
二、运行命令
三、赛氪数据源事实与合规边界
四、使用隔离红线
五、管理后台
六、前端展示规范（已定稿，勿回退）
七、未完成项与已知边界
八、测试与验证
九、云端部署与运维
十、JS 渲染站点抓取 SOP（playwright-mcp）

---
## 零、接手必读（30 秒版）
### 0.1 当前阶段
采集闭环、AI 内容闭环、管理后台（候选审核 + 用户反馈）都已在本地跑通；云端 2026-11-08 14:33 原地升级上线（`/root/KuAo`，systemd `kuao-backend`，前端根 `/var/www/html`）。11-09/10 的新能力（双轨日期、简称兜底、审核后自动补全、批量审核）**只在本地，云端未同步**；候选池仍在审核中。
### 0.2 下一步（按优先级）
1. **云端同步（阻塞其余任务）**：上传新 `frontend/dist` + 后端代码到 `/root/KuAo`，`systemctl restart kuao-backend`。云端只部署到 `81e7686`，缺详情三列、AI 提纯、简称/日期补全。
2. **双账号复验** sessionStorage 隔离；清理旧 `localStorage` token 后全员需重新登录（见第七节「账号串号」）。
3. **审核收尾**：57 条 pending（全是 `edu-roster`）逐条过后台或 CLI；「辅助报名 / 考试入口 / 作品征集」类服务广告直接 reject。
4. **库对齐**：本地生产库副本已 50 条，云端仍 33 条 → 走 9.3 的「本地快照 → 云端覆盖」。
5. 16 条报名冲突逐条裁决（默认保留库内值）；新闻 `LIST_URL` 定下来后补一条 cron。
### 0.3 五条红线（细节见第四节）
1. 默认只写 `test_crawl.db`；写 `competitions.db` 必须 `--apply` + `--allow-production` + 写前自动备份成功，三者缺一即拒。
2. 一律只补 NULL：`reg_*` / `contest_*` / `image_url` / 日期列已有值一律不覆盖（报名时间尤其如此）。
3. 候选只有 `pending` 能 approve；主表重名拒绝入库；新入库行 `sort_order=9999`（首页不曝光）。
4. 封面必须过 `image_guard`：二维码/logo/banner、宽高比 >2.6、死链一律不写，阈值不许放宽。
5. AI 只能走 `ai_jobs` 抢占 + 登录态；不绕过任务表直接写 `full_process_guide`；长文只在详情接口返回。
### 0.4 数据库快照（2026-11-10 实测）
| 对象 | 现状 |
|---|---|
| `competitions.db`（本地生产库副本） | 赛事 **91**（可见 84 = 教育部 84 项全集；隐藏 7 条带 awards 的省级占位）/ 获奖 **87** / 候选 **0**（已清空）/ 攻略 **7** / `short_name` 全覆盖 / `ai_jobs` **0** |
| `test_crawl.db`（测试库） | 赛事 **33** / 获奖 **88** / 候选 **82**（全 pending）/ 攻略 **9** / `ai_jobs` 2（均 done）；**schema 落后**，缺 `*_date` 四列，需 `db.init_db('test_crawl.db')` 补列后再跑日期类脚本 |
| 云端 `competitions.db` | 2026-11-08 迁移后 33 / 87 / 82 / 攻略 7 / ai_jobs 0，SHA `297e17df…`；本地新数据尚未同步 |
| 候选来源 | `edu-roster` 62 / `saikr` 20，`UNIQUE(source, source_key)` 零重复 |

> 数字随审核变化，以 `PRAGMA` / `COUNT` 实测为准。11-08 里程碑值：33 赛事 / 87 获奖 / 82 候选（80 pending）/ 攻略 7 条。
### 0.5 里程碑
| 时间 | 事件 |
|---|---|
| 11-06 | 首个生产库快照备份 |
| 11-07 | 新闻/赛事采集器落地、封面闸门、合并 id=34/35 |
| 11-08 12:05 / 12:34 | 赛氪补全首次写生产库（10 字段成功，备份 `…120553`）；候选表上线（82 条） |
| 11-08 13:09–13:15 | `ai_jobs` + 4 个 `guide_*` 列、孤儿清理、攻略接口上线 |
| 11-08 14:33 / 15:29 | 云端上线 / crontab 生效 |
| 11-08 19:56 / 20:14 | 详情三列 + AI 提纯（`077dce0` / `d685432`） |
| 11-08 22:30 | P0 账号串号修复（sessionStorage）、搜索框定宽 240px |
| 11-09 | 管理后台、双轨制日期、两字简称、批量审核 |
| 11-10 | 修「审核通过即空壳」：通过后自动补简称 + 赛氪详情/日期/封面；晚间再修名录匹配：名称 → 赛氪本地索引（`saikr_match.py`），清理 5 条空壳，详情页隐藏名录目录链接 |

---
## 一、架构、数据契约与文件清单
### 1.1 三条数据流
- **新闻获奖**：`crawler_news.py` 抓列表/详情 → `llm.chat_json` 抽奖 → 写 `awards`（按 `source_url` 去重，`content_hash` 唯一索引兜底；匹配不到赛事自动建占位）。
- **赛事主数据**：`crawler_competitions.py` 单页 upsert → `batch_update_competitions.py` 批量补全，全部**只填空**。
- **新赛事发现**：`discover_*` 只写 `competition_candidates` → 人工/后台/CLI 审核 → `promote_candidate()` 同事务写 `competitions` + 标记 approved → 自动补简称与详情。
### 1.2 数据表与关键常量
- **DB 路径**：`db.DB_PATH` 硬编码 `competitions.db`，即 `os.path.join(os.path.dirname(os.path.abspath(__file__)), "competitions.db")`。带 `db_path` 参数的函数优先用传入值；CLI 一律用 `--db-path` 切库，**不要靠改 DB_PATH**。
- **双轨制日期（2026-11-09 起）**：
  - 月份轨 `reg_start_month / reg_end_month / reg_cross_year / contest_start_month / contest_end_month`：老数据，**永不删**；
  - 日期轨 `reg_start_date / reg_end_date / contest_start_date / contest_end_date`：`'YYYY-MM-DD'` 或 `'MM-DD'`；只有原文出现「日」才写，没有就留 NULL，**绝不拿月份凑日期**；
  - `db._HAS_DATE_SQL` 用日期轨算「正在报名 / 尚未开始 / 已过期」排序，前端 `services/timeFormat.js` 按有无日期决定展示粒度。
- **`competition_candidates`**（`db.CANDIDATES_DDL`；`init_db(db_path)` 或 `ensure_candidates_table()` 均幂等建表）：
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
```
  配套索引：`(status, id)` / `(norm_name)` / `(source)`；候选池另有 `reg_start_date` 等四列，由 `backfill_candidates_date.py` 补。

- **`ai_jobs`**（`db.AI_JOBS_DDL`）：`kind / competition_id / trigger / status / attempts / tokens / error / requested_by / request_ip`；关键索引 `UNIQUE(kind, competition_id) WHERE status IN ('queued','running')` → 同赛事同类只有 1 个活跃任务，跨进程/多 worker 都安全。
- **`competitions` 关键列**：`participants / content_scope / exam_schedule`（详情三卡）、`guide_generated_at / guide_model / guide_prompt_ver / guide_input_hash`、`short_name`、`discover_source / last_checked_at / is_active`。
- **常量**：`AI_GUIDE_DAILY_LIMIT=8`、`AI_GUIDE_QUEUE_MAX=5`、`AI_GUIDE_FAIL_COOLDOWN_MIN=30`；`image_guard`：`IMAGE_MIN_WIDTH=300`、`IMAGE_MIN_HEIGHT=150`、`IMAGE_MAX_ASPECT=2.6`、`IMAGE_MIN_ASPECT=0.45`、`IMAGE_MAX_BYTES=3MB`。
- **`saikr_map.json`**：32 条 `competition_id ↔ saikr_contest_id` 静态映射（exact 22 / parent 7 / approx 1 / none 2，`needs_review` 9），带 `saikr_url / saikr_level / match_type / note`。它是**存量映射**，新入库赛事不在其中（见第七节 `no_mapping` 降级）。
- **`url-map.json`**：`{"赛事id": "通知页URL"}` 覆盖采集入口，当前 5 条（1/2/10/12/13）。
### 1.3 文件清单
| 文件 | 一句话职责 |
|---|---|
| `llm.py` | DeepSeek 封装：`chat()` / `chat_json()`（清洗 + 修复 + 重试）/ `ping()` / `LLMError` |
| `crawler_news.py` | 新闻获奖采集（单页 / 列表批量 / 自动翻页 / 非获奖过滤 / 未知赛事建占位） |
| `crawler_competitions.py` | 单赛事采集：正文 + `og:image` 提取、AI 抽字段、upsert、回读验证 |
| `batch_update_competitions.py` | 存量赛事批量补全：抓官网 → 抽字段 → 只填空；限速/重试/封面闸门 |
| `merge_test_competitions.py` | 测试库 → 生产库合并（name + `content_hash` 去重，写前备份） |
| `pipeline_update.py` | 赛氪 + 通知页统一预检/清洗；默认只读，`--apply` 写库，`--from-report` 复用报告 |
| `enrich_saikr_details.py` | 赛氪详情补充：抓 `content` → 章节抽取 → AI 提纯 → 写三卡；`--backfill` / `--daily-update`；`fill_dates_and_cover()` |
| `backfill_dates.py` | 主表精确日期补全探测：抽日期 + 逐条核对原文，默认只读，只补 NULL |
| `backfill_candidates_date.py` | 候选池日期补全：赛氪候选走 `raw_json`（零网络），其余走模型 + 原文核对 |
| `cleanup_bad_covers.py` | 清理主表垃圾封面（重过 `image_guard`，不合格置 NULL，transient 待复查） |
| `discover_common.py` | 发现公共层：目标库保护/备份、建候选表、入库循环、去重统计、类别猜测 |
| `competition_whitelist.py` | **教育部 84 项核心国赛白名单唯一事实源**：名单 + 别名 + `match()` / `is_provincial()` / `gate()` 统一入库闸门（见 2.17） |
| `cleanup_non_core_races.py` | 主库收敛为「84 项智库」：非白名单/省级赛删除或转占位、`--wipe-candidates` 清空候选池（见 2.17） |
| `init_84_races.py` | 84 项全量补齐：本地索引优先解析赛氪 `contest_id`、未命中才走搜索兜底、`--skeletons` 给未收录项建骨架、`--stale-partial` 往年届次只写稳定字段（见 2.18） |
| `discover_university.py` | 84 项高校竞赛目录页解析（文本行/表格/序号变体），默认 dry-run |
| `discover_saikr.py` | 赛氪列表前 2 页（≤20 条/天）增量发现，3–5 秒延迟，每日守卫，往年届次跳过 |
| `review_candidates.py` | 审核 CLI（`--list/--approve/--reject`）：重过封面闸门 + 同事务写主表 + 自动补简称/详情 |
| `saikr_match.py` | 赛氪「名称 -> contest_id」解析：抓全量列表建本地索引（100/页 × 52 页，1.2s 间隔 ≈80s）+ 归一化相似度匹配；CLI `--build / --query / --stats` |
| `cleanup_directory_shells.py` | 清理教育部门名录空壳赛事：识别「已通过但详情全空」→ 退回候选 pending（默认 dry-run，`--apply` 写库，已有 awards/收藏的行跳过保护） |
| `bulk_approve.py` | 批量审核「带精确日期」的 pending 候选（同 `--approve` 逻辑）；`--with-ai` 才补详情 |
| `promote_admin.py` | 管理员标记：`--list` / `--revoke`，只改 `users.is_admin` |
| `ai_content.py` | AI 生成核心：v2 攻略 Prompt（注入月份 + 获奖作品、禁编造）、`generate_guide_for()`、`generate_inspiration()`、`guide_input_hash()` |
| `ai_jobs_runner.py` | 任务编排：单线程池、预算/队列/冷却、`request_guide()`、`run_guide_job` / `run_inspiration_job` |
| `daily_ai_worker.py` | 每日慢刷：选片、预算熔断、3 天冷却、自动备份、JSON 报告、退出码 0/2/3/4 |
| `generate_guides.py` | 兼容用的攻略批量 CLI（内部已是 v2 Prompt，`--only-missing / --id`） |
| `analyzer.py` | 灵感生成唯一实现（`insights` + `ideas`，单条手动），被 `generate_inspiration()` 复用 |
| `cleanup_orphan_ai.py` | 清理孤儿 `insights/ideas`（`competition_id` 空或悬空），默认 dry-run |
| `fill_core_skeletons.py` | 核心赛事骨架补全（封面/日期/参赛对象）：名称匹配赛氪 → 详情 → AI 提纯 → 只补空字段；默认 dry-run，`--allow-stale` 只补往年封面/对象不写日期，`--parent-fallback` 省级赛回退国赛只复制对象/范围（见 2.16） |
| `generate_short_names.py` | 批量生成两字简称（AI，默认只补空，`--force` 覆盖） |
| `short_names.py` | 赛事名 → 两字简称（只算不写库，落库由调用方完成） |
| `db.py` | 数据层：建表/迁移、CRUD、候选表、ai_jobs 抢占、日期排序 SQL |
| `models/schemas.py` | Pydantic 模型（列表模型不含长文，详情模型含攻略/三卡/日期） |
| `routers/competitions.py` | 赛事列表/详情/攻略接口、`_row_to_competition` 字段映射 |
| `routers/admin.py` | 管理后台接口（候选/反馈），approve 后自动补全 |
| `routers/auth.py` | 注册/登录/`me`，`UserInfo.is_admin` |
| `main.py` | FastAPI 入口、CORS、启动时 `db.init_db()` |
| `security.py` | 口令/JWT/依赖注入（`get_current_user`、`get_current_admin`） |
| `config.py` | 配置单一读取点（DeepSeek / JWT / CORS / AI 限额） |
| `migrate_production.py` | 生产库体检 + 备份 + 补列（`TARGET_COLUMNS` 含三卡与 `guide_*`） |
| `migrate_awards.py` | awards 表幂等补列迁移（只加列不动数据） |
| `schema_v2.sql` | 全新建库 DDL（图片、比赛时间、采集溯源、上下架、三卡、日期、简称） |
| `backup_db.py` | 每日备份（crontab 02:30），WAL 安全 + integrity/行数校验 |
| `verify_crawl.py` | awards 读回校验（拒绝生产库） |
| `image_guard.py` | 封面闸门（采集/补全/清理共用） |
| `embedder.py` / `retriever.py` | 检索内核：在线 Embedding API + Chroma 两段检索（索引可重建） |
| `CLOUD_DEPLOYMENT_GUIDE.md` | 云端部署手册（依赖、迁移、systemd、Nginx、crontab、验收、回滚） |
| `saikr_map.json` | 赛氪 contest_id 映射（32 条，按 `competition_id`；新赛事走 `saikr_index.json` 名称匹配） |
| `saikr_index.json` | 赛氪全量赛事名称索引（5164 条，`saikr_match.py --build` 生成，14 天过期自动重建，勿手工编辑） |
| `test_crawler_units.py` / `test_crawler_boundaries.py` | 单测（19 例：`extract_image_url`、`_normalize_level`）/ 上云边界（20 例：网络异常、残缺 JSON、空网页） |
| `test_ai_guide.py` | AI 链路：ai_jobs、幂等、409/429、列表瘦身、Prompt、孤儿清理（16 例） |
| `test_enrich_saikr.py` | 详情补充器（11 例：规范化、防照抄、双模式、审核联动） |
| `test_auth_isolation.py` | 账号隔离回归（10 例） |
| `test_api_boundaries.py` / `test_image_guard.py` / `test_embedder.py` | 接口边界 / 封面闸门 / 向量层 |
### 1.4 目录与产物
- `workspace/`：预检报告与截图。常看 `pipeline_report.json/.md`（32 条赛氪预检 + 16 条报名冲突）、`pipeline_apply_*.json`、`saikr_catalog.json`（5172 条目录缓存 3.9 MB，**别重复拉**）、`batch_competitions_*.json`、`ai_worker_*.json`、`cover-*.png`、`roster/scnu/saikr_preview.json`、`saikr_daily_state.json`。**能复用报告就别再联网烧额度。**
- 备份：本地 `backend/*.db.bak-*`，云端 `backups/`。每次写库前自动生成、永不覆盖。恢复：**先删 `-wal/-shm`**，再 `Copy-Item <备份> .\competitions.db -Force`。

---
## 二、运行命令
### 2.1 从生产库复制测试库
```powershell
cd D:\agent-lab\KuAo-Platform\backend
# 没有后端服务在写库时可直接复制
Copy-Item .\competitions.db .\test_crawl.db -Force
```
后端在跑（WAL 未 checkpoint）时改用 SQLite backup API（`backup_db.py`），避免拿到不一致快照。
### 2.2 新闻单页：先 dry-run，再真实写测试库
```powershell
# 只看抽取结果，不写库
.\venv\Scripts\python.exe crawler_news.py `
  --db-path .\test_crawl.db `
  --dry-run `
  --show-raw `
  --url "https://cise.hunnu.edu.cn/info/1165/7066.htm"
# 去掉 --dry-run 即真实写测试库
.\venv\Scripts\python.exe crawler_news.py `
  --db-path .\test_crawl.db `
  --url "https://cise.hunnu.edu.cn/info/1165/7066.htm"
```
### 2.3 新闻列表：批量抓取 + 自动翻页
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
### 2.4 赛事采集
```powershell
# 插入新赛事
.\venv\Scripts\python.exe crawler_competitions.py `
  --url "https://www.sohu.com/a/1091484812_122595727" `
  --db-path .\test_crawl.db `
  --save-json .\workspace\competition_insert_edu_robot.json
# 更新指定占位赛事
.\venv\Scripts\python.exe crawler_competitions.py `
  --url "http://smve.cqjtu.edu.cn/info/1037/6028.htm" `
  --db-path .\test_crawl.db `
  --competition-id 34 `
  --save-json .\workspace\competition_update_robot.json
# 只抽取不写库
.\venv\Scripts\python.exe crawler_competitions.py `
  --url "https://www.sohu.com/a/1091484812_122595727" `
  --db-path .\test_crawl.db `
  --dry-run `
  --show-raw
```
### 2.5 awards 读回校验
```powershell
.\venv\Scripts\python.exe verify_crawl.py `
  --db-path .\test_crawl.db `
  --award-id 86 87 88 89
```
### 2.6 语法/编译检查（不连网、不写库）
```powershell
.\venv\Scripts\python.exe -m py_compile crawler_news.py crawler_competitions.py db.py models\schemas.py routers\competitions.py verify_crawl.py
.\venv\Scripts\python.exe -m pytest test_crawler_units.py -q     # 19 passed
.\venv\Scripts\python.exe -m pyflakes crawler_news.py crawler_competitions.py db.py
```
### 2.7 批量补全：演练（不写库，但花大模型额度）
```powershell
# 单条看效果
.\venv\Scripts\python.exe batch_update_competitions.py --ids 10 --allow-production
# 5 条通知页（用 url-map 覆盖入口）
.\venv\Scripts\python.exe batch_update_competitions.py `
  --ids 1,2,10,12,13 --url-map .\url-map.json --allow-production --show-raw
```
### 2.8 批量补全：真实写库（自动备份）
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
> 每次 `--apply` 先自动备份 `competitions.db.bak-<时间戳>`，再输出汇总 + `workspace/batch_competitions_*.json`。
> `--ids` 可以绕开 `--filter`，但**仍然只填空**；要改已有值只能手工 SQL。
### 2.9 测试库 → 生产库合并 / 生产库体检（一般用不到，后端启动会自动 init_db）
```powershell
# 演练
.\venv\Scripts\python.exe merge_test_competitions.py
# 真正合并（自动备份）
.\venv\Scripts\python.exe merge_test_competitions.py --apply
# 连占位赛事一起合并（默认跳过 category='待确认'）
.\venv\Scripts\python.exe merge_test_competitions.py --apply --include-placeholders
# 生产库体检（只读）/ 补列（备份 + 补列）
.\venv\Scripts\python.exe migrate_production.py            # 只读体检
.\venv\Scripts\python.exe migrate_production.py --apply    # 备份 + 补列
```
### 2.10 赛氪映射 + 统一清洗（默认只读，`--apply` 写库）
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
- 预检报告写 `workspace/pipeline_report.json`；生产基线对比见 `pipeline_report_prod.json` 与 `pipeline_report.md`。
- `--apply` 前打印「将写 / 将跳过」清单并自动备份，写后输出覆盖率、SHA-256、`workspace/pipeline_apply_<时间戳>.json`。
- 不带 `--from-report` 时 `--apply` 会重新联网 + 调模型。**补数量验证优先复用报告。**
### 2.11 新赛事发现：候选表 POC（默认 dry-run）
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
- 预览：`workspace/roster_preview.json`、`workspace/scnu_preview.json`、`workspace/saikr_preview.json`；每日守卫状态在 `workspace/saikr_daily_state.json`（仅 `--apply` 成功后写）。
- 候选表统计（只读）：
```powershell
.\venv\Scripts\python.exe -c "import db; print(db.candidate_stats('test_crawl.db'))"
```
### 2.12 候选审核：候选 → 主表
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
- `--approve`：候选必须 `pending`；主表已有同名/高相似 → 拒绝；封面不合格 → 仍入库但 `image_url=NULL`；新赛事固定 `sort_order=9999`；成功后自动补两字简称 + 赛氪详情/日期/封面（失败只告警，不回滚），离线可加 `--skip-enrich`。
- `--reject`：只写 `status='rejected'` 与 `reviewed_at`。
- 批量（同逻辑，只处理带精确日期的 pending）：
```powershell
.\venv\Scripts\python.exe bulk_approve.py --dry-run
.\venv\Scripts\python.exe bulk_approve.py `
  --db-path .\competitions.db --allow-production
```
### 2.13 详情补充 / 日期补全 / 简称 / 封面清理
```powershell
# 赛氪详情：存量回填（AI 提纯，约 25 次 LLM，5–10 分钟）/ 每日增量（只碰最近 7 天入库）
.\venv\Scripts\python.exe enrich_saikr_details.py --backfill --apply --allow-production --db-path .\competitions.db --preview saikr_enrich_prod_backfill.json
.\venv\Scripts\python.exe enrich_saikr_details.py --daily-update --apply --allow-production --db-path .\competitions.db
# 精确日期：先探测（不写库），再加 --apply 只补 NULL
.\venv\Scripts\python.exe backfill_dates.py --limit 5
.\venv\Scripts\python.exe backfill_dates.py --ids 13,12,2,1,35
.\venv\Scripts\python.exe backfill_dates.py --limit 5 --apply --allow-production
# 候选池日期（赛氪候选走 raw_json，零网络）/ 两字简称（AI，只补空）/ 垃圾封面清理
.\venv\Scripts\python.exe backfill_candidates_date.py --apply
.\venv\Scripts\python.exe generate_short_names.py --db-path .\competitions.db --allow-production
.\venv\Scripts\python.exe cleanup_bad_covers.py --db-path .\competitions.db --allow-production --apply
```
> `--apply` 才有写动作，写前都自动备份；已有值一律保留。`enrich_saikr_details.py` 退出码 1 表示有条目失败，**原命令重跑**即可，已填字段自动跳过。3 条无映射 / 5 条往年届次 / 页面无对应章节的保持 NULL，属设计行为。
### 2.14 AI 攻略 / 灵感
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
  - `POST /api/competitions/{id}/generate_guide` → 202 `{status: queued, job_id, poll_after_ms}`；已有攻略 200；失败冷却 409；预算/队列 429；未登录 401；赛事不存在 404。
  - `GET  /api/competitions/{id}/generate_guide` → `{status, job_id, attempts, guide_length, generated_at}`。
- 详情接口返回 `full_process_guide` + `guide_status` + `guide_generated_at`；**列表接口不返回长文**。
- 报告：`workspace/ai_worker_<时间戳>.json`；失败原因在 `ai_jobs.error`（不回传前端）。
### 2.15 孤儿 AI 数据清理（一次性，已执行）
```powershell
# 只统计（默认 dry-run）
.\venv\Scripts\python.exe cleanup_orphan_ai.py --db-path .\competitions.db
# 真正删除（自动备份；生产库必须 --allow-production）
.\venv\Scripts\python.exe cleanup_orphan_ai.py `
  --db-path .\competitions.db --apply --allow-production
```
只删 `competition_id IS NULL` 或悬空行。

### 2.16 核心赛事骨架补全（封面 / 日期 / 参赛对象，2026-11-11 已执行）
```powershell
# 1. 预演（默认：只读库 + 抓详情 + 过封面闸门，不写库、不调 AI）
.\venv\Scripts\python.exe fill_core_skeletons.py
# 2. 真实写生产库（自动备份）
.\venv\Scripts\python.exe fill_core_skeletons.py --apply --allow-production --allow-stale
# 3. 只处理指定赛事 / 往年届次
.\venv\Scripts\python.exe fill_core_skeletons.py --ids 2,10,17 --apply --allow-production --allow-stale
```
- 目标 = 「`image_url` / `participants` / `reg_start_date` 三列全空 + 至少一条下游数据（awards / ideas / insights / user_preparations）」的核心赛事（本次 8 条）；`--ids` 可显式指定。
- 链路复用审核入库同一套：`saikr_match` 名称匹配 → 赛氪详情 API → AI 提纯（禁止照抄）→ 只补空字段；封面仍过 `image_guard`，不合格保持 NULL 走前端兜底。
- **往年届次护栏**：`--allow-stale` 下只补封面/参赛对象，**日期四列一律不写**（避免把 2019/2020 的报名日期写到 2026 赛事上）。默认不带 `--allow-stale` 时整条跳过。
- **`--parent-fallback`（省级赛 → 国赛）**：省级赛在赛氪索引里往往没有条目，按「去后缀 / 去『省级』/ 补『全国』」生成变体回退匹配国赛（相似度 ≥0.92 才采用）。红线：**只复制 `participants` / `content_scope` 两类静态文本**；日期、封面、赛程（`exam_schedule` 里混时间）一律不写；省级赛这两个字段已有值时直接跳过。文本里的「2024年6月1日前」这类届次时间用 `strip_parent_dates()` 剥掉再入库（保留资格描述本体）。实测 #30 挑战杯·省级赛 回退到第 19 届国赛，写入参赛对象 235 字（无年份），日期/封面/赛程保持 NULL。
- 实测（2026-11-11）：8 条中命中 5 条、未命中 3 条（计算机设计大赛·省级赛 / 学创杯 / 挑战杯·省级，需人工补）；写库 4 条（#2 挑战杯对象 266 字 + 封面；#17 信息安全对象 222 字 + 赛程 157 字；#25 软件测试范围 286 字 + 赛程 268 字 + 封面；#10 计算机设计大赛 2026 报名日期）。#4 年会展示详情无章节且封面被闸门拒绝，保持空壳。
- 全库封面 22→23、参赛对象 25 条、报名日期 18 条；赛事总数 46 不变。

### 2.17 收敛为「教育部 84 项核心国赛智库」（2026-11-11 已执行）
```powershell
# 1. 预演（默认 dry-run：只打印将删/将占位/待复核）
.\venv\Scripts\python.exe cleanup_non_core_races.py --wipe-candidates
# 2. 真实执行（自动备份到 %TEMP%\kuo-verify）
.\venv\Scripts\python.exe cleanup_non_core_races.py --apply --allow-production --wipe-candidates
# 3. 自检白名单匹配
.\venv\Scripts\python.exe competition_whitelist.py "蓝桥杯全国软件和信息技术专业人才大赛"
```
- **白名单唯一事实源** = `competition_whitelist.py`：84 条「序号 + 官方名 + 官网」+ 别名表，来源 `sdaeu.edu.cn` 教务处榜单页；`match()` 走「归一化 + 最长命中」（去标点/年份/届次，取命中最长的键，避免「挑战杯」把创业计划赛误判成课外学术作品赛）。
- **清理口径（双重条件）**：① 名称带 `省赛/省级/区域赛/赛区/选拔赛` 字眼；② 不在 84 项名单。命中且**无下游数据**→删除（级联 awards/ideas/insights/备赛计划/AI 任务，候选解绑）；命中但**有真实获奖**→转占位（`category='待确认'`、`sort_order=9999`、`is_active=0`，行与 awards 全保留，前端不再展示）。非白名单但有获奖 → 只列「待复核」，不自动动。
- **实测（2026-11-11）**：46 → 31 条（删 15 条水赛、7 条省赛转占位）。删除：汉语言文字、商务外语词汇、机器人与算法大赛（含两赛道）、中外杯、英语世界杯、英才计划、诗经吟唱、中国故事大赛×2、中西部外语翻译、环保知识大赛、华文奖、云帐房、悦·读者杯。占位：#23/#24/#25/#27/#28/#29/#30（均带 awards）。**候选表 82 条清空归零**。
- **闸门已固化到三条链路**（默认生效，无开关）：`discover_saikr` 写候选前、`discover_university` 解析目录后、`review_candidates --approve` 与 `POST /api/admin/candidates/{id}/approve` 入库前，统一调 `whitelist.gate(name)`；非白名单/省级直接丢弃或 400。应急破例只能 `review_candidates --allow-non-whitelist`（CLI 显式，无 API 入口）。
- 前端同步去掉「赛事级别」：卡片徽章、列表筛选、详情三卡（`sm:grid-cols-3`）里的级别项全部移除，只留「赛事类别」；`list_competitions` / `list_hot_competitions` / `get_categories` 补 `is_active=1`，占位赛事不再泄漏到前台（实测列表 24 条、无省级）。

### 2.18 84 项全量补齐（2026-11-11 本地已执行，云端待同步）
```powershell
cd backend
# 1. 预演：本地索引优先，未命中/往年届次才走赛氪搜索兜底
.\venv\Scripts\python.exe -X utf8 init_84_races.py --dry-run
# 2. 真实落库（自动备份到 %TEMP%\kuo-verify）
.\venv\Scripts\python.exe -X utf8 init_84_races.py --apply --allow-production `
    --skeletons --stale-partial
# 3. 只看白名单匹配自检（0 网络）
.\venv\Scripts\python.exe competition_whitelist.py
```
- **结果**：84 项里主库原有 24 → 缺 60；`init_84_races.py` 解析出 **52 条赛氪可匹配**（本地索引 36 + 搜索兜底 16），**8 条赛氪未收录**（#7 医学技术技能、#29 地质技能、#60 水利创新设计、#73 企业模拟、#74 数智化沙盘、#80 RoboTac、#82 世界技能大赛中国选拔赛、#84 码蹄杯）建骨架行（官方名+官网+类目，`is_active=1`）。主库 **31 → 91**，前台可见 **24 → 84**，白名单覆盖 **84/84**。
- **防误配三重校验**（`_validate()`）：① 相似度 ≥0.85；② 剥掉年份/届次/`【榜单赛事】`/白名单内登记的赞助杯名（高教社杯/正大杯/科云杯…，`CUP_PREFIX` 一一对应序号）后，必须以官方核心名**开头**——「天府杯全国大学生数学建模竞赛」因此被否掉；③ 名称被官方名包含（官方名更长时兜底）；④ 命中白名单**显式别名**且 ≥6 字（兜「论坛(大赛) vs 大赛」「赛例 vs 案例」）。实测拦下 2 条真误配（#5 天府杯、#29 院内通知）。
- **往年届次处理**：赛氪详情报名结束超 400 天的按 `stale-partial` 处理——参赛对象/内容范围/封面照写，**报名与比赛日期一律清空**（`clear_dates()`），避免学生看到 2020/2022 的过期日期；20 条走这条路径。
- **赛氪搜索接口的合规边界**：`/api/pc/search/info` 对应 robots 禁止的 `/search`，只在 `init_84_races.py` 里做**一次性孤儿兜底**（≤16 次、6 s 间隔、`--no-search` 可全关），不进任何 cron。
- **`match()` 改为三趟**（2026-11-11 修复）：① 完全相等 → ② 键被名称包含 → ③ 名称被键包含。修的是「世界技能大赛」被 #82「世界技能大赛中国选拔赛」截走、导致 #81 在库里缺席的问题（等价类：`全国大学生机器人大赛` vs `-RoboTac`）。
- **学科分类**：`competition_whitelist.guess_category()` 输出**前端 33 个细分方向的原名**（不再自造类目），前端 `resolveCategory()` 一次命中；84 项里 `职业技能`（3 条）与 `农林食品`（1 条）原本没有对应方向，已补进 `competitionCategory.js` 的 `CATEGORY_TREE`。实测可见 84 条落在 **20 个类目**、`待确认` 归零。
- **数据丰度现状（如实记录）**：84 条中 **66 条**至少有「参赛对象/内容范围/考核方式/报名日期/封面」之一，**18 条全空骨架**（8 条赛氪未收录 + 10 条赛氪详情无字段或往年届次被清空）；`intro`/`summary` 仍为空——这两列靠 `generate_guides`/AI 生成，不属补齐脚本职责。

### 2.19 简称与简介补齐（2026-11-11 本地已执行）
```powershell
cd backend
# 2 字简称（走 llm，只补空值）
.\venv\Scripts\python.exe -X utf8 generate_short_names.py `
    --db-path competitions.db --allow-production --sleep 0.3
# 一句话简介 + 赛事介绍（走 llm，只补空值）
.\venv\Scripts\python.exe -X utf8 backfill_intro.py --apply --allow-production --sleep 0.4
```
- **注意 `--db-path`**：`generate_short_names.py` 的 `DEFAULT_DB_PATH` 指向 `test_crawl.db`，不带 `--db-path competitions.db` 会白跑一遍测试库（实测踩过）。
- **结果**：简称 **60/60** 补齐（如「码蹄」「世技」「机战」）；简介 **62/62** 补齐，`summary` + `intro` 均落库，用量 109 次调用 / 14.0 万 tokens。可见 84 条的 `summary`、`short_name` 缺口双双归零。
- **`backfill_intro.py`** 只管 `summary`/`intro` 两列，不碰日期、封面、参赛对象，也不生成整篇攻略（那是 `generate_guides.py`）；提示词明确「不编造年份/日期/数量」，默认 dry-run，写生产库要 `--apply --allow-production`，写前自动备份。
- **封面未做（已放弃一条路）**：试过按 `official_url` 抓 `og:image` 补 67 条缺口——6 个样本只有 1 个命中，且抓回来的是 `dycode.png` 二维码，`image_guard` 的 URL 黑名单（qr/qrcode/erweima/logo/icon/banner）拦不住这种命名，脚本已删除。当前 67 条赛事继续走 `coverFallback.js` 渐变兜底；若以后要补，正路是赛氪 `contest/lists` 的列表级封面字段（需重建本地索引带上 image），而不是逐站抓官网。
- **剩余缺口（18 条骨架的参赛对象/内容范围）**：赛氪详情本身没有这些字段，只能从官网通知页抓正文再抽取，属下一轮工作（见 §2.20）。

### 2.20 官网正文抽取补「参赛对象 / 内容范围 / 考核方式」（2026-11-11 本地已执行）
```powershell
cd backend
# 预演：抓官网首页 → 简介/规程/通知页，抽正文，模型只做抽取
.\venv\Scripts\python.exe -X utf8 backfill_official_fields.py --delay 2
# 落库：按 id 白名单只写审过的那批
.\venv\Scripts\python.exe -X utf8 backfill_official_fields.py --apply --allow-production `
    --delay 2 --ids "2,9,13,60,61,71,75,78,80,91,93,103,113,115,117,118"
```
- **流水线**：`official_url` → 首页正文 → 不合格则顺带抓 1–3 个「简介/介绍/章程/规程/通知/组织」页 → 命中页正文按关键词取 5200 字窗口 → `llm.chat_json` 只抽取三个字段。提示词硬约束「原文没有就返回空字符串、禁止补常识」，所以写入内容都能回指原文。
- **两道质量闸**：`good_text()` 要求正文 ≥380 字且出现「参赛」或「参赛对象/资格/面向」；`tidy()` 丢弃含「原文未/未具体写明/仅提及/页面未…」等元叙述的值、补齐句末标点、短于 12 字丢弃。
- **结果**：62 条有 `official_url` 的可见赛事跑完，**16 条写入**（含数学建模、电子设计、CURC、RoboTac、地质技能、世界技能大赛中国选拔赛）；可见行「全空骨架」由 **18 条降到 10 条**。其中 9 条抽取值含转述腔或截断，用一次性补丁（`workspace/fix_official_values.py`，未入库）改写成完整句，事实仍取自原文。
- **主动排除的源**：`65 智能汽车` 官网只有 `eepw.com.cn` 的 2012 年旧赛资料、`3 挑战杯创业计划` 与 `2 大挑` 抓到同一篇通稿、`110 世界技能大赛` 与 `118 中国选拔赛` 抓到同一页——重复/过期内容比留空更糟，宁可不写。
- **仍空的 10 条**：`4 大创年会`（ASP 导航壳）、`63 结构设计`/`98 化学实验`/`102 基础医学`/`106 华为算法`/`111 金砖技能`/`119 码蹄杯`（SPA 空壳）、`116 数智沙盘`（域名 `spbk.seentao.com` 已失效）、`112 医学技术技能`/`114 水利创新设计`（本来就无 `official_url`）。这些要么人工补官网，要么等官方页面改版，脚本层面无解。
- **注意**：脚本默认会跑「三个字段没全填」的所有行（不只 18 条骨架），dry-run 时先看报告再 `--ids` 白名单落库；`--apply` 会自动备份生产库。

---
## 三、赛氪数据源事实与合规边界
- Vue SPA，HTML 只有 4.6 KB 空壳，**必须走内部 JSON API**：列表 `GET https://apiv4buffer.saikr.com/api/pc/contest/lists?page=&limit=&sort=&class_id=&level=`（`sort=2` 最近更新）；详情 `GET https://apiv4buffer.saikr.com/api/pc/contest/info?contest_id=<id>`。匿名可调，无 Token / 签名 / 强制 Cookie；`acw_tc` 只是阿里云 WAF 被动标记。
- **主列表接口没有关键词搜索参数**（只有 page / limit / sort / class_id / level）。另有关键词搜索接口 `GET /api/pc/search/info?type=0&keyword=<kw>&page=&limit=`（2026-11-11 实测可用，见 10.4），但它对应的 `/search` 页面被 robots 禁止 → **合规上不得用于批量名称反查**，所以在「审核后补全」里只能降级用本地索引，这是该降级的前提。
- `robots.txt`：`Disallow: /api/`、`Disallow: /search`、`Disallow: /*?*`、`Crawl-delay: 10`。技术上违反 robots，定位是「低频、低风险、**非授权**」；前提：不商用、署名回链、只存 ID+名称+URL、间隔 ≥3 秒（建议 10）、每日 ≤2 页 20 条、遇 429/验证码立刻停。结论：只做**存量 32 条补全 + 每日 20 条增量候选**，不做全量抓取。
- **`image_guard` 三条拒绝规则**（所有写封面入口共用 `check_image()`）：
  1. **URL 黑名单**：`qr / qrcode / erweima / logo / icon / banner`（子串 + `qr`/`ewm` token 匹配）→ 拒；
  2. **尺寸与比例**：最小 300×150，宽高比必须在 0.45–2.6；超 2.6 判 banner，超长竖图（高/宽 >2.2）→ 拒；
  3. **死链 / 非图片**：HTTP 非 200、`Content-Type` 非图片、体积 >3 MB、解析不出尺寸 → 拒；能解出二维码/条码（zxing-cpp）→ 拒。
  网络超时、5xx 归 `transient`：不删库内已有图，只标「待复查」。
- 往年届次护栏：报名结束超过 400 天视为陈旧，整条跳过（id=2/4/17/26/30；`--allow-stale` 才强制）。限速：赛氪 3–5 秒、通知页 5–10 秒、新闻批量 2–5 秒，**严禁调小**。

---
## 四、使用隔离红线
1. 测试优先 `--db-path .\test_crawl.db`；只有明确针对生产库的任务（批量补全、合并、审核入库）才写 `competitions.db`。
2. 写生产库三件套：`--apply`（默认 dry-run）+ 显式 `--allow-production` + **写前自动备份成功**，缺一即拒。
3. 批量模式在 `db.init_db()` 之前就拦截生产库；`migrate_production.py` 的只读体检不得改成写连接。
4. 不要为「看效果」随手启后端：`main.py` 启动会 `db.init_db()`（幂等补列 + 回填 summary/tech_stack），会改库文件字节、让 SHA 变，排查时易误判。
5. 禁止对生产库手工 `DROP` / `DELETE` / 重建表；改数据先备份，再用 `UPDATE` 精确改字段。每次写库前先看脚本打印的 `[目标库] ...`。
6. `verify_crawl.py` 拒绝查询生产库；校验 awards 请指向测试副本。
7. **图片只存 URL，不下载落盘**（闸门只读前 3 MB 字节判尺寸）；抓取遵守 robots 与转载规定，延迟不许调小。
8. 封面宁缺毋滥：闸门不合格就不写，绝不为「有图」放宽宽高比塞 banner。
9. 比赛时间宁缺毋滥：Prompt 已规定「报名/材料提交截止不算比赛时间」；发现误读就回滚该字段并记录。
10. 每次写库后更新本文件的 SHA 快照；历史快照只用于识别最老版本备份。
11. 赛氪写入策略：`reg_*` **只填 NULL**、`image_url` 只补空且过闸门、`contest_*` 只填空。16 条报名冲突一律保留库内值。
12. 赛氪只做低频小批量（每日 2 页 / 20 条，3–5 秒间隔）；扩量或商用前重新做合规评估。
13. 发现脚本只写 `competition_candidates`（`status=pending`），**禁止自动 INSERT/UPDATE `competitions`**；入库必须走 `review_candidates.py` / `bulk_approve.py` / 后台，禁止手写 INSERT。
14. AI 生成只能走 `ai_content` + `ai_jobs` 抢占，**禁止绕过任务表直接写 `full_process_guide`**；按需接口必须保留 `Depends(get_current_user)`，不许改回匿名。
15. `claim_ai_job` 拿写锁后会再查攻略是否为空，非空直接返回 `exists`；不做覆盖式重刷，`--force` 只在人工单条使用，**cron 永不带 `--force`**。
16. 攻略长文只留在详情接口：不要把 `full_process_guide` 加回 `Competition`（列表）模型。
17. 孤儿数据只删无归属行（`competition_id IS NULL OR NOT IN (SELECT id FROM competitions)`），不要扩成「按日期删」。
18. `.env` 不进仓库；`.db`、`.db.bak-*`、`workspace/` 都是本地产物，不提交。

---
## 五、管理后台
### 5.1 设计目标与权限模型
把「审核候选赛事」和「处理用户反馈」从命令行搬到网页。**双层拦截**：

| 层 | 位置 | 行为 |
|---|---|---|
| 前端守卫 | `frontend/src/router/index.js`，路由 `meta: { requiresAuth: true, requiresAdmin: true }` | 无 token → 弹登录；有 token 非管理员 → `showToast('无权限访问管理后台')` + 回 `/` |
| 前端入口 | `frontend/src/components/Header.vue` | 「管理后台」链接 `v-if="isAdmin"`（`authState.user?.is_admin`） |
| 后端 | `backend/security.py::get_current_admin()` | 每个 admin 接口挂 `Depends(get_current_admin)`，非管理员 403 `{"detail": "需要管理员权限"}`。**前端守卫只是体验层，真正的墙在服务端** |

管理员标记在 `users.is_admin`（INTEGER DEFAULT 0，`schema_v2.sql` + `db.migrate_user_columns()` 幂等补列）；`routers/auth.py` 的 `UserInfo.is_admin` 随登录 / `/api/auth/me` 返回，前端只认这个字段。
### 5.2 后端接口（`backend/routers/admin.py`，全部挂 `/api/admin`）
| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/admin/candidates?limit=&offset=` | 候选列表（默认只取 `status='pending'`），返回 `{code, total, items}` |
| POST | `/admin/candidates/{id}/approve` | 走 `db.promote_candidate()`：重名/`check_image()` 闸门 → 同事务写 `competitions` + 标记 `approved`；**随后同步补 2 字简称 + 赛氪详情/日期/封面（见 5.6）**；返回新赛事 id、`short_name`、`enrich` 结果与提示语 |
| POST | `/admin/candidates/{id}/reject` | 只标记 `rejected`，不动主表 |
| GET | `/admin/feedback?limit=&offset=` | 反馈列表（新的在前）+ `unread` 未读数；`username` 由 `users` 联查，注销用户回退「已注销用户」 |
| PUT | `/admin/feedback/{id}/read` | 标记已读；`busyId` 防重复点击，前端同步减角标 |
| GET | `/admin/competitions?limit=&offset=&keyword=` | 正式赛事列表（id 倒序，轻字段 + `awards_count`），供后台「赛事管理」分页 |
| DELETE | `/admin/competitions/{id}` | 永久删除赛事：单事务级联清 `awards / ideas / insights / user_preparations / ai_jobs / ai_analysis_cache / ai_usage`，候选表 `competition_id` 置 NULL；日志 `[管理员操作] 删除了赛事：{name}`（见 5.8） |

约定：候选不存在 → 404，状态不是 `pending` → 400 `"该候选已处理"`；approve 复用 CLI 同一套 `_build_competition_data` / `_infer_level` / `_clean_name`，网页与 CLI 写库结果一致。`main.py` 的 CORS 必须允许 `PUT`，否则「标记已读」预检直接挂。
### 5.3 前端页面（`frontend/src/views/AdminView.vue`）
- 布局：`Header` + 左 220px 侧边栏（候选赛事 / 用户反馈 / 赛事管理，带待审、未读、赛事总数角标）+ 右内容区；`lg` 以下堆叠为横向 tab。沿用全站 Tailwind 暗色/极简风，**未引入 Element Plus 等 UI 库**。
- 候选赛事表格列：赛事名称（含来源链接）/ 级别 / 来源 / 抓取月份 / 操作；来源映射 `saikr→赛氪`、`edu-roster→教育部门名录`、`manual→人工录入`，未知值原样显示。用户反馈为卡片列表，未读高亮蓝底 + 「未读」角标，正文 `whitespace-pre-wrap break-words`。
- **二次确认**：通过/拒绝都会写主库，点按钮先弹轻量弹窗（标题「确认审核通过这个赛事？」/「确认拒绝这个赛事？」，正文带《赛事名》+ 后果说明），按钮【取消】/【确认通过】（拒绝态转红 `bg-red-600`）；提交中禁用并显示「提交中…」；支持 `Esc` / 点遮罩关闭；`Teleport to="body"` + `Transition` 淡入。
- 深链 `/admin?tab=feedback` 直开反馈面板，切 tab 用 `router.replace` 同步地址栏；首屏两个面板各拉一次列表（两个角标需要两份数据）。状态齐全：骨架屏、空态（「没有待审核的候选赛事，全部处理完了 🎉」/「还没有用户反馈」）、错误横幅 + 「刷新」、`busyId` 行级 loading。
- **踩坑（保留）**：`main.js` 启动调一次 `restoreSession()`，`/admin` 守卫也要调；原实现遇 `loading=true` 直接 `return null`，守卫误判未登录 → 管理员刷新 `/admin` 被弹登录框（后端日志却是 `/api/auth/me` 200）。现改为 `services/auth.js` 的 **in-flight Promise 去重 `inflightRestore`**。以后新增「守卫里查登录态」的路由都走它，别自己发请求。
### 5.4 `promote_admin.py` 用法（授权 / 撤权 / 查看）
```powershell
cd D:\agent-lab\KuAo-Platform\backend
# 查看当前所有管理员
venv\Scripts\python.exe promote_admin.py --list
# 授权（按用户名；用户必须已存在，否则报错退出）
venv\Scripts\python.exe promote_admin.py xas
# 撤权
venv\Scripts\python.exe promote_admin.py --revoke xas
```
- 只改 `users.is_admin`，**不碰密码**；改完让用户重新登录或刷新页面才生效（`/api/auth/me` 会带回新 `is_admin`）。
- 支持 `--db-path` 指向别的库（默认 `competitions.db`）。
- 现状：`tester_e2e`(id 1)、`xas`(id 4) 是管理员；`alice`(id 2) 保留为**非管理员越权测试账号**，别顺手提权。
### 5.5 本地验收清单
```powershell
# 0) 服务（两个都要起）
cd backend; venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
cd frontend; D:\Tools\node.exe D:\Tools\node_modules\npm\bin\npm-cli.js run dev
```
1. `http://localhost:5173`（**必须 localhost**，Vite 不监听 127.0.0.1）用 `xas` 登录 → 头像下拉 →「管理后台」，或 `/admin` 直达。
2. 候选赛事：点【通过】→ 弹确认框 →【确认通过】→ 该行消失、Toast、`competitions` 多一条；【取消】/`Esc` 不产生任何请求。
3. 反馈面板：未读角标数字 = 未读条数，点【标记已读】→ 卡片变灰、角标 -1、`feedbacks.is_read=1`。
4. 越权复验：`alice` 登录访问 `/admin` → Toast「无权限访问管理后台」并落回首页；用 `alice` 的 token 直连 `GET /api/admin/candidates` → 403。
5. 造测试反馈（**只对本地库**）：`backend/tests/tools/seed_feedback.py` 插 2 条中文反馈（`xas` / `alice` 各一条，未读），验收完 `DELETE FROM feedbacks WHERE id IN (...)` 清掉。
6. 自动补全复验：点【通过】后按钮保持「提交中…」20~60 秒，同时到后端控制台看 `[审核补全]` 三行日志；完成后 Toast 形如「已通过并入库：XX（简称=机人；详情补全 2 项（另补日期 2 项））」；查库确认 `short_name` / `participants` / `reg_start_date`。看到「详情未补（无赛氪 contest_id）」属**预期降级**（见 5.6）。

**已知边界**：列表暂无分页（`limit=200`，候选过千要加）；反馈无删除/回复；审核结果不能从网页撤销（需 CLI 或改库）。
### 5.6 审核后自动补全（2026-11-10，修复「审核通过即空壳」）
**症状**：后台点【通过】后，卡片只显示首字（无两字简称），详情页全空（无参赛对象 / 时间 / 攻略上下文）。

**根因（三条，缺一不可）**：

1. `routers/admin.py` 当初为「点击即响应」**刻意不调用**简称与详情（原 docstring 写着「通过后不自动生成简称 / 不自动抓详情」），而 `review_candidates.py`（CLI，L260/L264）与 `bulk_approve.py`（L134/L135，需 `--with-ai`）都调用了 —— **CLI 改了，API 没跟上**。
2. `enrich_competition()` 的 `DETAIL_FIELDS` 只有 `participants / content_scope / exam_schedule`，**不含 `image_url` 与双轨制日期**（`reg_start_date` 等），即使调用了它，详情页时间/封面依旧空。
3. 附带：`saikr_map.json` 是按 `competition_id` 生成的静态映射（32 条存量），新入库赛事不在其中；57 条 pending 全是 `edu-roster`、`raw_json` 无 `contest_id`，名称能对上映射的仅 1 条。

**修复**：

| 文件 | 改动 |
|---|---|
| `routers/admin.py` | 新增 `_auto_complete_after_approve()`：① `short_names.extract_short_name()` + `db.save_short_name()`；② `enrich.enrich_competition(contest_id 取自候选 source_key 尾部数字 / raw_json.contest_id)`。异常只 `logger.warning("[审核补全] …")` 并在响应回传原因，**不回滚、不抛 500**（路由是 `def`，FastAPI 放线程池，阻塞期不挡其它请求）；新增 `_auto_complete_summary()` 生成 toast 文案，响应新增 `short_name` / `enrich{status,written,extra,reason}` |
| `enrich_saikr_details.py` | 新增 `fill_dates_and_cover()`：按 `SAIKR_DATE_KEYS`（`regist_start_time / regist_end_time / contest_*_time`）补空的日期列；按 `SAIKR_COVER_KEYS`（实测字段 `web_pic_big`）取封面且**必须过 `image_guard.check_image()` 闸门**。只补空值，结果放 `extra{dates,cover}` |
| `db.py` | `update_competition(cid, data, db_path=None)` 支持指定库（原写死模块级 `DB_PATH`，CLI 指定测试库时会写错库） |
| `frontend/src/services/api.js` | `approveAdminCandidate()` 超时 **60s → 180s**（补全常见 20~60s，默认超时会把已入库成功的请求判成失败） |
| `frontend/src/views/AdminView.vue` | 审批前先 toast「审批中：正在补全简称与赛氪详情（约 20~60 秒）…」，完成后显示 `data.message` |

**实测**（隔离库 `%TEMP%\kuo-verify\approve_api_test.db`，真实 HTTP + Admin Token，未写 `competitions.db`）：

| 候选 | 结果 | 耗时 |
|---|---|---|
| 候选 3《全国大学生电子设计竞赛》（无 saikr 映射） | `short_name=电设`；`enrich.status=no_mapping`（原因「无赛氪 contest_id」）；message 明示「详情未补…已入库待补」 | 2.3s |
| 候选 26《中国机器人大赛暨RoboCup机器人世界杯中国赛》（映射 saikr 58028） | `short_name=机人`；写入 `participants`、`exam_schedule`；`extra.dates=[reg_start_date=2026-03-01, reg_end_date=2026-04-30]` | 5.9s |

后端日志（排障入口）：
```text
[审核补全] competition=53 简称=机人
[审核补全] competition=53 开始抓取赛氪详情（contest_id=58028，约 20~60 秒）
[审核补全] competition=53 详情已写入 2 项：参赛对象、考试安排
```
**封面仍可能为空的真实原因（不是 bug）**：赛氪 `web_pic_big` 直链对任何请求头（裸 / UA / UA+Referer）均返回 **HTTP 404**，`image_guard` 判「死链」拒收 → 保持 NULL，走前端无图兜底。封面正路仍是 `batch_update_competitions.py` 从**官网**抓。

**范围决策已变更（2026-11-10 晚）**：原定「方案 B 只保底、不做名称解析」，但现场确认 57 条 pending **全部**是 `edu-roster`、`source_url` 全指向同一目录页，`no_mapping` 等于永远空壳 → 改为**按名称匹配赛氪**，见 5.7。

**回归与自测脚本**：`pytest test_enrich_saikr.py` → 11 passed（新逻辑只补空值，未破坏原断言）。`backend/tests/tools/` 下新增/收纳 `prep_test_db.py`（复制生产库并选样）、`run_api_patched.py`（用隔离库起服务）、`smoke_approve.py`（POST 审批 + 回读字段）、`find_enrichable_candidate.py`（找可补全样本），临时库统一落 `%TEMP%\kuo-verify\`。

### 5.7 名录候选按「名称」匹配赛氪（2026-11-10 晚重构）

**问题**：`edu-roster` 候选的 `source_url` 全是同一个目录页 `https://www.sdaeu.edu.cn/jwc/info/1047/2528.htm`，用它当锚点只能得到 404；赛氪列表接口**不认关键词**（实测 `keyword / search / title / name` 全被忽略，`total` 恒为 5164），所以只能自建本地名称索引。

| 组件 | 说明 |
|---|---|
| `saikr_match.py` | ① 索引：`/api/pc/contest/lists` 100 条/页 × 52 页、1.2s 间隔（实测 80s，5164 条）→ `saikr_index.json`；② 匹配：名称归一化 + 相似度；CLI `--build / --query <名称> / --stats` |
| `saikr_index.json` | 索引产物（`contest_id / name / norm / url / level`），14 天过期自动重建；审批时只读本地，**不每次联网** |
| `enrich_saikr_details.py` | `enrich_competition(..., allow_name_match=True)`：无 contest_id 时先查 `saikr_map`，再按名称匹配；命中记 `match="名称匹配(exact/contains/fuzzy/…)"`，未命中返回 `status="no_match"` + 「通过名称未匹配到赛氪」（CLI 默认仍为 `no_mapping`，行为不变） |
| `routers/admin.py` | 审批调用改传 `allow_name_match=True`，**明确不使用 `source_url`**（代码注释写明目录页不可当锚点）；日志把 `contest_id` 显示为「待按名称匹配」 |

**归一化与阈值**：剥 `【倒计时4天】` 类前缀、第 X 届、年份、括号补充、标点空格；相似度阈值 0.86，包含关系按长度占比加分；第一名与第二名差 < 0.02 判为歧义**直接放弃**（宁缺毋错，避免把 A 赛事详情写到 B）。

**实测**（隔离库，真实 HTTP）：

| 名称 | 结果 |
|---|---|
| 全国大学生统计建模大赛（候选 47） | `contest_id=57763`（exact 1.00，赛氪名「2026年第十二届全国大学生统计建模大赛」）→ 简称「统模」+ `participants` + `exam_schedule` + 4 条日期 + 封面，**6/7 关键字段非空** |
| 全国大学生电子设计竞赛 / 中国大学生工程实践与创新能力大赛 / 中国机器人及人工智能大赛 / 全国大学生数字媒体科技作品及创意竞赛 / 全国大学生数学建模竞赛 | 全部 exact 命中 |
| 「挑战杯」中国大学生创业计划大赛 | 未命中（赛氪上是「课外学术科技作品竞赛」，属不同赛事）→ `no_match` 降级 |

**历史空壳清理**：`cleanup_directory_shells.py`（默认 dry-run）识别「`edu-roster` 已通过 + 主表详情全空」，`--apply` 后候选退回 `pending`（`competition_id=NULL`）并删除主表行；已挂 `awards` / `user_preparations` 的行自动跳过。2026-11-10 清理 **5 条**（挑战杯 / 电子设计 / 工程实践 / 机器人及AI / 数字媒体），本地 54 → **49** 行，pending 52 → **57**。

**定时刷新**：`0 4 1,15 * * … saikr_match.py --refresh-if-stale`（详见 9.2）——每月 1 号 / 15 号 04:00，半月一次；索引仍新鲜时脚本 0 请求退出，**不要改成每日**（52 页/次有封 IP 风险）。

**前端**：`CompetitionDetailView.vue` 新增 `isRealOfficialSite()` —— 名录站域名（`sdaeu.edu.cn`）与 CMS 通知页路径（`/info/<数字>/<数字>.htm`）**不渲染「官网」按钮**，改显示纯文本「来源：教育部门名录」；真实官网不受影响（`frontend/tests/official-url.check.mjs` 7 例全过）。

### 5.8 赛事管理：删除赛事 + 空壳清理（2026-11-10 深夜）
**产品决策**：赛氪都没收录的冷门赛事大概率为「水赛」，不再全网搜，直接清理；后台给管理员一个可视化删除入口。

**清理脚本 `backend/cleanup_empty_shells.py`**（默认 dry-run）
- 判定：`image_url` / `participants` / `reg_start_date` **三字段全空**（`COALESCE(TRIM(x),'')=''`，NULL、空串、纯空白都算空）。
- 保守口径：默认只删「无 awards / ideas / insights / user_preparations / ai_jobs」的空壳；带数据的先保留待 enrich 补字段，加 `--include-with-data` 才会连带删。
- `--apply` 前自动备份到 `%TEMP%\kuo-verify\competitions.bak-<时间戳>.db`；单事务删 7 张子表 + `competition_candidates.competition_id` 置 NULL。
- 实测：51 条里三字段全空 13 条 → **删 5 条**（全球校园AI算法精英大赛 / 信息安全竞赛·省赛区 / 中国教育机器人大赛 / 数学建模竞赛 / 未来设计师）**51 → 46**；保留 8 条（挑战杯、计算机设计大赛、信息安全竞赛等，共 20 awards / 4 ideas / 3 insights / 1 备赛计划）。

**接口**（见 5.2 表）
- `GET /api/admin/competitions`：分页 + `keyword` 模糊搜索，返回 `{code,total,limit,offset,items}`，`source` 取 `discover_source`（`saikr:list` / `edu:roster`）。
- `DELETE /api/admin/competitions/{id}`：`db.delete_competition_cascade()` 同事务级联，返回 `{name, deleted:{表:条数}, candidates_unlinked, message}`；不存在 → 404。子表清单常量化在 `db.COMPETITION_CHILD_TABLES`，与清理脚本保持同一口径（新增关联表两处都要补）。

**前端 `AdminView.vue` 第三个 tab「赛事管理」**
- 表格列：赛事名称（链到详情页新窗口）/ 级别 / 来源 / 获奖数 / 入库月份 / 操作；分页 10 条/页，`上一页 / 第 x/y 页 / 下一页`，删掉当前页最后一条自动回退一页。
- 红色【删除】→ **自定义二次确认弹窗**（标题「确认要永久删除吗？」，正文《赛事名》+「此操作不可恢复！」+ 将连带删除的获奖条数），【取消】/【永久删除】（红），提交中禁用，支持 `Esc` / 点遮罩关闭，成功后 Toast「已删除《XXX》」并刷新列表。

**验证**：TestClient 副本库 15 项断言全 PASS（分页/关键词/403/401/404/级联 awards=8/候选解绑/总数收敛）；浏览器 E2E 在隔离库上跑通「点删除 → 弹窗 → 永久删除 → 列表 46→45 + Toast」，生产库计数不变。**注意**：隔离后端必须用 `backend/tests/tools/run_api_patched.py`（它才读 `KUO_DB_PATH`），直接 `uvicorn main:app` 会指向生产库。

> **踩坑（2026-11-10 夜，已修复）**：E2E 探针误连生产库，两次跑各删掉当时列表首行（#59 统计建模、#48 环保知识大赛），均从备份 `competitions.db.bak-*` 完整回填（含候选绑定）。教训：探针只准连副本库，跑前先 `SELECT COUNT(*)` 留底；删除类 E2E 一律 `run_api_patched.py --port 8010` + `--db-path` 副本。

---
## 六、前端展示规范（已定稿，勿回退）
- **列表卡片**（`components/CompetitionCard.vue`）：`cover-media` 固定 `aspect-[16/9]`，图片 `w-full h-full object-cover object-center`——海报式铺满、不留白、不变形；布局 `flex-col sm:flex-row`，桌面端图占 42%、上限 430px。
- **详情页头图**（`views/CompetitionDetailView.vue`）：不设死高度，`w-full h-auto` + `max-height: 400px` + `object-contain`，容器底色 `#f8f9fa`（暗色 `slate-800/60`）承接留白——**任何情况下不裁掉海报文字**。
- **无图兜底**：`utils/coverFallback.js` 生成独立渐变层（赛事名哈希 → 稳定色相 0–359，`data-tone` 按学科分色），优先显示 `short_name` 两字简称，缺则取首字（跳过引号/括号）；样式在 `assets/main.css` 的 `.cover-fallback / __mark / --hero`，暗色同步；`@error` 加载失败也走兜底，换图自动重试。
- **三主题**（`composables/useTheme.js`）：`STORAGE_KEY = 'theme-v2'`，主题 `official`（默认，校园底图 + 半透明白）/ `minimal`（纯白留白）/ `tech`（深色底 + 蓝色高光）；id 同步到 `<html>` 的 `theme-*` class，**tech 额外加 `.dark`** 复用 Tailwind `darkMode: 'class'` 的全部 `dark:` 工具类；旧 `theme` 键的 `dark/light` 一律回落 `official`；`index.html` 内联脚本在 CSS 加载前写 class，防首屏闪白。
- 时间展示：`services/timeFormat.js` 统一；有日期轨显示到「日」，只有月份轨显示「月」（同月 → `11 月`，跨月 → `10 月 - 11 月`，跨年 → `12 月 - 次年 1 月`，缺值 → `待定`）。封面统一走 `resolveImage()`，只接受 `http(s)` 绝对地址。
- **详情页「有没有时间」必须查两条轨**：`CompetitionDetailView.vue` 的 `hasRegTime / hasContestTime` 要同时检查 `parseDate(*_date)` 与 `toMonth(*_month)`（11-10 修过一次只查月份的 Bug，表现是「列表卡片有 2026-04-01、详情页却写暂未收录」）；文案统一由 `regTimeText / contestTimeText`（`formatSignUp / formatContest`）产出，模板里不要再直接读 `detail.signUpTime`。
- 列表接口不返回长文；攻略按钮未登录先 `openAuthModal('login')`，401 由拦截器清凭证并广播 `auth:unauthorized`。

---
## 七、未完成项与已知边界
| 问题 | 状态 | 位置 | 备注 |
|---|---|---|---|
| 云端代码/前端未同步 | 未处理 | 云端 `/root/KuAo`、`/var/www/html` | 云端只到 `81e7686`，缺详情三列、AI 提纯、简称/日期补全；上传新 `dist` + 拉代码 + `systemctl restart kuao-backend` |
| 账号串号（P0 越权） | 已修复（本地） | `tokenStore.js`、`auth.js`、`api.js`、`security.py` | token 改 `sessionStorage` 按标签页隔离、启动清旧 `localStorage`、`plan_v2_${uid}_${cid}` / `prep_favorites_v1_${uid}` 按用户分区、`/api/*` 加 `Cache-Control: no-store, private`、`decode_access_token` 强制 `exp`/`sub`；回归 `test_auth_isolation.py` 10 例。云端部署后必须双账号复验 |
| 顶部搜索框随分辨率变形 | 已修复 | `assets/main.css` `.header-search-box` | `width/min-width: 240px` + `flex-shrink: 0`；云端 dist 更新后按 360/768/1024/1440 复验 |
| `api.js` 反馈双前缀（`/api/api/feedback`） | 代码已修 | `services/api.js::submitFeedback()` | 已改为 `api.post('/feedback')` 并加注释；**云端旧 dist 未重建时仍会 404**，部署后复验 |
| 详情三卡多行列表不换行 | 未处理 | `CompetitionDetailView.vue` | AI 输出为「1. 短句」多行，`<p>` 需 `whitespace-pre-line` |
| 备份未归置 | 未处理 | `backend/*.db.bak-*` | 上云后挪 `backups/`，避免日志清理 `find` 误伤 |
| 测试库 schema 落后 | 已知 | `test_crawl.db` | 缺 `*_date` 四列；先 `db.init_db('test_crawl.db')` 再跑日期脚本 |
| 16 条报名时间冲突 | 待裁决 | `workspace/pipeline_report.md` 第三节 | 默认保留库内值，改用赛氪值须单条人工确认 |
| id=34 报名 3-7 混合来源 | 待确认 | `competitions` id=34 | 赛氪窗口 3-4；现为 `reg_start_month=3`（补）+ `reg_end_month=7`（原值） |
| 低置信比赛时间 | 待复核 | id=9（6-11 月，置信 0.45）、id=2（只给「9 月起」） | 抽检时优先看 |
| 新赛事无赛氪映射 | 已缓解 | `saikr_map.json` + `saikr_index.json` | 名录候选改为按名称匹配本地赛氪索引（5.7）；抽样 6/7 exact 命中，「挑战杯」创业计划大赛仍 `no_match`（赛氪上是另一赛事），属正常降级 |
| 名录空壳赛事 | 已清理 | `cleanup_directory_shells.py` | 11-10 退出 5 条（候选已回 pending），后续新审批走名称匹配不再产生空壳 |
| 赛氪封面 404 | 非 bug | 赛氪 `web_pic_big` | 任何请求头均 404，闸门判死链；封面正路是官网抓取 |
| 后台列表无分页 | 已知边界 | `routers/admin.py` | `limit=200`，候选过千要加 |
| 反馈无删除/回复；审核不可网页撤销 | 已知边界 | `AdminView.vue`、`admin.py` | 需 CLI 或直接改库 |
| `chroma_db/` 仍被 git 跟踪 | 未处理 | 仓库根 | 约 5 MB、可重建；用 `git rm -r --cached chroma_db` + 重建索引 |
| 时间格式化重复代码 5 处 | 未处理 | 5 个视图各自的 `toMonth/formatSignUp/formatContest/resolveImage` | 冻结期不动，之后抽 `services/competitionFormat.js` |
| 占位赛事 id=33「华为杯」 | 保留 | `test_crawl.db` | 用户要求留作测试用例，别并入生产 |
| 服务/广告类候选 | 待 reject | 候选池 | 「辅助报名」「考试入口」「作品征集」等，能抓到也别提拔 |

---
## 八、测试与验证
```powershell
# 单测（不连网、不写库）
.\venv\Scripts\python.exe -m pytest test_crawler_units.py -q                 # 19 passed
.\venv\Scripts\python.exe -m pytest test_crawler_boundaries.py -q            # 20 passed
.\venv\Scripts\python.exe -m pytest test_ai_guide.py test_api_boundaries.py test_crawler_units.py -q
.\venv\Scripts\python.exe -m pytest test_enrich_saikr.py -q                  # 11 passed
.\venv\Scripts\python.exe -m pytest -q                                       # 11-08 20:02：138 passed / 2 skipped（之后新增的 test_image_guard 等未计入）
.\venv\Scripts\python.exe -m pyflakes ai_content.py ai_jobs_runner.py daily_ai_worker.py generate_guides.py cleanup_orphan_ai.py db.py routers\competitions.py
# 前端构建（PowerShell 里 npm.ps1 会被执行策略拦，必须 npm.cmd 或直调 node）
cd ..\frontend; npm.cmd run build
cd ..\frontend; D:\Tools\node.exe D:\Tools\node_modules\npm\bin\npm-cli.js run build
```
**测试工具脚本用途**（`backend/tests/tools/` 与 `frontend/tests/`，均已纳入版本库；临时库统一落 `%TEMP%\kuo-verify\`）

| 脚本 | 用途 |
|---|---|
| `backend/tests/tools/prep_test_db.py` | 复制生产库 → `%TEMP%\kuo-verify\approve_api_test.db`，挑一条可审样本并保证库里有管理员账号 |
| `backend/tests/tools/run_api_patched.py` | 用隔离库起后端（`KUO_DB_PATH=<库> KUO_PORT=8010`），绝不写 `competitions.db` |
| `backend/tests/tools/smoke_approve.py` | 走真实 HTTP 审批一条候选，打印响应与入库结果（`python smoke_approve.py <candidate_id> [base_url]`） |
| `backend/tests/tools/verify_admin.py` | 管理后台接口验收：反馈已读 / 越权 403 / 通过 / 拒绝 / 重复审批 400，临时副本库上跑 |
| `backend/tests/tools/find_enrichable_candidate.py` | 找出「应能补全成功」的 pending 候选（`raw_json` 带 `contest_id` 或名称能对 `saikr_map`） |
| `backend/tests/tools/seed_feedback.py` | 往**本地**库插 2 条测试反馈（勿在生产执行） |
| `backend/tests/tools/check_smart_order.py` | 智能排序回归：合成 17 行边界数据，断言三阶段 / 月份兜底 / 脏值沉底 / 热门筛选 / limit 收敛 |
| `backend/tests/tools/stats_dates.py` | 盘点日期轨覆盖率（主表 + 候选池） |
| `backend/tests/tools/audit_cover_fallback.py` | 审计兜底在真实数据上的表现：多少卡片走兜底、简称是否撞名/撞形、哈希色相是否撞色 |
| `backend/tests/tools/wcag_contrast.py` | 校验封面兜底配色的 WCAG 对比度（纯计算，不看图），口径对齐 `main.css` |
| `frontend/tests/timeformat.check.mjs` | Node 校验 `timeFormat.js`（日期/月份/倒计时/跨年/非法日期）：`node frontend/tests/timeformat.check.mjs` |
| `frontend/tests/official-url.check.mjs` | Node 校验详情页「官网链接」判定（名录目录页/非法协议必须拒绝，真实官网放行） |

**验收口径**：改动后至少跑 `py_compile` + 相关单测 + 一次 dry-run；写库改动先在隔离库（`%TEMP%\kuo-verify\*.db`）跑通再碰正式库。`.verify/` 已进 `.gitignore`，只作本机临时取证目录，**新脚本不要再放进去**。

---
## 九、云端部署与运维
### 9.1 线上现状
| 项 | 值 |
|---|---|
| 服务器 | 阿里云 Ubuntu 24.04.2 LTS · `112.126.90.252` · 1.6 GiB RAM（+2 GiB Swap） |
| 部署方式 | **原地升级** `/root/KuAo`（未用 `/opt/kuao` + 独立用户；systemd 以 root 运行） |
| 发布 commit | `81e768633e420163982bda692239e8dd590760af`（2026-11-08 14:33 切换，15:29 cron + 验收完成） |
| 服务 | systemd `kuao-backend`（`127.0.0.1:8000`，`--workers 1`）+ Nginx `:80` 反代 `/api/` |
| 前端根 | **`/var/www/html`**（不是 `/opt/kuao/frontend/dist`，也不是 `/root/KuAo/frontend/dist`；以 `nginx -T` 的 root 为准） |
| 备份 | `/root/KuAo/backend/backups/`（保留 30 天）；切换前备份 `/root/kuao-backups/20261108-143324/` |
| 回滚 | `bash /root/kuao-rollback.sh 20261108-143324`；代码 `git checkout 3ee9dd5`；库用上述备份目录里的 `competitions.db` |
### 9.2 crontab（时区 `Asia/Shanghai`；Ubuntu cron 不支持 `CRON_TZ`，必须设系统时区）
云端现有 5 条任务 + 本轮新增 1 条（名称索引半月刷新，随下版部署写入；完整命令与 `flock`/日志切分见 `CLOUD_DEPLOYMENT_GUIDE.md` 第 7.4 节）：
- 02:30 `backup_db.py` 备份 → 04:10 `discover_saikr.py --apply --allow-production`（只写候选）→ 04:40 `daily_ai_worker.py --limit 4 --idea-limit 2 --budget-tokens 300000` → 周日 05:20 `pipeline_update.py --limit 20` → 05:40/05:50 清理日志与旧备份（只清 `logs/`、`backups/`）。
- **04:00（每月 1/15 日）`saikr_match.py --refresh-if-stale`**：刷新 `saikr_index.json`（52 页 × 1.2s ≈80s），**刻意压到半月一次防封 IP**；索引新鲜时只读本地、0 请求直接退出，重复跑无副作用；`flock -n locks/saikr_index.lock`，日志 `logs/saikr-index-YYYYMM.log`。
- 新闻采集**未挂**：`LIST_URL` 确定后按同一模板加一条（周一/四 03:00，`--max-pages 4 --allow-production`）。`discover_university.py` 不挂日任务，名单年度更新时人工跑一次。
- **顺序不能反**（备份在所有写库之前）；先逐条手动执行确认退出码 0 再挂 cron，`crontab -l` 留档，连续失败 2 次暂停。
- 线上实际路径是 `/root/KuAo`，示例中的 `/opt/kuao/backend` 按实际改；**将来迁到 `/opt/kuao` 必须同步改 systemd `WorkingDirectory`、cron 的 `cd`、日志/备份路径**，并先停服务、删 `-wal/-shm` 再移库。


### 9.3 ~~本地 → 云端库覆盖~~（**已废止，见 9.4**）

> ⚠️ 本节旧流程（本地 `backup_db.py` 出快照 → `scp` → 云端 `install -m 644` 整库替换）**已于 2026-11-11 废止**：它把云端 `users` 表换成本地版本，真实注册用户全部丢失。**不要再照着做，`install` 本地库到云端一律禁止。**
> 现行流程只有一条：用 `sync_prod_safe.py` 合并三张业务表，见 **§9.4**；完整命令单在 `backend/workspace/cloud-sync-runbook.md`（v2，本地留存）。

浏览器验收务必 **Ctrl + F5** 强刷；仍是旧页面就先 `curl -s http://112.126.90.252/ | grep -o 'assets/index-[^"]*'` 确认引用的是新哈希。线上无强缓存（只有 `ETag` / `Last-Modified`），旧包通常是文件没进 Web 根。

每日备份：`./venv/bin/python backup_db.py --db competitions.db --backup-dir backups --keep-days 30 --label daily`。用 `sqlite3.Connection.backup()`（不是 `cp`）生成含已提交 WAL 的一致快照，备份库为 `journal_mode=DELETE`、恢复不依赖 `-wal/-shm`；备份后自动 `integrity_check` + 四表行数校验。备份 SHA 与线上库不同属正常，验收只看 `integrity_check` 与行数；恢复见 1.4。

### 9.4 安全同步 `sync_prod_safe.py`（2026-11-11 事故后的强制流程）

**事故**：用 `install`/`cp` 整库覆盖云端 `competitions.db`，`users` 表被本地版本替换，真实注册用户全部丢失。**整库覆盖从此禁止**——即使先备份，也别赌恢复链路。

`backend/sync_prod_safe.py` 是唯一允许的"本地 → 云端"数据通道，硬约束如下：

| 约束 | 实现 |
|---|---|
| 只写三张业务表 | `BUSINESS_TABLES = ("competitions", "awards", "competition_candidates")` 硬编码，CLI 无参数可改 |
| 用户资产绝不可写 | 写入事务内对**除白名单外所有表**做行级 SHA-256 指纹，前后不一致立即 `ROLLBACK` 并抛 `SyncError` |
| 绝不删行 | 只 `INSERT ... ON CONFLICT(id) DO UPDATE`；生成语句经 `guard_sql()` 拦截 `DELETE/DROP/TRUNCATE/REPLACE/ALTER`。`REPLACE` 也必须禁：它先删后插，会触发 `user_preparations` 的 `ON DELETE CASCADE` |
| 跨库读取 | `ATTACH DATABASE 'file:...?mode=ro'`，源库只读挂载；`file_uri()` 负责 Windows 路径转 URI |
| 写入前备份 | `sqlite3.Connection.backup()` 生成 `backups/<库名>.bak-<时间>-pre-sync` |
| 结构漂移即中止 | 三张表列集合不一致（源多列/目标多列）直接拒绝，避免半升级写坏库 |
| 唯一键冲突不覆盖 | 目标库已存在同 `content_hash` / `(source,source_key)` 的行 → 跳过并列入报告 |
| 默认 dry-run | 不加 `--apply` 只打印计划，零写入 |

三种用法（本地执行）：

```bash
# 1) 看计划
python sync_prod_safe.py --source competitions.db --target workspace/prod-snapshot.db
# 2) 落盘（自动备份目标库）
python sync_prod_safe.py --source competitions.db --target workspace/prod-snapshot.db --apply
# 3) 不碰目标库，只生成差异 SQL 包，回服务器执行
python sync_prod_safe.py --source competitions.db --export-sql workspace/sync.sql
```

推荐的云上姿势（完整命令见 `backend/workspace/cloud-sync-runbook.md` v2）：云端 `backup_db.py --label for-sync` 出一致快照（**别直接 `scp` 线上库——WAL 模式下只拷主文件会漏提交**）→ `scp` 下到 `workspace/cloud-snapshot.db` → 本地 dry-run 看计划 → `--apply` 就地合并 → `scp` 回去 → 云端**先对账**（合并库与现库的 `users/user_preparations/feedbacks` 行数、`sum(id)` 必须逐项相同）→ `systemctl stop` → `install -m 644` + `rm -f competitions.db-wal competitions.db-shm` → 起服务。这一刀仍是整库替换，但内容 100% 源自云端自身，三张业务表才是本地增量，所以安全；对账不一致必须中止。

报告落在 `backend/workspace/sync_prod_safe_<时间>.json`，含每表 `inserts/updates/unchanged/conflicts/stale_on_target`、受保护表行数、`integrity_check`。测试：`tests/test_sync_prod_safe.py`（6 条，覆盖"写业务表不改用户表""救援回灌""SQL 包无破坏性语句""结构漂移中止"）。

#### 事故恢复：只回灌用户资产

```bash
# 云端找事故前备份（02:30 日备份 或 覆盖前 pre-db-sync）
ls -1t /root/KuAo/backend/backups/competitions.db.bak-* | head
# 拉回本地后：
python sync_prod_safe.py --target workspace/cloud-current.db \
    --rescue-from workspace/cloud-pre-overwrite.db --apply
```

`--rescue-from` 只处理 `users / user_preparations / feedbacks / ai_usage`：按主键补齐**缺失**行，`INSERT ... ON CONFLICT DO NOTHING`，不删、不覆盖已有账号；用户名相同但 id 不同会打印"需人工裁决"而不是静默合并。会校验其它表指纹未变。

也可以在服务器上用 sqlite3 就地救（等价逻辑，注意先 `systemctl stop`）：

```bash
cp competitions.db competitions.db.before-rescue
sqlite3 competitions.db <<'SQL'
ATTACH '/root/KuAo/backend/backups/<事故前备份>' AS bak;
BEGIN IMMEDIATE;
INSERT OR IGNORE INTO users (id, username, password_hash, created_at, is_admin)
  SELECT id, username, password_hash, created_at, is_admin FROM bak.users;
INSERT OR IGNORE INTO user_preparations (user_id, competition_id, created_at)
  SELECT user_id, competition_id, created_at FROM bak.user_preparations
  WHERE user_id IN (SELECT id FROM users);
COMMIT;
SQL
```

抢救后立刻用 `sync_prod_safe.py` 的 `--apply` 路径做一次"业务表同步"，确认用户表行数不回退。

---

## 十、JS 渲染站点抓取 SOP（playwright-mcp）

> 2026-11-11 实测落地。核心思想：**用高成本的浏览器去找低成本的接口**——浏览器只做「探路」，量产永远走 `requests` + JSON API。

### 10.1 先判断要不要上浏览器（按顺序试，能停就停）
1. `curl` / `requests` 拿原始 HTML，**grep 目标字段**（如赛事名、`result-item`、总数）。
   - 命中 → 静态或服务端渲染，直接解析，**别开浏览器**。
   - 只有 4–8 KB 空壳、字段计数为 0 → SPA，进第 2 步。
2. 查 `robots.txt` 与站点条款，确认目标路径是否 `Disallow`（合规判断先行，见 10.4）。
3. 打开 MCP 浏览器抓包定位真实接口，再用 `requests` 重放。

> 对照数据（赛氪 `/search`，2026-11-11）：原始 HTML **4247 字节**，`云帐房` / `result-item` / `5163` 出现次数均为 **0**；而 DOM 渲染后有 10 张卡片 → 典型第 1 步就该判掉的 SPA。

### 10.2 标准五步（工具名即 `playwright-mcp` 的工具）
0. **前置**：本站装了 playwright-mcp（见 10.5），工具前缀 `mcp__browser__`，共 25 个。
1. **开页面**：`browser_navigate(<列表页 URL>)`
   - 注意路由：赛氪 `/races`、`/vs` 都是 404，真实列表页是 **`/search`**；别拿猜想 URL 当结论，先开首页 `browser_evaluate` 收集 `a[href]`，再定位入口。
2. **读渲染结果**：先 `browser_snapshot` 看结构，再 `browser_evaluate` 精确抽字段（比 snapshot 省 token、比正则稳）：
   ```js
   () => [...document.querySelectorAll('li.result-item')].map(li => ({
     title: li.querySelector('a[title]')?.getAttribute('title'),
     href:  li.querySelector('a[href*="/vse/"]')?.href,
   }))
   ```
   - 卡片 `innerText` 为空时说明文字在子节点/兄弟节点，往上找 `closest('div[class]')` 或改用 `a[title]`。
3. **制造一次「翻页」并抓包**：赛氪是 `button.load-more`（「点击加载更多」），不是页码。
   `browser_click('button.load-more')` → `browser_network_requests({filter:'search/info'})`
   - 断言方式：**点击前后 DOM 条数变化**（10 → 20）+ **新增一条带 `page=2` 的请求**，两者同时成立才算确认「数据来自接口」。
4. **取请求头**：`browser_network_request(index, part:'request-headers')`
   - 只关心有没有 `Authorization` / `X-Sign` / 强制 `Cookie`。赛氪实测只有 `user-agent`、`referer: https://www.saikr.com/`、`accept: application/json` → 可直接重放。
5. **重写成 requests 并核对分页字段**：换成 `requests`/`urllib` 循环 `page`，比对 `total` / `total_page` / `limit`，字段名以响应为准（赛氪：`contest_id`、`contest_name`、`contest_url`、`regist_start_time`(秒级时间戳)、`level_name`、`organiser`、`thumb_pic`、`labels[]`）。
   - 落地位置：`discover_saikr.py`（列表）、`enrich_saikr_details.py`（详情）、`saikr_match.py`（索引）。**新站点照这三个文件的形状写**，别另起一套。

### 10.3 三条现实提醒（别以为「有浏览器就无死角」）
1. **能直连 API ≠ 可以高频跑**。接口都挂了 WAF/限流，超频直接 403 或返回空列表。
   - 铁律沿用 §三：赛氪 3–5 秒（建议 10）、通知页 5–10 秒、新闻 2–5 秒；每日 ≤2 页 20 条；遇 429 / 验证码 / 空列表立即停。
   - 抓包得的 `limit` 上限别试探性拉大（`limit=15` 与 `limit=10` 的 `total_page` 不同，大 limit 更容易触发风控）。
2. **有签名的站点，浏览器只用来「拿环境」**。小红书、部分小程序端那种 `X-Sign` / `mtgsig`：
   - 用 `browser_evaluate` / `browser_run_code_unsafe` 在页面上下文里**直接调用它自己的签名函数**（`window._sign(...)`），把签名后的头捞出来再交给 requests；
   - 没必要时**不要**逆向混淆 JS——那是时间黑洞，浏览器内 hook 才是正解。
3. **反爬分级（先定位级别，再选工具，别一上来就上浏览器）**：

| 级别 | 特征 | 打法 |
|---|---|---|
| L1 静态 | HTML 里就有数据 | `requests` + 解析，成本最低 |
| L2 纯 JS 渲染 | HTML 是空壳，数据走 XHR/JSON | **本节 SOP**：浏览器探路 → requests 重放 |
| L3 强签名/指纹 | 请求带 `X-Sign`、TLS 指纹校验、`mtgsig` | playwright 留在页面里签（`browser_run_code_unsafe`），或 `--user-data-dir` 持久化会话 |
| L4 登录态 | 必须登录 / 扫码 | playwright 登录一次存 `storageState`，requests 带 Cookie 复用 |

> 赛氪属 **L2**，是最便宜的一级；判级成本 1 分钟，判错会浪费半小时。

### 10.4 合规红线（升级工具 ≠ 放宽边界）
- §三 的 robots 结论**不变**：赛氪 `Disallow: /api/`、`/search`、`/*?*`，`Crawl-delay: 10`。技术上能做，定位仍是「低频、低风险、**非授权**」，只做存量补全 + 每日 20 条增量候选，**不因新增抓包能力而扩量**。
- **修正 §三 的一条旧结论**：列表接口**确实有关键词搜索**——`GET /api/pc/search/info?type=0&keyword=<kw>&page=&limit=`（2026-11-11 实测「挑战杯」total=22、「学创杯」total=6、「计算机设计大赛」total=15）。
  但该接口对应 `/search` 页面被 robots 禁止，**因此仍不得拿它做批量名称反查**；`saikr_match` 继续用本地 `saikr_index.json`（§三「无法按名称反查」的操作结论不变，只是原因从「接口不存在」改为「合规不允许」）。
- 抓到的页面快照、截图、日志统一落 `C:\Users\xiao\.dsh\mcp\out`（MCP 的 `--output-dir`），**不要**在仓库里生成 `.playwright-mcp/`；已 gitignore 的 `.verify/` 只放临时取证。

### 10.5 本机 playwright-mcp 现状（别人接手时先核对这段）
| 项 | 值 |
|---|---|
| 安装位置 | `C:\Users\xiao\.dsh\mcp\playwright-mcp`（本地依赖，非全局；`@playwright/mcp@0.0.85`，18.3 MB） |
| 浏览器内核 | `%LOCALAPPDATA%\ms-playwright`（chromium-1254 / chromium_headless_shell-1254 / ffmpeg-1013，约 740 MB） |
| 配置 | `C:\Users\xiao\.dsh\profiles\desktop\cordis.patch.yml` 的 `insert` 段，`serverName: browser`，stdio 启动 `node cli.js --headless --isolated --no-sandbox --viewport-size=1440,900 --output-dir=...\.dsh\mcp\out` |
| 生效方式 | **DSH 热加载，改完配置无需重启**；进程参数变化即说明 reload 成功（换 PID 属正常） |
| 备份 | `cordis.patch.yml.bak-20261111-1915` |
| 内核下载 | `cdn.playwright.dev` 在墙内会 `ECONNRESET`，必须 `PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright` 再 `playwright-core/cli.js install chromium` |

## 十一、本机 AI 工具链（三件套，2026-11-12 全部落地）

| 工具 | 作用 | 落地形态 |
|---|---|---|
| `oil-ui` | 界面设计与视觉评审（对比页、截图取证） | DSH 插件 `dsh-oil-ui`（`github:oil-oil/dsh-oil-ui`），提供 `oil_ui_*` 工具 + `oil-ui` Skill |
| `playwright-mcp` | 真实浏览器渲染抓取 | DSH 插件 `@deepseek-ai/dsh-mcp-client`（`serverName: browser`），见 §10 |
| `superpowers`（裁剪版） | 系统性调试 + 计划书写法 | 只装 2 个 Skill，走 `dsh-skill-filesystem` 的 `customSkillDirs`，**没有** npm 包、**没有** hooks |

### 11.1 superpowers 裁剪安装（为什么只装两节）

上游 `obra/superpowers`（`22f0b46` / v7.0.2，MIT）自带 15 个 Skill、Claude Code 的 `SessionStart` 钩子、以及 TDD / 子 Agent 评审等强制流程。**强制的 TDD 与逐任务评审会和本项目的验收节奏冲突**：KuAo 的验证是产物级（跑脚本、查库、看截图、pytest 全量），不是逐函数红灯绿灯，硬插进来只会拖慢且互相打断。

因此只取 `systematic-debugging`（四阶段根因定位）与 `writing-plans`（计划书写法），其余 13 个 Skill 与 `hooks/` 全部不落盘——是物理上没有，不是靠开关屏蔽。

**配置位置**（唯一改动的文件）：`C:\Users\xiao\.dsh\profiles\desktop\cordis.patch.yml`，`insert` 段追加：

```yaml
    - id: superpowers-skills
      name: '@deepseek-ai/dsh-skill-filesystem'
      config:
        providerName: superpowers          # 独立 provider，不与默认文件系统 provider 抢名
        includeDefaultRoots: false         # 不扫 <project>/.dsh/skills、~/.dsh/skills，避免混入仓库技能
        customSkillDirs:
          - 'D:\agent-lab\dsh-plugins\superpowers\skills'
        watch: true                        # 改 SKILL.md 后下一次调用即生效，无需重启
```

| 项 | 值 |
|---|---|
| 技能目录 | `D:\agent-lab\dsh-plugins\superpowers\skills\{systematic-debugging,writing-plans}\SKILL.md` |
| 来源与取舍 | `D:\agent-lab\dsh-plugins\superpowers\UPSTREAM.md`（含 commit、排除清单、升级步骤） |
| 上游副本 | `D:\agent-lab\dsh-plugins\_tmp\superpowers`（完整 clone，仅供升级时对照，未挂载） |
| 备份 | `cordis.patch.yml.bak-20261212-211254-pre-superpowers`（注意日期是系统中招后的 2026 年，非笔误） |
| 验收 | `plugin_manager` 里 `superpowers-skills` → `fiberPhase: active`；技能目录出现两条，且 `skill` 工具能加载正文 |

**升级**：`git -C D:\agent-lab\dsh-plugins\_tmp\superpowers pull` → 重新拷两个目录 → 更新 UPSTREAM.md 的 commit。

### 11.2 产品决策：`/hot` 宁可少，不用「刚截止」凑数（勿改）

2026-11-12 实测：84 条在架国家级赛事里，当天只有 3 条处于「报名中/未开始」，`/api/competitions/hot?limit=50` 就返回 3 条。

**这是正确行为，不是缺陷。** 首页是「精品国赛智库」的热门位，热门 = 当下真的能报。用已截止赛事补齐会让用户以为赛事过期，破坏信任；留白优于误导。

因此：
- **不要**给 `/hot` 加「用 `_phase=2` 补齐到 limit」的兜底逻辑；
- 前端按返回条数自适应渲染，不要写死 8 张卡；
- 若某天首页太空，正确的解法是补真实的在招赛事数据（`discover_saikr` + 审核），不是放宽筛选口径。

同批次还修了主列表的 `limit`：`db.list_competitions()` 原先**没有** limit 参数，`GET /api/competitions?limit=N` 被 FastAPI 静默忽略（`?limit=1` 返回全部 84 条）。现补为 `Query(None, ge=1, le=500)`，并**在 `month` 的 Python 过滤之后**再切片——若把 `LIMIT` 写进 SQL 就是「先截断再过滤」，会返回比 limit 更少的行。回归测试 `backend/test_competition_list_limit.py`（4 条）专门锁这个坑。

## 十二、比赛时间（`contest_*_date`）从哪来（2026-11-13 排查与重构）

### 12.1 结论先说：赛氪接口根本没有比赛时间

实测 `GET https://apiv4buffer.saikr.com/api/pc/contest/info?contest_id=58040`（中国大学生计算机设计大赛 / 蓝桥杯 58069 同样），72 个字段里与时间有关的只有 5 个：

| 字段 | 实测值 | 含义 |
|---|---|---|
| `regist_start_time` | `'2026/04/01 00:00:00'` | 报名开始（**字符串，不是时间戳**） |
| `regist_end_time` | `'2026/04/30 00:00:00'` | 报名截止 |
| `contest_start_time` | `'-0001/11/30 00:00:00'` | **占位符 = 未设置** |
| `contest_end_time` | `'-0001/11/30 00:00:00'` | **占位符 = 未设置** |
| `is_show_regist_time` | `1` | 是否展示报名时间 |

赛氪网页上的「比赛时间」是它自己的运营字段，不在这个接口里。所以 `_parse_api_time` 对 `-0001/...` 返回 `None` 是**正确**的，`fill_dates_and_cover` 每次都在「只补空」却永远补不到比赛日期——不是解析 bug。

**也没有护栏误伤**：`backfill_dates._plan_writes` 的年份窗口 `[今年-1, 今年+1]` 只拦截往年届次；`enrich_saikr_details.STALE_DAYS=400` 只作用于整条赛事的写入，不置空任何日期。历史数据里比赛日期为空的唯一原因就是**从来没写过**。

### 12.2 真正的断层：AI 文本里有日期，结构化字段没接上

排查时的存量：91 行 / 84 在架；`exam_schedule` 有正文 46 行；正文含「X月X日」26 行；但 `contest_start_date` 只有 5 行——AI 把「决赛于11月14日举行」写进了正文，却没有回写结构化列。

### 12.3 修复：先护栏，再反写

新增 `backend/contest_dates.py`（四道护栏 + 年份推断）：

1. **原文佐证**：正文必须真的出现该「月+日」（`11月14日`/`11-14`/`11/14`/`11.14`），否则丢弃（防模型编造）；
2. **年份护栏**：只接受 `[今年-1, 今年+1]`；**正文没写年份时不信模型猜的年份**，按「优先今年 → 明年 → 去年」重新推断（这条是修 `id=34` 时踩出来的：模型给 2025，正文无年份）；
3. **只补空**：`contest_start_date` / `contest_end_date` 已有值一律不覆盖；
4. **起止有序**：`end < start` 时丢弃 end。

链路改造：

* `enrich_saikr_details.py`（Prompt v2）：`purify_fields` 在三字段之外，额外抽取 `extracted_contest_date` / `extracted_contest_end_date`，返回 `(fields, dates)`；`enrich_competition` 与批量主循环都会过护栏后写回 `contest_*_date` + 补空的 `contest_*_month`；
* `backend/backfill_contest_dates.py`：对**存量**「有正文、无比赛日期」的行补做一次抽取；默认 dry-run，`--apply` 只补 NULL，写生产库必须 `--allow-production`，写前自动备份；报告落在 `workspace/backfill_contest_dates_*.json`。

**首轮回填结果**（2026-11-13 12:48）：处理 24 行 → 模型抽出 12 行 → 写入 9 行；另 3 行（`70/91/103`）是正文里的 2020/2021/2023 旧届日期，被年份护栏拒绝。`contest_start_date` 从 5 条增至 **13 条**，剩余 34 行「有正文仍无日期」里 15 行只写了报名截止或「7月至8月」这种月份区间——按「宁缺毋滥」保持 NULL，前端会自动降级成月份区间或「待公布」。

前端无需改动：`frontend/src/utils/timeFormat.js` 的 `formatContest()` 已经按「日期 → 月份 → 待公布」降级，`CompetitionCard.vue` 的倒计时徽章只在有精确日期时出现。

**重跑命令**：

```bat
:: 只看结果（不写库）
backend\venv\Scripts\python.exe backend\backfill_contest_dates.py --limit 20
:: 写生产库
backend\venv\Scripts\python.exe backend\backfill_contest_dates.py --apply --allow-production
```

**注意**：`--ids 34` 这类重跑前先确认库里该行确实为空；护栏只补空，已有值不会刷新。若发现写错（如年份推断失误），先把该行 `contest_*_date/month` 置 NULL 再重跑。