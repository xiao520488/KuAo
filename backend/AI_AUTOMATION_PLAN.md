# AI 内容生成与数据流自动化 · 方案评审稿

> 状态：**已确认并实施（2026-11-08 13:15）**；实施记录见下方，代码细节见 [CRAWLER_HANDOVER.md](CRAWLER_HANDOVER.md) 第十节。
> 日期：2026-11-08（Asia/Shanghai）
> 基准库：方案调研基于 SHA-256 `ce499688…`；实施后生产库 SHA-256 `3af3af9c…`
> 关联文档：[CRAWLER_HANDOVER.md](CRAWLER_HANDOVER.md)

## 实施记录（2026-11-08 13:15）

| 决策 | 结果 |
|---|---|
| 按需接口必须登录 | 已实现，`POST/GET` 均 `Depends(get_current_user)`；未登录 401 |
| 不自动触发，按钮触发 | 已实现：详情页「✨ 生成 AI 攻略」+ 未登录引导 + 轮询 |
| 每天 4 篇攻略 + 2 条灵感 | `daily_ai_worker.py` 默认 `--limit 4 --idea-limit 2`，cron 参数一致 |
| 不加 view_count，用国家级+获奖数+sort_order | `db.select_guide_candidates()` 按此排序 |
| 孤儿数据直接删除 | 已执行：insights 10 / ideas 12 删除，剩余 0 |
| 新闻改固定周调度（周一/周四） | 已写入 crontab：`0 3 * * 1,4` |
| 列表接口剔除 full_process_guide | 已实现：`Competition` 移除，`CompetitionDetail` 保留 |
| 生产库建 ai_jobs + 4 列 | 已执行：`migrate_production.py --apply`，备份 `competitions.db.bak` |

**实测**：测试库真机生成 2 篇（3378 / 2945 tokens，2104 / 2543 字）；测试 107 passed / 2 skipped；前端 build 7.36s。

---

## 0. 结论摘要（先看这 10 条）

1. `analyzer.py` 是**单条手动触发**（`--competition_id` 必填），产出写入 `insights` + `ideas` 两张表，**不碰** `competitions`。
2. `generate_guides.py` 是**离线批量扫描** `competitions`（默认跳过已有攻略），产出写入 `competitions.full_process_guide`，**不碰** `ideas`。
3. 两个脚本都封装了 `main(argv)` + `if __name__ == "__main__"`，**可以当命令行脚本调用**；但都**不能指定 `--db-path`**（写死 `db.DB_PATH` = 生产库），**不能直接挂 cron**。
4. 生产库现状：**33 条赛事**，只有 **7 条有攻略**（id=1–6、10），**26 条为空**；`ideas` 16 条里 **12 条 `competition_id=NULL`**、`insights` 13 条里 **10 条 NULL**——这些历史孤儿数据在按赛事隔离的接口下**永远不会显示**，等于白花钱。
5. 缺口不只是“生成”，而是**没有任务状态、没有去重、没有成本上限、没有失效判定**：同一条比赛可以被反复生成，失败没有冷却，攻略改了数据也不会刷新。
6. 按需接口**能做**，建议 `POST /api/competitions/{id}/generate_guide` → **202 + 异步执行 + 轮询**；但必须配 `ai_jobs` 任务表（原子抢占、幂等、预算、审计），否则并发点开详情页会重复烧钱。
7. 「热门」目前**没有真实热度数据**（库里没有 `view_count`/曝光表）。建议先用「国家级 + 有官网 + 获奖作品数 + 运营 `sort_order`」做确定性排序，后续再加浏览计数。
8. 定时慢刷建议独立进程 `daily_ai_worker.py`（复用 `discover_common.prepare_target` 的生产库保护 + 自动备份），每天 3–5 条，**当天没有候选就 0 成本退出**。
9. Crontab 最终链：`02:30 备份 → 03:00 新闻(周一/周四) → 04:10 新赛事发现 → 04:40 AI 慢刷 → 05:10 周度补全 → 05:30 日志清理(7天)`，任务间用 `flock` 隔离，统一 `Asia/Shanghai`。
10. 一次性补齐 17 条“国家级+有官网+空攻略”约 **7 万 tokens**；给其中 5 条有 ≥3 条获奖的比赛生成灵感约 **7–13 万 tokens**（合并 insights 请求后可再省 40%）。稳态后每天新增量级只有几千 tokens。

---

## 1. 任务一：现有 AI 生成逻辑排查

### 1.1 `analyzer.py`（“备赛灵感”）

