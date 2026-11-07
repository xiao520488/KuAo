# 前端 Code Review 报告（交付前）

| 项目 | 内容 |
| --- | --- |
| 审查对象 | `KuAo-Platform/frontend`（Vue 3 + Vite + Tailwind） |
| 审查时间 | 2026-11-07 |
| 审查方式 | 全量静态代码阅读 + 构建产物（`dist/`）体积分析；**未运行测试脚本、未做真机与压测** |
| 覆盖范围 | `src/` 下 23 个 Vue/JS 源文件、`index.html`、`vite.config.js`、`tailwind.config.js`、`package.json`、`.env`、`.gitignore` |
| 结论 | **暂不建议直接上线**：存在 3 个阻断级问题（接口地址、密钥、鉴权与本地缓存边界），另有 2 个高优体验/性能问题（首屏图片、深链登录回跳） |

---

## 0. 结论摘要

### 0.1 上线判定

| 维度 | 评价 | 说明 |
| --- | --- | --- |
| 功能完整度 | 较好 | 首页检索、列表筛选、详情、备赛列表、AI 计划、作品分析、灵感收藏均已打通 |
| 异常兜底 | 较好 | 存储、Markdown、请求竞态、主题闪白均有针对性处理，注释质量高 |
| 可部署性 | **不通过** | API 地址硬编码 `127.0.0.1:8000`，生产环境必然不可用 |
| 安全边界 | **不通过** | 真实 API Key 落在前端目录；登录态失效后本地缓存不清、缓存不区分用户 |
| 性能 | 待改进 | 首屏 965 KB 背景图 + 约 300 KB JS/CSS；无响应式图片与懒加载 |
| 可访问性 | 待改进 | 多处 2.6:1 对比度、弹窗无焦点陷阱、卡片语义不合法 |
| 可维护性 | 待改进 | 映射/格式化逻辑三处重复、无 Lint/测试/CI、根目录残留大量补丁脚本 |

### 0.2 必须先修的 5 件事

| 顺序 | 事项 | 对应问题 | 预估工作量 |
| --- | --- | --- | --- |
| 1 | API Base URL 改为环境变量 + 同源反向代理 | FR-01 | 0.5 天 |
| 2 | 轮换并移除前端目录中的 DeepSeek Key，补 `.env.example`、补 `.gitignore` | FR-02 | 0.5 小时 + 轮换 |
| 3 | 登录态失效/退出时清理用户态缓存，缓存按用户隔离，401 后离开受保护页 | FR-03 | 1 天 |
| 4 | 首屏背景图压缩为 AVIF/WebP + 响应式尺寸，删除重复大图 | FR-04 | 0.5 天 |
| 5 | 登录成功后回跳原目标路由；深链场景补 `redirect` | FR-05 | 0.5 天 |

### 0.3 值得保留的设计（做得好的地方）

1. **检索竞态处理**：`HomeView` 用 `requestId` + `unmounted` 标记保证只有最后一次请求能写 UI，且 `finally` 无条件复位 loading，避免骨架屏卡死（`src/views/HomeView.vue:252-315`）。
2. **切赛事守卫**：工作台对计划生成、共性分析、作品解析、灵感加载都做了 `cid` 快照比对，过期响应直接丢弃（`PreparationWorkspaceView.vue:302-357`、`495-525`、`639-681`、`777-800`）。
3. **XSS 防护意识**：Markdown 统一走 `marked → DOMPurify.sanitize`，且所有 `v-html` 均来自该管道（`src/services/markdown.js`）。
4. **存储降级**：`localStorage` 的读写、探测、隐私模式异常全部 `try/catch` 静默降级，不会阻断主流程（`auth.js`、`planCache.js`、`useTheme.js`）。
5. **首屏防闪白**：`index.html` 内联脚本在 CSS 加载前写入主题 class，避免 FOUC（`index.html:8-25`）。
6. **动效可关闭**：首页波浪、卡片入场均适配 `prefers-reduced-motion`（`HomeView.vue:763-768`、`810-817`）。
7. **路由懒加载**：除首页外全部 `() => import()`，首屏未打包工作台等重页面（`src/router/index.js:18-42`）。
8. **缓存读取归一化**：`planCache` 读取时对 role/content/长度做结构校验，脏数据不会进入渲染层。

---

## 1. 风险总览

