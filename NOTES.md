# NOTES

## 2026-11-23 数据体检 + 路线图（ROADMAP.md v1.0）

**结论**：85 个赛事里 26 个完全无日期、68 个无获奖作品；14,596 条获奖记录有 14,542 条只属于 2 个赛事（创新大赛 14,178 + 金种子杯 364）。
**根因**（不看代码会想不起来）：
- 26 个无日期赛事 **100% 来自 `edu:84` 名录导入**（`discover_source='edu:84'`），当年只写了 name+official_url，从未跑过官网抽取；其中 24 个有官网，技术上可补。
- `competition_facts_staging` 历史累计只有 applied 5 / rejected 13 → 抽取流水线设计完整但几乎没跑（cron 每周一次 `--limit 8` + 需人工逐条采纳）。
- `awards.detail_md` 填充率 0%、`highlights` 0.3%、`tech_stack` 2.8% → 现有数据是"获奖人员名单"，不是"作品"。
**连带影响**：AI 选题灵感/获奖共性分析以 awards 为唯一素材，`awardsCount===0` 时按钮置灰（`PreparationWorkspaceView.vue` L1074/L1203），所以 68 个赛事的 AI 面板实际不可用。
**下一步**：按 ROADMAP.md 里程碑 S0 → S1 推进；体检脚本在 `out/data_gap_*.py`（只读，不入库），复跑即可核对进度。

## 2026-11-23 S0 执行：日期 26 → 4

**做法**：脚本探测（`competition_facts.py`）先跑 26 个 dry-run + 11 个下钻，只碰出 2 个日期（#103 可信、#118 张冠李戴）；改成派 3 个子代理核查官方通知页，**26 个里 22 个拿到可用日期**，共写 64 个字段。
**为什么脚本不行**（关键，别再重试同一条路）：6 个站点是 SPA/JS 渲染（`requests` 拿到空壳）、3 个官网域名已失效（dtcup.dtxiaotangren.com / chinaskills.icve.com.cn / spbk.seentao.com）、其余首页只有导航，日期在通知页且栏目命名各异，`fetch_notice_pages` 的启发式基本打不中。**结论：官网抽取只配当探测，主力是"官方通知页 + 人工/AI 核查"。**
**写入方式**：`db.set_competition_facts(cid, fields, source_url, verified_by)`——与后台采纳同一条路径，自动写 `competition_fact_ledger` 并置 `facts_checked_at`，前台详情页直接显示「日期核对于 X」（`CompetitionDetailView.vue` L646）。写前已备份 `backups/competitions.before-dates-20261123-132939.db`。
**新增护栏**：`sanitize_facts` 加跨对矛盾检测（报名晚于比赛 / 比赛早于报名），丢比赛对保留报名对；测试 `backend/tests/test_facts_sanitize.py` 9 条。
**剩余 4 个**：#92 21世纪杯、#93 工行杯、#111 金砖大赛（官方只到月份）；#112 医学技术技能大赛（无全国赛通知）—— 下一年度通知发布后再补，不要再跑脚本。
**核查产物**：`out/dates_g1.md`、`dates_g2.md`、`dates_g3.md`（含来源 URL + 原文片段 + 可信度），`out/dates_plan.json` 是写入用的机器可读计划。

## 2026-11-23 赛事边界确认：只收 84 项 + 金种子杯

**用户明确要求**：主表只能是教育部 84 项学科竞赛 + 自己加的金种子杯，之前的野赛不许回来。
**当前状态**：`out/whitelist_audit.py` 跑出 `total=85 allowed=85 denied=0 missing=0` —— 正好 84+1，一条不多一条不少；我只 UPDATE 日期，从未 INSERT 赛事；线上无 `candidates` 表，也没有"待审野赛"回流通道。
**修的两个反向 bug**：
- `PROVINCIAL_RE` 含「选拔赛」，把白名单 #82「世界技能大赛中国选拔赛」误判成省级赛（`gate` 先把完全相等的白名单名放行，再做省级判定）。
- `saikr_whitelist_update.py` 用 `is_whitelisted()` 过滤，导致特批的金种子杯永远拿不到每日更新；改 `gate()`。
**以后动数据的规矩**：改前改后各跑一次 `out/whitelist_audit.py`；做 S1 获奖作品时只碰白名单内赛事；不扩容、不加省级赛。