| 维度 | 现状 |
|---|---|
| 触发方式 | **单条、手动、命令行**。`--competition-id` 是 `required=True`（[analyzer.py:603-609](analyzer.py)），没有批量/全量入口，也没有 cron 相关代码 |
| 输入 | 该赛事的 `competitions` 行 + `awards`（按 `competition_id`）；无获奖作品直接**退出码 2，不生成**（[analyzer.py:521-528](analyzer.py)） |
| 输出 1 | `insights` 表：`experience / trend / gap` 三个 kind，**每个 kind 一次独立大模型调用**，Markdown ≤200 字 |
| 输出 2 | `ideas` 表：默认 4 条，**每 2 条一批请求**（`IDEA_BATCH_SIZE=2`），JSON 解析，`status='draft'` |
| 去重 | 已有 3 条 insights + 4 条 ideas 且未 `--force` → 直接退出 0；`--force` 会**先删除**该赛事全部 insights/ideas 再重生成（[analyzer.py:537-568](analyzer.py)） |
| 成本 | 每赛事 5 次调用（3 insight + 2 idea 批），上下文是“赛事信息 + 最多 24 条获奖作品”，**同一份上下文被重复发送 5 次** |
| 可调用性 | 有 `generate(competition_id, ...)` 纯函数式入口，可被 worker `import` 复用；但 `db.init_db()` 与所有写库都走全局 `DB_PATH` |

### 1.2 `generate_guides.py`（“全过程攻略”）

| 维度 | 现状 |
|---|---|
| 触发方式 | **离线批量扫描**：`db.list_competitions_for_guide()` 默认取全部比赛，未加 `--force/--only-missing` 时在内存里过滤掉已有攻略的（[generate_guides.py:163-184](generate_guides.py)）；支持 `--id`、`--limit`、`--sleep` |
| 输入 | 仅 `name / category / level / official_url / intro|summary`（[generate_guides.py:59-86](generate_guides.py)）——**没有报名月份、比赛月份、获奖作品**，所以生成的是“通用流程”，不含具体时间 |
| 输出 | **`competitions.full_process_guide`**（Markdown），写库函数 `db.update_full_process_guide(cid, guide)`（[db.py:461-467](db.py)） |
| 重试 | 内层 `llm` 3 次 + 外层 `GUIDE_MAX_RETRIES=3`（[generate_guides.py:239-274](generate_guides.py)） |
| 参数 | `max_tokens=4096`、`temperature=0.4`，单次调用；`--dry-run` 只打印长度 |
| 可调用性 | 有 `generate_one(comp)` / `generate_one_with_retry(comp)` 可复用；但 `main()` 里 `db.init_db()` 写死默认库，**没有 `--db-path`**，且 **`--dry-run` 也会执行 `init_db()` 改库文件**（伪 dry-run） |

### 1.3 两者对比

| | `analyzer.py` | `generate_guides.py` |
|---|---|---|
| 触发 | 单条 | 全表扫描（可 limit/id） |
| 落库 | `insights` + `ideas` | `competitions.full_process_guide` |
| 依赖数据 | 必须有 `awards` | 不需要 |
| 调用次数/赛事 | 5 | 1 |
| 幂等 | 有“已满则跳过” | 有“非空则跳过” |
| 可用作脚本 | ✅ `python analyzer.py --competition-id 10` | ✅ `python generate_guides.py --only-missing` |
| 能安全挂生产 cron | ❌ 无库路径参数、无备份、无生产库闸门、无预算 | ❌ 同上，且默认会扫全表 |

### 1.4 现有攻略质量抽样（id=1）

已生成的 7 条攻略是**“安全但通用”**的：开头有「以官方通知为准」的免责声明，正文里**没有出现任何年份/月份**（正则实测 years/months 均为空）。好处是不会编造；代价是**没有“何时报名、何时比赛、今年几月”这类用户最需要的信息**——因为 Prompt 里根本没喂这些字段。这直接决定了第 2.6 节的 Prompt v2。

### 1.5 库内真实状态（**本次只读实测，与交接文档有出入，请以这里为准**）

| 指标 | 实测值 | 交接文档 |
|---|---|---|
| competitions | **33** | 32 |
| 有攻略 | **7**（id=1,2,3,4,5,6,10；长度 2.6–5.1KB） | 未记录 |
| 攻略为空 | **26** | — |
| 国家级 + 有官网 + 攻略为空 | **17** | — |
| `ideas` | 16 条，其中 **12 条 `competition_id=NULL`**；id=10 有 4 条 | — |
| `insights` | 13 条，其中 **10 条 `competition_id=NULL`**；id=10 有 3 条 | — |
| candidates | 82 条：**pending 80 / approved 1 / rejected 1** | 82 pending |
| 新赛事 | **id=36**（由赛氪候选 82 approve 而来，`category='待确认'`、无官网、`sort_order=9999`） | 主表 32 条、未动 |
| SHA-256 | `ce499688…` | `80092384…` |

> 结论：交接文档的「主表未动 / 临时库测过 approve」已过期——生产库在 12:44–12:45 真实发生过一次 approve + 一次 reject，并且后端启动又跑过 `init_db()`。**上云前必须重新拍一次基线快照**，否则迁移校验对不上。

### 1.6 直接上云会踩的 7 个坑（必须先改）

