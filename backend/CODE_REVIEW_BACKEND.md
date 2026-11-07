# 后端代码审查报告（CODE_REVIEW_BACKEND）

> 审查对象：`KuAo-Platform/backend`（FastAPI + SQLite + Chroma + DeepSeek/OpenAI 兼容接口）
> 审查时间：2026-11-07
> 审查方式：静态代码审查 + 数据流/调用链推演 + 依赖与部署配置核对。**本次未执行任何测试，也未发起任何真实模型/Embedding 请求。**
> 配套文档：[TEST_CASES_BACKEND.md](TEST_CASES_BACKEND.md)；配套测试：[test_api_boundaries.py](test_api_boundaries.py)

---

## 0. 结论摘要

**当前状态不建议直接公网发布。** 代码整体结构清晰、SQL 参数化习惯良好、AI 缓存与配额设计有明确意图，但存在若干交付前必须处理的风险：

| 级别 | 数量 | 典型问题 |
|---|---|---|
| P0（阻断发布） | 3 | `.env` 明文密钥 + JWT 默认开发密钥、CORS 全开、请求体/频率限制缺失 |
| P1（灰度前必须修） | 8 | 7 天 JWT 无吊销、LLM 重试与超时叠加、SQLite 并发写、`kb.py` 缺失导致检索静默降级、依赖清单不可部署、insights 新库缺列 |
| P2（发布后 1~2 个迭代） | 4 | 错误信息泄露、全表扫描、迁移与多 worker、可观测性 |
| P3（持续优化） | 1 | 一致性、命名、注释与死代码等杂项 |

**最快上线路径**：先修 P0（密钥轮换、CORS 白名单、请求体上限、JWT 密钥强校验），再修 P1 中的 JWT 生命周期、LLM 超时/重试、SQLite 事务与依赖清单，按 [TEST_CASES_BACKEND.md](TEST_CASES_BACKEND.md) 跑完回归后再灰度。

### 本次修复记录（2026-11-07）