## 2026-11-23 收尾状态（下次开工从这里接）

**线上**：https://www.kuao.xyz 跑在 db4fa64，kuao-backend / 
ginx 均 active；本地仓库干净（未跟踪的只有 out/、shots/、erify/，都不入库）。
**今天做完的**：① 登录/注册图形验证码 + 5 次错密码锁 15 分钟（已上线验收）；② 26 个无日期赛事 → 4 个（写 64 个字段，全部带 ledger）；③ 白名单 bug 两处；④ ROADMAP.md + NOTES.md 落盘。
**待办（按优先级）**：
1. **S1 获奖作品**——68/85 赛事 0 条，直接导致 AI 灵感按钮置灰。先做「获奖名单来源普查」：为 T1 十个头部赛事找到公示 URL 并验证可解析，再写 ward_ingest.py + ward_sources 表。别先写爬虫。
2. 剩 4 个赛事日期（#92 #93 #111 只到月份；#112 无全国赛）——等下一年度通知。
3. ROADMAP 里的 P0：截止提醒（邮件基建）、收藏云同步、SEO（线上确认无 robots.txt / sitemap.xml）。
**可复跑脚本**（都在 out/，只读，scp 到 /tmp 后用 PYTHONPATH=/root/KuAo/backend 跑）：data_gap_survey.py（数据体检）、whitelist_audit.py（赛事边界，必须 allowed=85 denied=0）、queue_check.py（事实队列）、pply_dates.py --dry-run（批量写日期）。
**坑**：PowerShell 直接 ssh kuao "python -c '...'" 引号必炸 → 一律写成 .py/.sh 文件 scp 过去再跑；读中文文件用 read 工具，不要 Get-Content。

## 2026-11-23 S1 获奖作品：6 个赛事 / 9,163 条上线

**做了什么**：先「来源普查」再写工具（ROADMAP 2.3 原计划的顺序是对的）。10 个 T1 赛事各派一个核查员实抓验证（产物 `out/award_survey/cid-*.md`），只有 6 个能当天自动化，然后写声明式工具 `award_ingest.py` + 新表 `award_sources` 入库。
**结果**：服创 1479 / 计设 3623 / 软件杯 603 / 信安 285 / 嵌入式 1200 / 数模 1973，上线已验证（`GET /api/competitions/21/awards` → total=1200）。奖项分布与官网正文自述逐项吻合（如服创 50/288/1141、计设 147/989/2487）。
**设计口径（以后加赛事照这个来）**：不加爬虫，加一行 `award_sources`（adapter + parse_spec JSON）；四个 adapter：html_table / xlsx_sheet / docx_table / pdf_table（mode=table 或 lines）；扫描件一律 failed 不 OCR。
**关键坑（不看代码想不起来）**：
- 计数对不上先查「合并单元格」：服创 103 页一队多行，不 fill_down+group_by 会把 1479 队写成 6446 条；PDF 段标题只印在段首页，奖项必须按行 y 坐标粘住跨页传递（否则数模 1900 多条全落到第一段）。
- vsb9 站点（计设 jsjds.blcu.edu.cn）附件裸 GET 必撞「请输入验证码」，必须先 GET 文章页建会话再带 Referer → parse_spec 的 `bootstrap_url` / `referer`。
- 前端与 AI 的「有获奖作品」判断只看 `review_status<>'rejected'`，**pending 是可见的**；想"审核后才上线"必须改代码，别以为写 pending 就挡住了。
- 服务器 venv 缺 pdfplumber/openpyxl/python-docx（requirements 有但没装），首次 `--all --apply` 只成功 HTML 那个；`pip install` 后重跑幂等（软件杯 603 条全部 skipped 不重复）。
**清掉的脏数据**：47 条 `source_type='mock'`（"四足机器人…/机器人方向第2组"）原为 approved，会进前台和 AI 素材，已全部标 rejected（可回滚）。真实覆盖率因此从"17"变 **9/85** —— 旧的 17 里有 12 个赛事只有 mock。
**下一步（不用再查来源）**：① 系统能力 #14 的 2025 届三份 PDF 有文字层，直接登记即可；② 4 个 OCR 赛事（#2 挑战杯 61 页扫描、#34、#71 坏 CID、#14-2026）；③ 重生成受 mock 影响的 AI 攻略/洞察（#21 正文仍在描述假数据）；④ `award_sources` 不在 sync_prod_safe 白名单，跨机同步靠 `--seed` 在服务器自建。