1. **无 `--db-path`**：两个脚本只能写 `db.DB_PATH`。按仓库约定（`crawler_news.py:1493`、`discover_common.py:64`）必须先 `db.DB_PATH = path` 并过生产库闸门。
2. **无备份、无 `--apply/--allow-production`**：违反 `CRAWLER_HANDOVER.md` 第四节红线 2（写生产库 = 申请 + 备份 + 显式开关）。
3. **无任务状态**：崩了不知道生成到哪、也不知道是不是正在跑；进程被 kill 后没有任何痕迹。
4. **无并发控制**：两个进程同时跑会重复调用同一条赛事（生产上直接翻倍烧钱）。
5. **无预算上限**：一旦 `--force` 或全量扫描被误触发，一次能烧掉整月额度。
6. **无失效判定**：赛事的官网/月份/获奖数据更新后，旧攻略不会重建；反过来，没有任何字段能判断“这条攻略是什么时候、用什么模型、基于什么数据生成的”。
7. **接口会把攻略塞进列表页**：`Competition`（列表模型）里带 `full_process_guide`（[models/schemas.py:54-60](models/schemas.py)），26 条攻略补齐后 `GET /api/competitions` 的响应会无故膨胀 ~150KB。建议：列表模型去掉该字段，只保留在 `CompetitionDetail`。

---

## 2. 任务二：按需生成 + 定时慢刷

### 2.1 总体架构

```
                    ┌──────────────────────────────┐
  前端详情页 ──POST──▶│ /api/competitions/{id}/      │──INSERT OR IGNORE──┐
  (攻略为空)         │ generate_guide  (202)        │                    │
                    └──────────────────────────────┘                    ▼
                                                                  ┌───────────┐
  cron 04:40 ──python──▶ daily_ai_worker.py ──选片 SQL──────▶     │  ai_jobs  │
                                                                  │ (唯一索引) │
                    ┌──────────────────────────────┐   claim      └─────┬─────┘
                    │ 生成执行器（线程池 1~2 并发） │◀─────────────────────┘
                    │  ai_content.generate_guide() │──1 次 LLM──▶ 写 full_process_guide
                    │  ai_content.generate_inspira │──5 次 LLM──▶ 写 insights + ideas
                    └──────────────────────────────┘
```

**为什么不让接口直接同步生成**：单条攻略 20–60s（`deepseek-flash` 的 reasoning 也计 token），HTTP 同步会拖住连接、超时、被 Nginx 切断，且用户重复刷新会重复触发。异步 + 轮询是唯一稳的做法。

### 2.2 数据模型改动（一处迁移，全部幂等）

**新增 `ai_jobs` 表**（`db.init_db()` 里像 `CANDIDATES_DDL` 一样建，附 `ensure_ai_jobs_table()`）：

```sql
CREATE TABLE IF NOT EXISTS ai_jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL CHECK (kind IN ('guide','inspiration')),
    competition_id INTEGER NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
    trigger        TEXT NOT NULL DEFAULT 'manual'
                   CHECK (trigger IN ('on_demand','daily','manual')),
    status         TEXT NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued','running','done','failed','skipped')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    tokens         INTEGER,
    requested_by   INTEGER,          -- 用户 id，可空
    request_ip     TEXT,             -- 匿名防刷用
    error          TEXT,             -- 截断 500 字，不回传前端
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at     TEXT,
    finished_at    TEXT
);

-- 同一条赛事同一类任务，同时只允许 1 个活跃任务（原子去重的核心）
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_jobs_active
    ON ai_jobs (kind, competition_id) WHERE status IN ('queued','running');

CREATE INDEX IF NOT EXISTS idx_ai_jobs_created ON ai_jobs (kind, created_at);
```

**`competitions` 新增 4 列**（走 `db.py` 的 `_MIGRATION_COLUMNS`，服务启动自动补）：

| 列 | 用途 |
|---|---|
| `guide_generated_at TEXT` | 生成时间（前端展示 + 失效判定） |
| `guide_model TEXT` | 生成模型（换模型后可批量重刷） |
| `guide_prompt_ver TEXT` | Prompt 版本，如 `v2` |
| `guide_input_hash TEXT` | 生成时输入指纹（官网/月份/获奖变化 → 可判定 stale） |

> 为什么不用内存字典/Redis：上云后多进程 + 重启都会丢状态；SQLite 单文件已经足够，且天然提供原子写。

### 2.3 按需生成接口设计

#### 路由

```
POST /api/competitions/{id}/generate_guide
GET  /api/competitions/{id}/generate_guide      # 轻量状态查询（可选，前端也可直接轮询详情）
```

请求体（可选）：`{"force": false}` —— `force` 仅登录用户/管理端可用，默认永久 false。

#### 响应

| 场景 | 码 | 体 |
|---|---|---|
| 已有攻略且未 force | 200 | `{"status":"ready","guide_length":4300}` |
| 首次触发/成功抢占 | 202 | `{"status":"queued","job_id":123,"poll_after_ms":3000}` |
| 已有 queued/running 任务 | 202 | `{"status":"running","job_id":123,"poll_after_ms":3000}`（**同一 job，幂等**） |
| 赛事不存在 | 404 | — |
| 3 次失败冷却期内 | 409 | `{"status":"failed","retry_after_sec":86400}` |
| 超出当日预算/队列已满/IP 冷却 | 429 | `{"status":"throttled","reason":"daily_budget"}` + `Retry-After` |

