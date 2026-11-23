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