**部署状态**：提交 `ecba3f3` 已 push 到 GitHub master；服务器 `/root/KuAo/backend/` 已同步 award_ingest.py / award_sources.seed.json / db.py / schema_v2.sql 并装好 pdfplumber 0.11.10 + openpyxl 3.1.5 + python-docx 1.2.0（原 venv 缺这三个，requirements 里有但没装）。

### 2026-11-23 续：系统能力 #14 补到 2025 届 + 修掉两个静默丢数据的 bug

**新增入库**：#14 三份 2025 届 PDF（编译 59 / OS 126 / DB 48 = 233 条），全库 9,396 条、覆盖赛事 **10/85**。
**顺手挖出的 bug（重要，不看代码想不到）**：
1. `db._AWARD_WRITABLE_FIELDS` 里**没有 `session` / `track`** —— `create_award` 按这个元组裁剪，于是这两个字段被静默丢弃，而我写的 `content_hash` 又是带 track 算的。修好后历史 7,190 行用 `backend/backfill_award_track.py`（重解析→按 content_hash 对齐→UPDATE）补齐，不删不重建。
2. 官方 PDF 常把奖项写成「特等/一等/优胜」，全库既有口径是「特等奖」→ 新增 `_normalize_grade()`（裸档位补「奖」字，`parse_spec.grade_map` 可精确覆盖）。
3. PDF 单元格折行（`哈尔滨工业大学\n（深圳）`）会带着换行入库 → `_clean()` 里把换行压成空格并去掉括号前空格。
**下次加赛事记住**：`award_ingest.py` 的 `parse_by_adapter()` 是唯一分发口；解析新站点先 `--source-id N --print-rows 5` 看候选，再 `--apply`。
**还剩**：OCR 管线（#2/#34/#71/#14-2026）+ 重生成受 mock 影响的 AI 攻略。

### 2026-11-23 第三批：T2 八路并行普查 → 又 +4 个赛事 / 2,350 条

**打法（可复制）**：一次派 8 个核查员，每人一个赛事，只在官方站点找「名单页→附件直链」，实体抓下来用 pdfplumber/bs4 真解析一遍，报告里必须给 adapter + parse_spec + 抽样行。结果：**可用 4 / 需 OCR 1 / 需翻页 1 / 版式极端 1 / 宕机 1**。
**入库**：外研社 #68 1,103（静态 HTML 表）· 统计建模 #59 891（67 页 PDF，与官方 90+174+627 差 0）· 结构设计 #63 250（PDF，44+63+143）· 物流 #67 105（2024 第八届，2025 届只在微信文章里、requests 撞验证码）。随后把 #68 其余 **13 个赛项页全登记**（短视频 525 / 笔译 285 / 多语种 9 页 649 / 口译 189），cid=68 合计 **2,751 条**。全库 **27,989 条 / 14 个赛事**（今日净增 13,393 条、9 个赛事）。
**新增能力**：`parse_spec.grade_allow`（奖项白名单，"优秀组织奖/机构奖"和混入的表头行一次滤掉）；`header_regex`（跨页重复表头兜底）。
**踩到的坑**：
- 结构设计名单一开始 0 条——不是解析失败，是 `positional` 没给 `title`，被 `drop_if_empty=["title"]` 全丢掉；解决办法是 `title_template: "{school}·{members}"`。看到「解析 254 / 写入 0」先查 drop_if_empty。
- 大广赛 #64 名单是 15 行/页 × 19 类目 × ~120 页（一届约 2,280 请求），当前一个来源=一个 URL 的设计扛不住，要加 paging 再登记。
- 机械创新 #62 是双线边框（5 列被认成 11 列）+ 序号单元格跨行，通用 pdf_table 做不了，子代理留了专用解析器 `out/probe_62_parse.py`（可复现 801 行）。
- 智能汽车 #65 官网 SPA 的后端 `82.157.23.29:5001` 已宕机 → 记 blocked，别反复试。