#### 执行模型

1. `INSERT OR IGNORE INTO ai_jobs(kind='guide', ..., status='queued')`；`rowcount==1` 才算抢到（唯一索引兜底并发）。
2. 抢到后交给**模块级 `ThreadPoolExecutor(max_workers=1)`** 执行（不是裸 `BackgroundTasks`——多请求时 BackgroundTasks 会无界起线程）。
3. 执行前 `UPDATE ... SET status='running', started_at=... WHERE id=? AND status='queued'`，再跑生成；成功写 `full_process_guide` + 4 个元数据列 + `status='done'`；失败 `status='failed'`、`attempts+1`。
4. **僵尸任务回收**：启动时 + 每次抢占前执行 `UPDATE ai_jobs SET status='failed', error='stale' WHERE status='running' AND started_at < datetime('now','-15 minutes')`（防止进程被重启/ kill 后永久卡住）。
5. 多 worker 部署也安全：抢占是 SQLite 单行原子写；但建议 `uvicorn --workers 1`（SQLite + 单机足够），并在抢占时额外检查“全局活跃 guide 任务 < 2”。

#### 防刷与预算（成本闸门）

| 配置（`.env`） | 默认 | 说明 |
|---|---|---|
| `AI_GUIDE_DAILY_LIMIT` | 8 | 全站每天最多新建攻略任务数（含失败） |
| `AI_GUIDE_QUEUE_MAX` | 5 | queued+running 上限 |
| `AI_GUIDE_FAIL_COOLDOWN_MIN` | 30 | 同赛事失败后的按需冷却（定时任务另有 3 天冷却） |

> **已按最终决策改为「必须登录」**：`POST/GET /api/competitions/{id}/generate_guide` 都依赖 `get_current_user`，未登录 401。匿名方案（IP 冷却/日上限）没有落地，避免额度被刷爆；上表只保留与成本相关的 3 个配置。Nginx 反代若以后要加 IP 维度限流，需配 `--proxy-headers` + `forwarded-allow-ips`。

#### 前端联动（`CompetitionDetailView.vue`）

现状：攻略为空时只显示“该赛事攻略正在收集中，敬请期待...”。最终实现（**不自动触发**）：

1. 攻略为空 → 展示「✨ 生成 AI 攻略」按钮；点击时若 `isLoggedIn()` 为 false，直接 `openAuthModal('login')`，不发请求。
2. 点击后 `POST`：`queued/running` → 骨架屏 + “AI 正在整理该赛事攻略…”；轮询 `3s × 20` 后 `10s × 18`（总上限约 4 分钟）。
3. 轮询到 `ready` → 自动重新拉详情并渲染；`failed` → 提示“AI 生成失败，请稍后重试”并保留重试按钮；`429` → 就地提示“今日额度已用完/队列已满”，不弹全局 Toast（拦截器只对带 `detail` 的 429 弹全局提示）。
4. 渲染保持现有 `marked + DOMPurify`，无新增 XSS 面；有攻略时展示 `guide_generated_at` 与“以官网通知为准”提示。

### 2.4 `daily_ai_worker.py`（定时慢刷）

#### CLI（默认 dry-run，沿用仓库红线）

```bash
python daily_ai_worker.py \
  --db-path /opt/kuao/backend/competitions.db \
  --limit 4 --idea-limit 2 \
  --sleep 5 --max-tokens 6144 \
  --budget-tokens 300000 \
  --apply --allow-production
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--limit` | 4（夹在 1–5） | 每天攻略条数 |
| `--idea-limit` | 2 | 每天灵感赛事数（比攻略更贵） |
| `--min-awards` | 3 | 灵感生成的最低获奖作品数（不足只做攻略） |
| `--sleep` | 5 | 条间间隔（3–8s 随机化） |
| `--budget-tokens` | 300000 | 当日 token 熔断，超了就停 |
| `--cooldown-days` | 3 | 失败赛事冷却天数，避免天天撞同一条 |
| `--refresh-stale` | 关 | 第二阶段：输入指纹变了/超过 30 天的重刷（默认不做） |

#### 选片逻辑（“热门”的确定性定义）

```sql
SELECT c.id, c.name, ...
FROM competitions c
WHERE c.is_active = 1
  AND c.level = 'national'
  AND TRIM(COALESCE(c.official_url,'')) <> ''
  AND TRIM(COALESCE(c.full_process_guide,'')) = ''
  AND NOT EXISTS (
      SELECT 1 FROM ai_jobs j
      WHERE j.kind='guide' AND j.competition_id=c.id
        AND (j.status IN ('queued','running')
             OR (j.status='failed'
                 AND j.finished_at > datetime('now', '-3 days')))
  )
ORDER BY (SELECT COUNT(*) FROM awards a WHERE a.competition_id = c.id) DESC,
         CASE WHEN c.sort_order IS NULL THEN 1 ELSE 0 END,
         c.sort_order ASC,
         c.id ASC
LIMIT :n;
```