| ID | 级别 | 主题 | 位置 | 一句话描述 |
| --- | --- | --- | --- | --- |
| FR-01 | **P0** | 部署 | `src/services/api.js:8` | 后端地址硬编码 `http://127.0.0.1:8000`，线上无法访问且 HTTPS 下会被拦截 |
| FR-02 | **P0** | 安全 | `.env:1`、`.gitignore` | 真实 DeepSeek Key 存放在前端目录，`.gitignore` 未忽略 `.env` |
| FR-03 | **P0** | 鉴权/隐私 | `main.js:15-17`、`api.js:83-88`、`planCache.js:59-61` | 401/退出后不清本地计划与收藏；缓存 key 不含用户，换账号可读到上一个人的数据 |
| FR-04 | P1 | 性能 | `HomeView.vue:728`、`src/assets/` | 首屏背景图 965 KB 且有一份 965 KB 重复文件；无响应式图 |
| FR-05 | P1 | 体验 | `router/index.js:78-81` | 被拦截后登录成功，不会回到原目标路由 |
| FR-06 | P1 | 状态隔离 | `PreparationWorkspaceView.vue:35-53`、`461-481` | 切换赛事时详情/获奖列表请求无过期守卫，旧响应会覆盖新赛事 |
| FR-07 | P1 | 数据正确性 | `HomeView.vue:95`、`CompetitionsView.vue:191` | 用数组下标伪造竞赛 ID，ID 缺失时会跳到错误详情 |
| FR-08 | P1 | 存储 | `planCache.js`、`PreparationWorkspaceView.vue:193-216` | 计划正文无长度上限、无淘汰/TTL，写满配额后每次都静默失败 |
| FR-09 | P1 | 可访问性 | 全站 | `text-slate-400` 在白底约 2.6:1，低于 WCAG AA 4.5:1 |
| FR-10 | P1 | 安全 | `CompetitionDetailView.vue:169-172`、`PreparationWorkspaceView.vue:622-628` | `official_url` / `originUrl` 未校验协议，`javascript:` 可直接执行 |
| FR-11 | P1 | 稳定性 | `PreparationWorkspaceView.vue:705-714` | 收藏缓存被篡改成对象时，`.includes` 抛错导致灵感 Tab 白屏 |
| FR-12 | P1 | 样式 | `PreparationWorkspaceView.vue:1844-1897` | `scoped` 样式作用于 `v-html` 内部节点会失效（缺少 `:deep()`） |
| FR-13 | P1 | 性能 | `HomeView.vue:109-132`、`CompetitionsView.vue:17-35` | 每张卡片单独请求一次备赛状态，列表页 N 次并发 |
| FR-14 | P2 | 鉴权 | `auth.js:126-146` | 恢复会话时任何异常（含断网）都会清 token，离线即被登出 |
| FR-15 | P2 | 鉴权 | `auth.js:68-75` | `/me` 失败时 token 已写入但用户态为空，状态不一致 |
| FR-16 | P2 | 路由 | `router/index.js:44-47` | 404 全部重定向首页，掩盖错误且无法定位坏链接 |
| FR-17 | P2 | 部署 | `router/index.js:7`、`vite.config.js` | 使用 History 路由但未约定服务端 fallback，刷新子路由可能 404 |
| FR-18 | P2 | 交互 | `AuthModal.vue:112-118`、`Header.vue:205-208`、`PreparationWorkspaceView.vue:440-443` | 多个组件直接覆写 `body.overflow`，互相解锁导致背景可滚动 |
| FR-19 | P2 | 交互 | `AuthModal.vue:69-72` | 提交中禁止关闭/取消，60s 超时期间用户被困 |
| FR-20 | P2 | 可访问性 | `AuthModal.vue:136-141` | 弹窗无焦点陷阱、无 `aria-labelledby`、关闭后焦点不归还 |
| FR-21 | P2 | 可访问性 | `CompetitionCard.vue:111-120` | `role="link"` 卡片内嵌按钮，仅响应 Enter 不响应 Space |
| FR-22 | P2 | 性能 | `CompetitionsView.vue`/`HomeView` | 卡片图片无 `loading="lazy"`、`decoding="async"`、`srcset`、失败占位 |
| FR-23 | P2 | 安全 | `api.js:35-50`、`auth.js:29-36` | Token 存 `localStorage`，任意 XSS 可窃取；退出未做服务端失效 |
| FR-24 | P2 | 一致性 | `Header.vue:109-115` vs `useToast.js` | 两套 Toast 实现，样式/时长/可访问性不一致 |
| FR-25 | P2 | 可维护性 | `HomeView.vue`、`CompetitionsView.vue`、`PreparationView.vue`、`competitionMapper.js` | 月份解析/报名时间/级别映射重复 3 份，`competitionMapper.js` 无人引用 |
| FR-26 | P2 | 体验 | `PreparationWorkspaceView.vue:405-430` | 发送失败后输入框已清空，用户需重新输入 |
| FR-27 | P2 | 体验 | `CompetitionCard.vue:77-82` | 加入/移出备赛失败只 `console.error`，用户无感知 |
| FR-28 | P2 | 数据 | `api.js:151-159`、`359-366` | 列表响应结构异常时静默返回 `[]`；`checkPrepared` 吞掉所有错误返回 `false` |
| FR-29 | P2 | 主题 | 全站 | 硬编码颜色与 `bg-white` 绕过主题变量，官方风/极简风在细节处不一致 |
| FR-30 | P3 | 工程化 | 仓库根目录 | 30+ 个一次性补丁脚本、`venv/`、`dist/`、0 字节文件混在项目根目录 |
| FR-31 | P3 | 工程化 | `package.json` | 无 Lint/格式化/单测/E2E/CI；`audit:dark` 绑定 Windows 虚拟环境 |
| FR-32 | P3 | 工程化 | `index.html:5` | `favicon.svg` 不存在，构建产物同样缺失，线上 404 |
| FR-33 | P3 | 可观测性 | `main.js` | 无全局错误处理、无埋点/监控、无版本号 |
| FR-34 | P3 | SEO | `index.html` | 无 description/OG/每路由 title，SPA 分享与检索表现弱 |

---

## 2. P0 阻断项（上线前必须解决）

### FR-01 后端地址硬编码，生产环境不可用