**本轮提交（都在 master，已 push）**：`ecba3f3` S1 工具+6 赛事 → `9aff2c1` #14 + session/track 修复 → `c58feba` 第二批 4 赛事 → `5dea34d` 外研社 13 赛项页。线上库备份：`/root/KuAo/backend/backups/competitions.db.bak-*`（每次 `--apply` 自动建）。

### 2026-11-23 第四批：单场上线 300 条（金>银>铜）+ 日期脏数据体检

**产品口径（用户定）**：每场赛事最多上线 **300** 条获奖作品，截断时按「金 > 银 > 铜」优先级保留，库里数据一条不删。实现在 `db.list_awards_by_competition()`：`PUBLIC_AWARDS_CAP=300` + `_AWARD_GRADE_RANK_SQL`（特等/总冠军 0 > 金/一等 1 > 银/二等 2 > 铜/三等 3 > 优胜/优秀/入围 4 > 其它 5），`offset` 越不过 300、`limit` 自动夹到剩余额度；路由 `total` 也封顶到 300，否则前端「加载更多」会翻到空页。
**为什么按关键词分层**：85 个赛事的奖项写法不统一（`本科组一等奖`、`金奖（冠军）`、`总冠军（一等奖）`、`二等奖(最佳创意奖)`），字符串相等会全落进「其它」。
**日期脏数据**：新增 `backend/audit_dates.py`（R1 报名窗口=比赛窗口且无报名月份 → 日期是抄的；R2 起止倒挂；R3 粒度不一致）。线上命中 2 条：#110 世界技能大赛（原文只写「比赛时间 9/22-27」，报名列却是同一段 → 报名清 NULL）、#13 软件杯（原文只有报名 3/24-6/30，比赛列是抄的 → 比赛清 NULL，赛段细节仍在 `stage_schedule`）。**结论：这 2 条都是补全流程把比赛日期复制进报名列，不是前端兜底**（`timeFormat.formatSignUp` 是诚实渲染，没有 fallback）。
**用户最终口径（2026-11-23）**：名单型记录**直接不收录**——「挖不到作品名就显示暂未收入获奖作品」。已执行：`purge_roster_awards.py` 删掉 4,973 条（外研社 2,751 / 数模 1,972 / 结构设计 250，删除前备份到 `out/purged/roster-awards-*.json`），再删 1 条解析残渣（`title='平'`，`purge_junk_rows.py`）。线上现 **22,968 条 / 11 个赛事**；#60/#63/#68 的获奖 Tab 返回 `total:0`，前端显示「暂未收入获奖作品」+「只有官方公示了作品名的赛事才会收录」。
**防复发**：`award_ingest.py` 支持 `parse_spec.skip`（命中就标记来源 `skipped`、不抓不算），seed 里给这 16 条来源都打了 skip，否则下次 `--all --apply` 又会把名单塞回来。
**另**：前端构建产物已重新 build 并 rsync 到 `/var/www/html`（`--exclude covers` 保住用户上传封面）；`/root/KuAo/frontend/dist` 是 Nov 14 的旧壳，**线上真正生效的是 /var/www/html**。