**为什么这样排**：库里没有浏览/收藏热度（`user_preparations` 只有寥寥几行，且不代表曝光）。能稳定表达的“热门”只有三件事——**国家级**、**有官网（可核验、可导流）**、**获奖作品多（数据厚、灵感才有料）**，再用运营 `sort_order` 兜底。第二阶段若要做真热度，加 `view_count` + 详情页 `UPDATE ... SET view_count=view_count+1`（或按天聚合），把排序换成“90 天浏览量”，这是唯一能反映真实需求的做法。

#### 单条执行顺序

1. `claim`（`ai_jobs` 插入，`trigger='daily'`）→ 失败/被抢则跳过。
2. **攻略**：`ai_content.generate_guide(comp, prompt_ver='v2')` → 写 `full_process_guide` + 元数据。
3. **灵感**（仅当 `awards >= min_awards` 且当天 `idea-limit` 未用完）：3 条 insights + N 条 ideas（复用 `analyzer.generate()`）。
4. `finish_ai_job(tokens=llm.get_usage() 增量)`；单条异常不中断整体。
5. 结束时打印：候选数 / 成功 / 跳过 / 失败 / token 用量 / 报告路径，退出码 `0 全绿`、`2 部分失败`、`3 预算耗尽`。

#### 生产库安全

复用 [discover_common.py:64-81](discover_common.py) 的 `prepare_target(db_path, apply, allow_production)`：打印目标库、生产库必须显式 `--allow-production`、`apply` 前自动 `sqlite3 .backup` 并 `integrity_check`。这是仓库里唯一已经过实战的写库闸门，直接复用，不新造。

### 2.5 成本模型（以 token 计，金额按账户实际账单核对）

| 项 | 单次调用 | 每条赛事 | 说明 |
|---|---|---|---|
| 攻略（v2） | 输入 ~0.6K / 输出 2–4K | **~3–5K** | 输出含 reasoning；`max_tokens` 给 6144 可避免“空正文→翻倍重试”的二次计费 |
| 灵感·insights | 5K 输入 × 3 次 | **~15K** | 同一上下文重复 3 遍（合并可省 2/3） |
| 灵感·ideas | 5K 输入 × 2 批 | **~10–12K** | 每批 2 条 |
| **灵感合计** | — | **~25–27K** | 是攻略的 5–6 倍 |

一次性补齐（按当前 17 条空攻略 / 其中 5 条 ≥3 获奖）：

- 攻略：17 × 4K ≈ **7 万 tokens**
- 灵感：5 × 27K ≈ **13.5 万 tokens**（若把 3 次 insight 合并为 1 次 → **约 8 万**）
- 合计 **≤ 21 万 tokens**，按日 4 条分 5 天跑完；跑完 worker 自动空转（无候选，0 成本）。

稳态：每天新增赛事若 1–2 条 → 每天 4–10K tokens。

#### 降本 9 条（按收益排序）

1. **只生成空攻略**，已有 7 条永不重跑；`--force` 不进 cron。
2. **不生成就不收费**：条目级状态机 + 失败冷却，避免无限重试。
3. **insights 三次调用合并为一次**（输出 3 个小节、每节 ≤200 字，总量 ~600 字不会截断）；失败再退回逐条。省 10K/赛事。
4. **灵感只给 ≥3 条获奖作品**的赛事；`awards=0` 的 7 条赛事直接跳过（现在 `analyzer` 会返回 2，纯浪费一次进程）。
5. **攻略输出定长**：Prompt 要求 800–1500 字，`max_tokens=6144`，避免 reasoning 吃光后翻倍重试。
6. **输入瘦身**：获奖作品从 24 条降到 12 条、单字段截断 160→120 字；上下文减半。
7. **`input_hash` 已就绪**：`insights` 有唯一索引，重复输入天然复用；攻略也补 `guide_input_hash`。
8. **失败重试上限 2 次**（内层 `llm` 已有 3 次，外层别再叠 3 次 = 最多 6 次调用）。
9. **模型分层**：`deepseek-flash` 的 reasoning token 按输出计费，攻略这种“结构化长文”可评估换 `deepseek-chat`（非推理）是否更便宜；“事实抽取”继续用现模型。

### 2.6 内容质量：Prompt v2（攻略）

现状 Prompt 只有 5 个字段，生成结果**没有任何时间信息**。v2 输入改为：

- 赛事名、类别、级别（`level_label`）
- **报名月份**（`reg_start_month/reg_end_month/reg_cross_year`）
- **比赛月份**（`contest_start_month/contest_end_month`）
- 官网、`intro`/`summary`
- **最多 12 条获奖作品**（`title` + `award_grade` + `tech_stack`，各截断 120 字）作为“真实事实锚点”
- 当前年份

硬性约束（写进 system prompt）：

1. 时间只能来自上面给出的月份；**库里为 NULL 就写“官方尚未公布，以官网通知为准”**，禁止编造具体日期/截止时间/奖项名额。
2. 不得编造赛制细节；不确定就指向官网。
3. 固定结构：赛前准备 / 报名与材料 / 比赛形式 / 展示与答辩 / 常见坑 / 官方信息核对清单。
4. 篇幅 800–1500 字，Markdown 二级标题。
5. 记录 `guide_prompt_ver='v2'`；换 Prompt 后可 `--refresh-stale` 分批重刷（33 条 × 4K ≈ 13 万 tokens，不贵）。

