# backend/tests/tools —— 验收 / 回归脚本

这些脚本从早先的本地临时目录 `.verify/` 迁移而来（`.verify/` 已加入 `.gitignore`，只作本机取证用）。
迁移时把所有硬编码的 `D:\agent-lab\KuAo-Platform\...` 改成了**相对仓库根**的路径，临时库统一写到
`%TEMP%\kuo-verify\`，因此 clone 到任何机器上都能直接跑，不会写脏 `backend/competitions.db`。

## 隔离库三件套（改审核/补全逻辑时用）

```powershell
$py = "backend\venv\Scripts\python.exe"

# 1) 复制生产库到 %TEMP%\kuo-verify\approve_api_test.db，并选一条可审候选
& $py backend\tests\tools\prep_test_db.py

# 2) 用隔离库起后端（端口 8010，绝不碰 8000 上的生产库）
& $py backend\tests\tools\run_api_patched.py

# 3) 另开一个窗口，走真实 HTTP 审批（candidate_id 由第 1 步打印）
& $py backend\tests\tools\smoke_approve.py <candidate_id>
```

`smoke_approve.py` 会自己用隔离库里的管理员账号签 token，并把入库结果逐字段打出来。

## 单项回归

| 脚本 | 命令 | 说明 |
|---|---|---|
| `verify_admin.py` | `& $py backend\tests\tools\verify_admin.py` | 后台接口全量验收（反馈已读、越权 403、通过/拒绝/重复审批 400），临时副本库 |
| `check_smart_order.py` | `& $py backend\tests\tools\check_smart_order.py` | 智能排序回归：17 行合成数据 + 月份兜底 + 脏值沉底 + 热门筛选 |
| `stats_dates.py`（只读） | `& $py backend\tests\tools\stats_dates.py` | 日期轨覆盖率盘点 |
| `audit_cover_fallback.py`（只读） | `& $py backend\tests\tools\audit_cover_fallback.py` | 封面兜底审计：兜底比例、简称撞名、色相撞色 |
| `wcag_contrast.py`（纯计算） | `& $py backend\tests\tools\wcag_contrast.py` | 兜底配色 WCAG 对比度，口径对齐 `frontend/src/assets/main.css` |
| `find_enrichable_candidate.py`（只读） | `& $py backend\tests\tools\find_enrichable_candidate.py` | 找「应能补全成功」的 pending 候选 |
| `seed_feedback.py`（**写库**） | `& $py backend\tests\tools\seed_feedback.py` | 只对本地开发库插 2 条测试反馈，勿在生产执行 |

前端侧还有两个 Node 脚本（位于 `frontend/tests/`）：

```powershell
D:\Tools\node.exe frontend\tests\timeformat.check.mjs     # 双轨日期 / 倒计时 / 跨年 / 非法日期
D:\Tools\node.exe frontend\tests\official-url.check.mjs   # 官网链接判定（名录页、非法协议必须拒绝）
```

## 约定

- 新脚本请放这里，**不要**再放回 `.verify/`（已忽略，不会进版本库）。
- 写库脚本一律先落到 `%TEMP%\kuo-verify\` 下的副本库；只有标注「写库」的脚本会碰 `backend/competitions.db`。
- 脚本内路径用 `Path(__file__).resolve().parents[3]`（或等价的多层 `dirname`）取仓库根，禁止再写死绝对路径。