| 审查项 | 状态 | 说明 |
|---|---|---|
| R1 JWT 默认密钥 | ✅ 已修复 | `.env` 已加入 64 位随机 `JWT_SECRET_KEY`；[config.py:115-125](config.py#L115-L125) 检测到默认 dev 密钥或长度 <32 位时直接 `RuntimeError` 拒绝启动 |
| R1 真实 API Key | ⚠️ 仍需人工处理 | 代码侧无法轮换；请在 DeepSeek / SiliconFlow 控制台禁用旧 Key 并换新 |
| R2 CORS 全开 | ✅ 已修复 | [main.py:25-37](main.py#L25-L37) 改为 `CORS_ALLOW_ORIGINS` 白名单，`allow_credentials=False`，并拒绝 `*` |
| R5 SQLite 并发锁 | ✅ 部分修复 | [db.py:133-141](db.py#L133-L141) 已加 `journal_mode=WAL`、`busy_timeout=10000`；insights 唯一索引/事务仍待处理 |
| R6 LLM 超时/重试 | ✅ 已修复 | [llm.py:117-130](llm.py#L117-L130) 设 `max_retries=0`，`connect=5s/read=45s`；应用层重试不再与 SDK 叠加 |
| R7 JWT 过期时间 | ⚠️ 部分修复 | [config.py:127-129](config.py#L127-L129) 默认有效期 7 天 → 24 小时；refresh/吊销机制仍未实现 |
| R9 输入长度上限 | ✅ 部分修复 | [routers/ai.py:61-73](routers/ai.py#L61-L73) `user_input≤8000`、`current_plan≤32000`；[routers/ai.py:726-728](routers/ai.py#L726-L728) `query≤200`；全局 Body 上限仍未实现 |
| R10 依赖清单 | ✅ 部分修复 | [requirements.txt](requirements.txt) 已补齐并锁定 `openai/bcrypt/PyJWT/json_repair/chromadb` 等；`kb.py` 缺失（R10 另一部分）仍未处理 |
| R3 请求体/限流 | ❌ 未修复 | 仍需 Body 上限中间件与 IP 限流 |

> 注意：本报告其余章节保留审查时（修复前）的描述，用于说明问题背景；以上表为准查看当前状态。

### 风险总览

| ID | 级别 | 主题 | 关键位置 | 一句话结论 |
|---|---|---|---|---|
| R1 | P0 | 密钥与 JWT Secret | [.env:10](.env#L10)、[.env:50](.env#L50)、[config.py:89](config.py#L89) | 真实 API Key 明文落盘；JWT 使用内置开发密钥兜底 |
| R2 | P0 | CORS | [main.py:22-28](main.py#L22-L28) | `allow_origins=["*"]` + `allow_credentials=True`，且无环境区分 |
| R3 | P0 | 请求体/频率限制 | [main.py](main.py)、[routers/ai.py:825](routers/ai.py#L825) | 无 Body 上限、无 IP 级限流；公开检索接口可被刷 Embedding |
| R4 | P1 | SQL 注入专项 | [db.py:431-457](db.py#L431-L457)、[db.py:582-600](db.py#L582-L600) | 未发现经典注入；LIKE 通配符未转义，存在全表扫描放大风险 |
| R5 | P1 | 并发写入/锁 | [db.py:112-145](db.py#L112-L145)、[db.py:1316-1345](db.py#L1316-L1345) | SQLite 默认日志模式 + 多语句写，存在锁等待与 insights 重复行风险 |
| R6 | P1 | LLM 超时/重试 | [llm.py:120-199](llm.py#L120-L199)、[config.py:62-66](config.py#L62-L66) | 3 次应用尝试 × SDK 默认 2 次重试，最坏 9 次上游请求、约 18 分钟；阻塞线程池 |
| R7 | P1 | JWT 过期时间 | [config.py:91](config.py#L91)、[security.py:54-85](security.py#L54-L85) | 7 天有效期、无 refresh/吊销/jti，令牌泄露窗口过长 |
| R8 | P1 | 配额语义 | [security.py:173-185](security.py#L173-L185)、[db.py:714-806](db.py#L714-L806) | 先扣费后调用，失败不退；`Retry-After` 固定 3600 不正确 |
| R9 | P1 | 输入校验/提示词注入 | [routers/ai.py:46-71](routers/ai.py#L46-L71)、[routers/ai.py:249-297](routers/ai.py#L249-L297) | 用户输入无长度上限、分隔符可伪造，存在提示词注入与费用放大 |
| R10 | P1 | 依赖与可部署性 | [requirements.txt:1-3](requirements.txt#L1-L3)、[retriever.py:29](retriever.py#L29) | 依赖清单缺 7 个直接依赖；`kb.py` 不存在导致语义检索必然降级 |
| R11 | P2 | 错误处理/信息泄露 | [routers/ai.py:371-376](routers/ai.py#L371-L376) | 502 响应回显上游异常文本 |
| R12 | P2 | 数据访问性能 | [db.py:511-544](db.py#L511-L544)、[routers/ai.py:892-900](routers/ai.py#L892-L900) | 奖项查询全表扫描；检索结果 N+1 查询 |
| R13 | P2 | 启动/迁移/多 worker | [main.py:43-46](main.py#L43-L46)、[db.py:314-329](db.py#L314-L329) | 每个 worker 启动都跑 DDL/ALTER；`on_event` 已弃用 |
| R14 | P2 | 可观测性 | [main.py:52-55](main.py#L52-L55) | 健康检查不探 DB/LLM；无请求 ID、无指标、无审计 |
| R15 | P3 | 一致性与杂项 | [routers/ai.py:303-322](routers/ai.py#L303-L322)、[db.py:615-622](db.py#L615-L622) | 标记解析兜底、计数口径、密码截断等细节 |
| R16 | P1 | 新库 schema 漂移 | [schema_v2.sql:174-182](schema_v2.sql#L174-L182)、[db.py:1334-1345](db.py#L1334-L1345) | `insights` 缺 `evidence/model` 列，全新部署写洞察会失败 |

---

## 1. 审查范围与方法

### 1.1 范围

| 层 | 文件 | 关注点 |
|---|---|---|
| 应用入口 | [main.py](main.py) | CORS、中间件、启动钩子、健康检查 |
| 配置 | [config.py](config.py)、[.env](.env) | 密钥、超时、JWT、配额 |
| 鉴权 | [security.py](security.py)、[routers/auth.py](routers/auth.py) | 密码哈希、JWT、注册/登录 |
| 业务路由 | [routers/competitions.py](routers/competitions.py)、[routers/ai.py](routers/ai.py)、[routers/preparations.py](routers/preparations.py) | 参数校验、鉴权、错误码、缓存 |
| 数据访问 | [db.py](db.py)、[schema_v2.sql](schema_v2.sql) | SQL 拼接、事务、索引、迁移 |
| LLM | [llm.py](llm.py)、[embedder.py](embedder.py) | 超时、重试、JSON 解析、用量 |
| 检索 | [retriever.py](retriever.py) | 依赖完整性、向量库一致性 |
| 工程 | [requirements.txt](requirements.txt)、[test_embedder.py](test_embedder.py) | 可部署性、测试覆盖 |

### 1.2 不在本次范围

- `agent2.py`、`analyzer.py`、`generate_guides.py` 等离线脚本只做了依赖关系核对，未深入审查其爬虫/工具调用逻辑。
- 前端仓库未在本次审查范围内；涉及 Markdown 渲染的 XSS 风险只给出前后端契约建议。
- 未做渗透测试、压测、依赖 CVE 扫描；这些属于上线前专项。

### 1.3 判定口径

- **P0**：不修不能上公网（密钥、默认口令、任意源 CORS、无上限资源消耗）。
- **P1**：可在小流量灰度前修复，否则故障/资损概率高。
- **P2**：影响稳定性、可维护性或安全纵深，发布后一个迭代内处理。
- **P3**：代码质量与一致性问题。

---

## 2. 详细发现与改进方案

### R1（P0）真实密钥明文落盘 + JWT 默认开发密钥

**位置**
- [.env:10](.env#L10)：`DEEPSEEK_API_KEY` 为真实可用 Key。
- [.env:50](.env#L50)：`EMBEDDING_API_KEY` 为真实可用 Key。
- [config.py:89](config.py#L89)：`JWT_SECRET_KEY` 未在 `.env` 中配置时，回落到仓库内的固定字符串 `dev-only-secret-key-...`。
- [.gitignore](../.gitignore)：`.env` 已忽略（好消息）。

**风险**
1. 任何拿到源码压缩包、构建产物、备份或截图的人都能直接消耗模型额度；Embedding Key 与 DeepSeek Key 可能绑定同一账号，泄露面更大。
2. `.env` 虽然未被 Git 跟踪，但已经存在于工作目录、可能进入部署包/镜像层；**必须视为已泄露并轮换**。
3. JWT Secret 使用公开默认值时，攻击者可自行签发任意 `sub` 的令牌，直接绕过登录。这是最严重的一条：只要生产环境忘了配 `JWT_SECRET_KEY`，鉴权形同虚设。

**改进方案**
1. **立即轮换**两把 Key（DeepSeek、SiliconFlow），在服务商控制台禁用旧 Key。
2. 生产环境不落 `.env` 文件，改用平台 Secret（Docker Secret / K8s Secret / 云密钥管理）。
3. 启动时强校验：

```python
# config.py
_DEFAULT_JWT_SECRET = "dev-only-secret-key-please-change-in-prod-4f8a2c9e1b7d"
IS_PROD = _get_bool("APP_ENV_PROD", False)

if IS_PROD and JWT_SECRET_KEY == _DEFAULT_JWT_SECRET:
    raise RuntimeError("生产环境必须通过 JWT_SECRET_KEY 配置独立密钥")

if len(JWT_SECRET_KEY.encode("utf-8")) < 32:
    raise RuntimeError("JWT_SECRET_KEY 长度至少 32 字节")
```

4. 提交 `.env.example`（只含占位符），并让 README/部署文档只引用它。
5. 在 CI 中加 secret 扫描（gitleaks / trufflehog），阻断真实 Key 再次入库。

> 注意：审查文档中不会复述真实 Key 的值；请以 `.env` 行号为准进行轮换。

---

### R2（P0）CORS 配置不适合生产

**位置**：[main.py:22-28](main.py#L22-L28)

```python
allow_origins=["*"],
allow_credentials=True,
allow_methods=["*"],
allow_headers=["*"],
```

**风险**
1. 浏览器规范不允许 `Access-Control-Allow-Origin: *` 与 `Allow-Credentials: true` 同时生效；Starlette 在通配符场景会返回 `*`，带 Cookie 的跨域请求会被浏览器拒绝，前端可能出现“开发能跑、上线偶发 CORS 失败”。
2. 当前鉴权走 `Authorization: Bearer`，并不依赖 Cookie，`allow_credentials=True` 并非必要；保留它只会扩大攻击面。
3. 任意源可调用公开接口（如 `/api/ai/search`），配合无频率限制可被第三方页面当作免费代理刷 Embedding。

**改进方案**
```python
import os
_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["http://localhost:5173"],
    allow_credentials=False,          # Bearer Token 不需要 Cookie
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)
```
生产同时在反向代理层做 Host/Origin 白名单；不要用 `allow_origin_regex=".*"` 代替。

---

### R3（P0）缺少请求体上限与 IP 级频率限制

**位置**
- [main.py](main.py) 没有 Body 限制中间件，也没有 `TrustedHostMiddleware`。
- [routers/ai.py:825](routers/ai.py#L825) `POST /api/ai/search` 无需登录。
- [routers/ai.py:46-71](routers/ai.py#L46-L71) `user_input` / `current_plan` 无 `max_length`。

**风险**
1. 攻击者发送 100MB JSON，进程先完整读入内存再校验，单请求即可造成 OOM；并发几个请求直接拖垮服务。
2. 即使没有 OOM，`user_input` 可以无限长，最终被拼进 prompt，按 token 计费，造成资损。
3. `/api/ai/search` 每请求调用一次 Embedding API；无鉴权、无 IP 限流时，可被脚本刷爆额度。
4. 登录/注册接口无频率限制，存在撞库与批量注册风险。

**改进方案**
1. 反向代理层：`client_max_body_size 1m;`（Nginx）。
2. 应用层加 ASGI Body 限制中间件（示意）：

```python
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(1024 * 1024)))

class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # 先看 Content-Length（可被伪造，仅作快速拒绝）
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared and int(declared) > self.max_bytes:
            await self._reject(send)
            return

        received = 0
        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._reject(send)

    @staticmethod
    async def _reject(send):
        body = b'{"detail":"请求体过大"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})
```
> 注意：若响应已开始，不能再发 413；生产更稳的做法是代理层兜底 + 应用层只处理 `Content-Length`。

3. Pydantic 加长度约束：`user_input: str = Field("", max_length=8000)`、`current_plan: Optional[str] = Field(None, max_length=32000)`、`SearchRequest.query = Field(..., min_length=1, max_length=200)`、`LoginRequest` 也加上限。
4. 限流：按「IP + 用户 + 路由」维度，Redis 滑动窗口；`/api/ai/search` 建议加登录或每 IP 每分钟 10 次；登录每 IP 每分钟 5 次并加失败退避。
5. 给 `/api/ai/plan`、`/api/ai/analyze`、`/api/ai/analyze-work` 保留现有「用户每日配额」，但不要把它当作唯一防线。

---

### R4（P1）SQL 注入专项：未发现经典注入，但有残留风险

**逐条核对结果**

| 位置 | 动态内容 | 是否用户可控 | 结论 |
|---|---|---|---|
| [db.py:443-447](db.py#L443-L447) `list_competitions` | `LIKE ?` 值 | 是 | 参数化，安全 |
| [db.py:213](db.py#L213)、[db.py:231](db.py#L231)、[db.py:282](db.py#L282) | 列名/DDL | 否（模块常量） | 安全，但维护者不能改成用户输入 |
| [db.py:399](db.py#L399)、[db.py:1039](db.py#L1039)、[db.py:1080](db.py#L1080) | 列名 | 否（白名单 `allowed` / `_AWARD_WRITABLE_FIELDS`） | 安全 |
| [retriever.py:192](retriever.py#L192) | `WHERE {where}` | 否（常量拼接） | 安全 |
| [retriever.py:212](retriever.py#L212)、[retriever.py:308](retriever.py#L308) | `IN (?,?,...)` | 数量来自批大小 | 占位符，安全 |
| [db.py:53-59](db.py#L53-L59) `_build_expr` | 关键词 | 否（常量） | 安全 |

**残留风险 1：LIKE 通配符未转义**
`keyword="%"` 会匹配全部记录，`keyword="_"` 匹配任意单字符。不是 SQL 注入，但可被用来做全表扫描/枚举，放大慢查询。
```python
def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

if keyword:
    sql += " AND name LIKE ? ESCAPE '\\'"
    params.append(f"%{_escape_like(keyword)}%")
```
`category` 同理。

**残留风险 2：无长度限制**
`keyword`、`category` 为 `Optional[str]` 且无长度上限，可传 1MB 字符串触发全表 LIKE。建议 `Query(None, max_length=64)`。

**残留风险 3：`%`/`_` 之外的分页**
`/api/competitions` 无分页，数据量增长后一次返回全表。建议 `limit/offset`（默认 50，上限 200）。

**结论**：当前不存在可通过用户输入直接拼接 SQL 的路径，参数化习惯值得保留；上线前补转义与长度限制即可。

---

### R5（P1）并发写入、锁与事务边界

**位置**
- 连接管理：[db.py:112-145](db.py#L112-L145)，`busy_timeout=5000`，未开启 WAL。
- 配额扣减：[db.py:714-806](db.py#L714-L806)。
- 备赛清单读后写：[db.py:837-860](db.py#L837-L860)。
- 奖项创建/更新先 `PRAGMA table_info` 再写：[db.py:1009-1082](db.py#L1009-L1082)。
- insights 先删后插：[db.py:1316-1345](db.py#L1316-L1345)。

**风险**
1. **`database is locked`**：SQLite 同一时刻只允许一个写事务。当前默认 `journal_mode=delete`，写事务持锁期间读也会被阻塞；并发 AI 配额 + 备赛 + 缓存写入时，5 秒超时后会抛 `OperationalError`，接口 500。
2. **多语句写的原子性不足**：`add_preparation` 先 `SELECT` 确认竞赛存在，再 `INSERT`；若期间竞赛被删除，`INSERT` 会因外键失败抛 `sqlite3.IntegrityError`，而路由只捕获 `ValueError`，最终变成 500。`create_award` 先 `PRAGMA table_info` 再 `INSERT` 同样是非原子组合。
   > 说明：Python `sqlite3` 默认只在 DML 前开启事务（`SELECT` 不持读事务），因此本项目当前的“读后写”大多是“读已结束再写”，风险主要集中在**写-写竞争、删除/写入交错**和**多语句业务原子性**；若未来改为 `isolation_level=None` 或显式 `BEGIN`，才会出现典型的延迟事务锁升级问题。
3. **insights 重复行**：`save_insight` 先 `DELETE` 再 `INSERT`，但表上只有 `input_hash` 的部分唯一索引，没有 `(competition_id, kind)` 唯一约束；两个 analyzer 进程并发跑同一赛事时，可能各插入一行，页面出现重复洞察。代码注释“同一赛事同一 kind 只保留最新一条”并未被数据库约束保证。
4. **启动迁移竞争**：多 worker 同时启动会在同一时刻执行 `executescript` + `ALTER TABLE`；虽然 `duplicate column` 被捕获，但仍可能出现锁等待。
5. **配额表增长**：`ai_usage` 每个用户每天一行，长期不清理会膨胀；`ai_analysis_cache` 在作品/赛事删除后留下孤儿行（无外键）。

**改进方案**
1. 连接初始化统一加：
```python
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA synchronous = NORMAL")
conn.execute("PRAGMA busy_timeout = 10000")
conn.execute("PRAGMA foreign_keys = ON")
```
WAL 下读不阻塞写，能显著降低锁冲突。
2. 对“多语句写”使用 `BEGIN IMMEDIATE`，避免读后写升级失败：
```python
@contextmanager
def get_conn(path=None, *, immediate=False):
    conn = sqlite3.connect(db_path, isolation_level=None)  # 手动事务
    ...
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if immediate:
            conn.execute("COMMIT")
    except Exception:
        if immediate:
            conn.execute("ROLLBACK")
        raise
```
`add_preparation`、`create_award`、`update_award`、`save_insight` 用 `immediate=True`。
3. insights 加唯一约束并 UPSERT：
```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_insights_comp_kind
    ON insights (competition_id, kind) WHERE competition_id IS NOT NULL;
```
```sql
INSERT INTO insights (...) VALUES (...)
ON CONFLICT(competition_id, kind) DO UPDATE SET
    content_md = excluded.content_md,
    evidence = excluded.evidence,
    input_hash = excluded.input_hash,
    created_at = datetime('now', 'localtime');
```
4. 迁移从应用启动中拆出，作为部署流水线的独立步骤（`python -m migrate`），应用启动只做轻量校验；生产建议单 worker，或直接迁移到 PostgreSQL（多实例写入场景 SQLite 不是合适选择）。
5. 定时清理：`DELETE FROM ai_usage WHERE usage_date < date('now','localtime','-90 day')`；删除 awards 时同步清 `ai_analysis_cache`（或加外键/触发器）。

**关于“死锁”的澄清**：SQLite 不会出现数据库级死锁环，但会出现锁等待/升级失败，表现为 `database is locked`。测试用 16 并发抢配额验证“不越界、不死锁、无异常”，见 [test_api_boundaries.py](test_api_boundaries.py) 的 `AiQuotaBoundaryTest.test_concurrent_quota_consumption_never_exceeds_limit`。

---

### R6（P1）大模型超时与重试机制：最坏约 18 分钟，且会阻塞线程池

**位置**
- [config.py:62-66](config.py#L62-L66)：`LLM_MAX_RETRIES=3`、`LLM_TIMEOUT=120`。
- [llm.py:167-199](llm.py#L167-L199)：`for attempt in range(MAX_RETRIES)`，实际是 **3 次总尝试（2 次重试）**，命名有歧义。
- [llm.py:116](llm.py#L116)：`OpenAI(..., timeout=DEFAULT_TIMEOUT)`，**未设置 `max_retries=0`**，SDK 默认还会重试 2 次。
- [llm.py:197](llm.py#L197)：无 jitter，未读取 `Retry-After`。
- [routers/ai.py:343](routers/ai.py#L343) 等路由都是同步 `def`，FastAPI 放进线程池执行。

**风险量化**
1. 单次调用最坏耗时 ≈ `3 × 120s + 1.5s + 3s ≈ 364.5s`；再叠加 OpenAI SDK 默认 2 次重试，实际可能 **9 次上游请求、约 18 分钟**（每次仍受 120s 超时约束的最坏情况）。
2. Nginx/网关通常 60s 就返回 504，但后端还在跑；用户重试会叠加更多在途请求，费用与线程同时放大。
3. 同步 LLM 调用占用 AnyIO 线程池（默认 40 个），40 个慢请求即可让所有同步路由（包括 `/api/health`）排队；表现为“服务假死”。
4. 429 重试不读取 `Retry-After`，会在服务商要求等待时继续打；没有熔断，上游故障时所有请求一起重试。
5. `chat_json` 的“解析失败重问”又包了一层 `_call`，最坏调用次数为 `(retries+1) × MAX_RETRIES`，缺少全局预算。

**改进方案**
1. **分离超时**，并关闭 SDK 自带重试：
```python
import httpx
from openai import OpenAI

client = OpenAI(
    api_key=API_KEY,
    base_url=BASE_URL,
    timeout=httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=5.0),
    max_retries=0,          # 重试统一由 llm.py 控制
)
```
2. **总预算**：一次请求的 LLM 总时长不超过 50s（按网关超时倒推），用 `time.monotonic()` 判断剩余预算，预算耗尽直接放弃。
3. 重试策略：
   - 仅对 `APITimeoutError`、`APIConnectionError`、429、5xx 重试；
   - 指数退避 + 随机抖动：`delay = base * 2**n + random.uniform(0, 0.3)`；
   - 429 优先读 `exc.response.headers.get("retry-after")`；
   - 最多 2 次重试（总 3 次尝试），与配置命名对齐（`LLM_MAX_ATTEMPTS`）。
4. **并发隔离**：改用 `AsyncOpenAI`，用 `asyncio.Semaphore(8)` 限制同时在途的 LLM 请求；或者把 LLM 调用丢进独立线程池/任务队列，避免挤占 API 线程池。
5. **熔断**：连续 N 次失败后短时间快速失败（返回 503/502 + `Retry-After`），恢复后半开探测。
6. **超时错误码语义**：上游超时返回 504，上游 5xx/协议错误返回 502，配额/限流返回 429；不要把所有异常都塞进 502。
7. **流式接口** `chat_stream` 同样要设置超时与断连处理；当前没有任何重试/超时，长连接会一直占用线程。
8. 记录 `request_id`、模型名、耗时、重试次数、token 用量，便于定位费用与慢请求。

---

### R7（P1）JWT 过期时间与吊销机制

**位置**
- [config.py:91](config.py#L91)：`JWT_EXPIRE_MINUTES = 60 * 24 * 7`（7 天）。
- [security.py:54-65](security.py#L54-L65)：payload 只有 `sub/username/iat/exp`。
- [security.py:68-85](security.py#L68-L85)：只校验签名与 `exp`（PyJWT 默认），未 `require` 关键声明。
- [routers/auth.py:85-97](routers/auth.py#L85-L97)：`expires_in` 直接返回 7 天。

**合理性评估**
- 对“AI 配额 + 用户备赛数据”这类应用，7 天是偏长的：令牌一旦泄露（日志、截图、前端 localStorage、共享设备），攻击者可在 7 天内持续调用并消耗配额。
- 当前没有 refresh token、没有登出接口、没有 `jti` 黑名单、没有 token 版本号；唯一能让令牌失效的方式是删除用户。
- `decode_access_token` 没有强制要求 `exp`/`sub`，虽然攻击者没有密钥时无法利用，但属于防御纵深不足。
- `expires_minutes=0` 会被 `or` 吞掉，回落到 7 天，无法签发“立即过期”的令牌（测试中需用 `-1` 表达过期）。

**改进方案**
1. 双令牌：
   - Access Token：15~30 分钟；
   - Refresh Token：7~30 天，服务端存哈希、可轮换、可吊销；
   - 刷新时旧 refresh 立即失效（rotation）。
2. 用户表加 `token_version INTEGER NOT NULL DEFAULT 0`，JWT 带 `ver`；改密码/登出全部设备时 `ver + 1`，校验不匹配即 401。
3. 登出接口把 `jti` 放入 Redis 黑名单（TTL = 剩余有效期）。
4. 解码时强制声明并留时钟偏移：
```python
jwt.decode(
    token,
    key,
    algorithms=[config.JWT_ALGORITHM],
    options={"require": ["exp", "iat", "sub"], "verify_aud": True},
    audience="kuao-api",
    leeway=30,
)
```
5. 签发时加 `iss="kuao-api"`、`aud="kuao-web"`、`jti=uuid4()`；密钥支持轮换（验证时尝试当前+上一个 Key）。
6. 如果暂时不做 refresh，至少把 access 降到 12~24 小时，并补登出/改密失效；不要维持 7 天且无吊销。

---

### R8（P1）AI 配额：先扣后调、失败不退、`Retry-After` 不准确

**位置**
- [security.py:173-185](security.py#L173-L185)：依赖里直接 `consume_ai_quota`。
- [db.py:714-806](db.py#L714-L806)：原子扣减实现。
- [security.py:183](security.py#L183)：`Retry-After: 3600`。
- [config.py:97](config.py#L97)：`AI_DAILY_QUOTA=30`。

**现状与问题**
1. **扣费时机**：FastAPI 先解依赖再执行路由，因此“竞赛不存在 404”“LLM 超时 502”“模型返回空 502”都会消耗一次配额。注释明确这是有意设计，但对用户不公平，客诉概率高。
2. **原子性**：单条带守卫 `UPDATE ... WHERE used < limit` + `rowcount` 判定是正确做法，并发不会越界（测试已覆盖）。
3. **重置时区**：`_today_str()` 用服务器本地时间。若容器默认 UTC，国内用户会在早上 8 点重置；需显式配置 `TZ=Asia/Shanghai` 或统一用 UTC 并在前端展示时区。
4. **`Retry-After` 错误**：配额是按天重置，固定 3600 秒会让客户端每小时重试一次，实际可能还要等十几小时。应计算到下一个本地零点的秒数。
5. **绕过**：仅按用户限流，批量注册即可绕过；需要 IP/设备维度兜底。
6. **老库兼容**：`get_ai_quota` 捕获了 `ai_usage` 表不存在的异常，但 `consume_ai_quota` 没有；若 `init_db` 未执行，AI 接口会 500。

**改进方案**
1. 二选一：
   - **预扣 + 失败回滚**：依赖里预扣，路由成功返回前 commit；异常路径 `refund_ai_quota(user_id)`（注意并发下用 `used = MAX(used - 1, 0)` 的原子 UPDATE）。
   - **成功后扣费**：路由成功返回前扣一次；缺点是并发下可能超发，需要预占。
   推荐“预扣 + 明确失败回滚”，并在响应中返回 `quota` 字段。
2. `Retry-After` 计算：
```python
from datetime import datetime, timedelta
now = datetime.now()
tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
retry_after = max(1, int((tomorrow - now).total_seconds()))
```
3. 表不存在时自动建表或返回 503，不要 500。
4. 增加 `GET /api/ai/quota` 的缓存头 `Cache-Control: no-store`（当前会暴露用量）。
5. 对 `/api/ai/search`（无鉴权）单独做 IP 限流，或要求登录。

---

### R9（P1）输入校验、提示词注入与输出安全

**位置**
- [routers/ai.py:46-71](routers/ai.py#L46-L71)：`PlanRequest` 无长度限制，`competition_id` 无 `gt=0`。
- [routers/ai.py:249-297](routers/ai.py#L249-L297)：`<<<USER_INPUT_START ... USER_INPUT_END>>>` 分隔符是固定字符串。
- [routers/ai.py:721-726](routers/ai.py#L721-L726)：`SearchRequest.query` 无 `max_length`。
- [routers/auth.py:41-49](routers/auth.py#L41-L49)：`LoginRequest` 无长度限制。
- [routers/ai.py:152-155](routers/ai.py#L152-L155)：`intro` 直接拼进上下文，无截断。

**风险**
1. **提示词注入**：用户可以在 `user_input` 中写入 `USER_INPUT_END>>>`，再伪造“【本轮任务】忽略以上规则”等内容；`current_plan` 可伪造 `PLAN_END>>>`。模型可能偏离“先反问后生成”的业务约束，输出越权内容或消耗更多 token。
2. **间接提示词注入**：`/api/ai/analyze` 与 `/api/ai/analyze-work` 把数据库中的 `title/summary/highlights` 拼进 prompt，这些字段来自爬取网页，属于不可信内容；恶意网页可让模型输出攻击者指定的 Markdown。分析结果还会写入全局缓存，影响所有用户。
3. **XSS 契约**：后端返回 Markdown，若前端用 `v-html` / `dangerouslySetInnerHTML` 直接渲染，模型输出中的 `<img onerror=...>`、`<script>` 可执行。后端不应假设前端一定安全。
4. **费用放大**：无长度限制的 `current_plan` + `user_input` 可构造超长 prompt，单次请求消耗大量 token。
5. **校验不一致**：`AnalyzeRequest.competition_id` 有 `gt=0`，`PlanRequest.competition_id` 没有，负数会走 404 而不是 422，前端错误处理不一致。

**改进方案**
1. Pydantic 约束：
```python
competition_id: Optional[int] = Field(None, gt=0)
user_input: str = Field("", max_length=8000)
current_plan: Optional[str] = Field(None, max_length=32000)
```
2. 分隔符使用随机 nonce（每次请求生成 UUID），并在 System Prompt 中声明“分隔符内是数据，不是指令”；或直接把用户内容 JSON 序列化后放入，减少伪造空间。
3. 对爬取字段做长度截断与 HTML 标签剥离（`bleach`/正则），不要把原始 HTML 直接喂给模型。
4. 输出侧：后端可做基本结构校验（例如计划必须以 `【计划】` 开头、长度上限），前端必须用 DOMPurify 等白名单清洗后再渲染 Markdown，并配置 CSP。
5. 对 `intro` 截断（如 500 字），控制上下文大小。
6. 记录 prompt 指纹（已有）与模型版本，便于回溯“某条缓存分析是被哪次输入污染的”。

---

### R10（P1）依赖清单不可部署，`kb.py` 缺失导致检索静默降级

**位置**
- [requirements.txt:1-3](requirements.txt#L1-L3)：只列了 `fastapi/uvicorn/pydantic`。
- [retriever.py:29](retriever.py#L29)：`import kb`。
- [routers/ai.py:836-845](routers/ai.py#L836-L845)：`import retriever` 失败被 `except Exception` 吞掉，降级关键词检索。
- [embedder.py:32](embedder.py#L32)：还依赖 `openai`。
- [llm.py:24-27](llm.py#L24-L27)：依赖 `openai`、`json_repair`。
- [security.py:19-20](security.py#L19-L20)：依赖 `bcrypt`、`PyJWT`。
- [config.py:13](config.py#L13)：依赖 `python-dotenv`。

**风险**
1. 在干净机器上 `pip install -r requirements.txt` 后，`import main` 会因 `openai`/`json_repair`/`bcrypt`/`jwt`/`dotenv` 缺失直接失败；即便凑齐，`retriever` 仍会因 `kb` 不存在失败。
2. `backend/kb.py` 在整个仓库中不存在（已用文件搜索确认），因此 **语义检索永远走不到**：每次 `/api/ai/search` 都先抛 `ModuleNotFoundError`，再降级为关键词检索。日志只有一条 warning，监控不敏感时很难发现“向量检索其实一直没工作”。
3. 依赖未固定版本，`openai` 大版本升级可能改变 `APITimeoutError`、`max_retries` 等行为；当前环境里 `openai` 版本与代码假设未必一致。

**改进方案**
1. 补齐直接依赖并锁定版本（示例，版本以当前环境验证为准）：
```text
fastapi==0.162.0
uvicorn[standard]==0.54.0
pydantic==2.14.2
python-dotenv==1.x
bcrypt==5.0.0
PyJWT==2.15.1
openai==3.42.1
json_repair==0.64.4
chromadb==1.5.9
httpx==0.28.1
```
   建议用 `pip-tools` / `uv lock` 生成锁文件，并固定 Python 版本（3.11/3.12）。
2. 补 `kb.py`（连接上下文 `conn_ctx`、`query`、`json_loads`），或重构 `retriever.py` 去掉对 `kb` 的依赖；两者选其一，不能保持现状。
3. 降级要可观测：`search` 返回 `degraded: true` 字段，并打点 `search_degraded_total`；连续降级告警。
4. 在 CI 里加“干净环境安装 + `python -c "import main"`”冒烟步骤，防止依赖再次漏列。

---

### R11（P2）错误处理与信息泄露

**位置**
- [routers/ai.py:371-376](routers/ai.py#L371-L376)：`detail=f"大模型调用失败: {exc}"`。
- [routers/ai.py:566-571](routers/ai.py#L566-L571)：同类。
- [routers/ai.py:683-687](routers/ai.py#L683-L687)：`raise HTTPException(...)` 未 `from exc`（异常链丢失）。
- [routers/ai.py:1059-1060](routers/ai.py#L1059-L1060)：`ValueError` 直接回显给客户端。

**风险**
- 上游异常文本可能包含 Base URL、请求 ID、模型名、甚至部分请求内容；对攻击者是有用的探测信息。
- 用户输入触发的 `ValueError` 文案可被枚举；应统一为稳定的业务错误码。
- 异常链丢失会让日志定位变难。

**改进方案**
1. 对客户端返回稳定文案 + `error_id`，详细信息只进日志：
```python
error_id = uuid.uuid4().hex[:12]
logger.exception("LLM 调用失败 error_id=%s", error_id)
raise HTTPException(502, detail={"code": "LLM_UPSTREAM_ERROR", "error_id": error_id})
```
2. 统一异常处理器：`RequestValidationError` → 422 结构化字段；`HTTPException` → `{code, message, error_id}`；未捕获异常 → 500 且不暴露堆栈。
3. `raise ... from exc` 保留异常链。
4. 不要把 `repr(exc)` 写进响应；日志中也要避免打印 API Key（当前未见打印 Key，但 `llm_config_hint` 会显示 Base URL，可接受）。

---

### R12（P2）数据访问性能：全表扫描与 N+1

**位置**
- [db.py:511-544](db.py#L511-L544)：`_fetch_source_url_map` 每次查询 `SELECT ... FROM awards` 全表。
- [db.py:582-600](db.py#L582-L600)：`list_awards_by_competition` 先取奖项，再全表找 URL。
- [routers/ai.py:892-900](routers/ai.py#L892-L900)：检索结果逐条 `get_competition(cid)`。
- [db.py:431-457](db.py#L431-L457)：`month` 过滤在 Python 内存中做。

**风险**
- awards 表增长到几万行后，每次打开赛事详情都做一次全表扫描；并发下 CPU 与 IO 放大。
- 搜索结果最多 20 条 → 20 次单独查询；慢查询叠加。
- 内存中过滤 month 会先加载全表，失去 SQL 下推优势。

**改进方案**
1. `_fetch_source_url_map` 增加标题过滤：
```python
placeholders = ", ".join("?" * len(titles))
rows = conn.execute(
    f"SELECT id, title, source_url FROM awards "
    f"WHERE title IN ({placeholders}) AND source_url IS NOT NULL "
    f"AND TRIM(source_url) <> '' ORDER BY id ASC",
    list(titles),
).fetchall()
```
2. 搜索结果批量取竞赛：`db.get_competitions_by_ids(ids)`，一次 `IN` 查询映射。
3. month 过滤用 SQL：
```sql
WHERE (
  (reg_cross_year = 0 AND reg_start_month <= ? AND reg_end_month >= ?)
  OR (reg_cross_year = 1 AND (reg_start_month <= ? OR reg_end_month >= ?))
)
```
4. 列表接口加 `limit/offset`，`SELECT` 只取 API 需要的列，避免把 `detail_md/raw_html_path` 读进内存。

---

### R13（P2）启动、迁移与多 worker

**位置**
- [main.py:43-46](main.py#L43-L46)：`@app.on_event("startup")`（FastAPI 已弃用，建议 lifespan）。
- [db.py:314-329](db.py#L314-L329)：启动时 `executescript` + `ALTER TABLE` + 回填 + 建索引。
- [main.py:58-61](main.py#L58-L61)：开发用 `reload=True`。

**风险**
- 每个 worker 启动都跑完整迁移，多 worker 并发 DDL 可能锁等待；`executescript` 会隐式提交，和 `get_conn` 的 commit 语义叠加后，事务边界不直观。
- 迁移失败会直接导致进程启动失败（这是好事，但需要明确的部署顺序）。
- `on_event` 未来 FastAPI 版本会移除。

**改进方案**
1. 用 lifespan：
```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield

app = FastAPI(..., lifespan=lifespan)
```
2. 生产部署：先跑独立迁移命令，再启动应用；应用启动只做 `PRAGMA` 与只读校验。
3. SQLite 场景固定 `--workers 1`；需要多实例时迁移到 PostgreSQL。
4. `init_db` 里的 `_backfill_detail_fields` 每次启动都执行 UPDATE，建议改为一次性迁移脚本。

---

### R14（P2）可观测性与健康检查

**位置**
- [main.py:52-55](main.py#L52-L55)：`/api/health` 只返回静态 ok。
- 全局无请求 ID、无访问日志格式、无指标端点。
- [routers/ai.py:937-940](routers/ai.py#L937-L940)：搜索日志打印完整 `query`（可能含个人信息）。

**风险**
- DB 锁死、Embedding 不可用、LLM 全失败时，健康检查仍然 200，负载均衡不会摘除实例。
- 没有指标就无法做容量规划和告警；搜索 query 全量落日志有隐私风险。

**改进方案**
1. 区分 liveness/readiness：`/api/health/live` 静态；`/api/health/ready` 执行 `SELECT 1`、Chroma `count()`、可选的 LLM ping（带缓存，避免每探针都花钱）。
2. 加请求 ID 中间件（`X-Request-ID`），日志结构化（JSON），错误日志带 `request_id`。
3. 加 Prometheus 指标：请求数/延迟/状态码、LLM 调用次数/耗时/重试/失败、配额 429 数、搜索降级次数、DB 锁错误数。
4. 日志脱敏：query 只记录长度或哈希；用户输入不落日志。

---

### R15（P3）一致性与杂项

| 问题 | 位置 | 建议 |
|---|---|---|
| 奖项计数包含 `rejected`，列表却排除 | [db.py:615-622](db.py#L615-L622) vs [db.py:582-590](db.py#L582-L590) | 计数加 `review_status <> 'rejected'`，或明确文档口径 |
| 公开列表包含 `pending` 作品 | [db.py:587-589](db.py#L587-L589) | 对外只返回 `approved`；后台另设接口 |
| 密码按字符限制 64，但 bcrypt 只取前 72 字节 | [security.py:31-35](security.py#L31-L35)、[routers/auth.py:32](routers/auth.py#L32) | 拒绝 >72 UTF-8 字节，或先 SHA-256 再 bcrypt |
| `expires_minutes=0` 被 `or` 吞掉 | [security.py:57](security.py#L57) | 改为 `if expires_minutes is None` |
| 标记解析兜底可能把“带标题的拒绝回答”判成 plan | [routers/ai.py:303-316](routers/ai.py#L303-L316) | 优先严格匹配标记；无标记时增加长度/结构校验 |
| `check/{id}` 不校验竞赛是否存在 | [routers/preparations.py:30-40](routers/preparations.py#L30-L40) | 不存在返回 404，或文档明确返回 `prepared=false` |
| 公开返回 `full_process_guide` 可能含模型生成的不准确信息 | [routers/competitions.py:53-76](routers/competitions.py#L53-L76) | 前端标注“AI 生成，仅供参考”，离线生成时做事实校验 |
| `search` 对 `top_k*4` 召回，但没有上限保护 | [routers/ai.py:838-840](routers/ai.py#L838-L840) | `top_k` 已有 20 上限，可接受；保留 |
| `get_month_timeline` 12 次全表扫描 | [db.py:481-486](db.py#L481-L486) | 离线脚本场景可接受，接口化时改单次查询 |
| `analyze_single_work` 异常链丢失 | [routers/ai.py:683-687](routers/ai.py#L683-L687) | `raise ... from exc` |

---

### R16（P1）全新部署时 `insights` 表缺列，写洞察会失败

**位置**
- [schema_v2.sql:174-182](schema_v2.sql#L174-L182)：`insights` 只有 `id/kind/scope/competition_id/input_hash/content_md/created_at`。
- [db.py:1334-1345](db.py#L1334-L1345)：`save_insight` 的 `INSERT` 写入 `evidence` 与 `model`。
- [db.py:258-287](db.py#L258-L287)：`_INSIGHT_MIGRATION_COLUMNS` 只补 `competition_id`。
- [routers/ai.py:1072-1100](routers/ai.py#L1072-L1100)：读取时用 `r.get("evidence")` / `r.get("model")`，缺列不会报错，只会静默为空。

**风险**
1. 线上老库可能恰好有 `evidence/model`（历史遗留），所以接口“看起来正常”；但**全新部署**（按 `schema_v2.sql` 建库）执行 `analyzer.py` 时，`save_insight` 会抛 `table insights has no column named evidence`，灵感工作台永远没有新数据。
2. CI 用新库跑测试时才会暴露；如果测试只覆盖读接口，会漏掉。
3. `ideas` 表与 `save_idea` 的列当前是对齐的，暂未发现同类问题。

**改进方案**
1. 在 `schema_v2.sql` 的 `insights` 建表中补上：
```sql
evidence TEXT,
model    TEXT,
```
2. 在 `_INSIGHT_MIGRATION_COLUMNS` 中补：
```python
_INSIGHT_MIGRATION_COLUMNS = {
    "competition_id": "INTEGER",
    "evidence": "TEXT",
    "model": "TEXT",
}
```
3. 增加“新库全量建表 + 所有写函数冒烟”的测试（本仓库已用 skip 标记：
   `DbBoundaryTest.test_save_insight_on_fresh_schema`，修好后去掉 skip）。
4. 建议对所有 `INSERT` 做列名与 schema 的自动比对（CI 脚本），避免再次漂移。

---

## 3. 上线前修复路线图

### 3.1 P0（阻断发布，必须在公网前完成）

- [ ] 轮换 DeepSeek / Embedding Key，禁用旧 Key；生产改用 Secret 管理。
- [ ] 配置独立 `JWT_SECRET_KEY`（≥32 字节），并在非开发环境禁止默认值启动。
- [ ] CORS 改为显式白名单，`allow_credentials=False`。
- [ ] Nginx + 应用双层 Body 限制（1MB），Pydantic 字段长度限制。
- [ ] 对 `/api/ai/search`、`/api/auth/login`、`/api/auth/register` 加 IP 限流。
- [ ] 确认 `.env` 未进入部署包/镜像；清理 Git 中的 DB 产物（见 3.4）。

### 3.2 P1（灰度前完成）

- [ ] JWT 改短有效期 + refresh/吊销方案（或至少 24h + 登出失效）。
- [ ] LLM 客户端 `max_retries=0`、分离 connect/read 超时、总预算、jitter、`Retry-After`、熔断。
- [ ] SQLite 开启 WAL、`BEGIN IMMEDIATE`、insights 唯一索引；多 worker 改为单 worker 或迁 PG。
- [ ] 补 `kb.py` 或移除 `retriever` 对它的依赖；检索降级打点。
- [ ] 修复 `insights` 新库缺 `evidence/model` 列的 schema 漂移，并补迁移。
- [ ] 修正依赖清单并加锁文件；CI 做干净安装冒烟。
- [ ] 配额失败回滚/成功后扣费；`Retry-After` 计算到零点。
- [ ] 502 不泄露上游异常；统一错误结构。
- [ ] `test_api_boundaries.py` 全绿（本次未执行，交付后由 CI 执行）。

### 3.3 P2（发布后 1~2 个迭代）

- [ ] 修复全表扫描与 N+1；列表分页。
- [ ] 迁移拆出启动流程；lifespan；readiness 探针。
- [ ] 结构化日志、请求 ID、Prometheus 指标、告警。
- [ ] 提示词注入防护与前端 Markdown 清洗/CSP。
- [ ] 清理 `ai_usage`、孤儿 `ai_analysis_cache`。

### 3.4 仓库卫生（建议本轮一起做）

已确认被 Git 跟踪的敏感/大文件：

| 文件 | 问题 | 处理 |
|---|---|---|
| [competitions.db-shm](competitions.db-shm)、[competitions.db-wal](competitions.db-wal) | SQLite 运行时文件，可能含未落盘数据 | `git rm --cached`，加入 `.gitignore` |
| [competitions.db.bak-20261106-115044](competitions.db.bak-20261106-115044) | 数据库备份，可能含用户密码哈希 | 立即从仓库移除并评估历史泄露 |
| [chroma_db/](chroma_db/) | 向量索引二进制，体积大且可重建 | 不纳入版本控制 |

`.gitignore` 当前只有 `*.db`，匹配不到 `*.db-shm`、`*.db-wal`、`*.db.bak-*`。建议补充：
```gitignore
*.db
*.db-*
*.db.bak*
chroma_db/
.env
.env.*
!.env.example
```

> 注意：从 Git 历史彻底删除需要 `git filter-repo`/BFG，并强制推送；如果仓库已外发，按“已泄露”处理（轮换 Key、评估用户数据）。

---

## 4. 关键改造代码片段（可直接参考）

### 4.1 生产配置校验（config.py）

```python
APP_ENV = _get_str("APP_ENV", "dev").lower()
IS_PROD = APP_ENV in ("prod", "production")

JWT_SECRET_KEY = _get_str("JWT_SECRET_KEY", "")
if not JWT_SECRET_KEY:
    if IS_PROD:
        raise RuntimeError("生产环境缺少 JWT_SECRET_KEY")
    JWT_SECRET_KEY = "dev-only-secret-key-please-change-in-prod-4f8a2c9e1b7d"

JWT_ACCESS_EXPIRE_MINUTES = _get_int("JWT_ACCESS_EXPIRE_MINUTES", 30)
JWT_REFRESH_EXPIRE_DAYS = _get_int("JWT_REFRESH_EXPIRE_DAYS", 14)
```

### 4.2 JWT 签发/校验（security.py）

```python
import uuid

def create_access_token(user_id: int, username: str, token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "ver": token_version,
        "iss": "kuao-api",
        "aud": "kuao-web",
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + timedelta(minutes=config.JWT_ACCESS_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, config.JWT_SECRET_KEY, algorithm=config.JWT_ALGORITHM)

def decode_access_token(token: str) -> dict:
    return jwt.decode(
        token,
        config.JWT_SECRET_KEY,
        algorithms=[config.JWT_ALGORITHM],
        audience="kuao-web",
        issuer="kuao-api",
        options={"require": ["exp", "iat", "sub", "jti"]},
        leeway=30,
    )
```

### 4.3 LLM 客户端与重试（llm.py）

```python
import random, time, httpx

def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not API_KEY:
            raise LLMError("缺少 DEEPSEEK_API_KEY，请检查 .env")
        _client = OpenAI(
            api_key=API_KEY,
            base_url=BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=5.0),
            max_retries=0,  # 关键：避免 SDK 与应用重试叠加
        )
    return _client

def _retry_delay(exc: Exception, attempt: int) -> float:
    base = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.3)
    resp = getattr(exc, "response", None)
    if resp is not None:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(max(float(retry_after), 0.5), 30.0)
            except ValueError:
                pass
    return min(base, 30.0)
```

### 4.4 SQLite 连接（db.py）

```python
@contextmanager
def get_conn(path: Optional[str] = None, *, immediate: bool = False):
    db_path = path or DB_PATH
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if immediate:
            conn.execute("COMMIT")
    except Exception:
        if immediate:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
```
> 改 `isolation_level=None` 后，所有写函数必须显式 `immediate=True` 或自行 `BEGIN`；这是较大改动，建议配回归测试。

---

## 5. 验收清单（上线前逐项打勾）

### 安全
- [ ] 真实 Key 已轮换；仓库历史中无有效 Key。
- [ ] 生产 JWT Secret 独立且 ≥32 字节；默认值启动被拒绝。
- [ ] CORS 仅允许生产前端域名；`allow_credentials=False`。
- [ ] 请求体 >1MB 返回 413；登录/搜索有 IP 限流。
- [ ] 所有 SQL 仍保持参数化；LIKE 已转义。
- [ ] 502/500 不包含内部异常文本；日志有 request_id。
- [ ] 前端 Markdown 渲染已做 XSS 清洗，CSP 生效。

### 稳定性
- [ ] SQLite WAL 开启；并发配额测试 0 异常。
- [ ] LLM 总耗时 < 网关超时；重试次数可观测；429 遵守 Retry-After。
- [ ] 单 worker 部署（或已迁 PG）；迁移在启动前独立执行。
- [ ] readiness 能反映 DB/向量库状态。
- [ ] `ai_usage`、`ai_analysis_cache` 有清理策略。

### 功能
- [ ] `test_embedder.py` 在目标环境通过（会真实调用 Embedding）。
- [ ] `test_api_boundaries.py` 全绿（本次仅编写，未执行）。
- [ ] `test_api_boundaries.py` 中两个 skip 用例在功能补齐后改为强制断言。
- [ ] 429、404、422、502 的前端提示与重试策略已联调。

---

## 6. 附录：逐文件速览

| 文件 | 评价 | 优先关注 |
|---|---|---|
| [main.py](main.py) | 结构清晰，但生产中间件缺失 | CORS、Body 限制、lifespan、健康检查 |
| [config.py](config.py) | 配置集中、容错好 | JWT 默认密钥、超时/重试、配额时区 |
| [security.py](security.py) | 密码与 JWT 封装干净 | 7 天过期、无吊销、bcrypt 截断、默认密钥 |
| [llm.py](llm.py) | 重试/JSON 修复考虑周到 | SDK 重试叠加、总预算、jitter、流式超时 |
| [db.py](db.py) | 参数化与上下文管理器规范 | LIKE 转义、WAL、BEGIN IMMEDIATE、全表扫描 |
| [routers/ai.py](routers/ai.py) | 业务完整、缓存设计好 | 输入上限、提示词注入、错误泄露、N+1 |
| [routers/auth.py](routers/auth.py) | 防枚举、幂等注册到位 | 无频率限制、密码字节截断 |
| [routers/competitions.py](routers/competitions.py) | 字段映射显式、兼容老库 | 无分页、pending 作品暴露 |
| [routers/preparations.py](routers/preparations.py) | 简洁、幂等 | check 不校验竞赛存在 |
| [retriever.py](retriever.py) | 两级检索设计合理 | `kb.py` 缺失、全表/批处理 |
| [schema_v2.sql](schema_v2.sql) | 表结构完整、索引较全 | insights 缺 `(competition_id, kind)` 唯一索引 |
| [requirements.txt](requirements.txt) | 严重不完整 | 补依赖、锁版本 |
| [test_embedder.py](test_embedder.py) | 在线自检脚本，非接口测试 | 依赖真实 API/Economy，不能替代 CI 回归 |

---

## 7. 与测试用例的映射

| 审查项 | 对应用例（见 [TEST_CASES_BACKEND.md](TEST_CASES_BACKEND.md)） | 自动化位置 |
|---|---|---|
| R1/R7 JWT 安全 | TC-AUTH-04~09 | `AuthBoundaryTest` |
| R2 CORS | TC-SEC-01（手工/集成） | 手工 |
| R3 请求体/限流 | TC-SEC-03~05 | `RequestSizeBoundaryTest`（skip 待实现） |
| R4 SQL 注入 | TC-COMP-05~06、TC-AUTH-10~11 | `CompetitionBoundaryTest`、`AuthBoundaryTest` |
| R5 并发/锁 | TC-QUOTA-05、TC-DB-05 | `AiQuotaBoundaryTest`、`DbBoundaryTest` |
| R6 LLM 重试 | TC-LLM-01~05 | `LlmRetryBoundaryTest` |
| R7 JWT 过期 | TC-AUTH-05 | `AuthBoundaryTest.test_expired_token_returns_401` |
| R8 配额 | TC-QUOTA-01~06 | `AiQuotaBoundaryTest` |
| R9 输入/注入 | TC-PLAN-02、TC-SEC-05 | `AiPlanBoundaryTest`、`RequestSizeBoundaryTest` |
| R10 依赖/检索 | TC-SEARCH-01~04 | `SearchBoundaryTest` |
| R11 错误泄露 | TC-PLAN-06~08、TC-ANA-04~05 | `AiPlanBoundaryTest`、`AiAnalyzeBoundaryTest` |
| R12 性能 | 压测专项（未自动化） | 手工/Locust |
| R13 启动/迁移 | TC-OPS-01~03（手工） | 手工 |
| R14 可观测性 | TC-OPS-04~05（手工） | 手工 |
| R15 一致性 | TC-COMP-07、TC-DB-06 | `CompetitionBoundaryTest`、`DbBoundaryTest` |
| R16 新库 schema | TC-DB-07 | `DbBoundaryTest.test_save_insight_on_fresh_schema`（skip） |

---

**审查结论**：代码基础不差，主要问题集中在“生产化配置”和“外部依赖治理”，而不是业务逻辑本身。把 P0 四项与 R5/R6/R7/R10 处理掉，再配合边界测试回归，具备小流量灰度条件。