### 2.7 灵感生成的特殊约束

- **必须有获奖作品**：没有 `awards` 的赛事只能生成“空泛选题”，且 `analyzer` 现在直接退出 2。策略：`--min-awards 3`。
- **孤儿数据**：`ideas` 12 条 + `insights` 10 条 `competition_id=NULL`，接口按赛事过滤永远看不到。建议二选一：① 直接删（推荐，属早期测试数据）；② 按标题回填 `competition_id`。**先确认再动**。
- **`--force` 会删除重建**：cron 永远不要带 `--force`；重刷走 `--refresh-stale` 的“先写新、成功再删旧”逻辑（或直接覆盖，不删）。
- **`ideas` 无 `model`/`prompt_ver` 列**：如需审计，补两列（可选，不影响本期）。

### 2.8 复用与重构清单（确认后才动手）

| 文件 | 动作 |
|---|---|
| `db.py` | 新增 `ai_jobs` DDL + `ensure_ai_jobs_table / claim_ai_job / finish_ai_job / fail_ai_job / get_active_ai_job / count_ai_jobs_today / reap_stale_ai_jobs / select_guide_candidates`；`_MIGRATION_COLUMNS` 补 4 列；`update_competition` 白名单补 `guide_*` |
| `ai_content.py`（新） | 从 `generate_guides.py` 抽出 `build_prompt_v2 / generate_guide / save_guide`；封装 `generate_inspiration(cid, ...)` 调 `analyzer.generate`；两者都返回结构化结果，**不 print** |
| `daily_ai_worker.py`（新） | 选片 + 预算 + claim + 报告 + `prepare_target` 闸门 |
| `routers/competitions.py` | 新增 POST/GET 两个路由；详情返回 `guide_status / guide_generated_at` |
| `models/schemas.py` | `Competition` 去掉 `full_process_guide`（列表减负），`CompetitionDetail` 补回并加 `guide_status` |
| `config.py` | 新增 6 个 `AI_GUIDE_*` 配置（全部有默认值） |
| `frontend/src/views/CompetitionDetailView.vue` | 空攻略自动触发 + 轮询 + 三态 UI |
| `frontend/src/services/api.js` | `requestGuide(id)` / `getGuideStatus(id)` |
| `generate_guides.py` | 保留 CLI 兼容，内部改为调用 `ai_content`（老命令不失效） |
| `analyzer.py` | 增加 `db_path` 注入、`quiet=True`、返回统计；保留 CLI |

---

## 3. 任务三：云端 Crontab 全链路

### 3.1 前置条件

- **时区**：服务器 `timedatectl set-timezone Asia/Shanghai`。不要依赖 `CRON_TZ`（Debian/Ubuntu 的 cron 不支持，只有 cronie 支持），这是上云最常见的时间错位来源。
- **路径**：`/opt/kuao/backend`，venv `/opt/kuao/backend/venv`，日志 `/opt/kuao/backend/logs/`，备份 `/opt/kuao/backend/backups/`。
- **锁**：全部 `flock -n /var/lock/kuao_xxx.lock`，拿不到锁立即退出（避免任务堆积）。
- **退出码**：非 0 即异常；cron 里用 `MAILTO` 或统一 webhook 告警。

### 3.2 任务清单

| # | 任务 | 时间（Asia/Shanghai） | 频率 | 主要命令 | 锁 | 预计耗时 |
|---|---|---|---|---|---|---|
| 0 | 生产库备份（本地+对象存储） | 02:30 | 每天 | `backup_db.py`（内部 `sqlite3 .backup` + `rclone copy`） | `kuao_backup.lock` | 5–20s |
| 1 | 高校新闻获奖抓取 | 03:00 | **每周一/周四** | `crawler_news.py --list-url "<URL>" --max-pages 4 --allow-production` | `kuao_news.lock` | 3–15 分钟 |
| 2 | 新赛事发现（候选表） | 04:10 | 每天 | `discover_saikr.py --apply --allow-production` | `kuao_saikr.lock` | 2–5 分钟 |
| 3 | **AI 慢刷**（攻略+灵感） | 04:40 | 每天 | `daily_ai_worker.py --apply --allow-production --limit 4 --idea-limit 2` | `kuao_ai.lock` | 3–15 分钟 |
| 4 | 存量数据补全（保留项） | 05:10 | 每周日 | `pipeline_update.py --apply --allow-production --limit 20` | `kuao_pipeline.lock` | 10–30 分钟 |
| 5 | **日志清理（保留 7 天）** | 05:30 | 每天 | `find logs -type f -mtime +7 -delete` | 无（幂等） | <1s |
| 6 | 备份清理（保留 30 天） | 05:40 | 每周日 | `find backups -mtime +30 -delete` | 无 | <1s |

> `discover_university.py`（84 项目录）**不挂日任务**：名单年度更新时人工跑一次（原交接文档结论不变）。

### 3.3 完整 crontab

