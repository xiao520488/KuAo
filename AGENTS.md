# AGENTS.md — 库奥平台（KuAo-Platform）

## 会话工作规则（standing rules）

1. **上下文要在任务执行中压缩，不要等收工才压。**
   - 触发点（任一满足即压）：① 阶段交界——探索/读文件收尾进实现前、批量验证收尾进下一轮修改前；② 体量估算已过 ~40K，或接近 50K；③ 出现"这个结论好像前面看过"、需要回头翻早前输出、需要重新读已读文件的信号。
   - 方式：`compact_context` 带上 `retain_tokens`（约 12000–15000），把当前正在做的那一步逐字保住（文件路径、报错原文、待验证项），只压缩陈旧的探索过程。
   - 同一任务内允许压缩多次；宁可提前压，不要临溢出紧急截断。
   - 每份压缩摘要都要保留：本规则、用户当轮明确要求、未完成的下一步。
2. **少想多做，省 token。** 不在一处反复推演；能并行的工具调用放同一个消息里；不回读刚读过的文件；结论给要点，不复述过程。
3. **每轮只问一次。** 只有出现真正的分歧点（会改变产物的选择）才发问，其余按最合理方案直接做。

## 环境事实（Windows）

- Shell 只有 PowerShell；npm 被 ExecutionPolicy 拦住，统一用：`D:\Tools\node.exe D:\Tools\node_modules\npm\bin\npm-cli.js <run build|run dev|run preview>`。
- 前端 Vite dev：`http://localhost:5173`（不要用 127.0.0.1，Vite 只监听 localhost/IPv6）；后端 FastAPI：`http://127.0.0.1:8000`，`/api` 由 Vite 代理。
- **坑**：dev server 运行中做原子写入式编辑（临时目录 + ReplaceFileW）会触发 Vite 的 `EBUSY` 并让 watcher 崩溃。批量改动前先停 dev，或用 `vite preview` 看构建产物再改。
- 验证手段：Chrome headless `--headless=new --screenshot --window-size=W,H --virtual-time-budget=30000`，再用 `read_image` 看结果；大图可用 bundles 里的 Python + Pillow 裁剪局部。

## 图片展示规范（已定稿，勿回退）

- **列表卡片**（`frontend/src/components/CompetitionCard.vue`）：`cover-media` 容器固定 `aspect-[16/9]`，图片 `w-full h-full object-cover object-center`——海报式裁切、铺满、不留白、不变形。布局 `flex-col sm:flex-row`，桌面端图占 42%、上限 430px。
- **详情页头图**（`frontend/src/views/CompetitionDetailView.vue`）：不设死高度，`w-full h-auto` + `max-height: 400px` + `object-contain`，容器底色 `#f8f9fa`（暗色 `slate-800/60`）承接左右留白——**任何情况下不裁掉海报文字**。
- 无图兜底（`frontend/src/utils/coverFallback.js`）为独立渐变层，铺满容器即可，逻辑不用改。