### 2026-11-23 第五批：把能补的都补了（+12,083 条 / +4 个赛事）

**线上现 35,051 条 / 15 个赛事**（本日 14,596 → 35,051）。这轮全靠并行子代理，5 路里 4 路出活：

| 赛事 | 来源 | 条数 | 关键点 |
|---|---|---|---|
| 大广赛 #64 | 官网分页 HTML，19 类目 × 500 页 | **7,219** | 一条来源 = 一个类目 + `url_template`/`page_count`，seed 不塞 500 个 URL；`class` 拼错会静默返 0 行，越界返回 200 要用「数据行==0」判停 |
| 挑战杯小挑 #3 | 中国青年网 3 个 xlsx（官方站只有扫描件） | **2,397** | track 来自「赛事名称」列；主体赛铜奖 1261 比官方 1262 少 1 行是发布侧缺行 |
| 挑战杯大挑 #2 | 官网第十八届授奖名单 PDF（131 页有文字层） | **1,671** | 段标题单独占行，`drop_if_empty` 必须同时含 title+school，否则多 8 条 `title=特等奖` 假记录；标题内折行要 `squeeze_cjk_space` |
| 机械创新 #62 | 双线边框 PDF | **800** | 新增 adapter `pdf_dual_border`（竖线聚类去重 + 序号跨行 + 段标题等级），复现 801 行，1 条同名被去重 |

**新增能力**：`parse_spec.url_template/page_count`（分页来源）、`members_split=space`、`strip_chars`、`ignore_columns`、`squeeze_cjk_space`；adapter `pdf_dual_border`。单测 70 passed。
**赛事信息补全**：`apply_meta_patch.py` + 子代理产出 `out/meta_patch.json`（11 赛事 / 19 字段，每条带官方 URL + 逐字引用），线上官网为空 5 → **1**，报名已截止且无比赛日期 7 → 6。**没补的 12 个赛事都是 SPA/需登录/无独立官网**（如 #14 compiler.educg.net、#34 robotreg、#96 cuidc.net），别重复试。
**没做的两场（有据可查）**：
- #61 电子设计：全国名单只有 52 页扫描件；上海赛区 486 行有文字层但**只有题号没有作品名**（按口径不收）；能直连带作品名的只有高校新闻页小样本（北华航天 11 行，另 4 校共 24 行），样本太小会让覆盖率失真，**主动放弃**。
- #65 智能汽车：真正的官网是 `smartcar.zone`（不是 SPA 的 smartcarrace.com），但总决赛 4 份 PDF 全是整页位图，无 OCR 环境 → 0 条。
**教训**：`award_ingest.py --apply` 会先备份库；批量跑多个 source 时不要在 `backend/` 目录下再写 `backend/...` 相对路径（会找不到文件），也别用 `Select-String 'pdf '` 过滤输出——有的行没有格式列，会把整批的"已写入"误判成失败。
**第二个工具 `backend/audit_data_health.py`**（只读体检，10 条规则）：线上结果 = 月份/日期不自洽 0、年份离群 0、表头误入 0；剩下的都是**已知且可接受**的：`cid=67` 15 条专项奖等级为空（名单里专项奖没有等级列）、`cid=7/10` 12 条单字队名（合法）、48 组「同赛事+同标题+同校+同奖」重复（多为不同队伍同名项目，`content_hash` 带 track/session 才没被去重）、13 个占位赛事只剩报名窗口、5 条 01-01/12-31 疑似占位日期（cid=65 的 `2026-01-01` 最可疑）。
**踩坑（重要）**：R1b 一开始 assume「有月份的那一侧是真实的」，对 #110 是**反的**——它把真实的比赛日期清掉、留下了抄来的报名日期，线上详情页一度变成「报名 9/22-27、比赛待定、状态已截止」。现在改成用 `stage_schedule` / `exam_schedule` 的原文证据判方向：出现「报名」→ 窗口属报名侧（清比赛）；只出现「比赛/决赛/开幕式」→ 窗口属比赛侧（清报名）；两边都有证据就**不猜**、只报给人看。教训：**任何「自动订正」都必须能说清依据来自哪段原文**，否则宁可不动。
**审计脚本里不要写 `SELECT ... FROM competition_whitelist`**——白名单不落库，是 `competition_whitelist.py` 里的封闭元组，要 import 后算。