```cron
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
MAILTO=""
# 服务器时区必须是 Asia/Shanghai（Debian cron 不支持 CRON_TZ）

# 0) 每天 02:30 生产库备份（本地 + 对象存储，保留 30 天）
30 2 * * * cd /opt/kuao/backend && flock -n /var/lock/kuao_backup.lock ./venv/bin/python backup_db.py --db competitions.db --keep-days 30 >> logs/backup-$(date +\%F).log 2>&1

# 1) 每周一/周四 03:00 高校新闻获奖批量（写 awards / 占位赛事）
0 3 * * 1,4 cd /opt/kuao/backend && flock -n /var/lock/kuao_news.lock ./venv/bin/python crawler_news.py --list-url "<LIST_URL>" --db-path competitions.db --max-pages 4 --allow-production >> logs/news-$(date +\%F).log 2>&1

# 2) 每天 04:10 新赛事发现（只写 competition_candidates，不加 --force）
10 4 * * * cd /opt/kuao/backend && flock -n /var/lock/kuao_saikr.lock ./venv/bin/python discover_saikr.py --db-path competitions.db --apply --allow-production >> logs/discover-$(date +\%F).log 2>&1

# 3) 每天 04:40 AI 慢刷（攻略 3-5 条 + 灵感 ≤2 条；预算熔断）
40 4 * * * cd /opt/kuao/backend && flock -n /var/lock/kuao_ai.lock ./venv/bin/python daily_ai_worker.py --db-path competitions.db --apply --allow-production --limit 4 --idea-limit 2 --budget-tokens 300000 >> logs/ai-$(date +\%F).log 2>&1

# 4) 每周日 05:10 存量补全（会调模型，限 20 条）
10 5 * * 0 cd /opt/kuao/backend && flock -n /var/lock/kuao_pipeline.lock ./venv/bin/python pipeline_update.py --db-path competitions.db --apply --allow-production --limit 20 >> logs/pipeline-$(date +\%F).log 2>&1

# 5) 每天 05:30 日志清理（保留 7 天）
30 5 * * * find /opt/kuao/backend/logs -type f -name '*.log*' -mtime +7 -delete >> /opt/kuao/backend/logs/cleanup.log 2>&1

# 6) 每周日 05:40 本地备份清理（对象存储保留 30 天才删本地）
40 5 * * 0 find /opt/kuao/backend/backups -type f -name 'competitions.db.bak-*' -mtime +30 -delete >> /opt/kuao/backend/logs/cleanup.log 2>&1
```

### 3.4 顺序与依赖（不能反）

1. **备份必须在所有写库任务之前**（02:30 < 03:00），否则当天出错没有“干净快照”可回。
2. **新闻 → 发现 → AI**：新闻可能新建占位赛事；AI 只处理 `competitions`，所以 AI 放最后能吃到当天新增。发现只写候选表，对 AI 无影响，但按用户要求排在抓取后。
3. **周度补全放 05:10**，与 AI 慢刷错开 30 分钟，避免两个进程同时调模型 + 同时写库。
4. **日志清理放最后**（05:30），确保当天日志已写完再按 mtime 删。

### 3.5 「每 3 天」的坑与最终选择

- **已按用户决策改为固定周调度**：`0 3 * * 1,4`（每周一、周四），彻底避开 `*/3` 的月末跳变（31 日跑完次月 1 日又跑、2 月天数不同）。
- 若今后想要严格 72 小时，可在脚本外加 `run_every_n_days.sh 3` 的 mtime 守卫（语义最准），但当前周调度更可预测。
- cron 的 `date +\%F` **必须转义 `%`**，否则 cron 会把 `%` 当换行，日志文件名会截断。

### 3.6 日志 / 备份 / 清理的分层

| 对象 | 位置 | 保留 | 清理方式 |
|---|---|---|---|
| 任务日志 | `logs/*.log` | **7 天** | cron 第 5 条 |
| 本地 DB 备份 | `backups/competitions.db.bak-*` | 30 天 | cron 第 6 条（对象存储成功后才删） |
| 对象存储备份 | OSS/S3 | 30 天 | 桶生命周期规则 |
| `workspace/*.json` 报告 | `workspace/` | 30 天 | 每月手动或单独 cron |
| `competitions.db-wal/-shm` | 与 DB 同目录 | 常驻 | **不要删**；备份用 `.backup` API 自动处理 |

> ⚠️ 现有 `.bak-*` 都在 `backend/` 根目录（与源码混在一起）。上云时建议统一挪到 `backups/`，否则 7 天日志清理的 `find` 很容易误伤——这是最容易出事的细节。

### 3.7 与交接文档现有 cron 的差异

| 项 | 交接文档 | 本方案 |
|---|---|---|
| 新闻频率 | 每天 03:10 | **每周一/周四 03:00**（按最终决策） |
| 新赛事发现 | 有（04:10 赛氪） | 保留不动 |
| AI 慢刷 | **无** | 新增 04:40 |
| 日志清理 | **无** | 新增 05:30（7 天） |
| 备份 | 02:30 内联 python | 改为 `backup_db.py` + 对象存储 + 30 天清理 |
| 时区 | 未写 | 明确系统时区（不用 CRON_TZ） |

