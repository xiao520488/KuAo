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