### 2026-11-23 收尾：S1 获奖作品告一段落

**结论**：S1「获奖作品」这一阶段收工。线上 **35,051 条 / 15 个赛事（18%）**，早上是 14,596 条 / 5 个真赛事。
- 今天新增 9 个赛事、20,455 条：大广赛 7,219 · 小挑 2,397 · 大挑 1,671 · 服创 1,479 · 嵌入式 1,200 · 统计建模 891 · 机械创新 800 · 软件杯 603 · 系统能力 233（细节见上面第 3/5 批）。
- 名单型（只有获奖者、没有作品名）**一律不收**：数模 1,972 / 外研社 2,751 / 结构设计 250 已删，seed 里打了 `parse_spec.skip` 防复发。
- 每场上线封顶 **300 条**，按「金 > 银 > 铜」优先级截断（`db.PUBLIC_AWARDS_CAP`）。
- 赛事信息补全 11 赛事 / 19 字段（官方 URL + 逐字引用），官网为空 5 → 1。

**明确不做（用户已拍板）**：OCR。理由：电子设计 52 页、智能汽车 9 页都是整页位图，OCR 认错人名/英文队名且无法与官方小计交叉校验，宁可显示「暂未收入获奖作品」。要做的时候按「先 9 页小样本验准确率 ≥98% 再全量」走，见上一轮对话。

**下次开工先看**：
- 数据现状：`cd backend && venv/bin/python breakdown.py`（按赛事列条数）
- 体检：`venv/bin/python audit_dates.py`、`venv/bin/python audit_data_health.py`
- 加赛事：`award_ingest.py --seed` → `--source-id N --print-rows 3` → `--apply`；新站点先看 `CRAWLER_HANDOVER.md` §0.9
- 覆盖率分母：`competition_whitelist.WHITELIST`（85 个，不扩容）

**还没动的活**：① 74 个零获奖赛事的补全（多数只能等官网发文字版名单）；② 13 个「只剩报名窗口」的占位赛事要等新一届通知；③ 前端「获奖作品」Tab 目前一次最多展示 300 条，若用户要「看全部」需要加「查看完整名单」的二次入口；④ 受 mock 数据影响的 AI 攻略仍待重生成。

### 2026-11-23 后台瘦身 + 去掉「日期核对」提示

**用户决定**：详情页那行「日期尚未人工核对／日期核对于 X」整条删除（含已核实的），理由是对用户没价值；后台「候选赛事」「赛事核对」两个面板一并下线，因为候选池/事实队列早就没人处理了（历史 5 采纳 / 13 忽略）。

**做法**：只删前端，后端接口与 competition_candidates / competition_facts_staging 两表**保留**（真删要备份，恢复成本高）。AdminView.vue 1733 → 1187 行；面板顺序改为 赛事管理 / 用户反馈 / 更新日志，默认 tab 从 candidates 改为「赛事管理」，旧深链 ?tab=candidates|facts 自动纠正到默认面板并改写地址栏（不会白屏）。
**注意**：删第一个面板后，剩下的第一个 <template> 必须从 -else-if 改成 -if，否则 Vue 编译报 Cannot read properties of undefined (reading 'type')。
**顺手停掉**：prod crontab 里每周一 05:10 的 competition_facts.py --limit 8（队列没人采纳，白烧 LLM），原行已注释，crontab 备份在 /tmp/crontab.bak.*。
**保留**：赛事管理列表里的「日期」按钮 + 校正弹窗（唯一手改数据入口），弹窗文案去掉了「前台会显示信息核对于今天」。