**位置**：[`src/services/api.js:8`](src/services/api.js#L8)

```js
baseURL: 'http://127.0.0.1:8000',
```

**问题**

- 构建后该字符串被写死进 bundle，部署到任何非本机环境都无法访问后端。
- 若站点以 HTTPS 提供，浏览器会以 Mixed Content 直接拦截 `http://127.0.0.1:8000` 请求。
- `.env` 中只有 DeepSeek 的配置，没有任何 `VITE_` 变量，说明构建期注入链路缺失。

**建议**

1. 改为 `baseURL: import.meta.env.VITE_API_BASE_URL || '/api'`，本地 `.env.development` 指向 `http://127.0.0.1:8000`。
2. 生产推荐同源反向代理（Nginx `/api` → 后端），同时消除 CORS 与 Mixed Content。
3. 在 `vite.config.js` 增加 `server.proxy`，开发期也不跨域。
4. 增加构建时校验：`VITE_API_BASE_URL` 缺失时 `vite build` 直接失败，避免带错配置发版。

---

### FR-02 前端目录存在真实 API Key

**位置**：[`.env:1`](.env#L1)、[`.gitignore`](.gitignore)

**问题**

- `.env` 内含真实 `DEEPSEEK_API_KEY`（本文不复制明文）。该文件与前端源码同目录，极易被打包脚本、Docker build context、`git add .` 一并带走。
- `.gitignore` 只忽略了 `node_modules`、`dist`、`*.local`，**没有忽略 `.env`**。目前该文件尚未被 Git 跟踪，但只要一次 `git add .` 就会入库。
- 前端任何 `VITE_` 变量都会进入 bundle，Key 放在前端本身即错误设计。

**建议**

1. 立即在 DeepSeek 控制台**吊销并轮换**该 Key（视为已泄露）。
2. 从 `frontend/` 删除 `.env`；Key 只放后端运行环境（如后端 `.env` / Secret Manager）。
3. `.gitignore` 增加 `.env`、`.env.*`、`!.env.example`；补 `.env.example` 只放占位值。
4. 增加提交前扫描（`gitleaks` / `detect-secrets`）作为 CI 门禁。

---

### FR-03 登录态失效后的数据边界不清（隐私风险）

**位置**：[`src/main.js:15-17`](src/main.js#L15-L17)、[`src/services/api.js:83-88`](src/services/api.js#L83-L88)、[`src/services/planCache.js:59-61`](src/services/planCache.js#L59-L61)、[`src/views/PreparationWorkspaceView.vue:687`](src/views/PreparationWorkspaceView.vue#L687)

**现象**

1. 401 时只执行 `clearAuthStorage()`（删 token）并派发事件，`logout()` 也只清 `authState.user`；**不会**清理：
   - `plan_${competitionId}` 备赛计划与对话；
   - `prep_favorites_v1` 收藏；
   - 页面上已经渲染出来的备赛数据。
2. 计划缓存 key 只含竞赛 ID，收藏 key 是全局常量，**都不含用户标识**。同一浏览器切换账号后，B 用户进入同一竞赛会直接恢复 A 用户的计划、对话与收藏。
3. 401 后用户仍停留在 `/preparation/workspace/:id` 等受保护页面，界面保留上一账号内容，直到手动刷新。

**影响**

- 共享电脑/实验室机房的真实隐私泄露；对“备赛计划”这类个人内容属于高敏感。
- 与“登录才可见”的产品预期冲突，验收时会被判定为越权展示。

**建议**

1. 缓存 key 命名空间化：`plan_v2_${userId}_${competitionId}`、`prep_favorites_v2_${userId}`；`userId` 取 `/me` 的稳定主键（无则用用户名哈希）。
2. 统一 `session:expired` 处理：清 token → 清当前用户内存态 → **清或隔离本地缓存** → `router.replace({ name: 'home', query: { redirect: to.fullPath } })` 并提示“登录已过期”。
3. 退出登录提供“保留本机缓存/清除本机缓存”选项；默认至少清除当前用户命名空间。
4. 若产品要求离线可用，则缓存加密或明确告知“本机缓存可能被同设备其他账号看到”。

---

## 3. 鉴权与路由边界

### FR-05 登录成功后不回跳原目标页

**位置**：[`src/router/index.js:64-82`](src/router/index.js#L64-L82)

守卫只做 `openAuthModal()` + `return false`，没有记录 `to.fullPath`；`AuthModal` 成功回调也没有导航逻辑。结果是：

- 用户直接访问 `/preparation/workspace/12`（或从收藏夹打开）→ 弹登录 → 登录成功 → 仍停在原页面，用户以为“登录没生效”。
- 首次导航被取消时，`router-view` 没有任何页面可渲染：登录弹窗背后是一片空白底色；登录成功后也不会自动补跳，用户会一直停在空白页。
- 移动端从抽屉点“我的备赛”被拦截后，登录完成仍需再点一次。

**建议**：守卫写入 `redirect`（query 或模块级变量），登录成功后 `router.replace(redirect || '/')`；对 `redirect` 做同源/白名单校验，避免开放重定向。

### FR-06 切换赛事时两个请求缺少过期守卫

**位置**：[`PreparationWorkspaceView.vue:35-53`](src/views/PreparationWorkspaceView.vue#L35-L53)（`loadCompetition`）、[`461-481`](src/views/PreparationWorkspaceView.vue#L461-L481)（`loadWorks`）

同一文件里 `loadInspirations`、`runCommonAnalysis`、`parseWork`、`requestPlan` 都做了 `routedId.value !== cid` 的丢弃逻辑，唯独这两个没有：

```js
const d = await getCompetitionDetail(id)
applyDetail(...)           // 旧赛事详情可能覆盖新赛事标题
...
works.value = res.items    // 旧赛事获奖列表可能覆盖新赛事
```

**复现**：从赛事 A 工作台快速返回并进入赛事 B（或连续点两个备赛项），在慢网下 A 的响应晚于 B 返回，顶部标题/作品列表会显示 A 的数据。

**建议**：与其他请求保持一致，在 `await` 后加 `if (routedId.value !== id) return`；`finally` 中仅在仍为当前赛事时复位 loading。更彻底的做法是统一封装 `useAsyncContext(key)` 或使用 `AbortController`。

### FR-14 断网会清掉有效 token

**位置**：[`auth.js:126-146`](src/services/auth.js#L126-L146)

`restoreSession()` 的 `catch` 不区分 401 与网络错误，一律 `clearToken()`。弱网/后端短暂不可用时，用户刷新页面就被登出，需要重新输入密码。

**建议**：仅当 `error.status === 401` 时清 token；网络错误保留 token 并设置“离线/服务不可用”状态，允许重试。为此 `api.js` 需要保留 `status` 字段（当前只抛 `new Error(message)`，状态码信息已丢失）。

### FR-15 `/me` 失败后 token 残留

**位置**：[`auth.js:68-75`](src/services/auth.js#L68-L75)

`setToken(token)` 在 `await fetchMe()` 之前执行；若 `/me` 抛错，token 已落库但 `authState.user` 仍为 `null`。此时路由守卫认为“已登录”（`hasToken()` 为真），Header 却显示未登录，用户可进入受保护页再连续吃 401。

**建议**：失败时回滚 token；或先 `fetchMe` 成功再落库（需要临时注入 header）。

### FR-16 / FR-17 404 兜底与 History 路由部署

- `/:pathMatch(.*)*` 直接 `redirect: '/'`，错误链接被静默吞掉，无法统计、无法定位；建议新增 `NotFoundView` 并展示 404。
- `createWebHistory` 依赖服务端把所有非静态路径回退到 `index.html`。交付文档必须写明 Nginx `try_files $uri $uri/ /index.html;`，否则刷新 `/competition/12` 会得到服务器 404。
- 建议补充 `scrollBehavior`：返回列表时恢复滚动位置，当前路由切换后滚动位置表现不一致（列表页翻到中部再进详情，返回常停在顶部）。

### FR-23 Token 存储与登出语义

- Token 放 `localStorage`，任何 XSS 均可读取；当前 Markdown 已做净化，风险可控，但仍建议评估 `HttpOnly + Secure + SameSite` Cookie 方案。
- `logout()` 仅清本地，没有调用后端失效接口；若使用无状态 JWT，应确保 TTL 足够短并提供刷新机制。
- 守卫只判断“字符串非空”，未校验 JWT `exp`；过期 token 会让用户进入页面后连续报错。建议解析 `exp`（仅用于提前登出，不作为安全依据）。

---

## 4. 本地存储（LocalStorage）治理

### 现状盘点

| Key | 写入方 | 内容 | 是否含用户 | 清理时机 |
| --- | --- | --- | --- | --- |
| `access_token` | `auth.js` | JWT | — | 401 / 退出 |
| `theme-v2` | `useTheme.js` | `official/minimal/tech` | 否 | 永不 |
| `plan_${competitionId}` | `planCache.js` | 计划正文 + 200 条消息 + 200 轮历史 + `savedAt` | **否** | 仅“重新生成”单场 |
| `prep_favorites_v1` | `PreparationWorkspaceView` | 灵感 key 数组 | **否** | 永不 |

### FR-08 配额与容量无治理

**位置**：[`planCache.js:19`](src/services/planCache.js#L19)、[`102-122`](src/services/planCache.js#L102-L122)、[`PreparationWorkspaceView.vue:140-141`](src/views/PreparationWorkspaceView.vue#L140-L141)

- 写入侧只限制“消息条数 ≤ 200”，**不限制单条长度、不限制计划正文长度**；读取侧却限制计划 ≤ 200,000 字符、单条消息 ≤ 20,000 字符。两侧策略不一致，导致“能写进去但读不出来”的缓存。
- 极端情况下：200 条 × 20,000 字符 ≈ 400 万字符，再叠加 `history` 同量级数据，JSON 序列化后远超 Chrome 约 5 MB（UTF-16 约 250 万汉字）的配额，`setItem` 必然抛 `QuotaExceededError`。
- 没有 TTL、没有 LRU、没有“清理全部缓存”入口；每参加一场比赛就多一个 `plan_*`，长期使用只增不减。
- 写失败只显示“本地缓存写入失败，刷新后可能丢失”，用户无法自救，也没有自动降级（如只存计划、丢弃对话）。

**建议**

1. 写入前做预算：计划正文 ≤ 100 KB、单条消息 ≤ 8 KB、总序列化 ≤ 500 KB；超限时按“保计划、丢早期对话”策略裁剪并提示。
2. 维护索引 `plan_index_v1`（记录 key、savedAt、大小），超过 20 场或 2 MB 时按 LRU 清理。
3. 增加 30 天 TTL 与版本号（`plan_v2_*`），升级时可迁移。
4. `QuotaExceededError` 单独处理：先清理最旧缓存重试一次，再降级为“仅本次会话可用”。
5. 设置页提供“清除本机缓存”。

### FR-11 收藏缓存类型未校验导致白屏

**位置**：[`PreparationWorkspaceView.vue:705-714`](src/views/PreparationWorkspaceView.vue#L705-L714)、[`722`](src/views/PreparationWorkspaceView.vue#L722)

```js
function loadFavorites() {
  try {
    const raw = localStorage.getItem(FAV_KEY)
    return raw ? JSON.parse(raw) : []   // 没有 Array.isArray 校验
  } catch { return [] }
}
const favoriteList = computed(() =>
  inspirations.value.filter((it) => favIds.value.includes(it.key))  // 对象没有 includes
)
```

只要本地值为 `{}`、`"abc"`、`123`（历史版本、手工修改、其他脚本写入），灵感 Tab 渲染即抛 `TypeError`，整页白屏。`planCache` 对消息/历史都做了归一化，收藏这里漏了。

**建议**：`const parsed = JSON.parse(raw); return Array.isArray(parsed) ? parsed.filter(x => typeof x === 'string') : []`；写入时同样校验。

### 其他存储问题

- **多标签页无同步**：未监听 `storage` 事件。A/B 两个标签同时编辑同一计划时，后写覆盖先写且无提示。建议监听并提示“该计划已在其他标签页更新”。
- **同步写放大**：`watch([plan, messages, history], ..., { deep: true })` 每次消息变化都对整个计划做 `JSON.stringify` + 同步写入（[`PreparationWorkspaceView.vue:220`](src/views/PreparationWorkspaceView.vue#L220)）。长计划下会造成输入卡顿，建议 300–800 ms 防抖 + `requestIdleCallback`。
- **可用性探测开销**：`planCache.store()` 每次调用都 `setItem/removeItem` 探测一次，属于同步 I/O，建议模块级缓存探测结果。
- **“已保存到本地”文案不准确**：模板固定展示该文案，同时可能展示“写入失败”警告（[`936-939`](src/views/PreparationWorkspaceView.vue#L936-L939)），两者矛盾；`planSavedAt` 也只在保存时格式化一次，停留在页面 1 小时后仍显示“刚刚”。
- **恢复竞态**：`hydrating` 在 `nextTick` 中复位，而落盘 watcher 是 `flush: 'post'`，极端时序下可能与恢复写入交错；建议用显式 `restoreGeneration` 计数或 `watch` 中判断 `hydrating` 而非依赖 tick 顺序。

---

## 5. 图片与性能

### 5.1 实测体积（`dist/`）

| 产物 | 大小 | 说明 |
| --- | --- | --- |
| `assets/hero-bg-*.jpg` | **964.9 KB** | 首屏 CSS 背景图，移动端同样下载 |
| `assets/index-*.js` | 223 KB | 首屏公共 JS |
| `assets/purify.es-*.js` | 79.3 KB | DOMPurify，详情/工作台共享 |
| `assets/index-*.css` | 78 KB | 全量 Tailwind 产物 |
| `assets/PreparationWorkspaceView-*.js` | 54.4 KB | 懒加载路由块 |

首页首屏（未计字体）约 **1.27 MB**：图片 965 KB + JS 223 KB + CSS 78 KB；进入详情/工作台还会再加载 79 KB 的 DOMPurify 共享块。图片占首屏体积约 **76%**。4G 中等网络下仅图片就需 4–6 秒，LCP 很难达到 2.5 s 的“良好”标准。

### FR-04 首屏大图

**位置**：[`HomeView.vue:727-732`](src/views/HomeView.vue#L727-L732)

```css
.hero-official-bg { background-image: url('@/assets/hero-bg.jpg'); }
```

- 源文件 988,060 字节；另有一份完全同尺寸的 `src/assets/campus_teal_hero_4k.jpg`（988,060 字节），当前未被引用，属于重复资产。
- 无 `srcset` / `<picture>` / `image-set()`，手机与 4K 桌面下载同一张图。
- 作为 CSS 背景无法使用 `loading="lazy"`，也无法被 `preload` 精确控制优先级。
- 图上叠 `bg-white/25 + backdrop-blur-sm`，低端机滚动时背景模糊与波浪动画叠加，容易出现掉帧。

**建议**

1. 导出 3 档宽度（640 / 1280 / 1920），AVIF + WebP + JPG 回退，单档目标 ≤ 150 KB。
2. 用 `<picture>` + `<img fetchpriority="high">` 替代 CSS 背景（或 `image-set()` + 媒体查询），并只在 `official` 主题下加载。
3. 移动端可降级为纯色/渐变，完全不加载大图。
4. 删除重复的 `campus_teal_hero_4k.jpg`，并在构建中加资源体积告警（> 300 KB 失败）。
5. 为 LCP 元素加 `<link rel="preload" as="image" imagesrcset=...>`。

### FR-22 业务图片缺少基础优化

| 位置 | 现状 | 建议 |
| --- | --- | --- |
| [`CompetitionCard.vue:124-129`](src/components/CompetitionCard.vue#L124-L129) | 无 `loading` / `decoding` / `srcset` / 错误占位 | 首屏前 2 张 `eager`，其余 `lazy`；`decoding="async"`；`@error` 切占位图；固定 `width/height` 或保留 `aspect-ratio` 防 CLS |
| [`CompetitionDetailView.vue:151`](src/views/CompetitionDetailView.vue#L151) | 全宽 `h-64` 直接加载后端原始图 | 后端生成缩略图/多尺寸；前端按 DPR 选择；加载失败回退图标 |
| [`PreparationWorkspaceView.vue:871`](src/views/PreparationWorkspaceView.vue#L871) | 顶部 96×64 小图加载原图 | 小图用缩略图 URL，避免为 96px 拉 2 MB 原图 |

后端若返回外链图片，建议统一接入图片代理/CDN，做尺寸与格式转换，避免用户上传图直出。

### FR-13 列表页 N+1 请求

- [`HomeView.vue:109-132`](src/views/HomeView.vue#L109-L132) 对最多 8 张卡片各发一次 `/api/preparations/check/{id}`。
- [`CompetitionsView.vue:17-35`](src/views/CompetitionsView.vue#L17-L35) 对**全部**竞赛各发一次；100 场即 100 个请求，浏览器同域并发 6，后端与首屏都会被拖慢。

**建议**：后端在 `/api/competitions` 返回 `prepared: boolean`，或提供 `GET /api/preparations/check?ids=1,2,3` 批量接口；前端按 20 个一批合并。未登录时直接跳过（当前已做）。

### 其他性能点

- **Bundle 组成**：`marked` + `dompurify` 约 79 KB 被详情页/工作台共享，详情页即使没有 `full_process_guide` 也会加载。建议对 Markdown 渲染做动态 `import()`，首屏不需要。
- **长 Markdown 同步渲染**：计划最大 200,000 字符，`renderMarkdown` 在主线程同步执行，且 `planHtml` 同时用于文档卡与全屏阅读（两处 `v-html` 各渲染一棵 DOM）。建议：超长内容分块渲染/`content-visibility: auto`、或放 Worker 解析；全屏阅读复用同一份 DOM（`Teleport` 移动节点而非复制）。
- **合成层过多**：每张卡片 `.card-in` 都带 `will-change: transform, opacity`，首页 8 张 + 波浪 3 层 + 固定头部 `backdrop-blur`，低端机显存压力大。建议动画结束后移除 `will-change`，或只对首屏元素启用。
- **滚动监听**：[`BackToTop.vue:21-24`](src/components/BackToTop.vue#L21-L24) 每次 `scroll` 读取 `scrollY` 并写 ref；虽为 passive，仍建议 rAF 节流，避免高频触发 Vue 更新。
- **无预连接**：API 域名未 `preconnect`；若最终跨域部署，建议加 `<link rel="preconnect">`（同源代理则不需要）。
- **favicon 404**：[`index.html:5`](index.html#L5) 引用 `/favicon.svg`，`public/` 与 `dist/` 均不存在该文件，每次首屏多一个 404 请求。

---

## 6. 切换比赛时的状态隔离

整体设计明显考虑过串台问题，但仍有两处漏网和若干体验缺口。

### 已正确隔离的部分

- 计划/对话/视图模式/全屏阅读在 `watch(routedId)` 中重置（[`56-67`](src/views/PreparationWorkspaceView.vue#L56-L67)、[`172-185`](src/views/PreparationWorkspaceView.vue#L172-L185)）。
- 作品弹窗 `closeWork()` 在切换时关闭。
- 共性分析 `resetCommonAnalysis()` 在切换时清空。
- 灵感加载、计划生成、作品解析均校验 `routedId` 快照。
- 缓存按 `competitionId` 分开存储。

### FR-06 详情与获奖列表竞态

见第 3 节，属于确定的状态污染缺陷。

### FR-32 作品解析的“重新打开同一作品”竞态

**位置**：[`parseWork` 657-680](src/views/PreparationWorkspaceView.vue#L657-L680)

`isStillCurrent()` 只比较 `routedId` 与 `selectedWork.id`。场景：解析作品 100 → 关闭弹窗 → 重新打开作品 100（仍在同一赛事）→ 旧请求返回 → `isStillCurrent()` 为真，旧结果写入新弹窗。虽然内容相同，但如果用户在此期间先点了“AI 解析”发起第二次请求，两次响应顺序不定，可能显示旧版本。

**建议**：增加 `parseRequestId` 自增并在回调比对，或把 `selectedWork` 换成一个带 `sessionId` 的对象。

### FR-33 Tab / 收藏视图未随赛事重置

`activeTab` 与 `showFavorites` 是组件级 ref，切换 `:id` 时保持不变。用户在 A 赛事停留在“我的收藏”视图，切到 B 赛事后仍处于收藏视图，看到空态，容易误判“B 没有灵感”。

**建议**：切换赛事时 `activeTab = 'ai'`（或保留 Tab 但重置 `showFavorites = false`），并在产品上明确“切换后回到默认 Tab”。

### 其他

- **在途请求未取消**：切走后请求仍占用连接与 AI 配额（后端可能已计费）。建议对 AI 请求使用 `AbortController`，路由离开时 abort。
- **滚动位置**：切赛事后未重置滚动，长计划页切到新赛事可能停在页面中部。建议 `window.scrollTo(0, 0)` 或滚动到顶部信息区。
- **收藏是全局的**：在 A 赛事收藏的洞察 key 会出现在 B 赛事的“我的收藏”里吗？`favoriteList` 从当前 `inspirations` 过滤，所以不会显示他赛数据，但 `favIds` 会无限累积他赛 key；切回 A 仍能恢复，属预期，但需要产品确认“收藏是否跨赛事聚合”。

---

## 7. 渲染安全（XSS / 外链）

### 已做好的部分

- 所有 Markdown 都经过 `marked → DOMPurify.sanitize`，默认配置会移除 `onerror`、`<script>`、`javascript:` 等危险内容。
- 纯文本插值（`{{ }}`）天然转义，用户名、标题等无注入风险。

### FR-10 外链协议未校验

**位置**：

- [`CompetitionDetailView.vue:168-181`](src/views/CompetitionDetailView.vue#L168-L181)：`<a :href="detail.official_url" target="_blank">`
- [`PreparationWorkspaceView.vue:622-628`](src/views/PreparationWorkspaceView.vue#L622-L628)：`window.open(selectedWork.originUrl, '_blank', 'noopener,noreferrer')`

Vue 不会过滤 `href` 协议。若后端数据被污染（爬虫抓取、运营录入、用户投稿），`javascript:alert(document.cookie)` 会在点击时执行；`window.open('javascript:...')` 同样有执行风险。

**建议**：统一 `safeExternalUrl(raw)`：仅允许 `http:` / `https:`，其余返回空并提示“链接不可用”；`<a>` 固定 `rel="noopener noreferrer nofollow"`。

### FR-12 `v-html` 与 `scoped` 样式失效

**位置**：[`PreparationWorkspaceView.vue:1844-1897`](src/views/PreparationWorkspaceView.vue#L1844-L1897)

```css
<style scoped>
.markdown-body h1 { ... }
.dark .markdown-body pre { ... }
</style>
```

Vue 的 `scoped` 会把选择器编译为 `.markdown-body h1[data-v-xxx]`。`v-html` 注入的节点**不会**被模板编译器处理，也就没有 `data-v-xxx` 属性，因此这些后代规则全部不生效；只有 `.markdown-body` 容器自身的 `font-size/color`（可继承）和 `.dark .markdown-body` 的颜色生效。

**影响**：共性分析、作品 AI 解析区域的标题、列表、表格、代码块、引用样式退化，暗色模式下表格/代码对比度失控，与设计稿不一致。

**建议**：改为 `:deep(.markdown-body h1) { ... }`，或把这套 Markdown 样式移到全局 `main.css` 的 `.markdown-body` 下。同时补充 `overflow-x: auto` 给表格，避免长表格撑破布局。

### 其他

- `renderMarkdown` 未对链接统一加 `target="_blank"`（产品若需要新窗口打开，需在渲染后处理并补 `rel`）。
- 建议配置 DOMPurify 白名单（禁止 `<iframe>`、`<form>`、`style`），降低未来内容来源扩大后的风险。
- `index.html` 未设置 CSP；上线后建议至少加 `default-src 'self'; img-src 'self' https: data:; connect-src 'self' https:`。

---

## 8. 交互、主题与可访问性

### FR-09 颜色对比度

以白底 `#ffffff` 计算（Tailwind slate 色板）：

| 类名 | 色值 | 对比度 | WCAG AA（正文 4.5:1） |
| --- | --- | --- | --- |
| `text-slate-400` | `#94a3b8` | **约 2.6:1** | 不通过 |
| `text-slate-500` | `#64748b` | 约 4.76:1 | 勉强通过 |
| `text-slate-600` | `#475569` | 约 7.5:1 | 通过 |
| `text-blue-600` | `#2563eb` | 约 5.2:1 | 通过 |
| 白色文字 / `bg-blue-600` | — | 约 5.2:1 | 通过 |

`text-slate-400` 被大量用于辅助说明、时间标签、计数、占位提示（如 `HomeView.vue:607`、`CompetitionsView.vue:400`、`PreparationView.vue:116`、`ProfileView.vue:187`），且常配 `text-[11px]`/`text-xs`，是当前最主要的可访问性缺口。`disabled:opacity-60` 叠加后更低。

**建议**：正文辅助信息统一提升到 `slate-500`（浅色）/ `slate-300`（深色）；仅图标与装饰保留 400；用 axe-core 或 Lighthouse 做门禁。

### 深色（科技蓝）主题专项

- 项目只有 `tech` 主题挂 `.dark`，`official` / `minimal` 始终浅色；“暗黑模式检查”应理解为 `theme-tech` 下全路由检查。
- [`App.vue:47`](src/App.vue#L47) 根容器固定 `bg-slate-50 dark:bg-slate-900`，[`HomeView.vue:392`](src/views/HomeView.vue#L392) 在 `official` 下强制 `bg-white`，主题切换时会出现区块底色不一致。
- `.dark .markdown-body` 依赖 scoped 选择器（见 FR-12），暗色下表格/代码块实际未套用预期样式。
- 大量 `bg-white` / `text-slate-900` 未带 `dark:` 前缀。建议梳理为语义 token（`surface`/`surface-muted`/`text-primary`/`text-secondary`），而不是逐处补 `dark:`。

### FR-20 / FR-18 弹窗与滚动锁

- `AuthModal` 有 `role="dialog" aria-modal="true"`，但无 `aria-labelledby`、无焦点陷阱、关闭后焦点不回到触发按钮；Tab 可跑到背景页面。
- 三个组件各自写 `document.body.style.overflow`：`AuthModal`、`Header`（移动抽屉）、`Workspace`（全屏阅读）。叠加场景（抽屉打开 → 登录弹窗 → 关闭登录）会把 body 解锁，抽屉仍开着却能滚动背景。
- 作品弹窗（[`1363-1367`](src/views/PreparationWorkspaceView.vue#L1363-L1367)）没有滚动锁，也没有 `role="dialog"`。

**建议**：抽一个 `useScrollLock(visible)`，内部维护引用计数；弹窗统一封装 `Modal` 组件（焦点陷阱、Esc、初始焦点、归还焦点、`inert` 背景）。

### FR-21 卡片语义

[`CompetitionCard.vue:111-120`](src/components/CompetitionCard.vue#L111-L120) 用 `<article role="link" tabindex="0" @keydown.enter>` 包裹内部三个 `<button>`：

- `role="link"` 里嵌按钮不符合 ARIA 语义，读屏器会念成“链接”但内部又有按钮。
- 只响应 Enter，不响应 Space（原生链接用 Enter，按钮用 Space，当前两者都不完整）。
- 建议：卡片标题用真正的 `<router-link>` 覆盖层（`::after` 扩展点击区），按钮放在覆盖层之上；或整卡用 `<button>` 但需接受内部无嵌套交互。

### FR-24 两套 Toast

`Header.vue` 本地 `toast` + `useToast.js` 全局 Toast 并存，样式、位置、时长、`aria-live` 均不同。建议统一到 `showToast()`，删除 Header 内部实现。

### 表单与输入

- 搜索框无 `maxlength`（[`HomeView.vue:530`](src/views/HomeView.vue#L530)、[`Header.vue:292`](src/components/Header.vue#L292)、[`1158`](src/views/PreparationWorkspaceView.vue#L1158) 的对话输入），超长粘贴会直接进入请求体与本地缓存。
- 用户名/密码无长度上限（[`AuthModal.vue:195`](src/components/AuthModal.vue#L195)），仅注册校验“密码至少 6 位”。
- 错误提示不是 `aria-live`，读屏用户无法感知提交失败。
- `AuthModal` 提交中禁止关闭（`close()` 直接 return），60 s 超时期间用户无路可退；建议保留关闭并 abort 请求。
- 发送失败后 `draft` 已清空（[`413`](src/views/PreparationWorkspaceView.vue#L413)），用户需重打；建议失败回填。
- 中文输入法已处理 `isComposing` / `keyCode 229`，这点做得对。

### 焦点可见性

`focus:outline-none` 的控件大多配了 `focus-visible:ring-*`，但竞赛筛选胶囊、Tab、收藏按钮、作品卡等依赖浏览器默认焦点环，视觉不统一。建议全站统一 `focus-visible` 样式，并在主题切换下保持 ≥3:1 对比。

---

## 9. 工程化与可维护性

### FR-25 重复逻辑与“下标当 ID”

月份解析、报名时间格式化、级别映射在四个地方各写一遍：

- [`HomeView.vue:32-103`](src/views/HomeView.vue#L32-L103)
- [`CompetitionsView.vue:141-202`](src/views/CompetitionsView.vue#L141-L202)
- [`PreparationView.vue:19-73`](src/views/PreparationView.vue#L19-L73)
- [`competitionMapper.js`](src/services/competitionMapper.js)（**全项目无人 import**）

而 `competitionMapper.js` 里明确写了“绝不用数组下标伪造 id”，但真正在用的两处却这么做了：

```js
// HomeView.vue:95
id: item.id ?? index,
// CompetitionsView.vue:191
id: item.id ?? index,
```

若后端某条数据缺 `id`，卡片会拿到 `0/1/2...`，点击进入 `/competition/0`，请求到不存在或错误的竞赛；`checkPrepared(index)` 也会查错对象。

**建议**：删除三份内联实现，统一使用 `competitionMapper.mapToCard`，`id` 非法返回 `null` 并由调用方过滤；补一条单测覆盖“缺 id 的脏数据”。

### 其他工程问题

- **无 Lint / 格式化 / 测试 / CI**：`package.json` 只有 dev/build/preview；无 ESLint、Prettier、Vitest、Playwright、GitHub Actions。交付前至少补 ESLint + `vue-tsc`（或 `tsc --noEmit` 不适用时用 `vue-tsc` 的 JS 检查）+ 冒烟 E2E。
- **根目录污染**：30+ 个 `fix_*.py`、`patch_*.py`、`audit_*.py`、`agent2.py`、`ctx.py`、0 字节的 `150/190/350/-p`、`venv/`、`dist/` 与业务代码混放；`fix_doublefetch.mjs` 这类一次性脚本也被 git 跟踪。建议移到 `scripts/archive/` 或删除，`venv/dist` 加入 `.gitignore`。
- **`audit:dark` 不可移植**：`"venv\\Scripts\\python.exe audit_dark.py"` 绑定 Windows 与本机虚拟环境，CI/Linux 无法执行。
- **无错误监控**：`main.js` 未设置 `app.config.errorHandler`、`window.onunhandledrejection`；线上白屏无法定位。建议接入 Sentry 或在构建注入版本号 + 简单上报。
- **无 README / 部署说明**：新同学无法得知 Node 版本、环境变量、Nginx fallback、CORS 配置。
- **ESLint 风格问题**：`console.error` 保留在业务代码（可接受但应统一走 logger）；`catch (e) {}` 空块较多，建议至少注释原因（当前多数有注释，较好）。
- **类型缺失**：纯 JS + 大量后端字段兼容分支（`d.title || d.name`、`image ?? cover`），建议用 JSDoc typedef 或迁移 TS，减少字段契约漂移。
- **`getCompetitions` 静默兜底**（[`api.js:151-159`](src/services/api.js#L151-L159)）：结构不符时返回 `[]`，前端展示“暂无竞赛”，把后端契约破坏伪装成空数据，排障成本高。建议结构异常时抛错。
- **`checkPrepared` 吞错**（[`api.js:359-366`](src/services/api.js#L359-L366)）：网络错误也返回 `false`，用户会看到“未加入”，重复点击可能触发重复请求。建议区分 404（未加入）与其他错误。
- **无请求取消 / 重试**：AI 请求 60 s 超时且不可取消，路由切走仍占用配额；建议 `AbortController` + 幂等重试（仅 GET）。
- **无 loading 骨架一致性**：部分页面用 spinner（备赛列表），部分用 skeleton，建议统一。

---

## 10. 修复计划建议

### 第 1 天（阻断项，必须）

| 项 | 内容 | 验收 |
| --- | --- | --- |
| 1 | API Base URL 环境变量化 + 反向代理 + 构建校验 | 生产域名下登录、列表、AI 全通；无 Mixed Content |
| 2 | 轮换并移除前端 Key，补 `.gitignore` / `.env.example` / 密钥扫描 | 仓库与构建产物中搜不到 `sk-` |
| 3 | 缓存按用户隔离 + 401/退出清理 + 跳转登录页 | 换账号看不到他人计划/收藏；401 后离开受保护页 |
| 4 | 登录后回跳 `redirect` | 深链 `/preparation/workspace/12` 登录后直达 |

### 第 2–3 天（高优）

| 项 | 内容 |
| --- | --- |
| 5 | 首屏图 AVIF/WebP 多尺寸，删除重复大图，preload LCP |
| 6 | `loadCompetition` / `loadWorks` 补竞态守卫；统一请求上下文封装 |
| 7 | 删除“下标当 ID”，统一 `competitionMapper` 并补单测 |
| 8 | 计划缓存预算、LRU/TTL、配额降级；收藏类型校验 |
| 9 | `text-slate-400` 全面提级；暗色主题逐组件走查 |
| 10 | `official_url` / `originUrl` 协议白名单 |
| 11 | Markdown scoped 样式改 `:deep()`，补表格横向滚动 |

### 第 1 周（迭代）

- 图片懒加载 + 错误占位 + 后端缩略图。
- 备赛状态批量接口，去掉 N+1。
- 弹窗组件统一（焦点陷阱、滚动锁引用计数）。
- 双 Toast 合并；发送失败回填；错误提示 `aria-live`。
- 收藏/计划跨标签页同步。
- ESLint + Vitest + Playwright + CI；清理根目录脚本；补 README。
- 错误监控、版本号、CSP/安全响应头。

---

## 11. 上线验收 Checklist

- [ ] 生产环境 API 域名可访问，HTTPS 页面无 Mixed Content
- [ ] 构建产物与仓库中不含任何 `sk-`、数据库口令、内网地址
- [ ] 未登录访问 `/profile`、`/preparation`、`/preparation/workspace/:id` 均弹登录且不渲染受保护内容
- [ ] 登录成功后回到原目标路由
- [ ] Token 过期/401 后自动离开受保护页，且本机不残留上一账号计划与收藏
- [ ] 同一浏览器两个账号互相看不到对方的备赛计划、对话、收藏
- [ ] `official` / `minimal` / `tech` 三主题在首页、列表、详情、工作台、个人中心均无错色/漏色
- [ ] 全站正文对比度 ≥ 4.5:1，UI 控件 ≥ 3:1（axe 扫描 0 serious）
- [ ] 首屏 LCP ≤ 2.5 s（Lighthouse 移动端 4G 模拟），CLS ≤ 0.1，INP ≤ 200 ms
- [ ] 首屏图片总体积 ≤ 300 KB，移动端不加载 4K 大图
- [ ] 切换赛事时标题、作品、计划、对话、收藏、弹窗、滚动均不串台（含慢网/快速切换）
- [ ] 刷新子路由不出现服务器 404（History fallback 已配置）
- [ ] `prefers-reduced-motion` 下所有持续动画停止
- [ ] 键盘可完成登录、检索、筛选、加入备赛、生成计划、关闭弹窗全流程，焦点可见
- [ ] 弱网/断网/后端 500/超时/429 均有可理解提示且可恢复
- [ ] 隐私模式 / localStorage 满 / 脏缓存下页面不白屏
- [ ] 404、无效竞赛 ID、后端字段缺失均有兜底页面或提示

---

## 附录 A：本地存储容量预算（估算）

| 数据 | 典型大小 | 极端大小 | 风险 |
| --- | --- | --- | --- |
| `access_token` | 0.3–1 KB | 2 KB | 低 |
| `theme-v2` | < 20 B | 20 B | 无 |
| `plan_*`（单场） | 10–50 KB | 计划 200 K 字符 + 200 条消息 ≈ 数 MB | **高，可能写满 5 MB 配额** |
| `prep_favorites_v1` | 1–5 KB | 无上限累积 | 中 |

> 浏览器配额通常按源 5 MB（UTF-16，约 250 万汉字）；Safari 隐私模式写入直接抛异常。当前实现能降级，但无法保证“刷新不丢”，需要产品明确缓存是“尽力而为”还是“必须可靠”（若必须可靠，应上后端）。

## 附录 B：关键文件索引

| 文件 | 关注点 |
| --- | --- |
| [`src/services/api.js`](src/services/api.js) | Base URL、超时、401/429、错误归一化、N+1 |
| [`src/services/auth.js`](src/services/auth.js) | token 生命周期、恢复会话、登出 |
| [`src/services/planCache.js`](src/services/planCache.js) | 配额、key 设计、归一化 |
| [`src/router/index.js`](src/router/index.js) | 守卫、404、History fallback、scrollBehavior |
| [`src/views/HomeView.vue`](src/views/HomeView.vue) | 首屏图、检索竞态、下标 ID、N+1 |
| [`src/views/PreparationWorkspaceView.vue`](src/views/PreparationWorkspaceView.vue) | 切赛事隔离、缓存、Markdown、滚动锁 |
| [`src/components/CompetitionCard.vue`](src/components/CompetitionCard.vue) | 图片、`role=link`、备赛按钮错误处理 |
| [`src/components/AuthModal.vue`](src/components/AuthModal.vue) | 焦点、滚动锁、提交中不可取消 |
| [`index.html`](index.html) | 主题防闪白、favicon、CSP、meta |

---

> 本次审查为静态分析，未执行测试脚本、未做真机验证；具体用例与执行步骤见 [`TEST_CASES_FRONTEND.md`](TEST_CASES_FRONTEND.md)。文中行号基于当前工作区版本，修改后请同步更新。