---

## 4. 自动化闭环里还剩哪些人工

| 环节 | 现状 | 目标态 | 备注 |
|---|---|---|---|
| 新闻抓取 | 手动 | cron 每周一/周四 | ✅ 本轮覆盖 |
| 新赛事发现 | 手动 | cron 每天 | ✅ 已有脚本 |
| 候选审核 | 人工 CLI | **仍人工**（80 pending + 每天 ~20 新增） | 合规底线：`CRAWLER_HANDOVER.md` 红线 16 禁止自动入主表 |
| 攻略/灵感 | 手动 | 按需 + 每日慢刷 | ✅ 本轮覆盖 |
| 语义索引同步 | 无任务 | 未覆盖 | `awards` 入库后 `embedding_state='stale'` 会一直脏，检索只认 `approved+ok`；建议后续单列一个 `sync_embeddings.py` 任务，**不在本轮范围** |
| 报名/比赛月份复核 | 人工 | 人工 | 16 条冲突、id=9 低置信、id=34 混合来源仍未裁决 |

> 审核是唯一无法“全自动”的环节，也是数据质量的红线。若一定要自动化，只能做“高置信度自动通过”（官网域名白名单 + 名称无营销词 + 封面过闸门 + 与已有赛事相似度低），并保留 7 天可回滚——**不建议本轮做**。

---

## 5. 决策结果（2026-11-08 已确认）

| # | 决策 | 最终选择 |
|---|---|---|
| 1 | 按需接口权限 | **必须登录**（JWT），不放匿名 |
| 2 | 触发方式 | **按钮触发**，不自动（详情页「✨ 生成 AI 攻略」） |
| 3 | 每日条数 | **攻略 4 + 灵感 2** |
| 4 | 热门口径 | **国家级 + 有官网 + 获奖数 + sort_order**，暂不加 `view_count` |
| 5 | 孤儿数据 | **直接删除**（已执行：insights 10 / ideas 12） |
| 6 | 新闻频率 | **周一/周四固定周调度**（避开 `*/3` 月末跳变） |
| 7 | 列表瘦身 | **同意**：列表移除 `full_process_guide`，只详情返回 |
| 8 | 生产库迁移 | **同意**：`ai_jobs` + 4 列，迁移前自动备份（已执行） |

---

## 6. 实施顺序（已按 A→F 完成）

| 阶段 | 内容 | 状态 |
|---|---|---|
| A | `db.py` 迁移（`ai_jobs` + 4 列）+ `ai_content.py` 抽取 | ✅ |
| B | `POST/GET generate_guide` + 前端按钮/轮询 + 列表瘦身 | ✅ |
| C | `daily_ai_worker.py`（选片/预算/报告/闸门） | ✅ |
| D | 生产库迁移 + 孤儿清理（自动备份） | ✅ |
| E | 测试（16 例新增 + 既有 91 例）与测试库真机 2 篇 | ✅ |
| F | 交接文档 + 本方案回填 | ✅ |
| 下一步 | 《云端部署手册》 + crontab 实机挂载 + 17 条空攻略分批回填 | ⬜ |

---

## 附录 A：本次实测用的只读核对命令

```bash
# 攻略覆盖
python -c "import sqlite3;c=sqlite3.connect('file:competitions.db?mode=ro',uri=True);\
print(c.execute(\"select count(*) from competitions where trim(coalesce(full_process_guide,''))<>''\").fetchone())"

# 候选状态
python -c "import sqlite3;c=sqlite3.connect('file:competitions.db?mode=ro',uri=True);\
print(list(c.execute('select status,count(*) from competition_candidates group by status')))"

# 孤儿灵感/洞察
python -c "import sqlite3;c=sqlite3.connect('file:competitions.db?mode=ro',uri=True);\
print(list(c.execute('select competition_id,count(*) from ideas group by competition_id')))"
```

> 一律用 `mode=ro` 只读连接，避免打开 WAL 写句柄改变库文件字节（排查 SHA 时这点很关键）。

## 附录 B：本次调研读过的代码位置

- `analyzer.py`：`generate()` 512–596、`main()` 641–672、Prompt 307–376、写库 416–509
- `generate_guides.py`：全文 277 行（Prompt 47–86、`main()` 125–228、重试 239–274）
- `db.py`：`_MIGRATION_COLUMNS` 212–230、`update_full_process_guide` 461–467、`list_competitions_for_guide` 687–702、`save_idea` 1457–1494、`init_db` 366–385
- `routers/competitions.py`：详情 117–131、列表 94–111
- `models/schemas.py`：`Competition` 20–90、`CompetitionDetail` 93–97
- `routers/ai.py`：inspirations 1029–1113、配额依赖 347/513/681
- `discover_common.py`：`backup_db` 36–61、`prepare_target` 64–81
- `llm.py`：`_call` 167–212（重试与 max_tokens 翻倍）、`chat` 219–237
- 前端：`CompetitionDetailView.vue` 165–201 / 367
