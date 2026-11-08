# 《云端部署手册》— KuAo 后端 / 爬虫 / AI 定时链

> 目标服务器：阿里云 Ubuntu（公网 IP `112.126.90.252`）
> 适用代码：`KuAo-Platform` 当前 `master`（爬虫闭环 + 候选审核 + AI 攻略 + 前端兜底）
> 读者：在服务器上亲自执行部署的人
> 原则：**先备份、再迁移；先手动验收、再挂 cron；SQLite 单实例、单 worker；任何写生产库的动作都必须可回滚。**

---

## 0. 部署蓝图（先看懂再动手）

### 0.1 拓扑

```text
                 ┌─────────────────────────────┐
  浏览器 ───────▶│ Nginx :80 / :443            │
                 │  ├─ /            → dist/    │  SPA 静态文件
                 │  └─ /api/        → :8000    │  反向代理
                 └──────────┬──────────────────┘
                            │ 127.0.0.1:8000
                 ┌──────────▼──────────────────┐
                 │ uvicorn main:app --workers 1│  systemd: kuao-backend
                 │ FastAPI + SQLite + Chroma   │
                 └──────────┬──────────────────┘
                            │
        ┌───────────────────┼────────────────────┐
        │                   │                    │
  competitions.db      chroma_db/            workspace/
  （唯一真值源）        （可重建索引）         （报告/缓存）
        │
        └── backups/  ← backup_db.py 每日 02:30
```

### 0.2 目录与端口规划

| 路径 | 用途 | 属主 / 权限 |
|---|---|---|
| `/opt/kuao` | Git 仓库根（`backend/` + `frontend/`） | `kuao:kuao`，755 |
| `/opt/kuao/backend` | FastAPI、爬虫、SQLite、venv | `kuao:kuao` |
| `/opt/kuao/backend/competitions.db` | **生产库（唯一真值源）** | `kuao:kuao`，640 |
| `/opt/kuao/backend/backups/` | 每日一致性备份 | `kuao:kuao`，700 |
| `/opt/kuao/backend/logs/` | cron 日志（按天） | `kuao:kuao`，750 |
| `/opt/kuao/backend/locks/` | `flock` 锁文件 | `kuao:kuao`，750 |
| `/opt/kuao/backend/workspace/` | 运行报告、每日守卫状态、预览 JSON | `kuao:kuao`，750 |
| `/opt/kuao/frontend/dist/` | 前端构建产物 | `kuao:kuao`，755（Nginx 可读） |
| `/etc/systemd/system/kuao-backend.service` | 后端服务 | root |
| `/etc/nginx/sites-available/kuao` | Nginx 站点 | root |

| 端口 | 用途 | 是否对公网开放 |
|---|---|---|
| 22 | SSH | 是（建议限制来源 IP） |
| 80 | HTTP（Nginx） | 是 |
| 443 | HTTPS（Nginx） | 是（有域名时） |
| 8000 | uvicorn | **否**，只绑 `127.0.0.1` |

### 0.3 时间线（建议维护窗口）

| 时间 | 动作 |
|---|---|
| T-1 天 | 阿里云控制台给 ECS 打快照；本地生成 DB 一致性快照并校验 |
| T 日 第 1 小时 | 装依赖、拉代码、建 venv、配 `.env` |
| T 日 第 2 小时 | 上传 DB、`integrity_check`、`migrate_production.py` |
| T 日 第 3 小时 | systemd + Nginx + 前端 dist，跑通健康检查 |
| T 日 第 4 小时 | 手动逐条跑 cron 命令，确认退出码 0 |
| T 日 第 5 小时 | 写入 crontab，观察一天 |
| T+1 天 | 验收日志、备份、候选增量；再决定是否开放公网 |

---

## 1. 服务器初始化

### 1.1 登录并确认系统

```bash
ssh root@112.126.90.252

cat /etc/os-release
uname -a
df -h
free -h
```

> 本手册同时覆盖 **Ubuntu 22.04（默认 Python 3.10）** 与 **Ubuntu 24.04（默认 Python 3.12）**。Python 版本分支见第 2.2 节。

### 1.2 更新系统与时区

```bash
apt update
apt upgrade -y

# 定时任务必须使用中国时区；Ubuntu 的 cron 不支持 CRON_TZ，只能改系统时区
timedatectl set-timezone Asia/Shanghai
timedatectl

hostnamectl set-hostname kuao-prod
```

确认输出包含：

```text
Time zone: Asia/Shanghai (CST, +0800)
```

### 1.3 创建部署用户

不要用 root 跑服务。所有代码、venv、cron 都用 `kuao`：

```bash
adduser kuao
usermod -aG sudo kuao

# 把 root 的 SSH 公钥复制给 kuao（保持密钥登录可用）
mkdir -p /home/kuao/.ssh
cp /root/.ssh/authorized_keys /home/kuao/.ssh/authorized_keys 2>/dev/null || true
chown -R kuao:kuao /home/kuao/.ssh
chmod 700 /home/kuao/.ssh
chmod 600 /home/kuao/.ssh/authorized_keys 2>/dev/null || true
```

**先另开一个终端验证 `ssh kuao@112.126.90.252` 能登录，再考虑禁用 root 登录。** 不要把自己锁在门外。

### 1.4 添加 Swap（内存 ≤ 2 GB 必做）

`pip install chromadb` / `onnxruntime` 在 2 GB 内存的机器上容易 OOM：

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h
swapon --show
```

### 1.5 防火墙（两处都要开）

**服务器内 UFW：**

```bash
apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
ufw status verbose
```

**阿里云控制台 → 安全组：** 只放行 `22`、`80`、`443`。**不要放行 8000**，uvicorn 只监听 `127.0.0.1`。

### 1.6 建目录

```bash
mkdir -p /opt/kuao
chown kuao:kuao /opt/kuao
chmod 755 /opt/kuao
```

---

## 2. 依赖安装

### 2.1 系统包

```bash
apt install -y \
  git curl wget unzip rsync sqlite3 \
  build-essential pkg-config \
  libsqlite3-dev libssl-dev libffi-dev \
  python3-dev python3-pip python3-venv \
  nginx ufw ca-certificates
```

校验关键命令：

```bash
git --version
sqlite3 --version
flock --version        # util-linux 自带；没有就 apt install -y util-linux
python3 --version
```

### 2.2 Python 版本

项目要求 **Python 3.10+**，推荐 **3.11 或 3.12**。不要用 3.13（部分依赖轮子尚未齐全）。

#### 情况 A：Ubuntu 24.04（默认 3.12，直接用）

```bash
python3 --version
# 期望 Python 3.12.x
```

#### 情况 B：Ubuntu 22.04（默认 3.10，装 3.11）

```bash
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.11 python3.11-venv python3.11-dev
python3.11 --version
```

后文命令里的 `python3.11` 在 24.04 上可替换为 `python3`。

### 2.3 Node.js（可选）

前端有两种部署方式：

| 方式 | 优点 | 缺点 |
|---|---|---|
| **本地构建 + 上传 `dist/`（推荐）** | 服务器不用装 Node，省内存 | 每次前端改动要重新上传 |
| 服务器构建 | 一条命令完成 | 需要 Node 20 + 约 1 GB 内存 |

如果选择服务器构建：

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs
node -v
npm -v
```

> 本手册默认 **本地构建 + 上传**，服务器构建作为备选写在 6.2。

---

## 3. 拉取代码与配置 `.env`

### 3.1 拉取代码

#### 方式 A：公开仓库（HTTPS）

```bash
sudo -u kuao -H git clone https://github.com/xiao520488/KuAo.git /opt/kuao
sudo -u kuao -H git -C /opt/kuao log -1 --oneline
```

> `/opt/kuao` 必须是空目录（第 1.6 节只建了空目录，可以直接 clone）。

#### 方式 B：私有仓库（Deploy Key，推荐生产）

```bash
sudo -u kuao -H ssh-keygen -t ed25519 -C "kuao-deploy" -f /home/kuao/.ssh/id_ed25519 -N ""
cat /home/kuao/.ssh/id_ed25519.pub
```

把公钥加到 GitHub 仓库 → Settings → Deploy keys（只读即可），然后：

```bash
sudo -u kuao -H git clone git@github.com:xiao520488/KuAo.git /opt/kuao
```

首次连接会提示 `yes/no`：

```bash
sudo -u kuao -H ssh -T git@github.com
```

### 3.2 固定发布版本（强烈建议）

不要直接漂在 `master` 上。记录并固定一个提交：

```bash
sudo -u kuao -H git -C /opt/kuao log -1 --format='%H %s'
sudo -u kuao -H git -C /opt/kuao checkout <发布 commit>
```

把 commit 写进你的发布记录。回滚时就是 `git checkout <上一个 commit>`。

### 3.3 创建 `.env`（只在服务器上）

`.env` 已被 `.gitignore` 忽略，**永远不要提交**。先确认克隆下来没有它：

```bash
ls -la /opt/kuao/backend/.env 2>/dev/null || echo "OK: 没有 .env，需要新建"
```

创建：

```bash
sudo -u kuao -H tee /opt/kuao/backend/.env >/dev/null <<'EOF'
# ============================================================
# KuAo 生产环境配置（只存在于服务器，权限 600）
# ============================================================

# ---------- 大模型 ----------
DEEPSEEK_API_KEY=sk-替换成真实的-DeepSeek-Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash

# ---------- 云端 Embedding（检索用；和 DeepSeek Key 通常不是同一个） ----------
EMBEDDING_API_KEY=sk-替换成真实的-Embedding-Key
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=bge-m3

# ---------- 鉴权 ----------
# 用 `openssl rand -hex 32` 生成；至少 32 位，建议 64 位
JWT_SECRET_KEY=替换成-openssl-rand-hex-32-的输出
JWT_EXPIRE_MINUTES=1440

# ---------- CORS（逗号分隔，禁止 *） ----------
# 有域名后改成 https://你的域名；纯 IP 访问时写 http://112.126.90.252
CORS_ALLOW_ORIGINS=http://112.126.90.252

# ---------- AI 配额 ----------
AI_DAILY_QUOTA=30
AI_GUIDE_DAILY_LIMIT=8
AI_GUIDE_QUEUE_MAX=5
AI_GUIDE_FAIL_COOLDOWN_MIN=30
EOF
```

生成 JWT 密钥：

```bash
openssl rand -hex 32
```

把输出替换到 `.env` 的 `JWT_SECRET_KEY=`。

设置权限：

```bash
chown kuao:kuao /opt/kuao/backend/.env
chmod 600 /opt/kuao/backend/.env
ls -l /opt/kuao/backend/.env
# 期望：-rw------- 1 kuao kuao
```

> **`config.py` 会 fail-fast**：如果 `JWT_SECRET_KEY` 缺失、等于内置 dev 默认值、或长度 < 32，后端直接拒绝启动。这是有意设计，不要改代码绕过。

### 3.4 创建 venv 并安装依赖

```bash
sudo -u kuao -H python3.11 -m venv /opt/kuao/backend/venv
sudo -u kuao -H /opt/kuao/backend/venv/bin/python -m pip install -U pip wheel
sudo -u kuao -H /opt/kuao/backend/venv/bin/pip install -r /opt/kuao/backend/requirements.txt
```

> 22.04 用 `python3.11`；24.04 用 `python3`。

如果卡在 `chromadb` / `onnxruntime`：

```bash
# 确认 swap 已启用
free -h
# 不用缓存重试，减少磁盘/内存峰值
sudo -u kuao -H /opt/kuao/backend/venv/bin/pip install --no-cache-dir -r /opt/kuao/backend/requirements.txt
```

### 3.5 安装后自检（不启动服务）

```bash
cd /opt/kuao/backend

sudo -u kuao -H ./venv/bin/python - <<'PY'
import fastapi, uvicorn, sqlite3, requests, bs4, chromadb
print("fastapi", fastapi.__version__)
print("uvicorn", uvicorn.__version__)
print("sqlite3", sqlite3.sqlite_version)
print("chromadb", chromadb.__version__)
print("deps ok")
PY

# 读取 .env 并校验 JWT（会触发 fail-fast）
sudo -u kuao -H ./venv/bin/python -c "import config; print('model=', config.LLM_MODEL); print('jwt_len=', len(config.JWT_SECRET_KEY)); print('cors=', config.CORS_ALLOW_ORIGINS)"
```

期望：`jwt_len >= 32`，`cors` 里是你的真实来源。

可选：跑一遍离线测试（不连网、不调模型）：

```bash
sudo -u kuao -H ./venv/bin/python -m pytest -q
```

---

## 4. 数据库迁移（核心，务必按顺序）

> 这一节解决三个问题：
> 1. 本地 `competitions.db` 是 **WAL 模式**，直接 `scp` 可能丢最近事务；
> 2. 上传后如何验证「没传坏」；
> 3. 上云后本地/服务器谁是主库、以后怎么同步。

### 4.1 为什么不能直接复制 `competitions.db`

SQLite 的 WAL 模式会把新提交的事务先写进 `competitions.db-wal`，主库文件不一定是完整的：

| 做法 | 结果 |
|---|---|
| `scp competitions.db`（只传主文件） | **可能丢掉 WAL 里最近提交的数据** |
| `scp competitions.db competitions.db-wal competitions.db-shm` | 服务还在写时，三个文件不是同一时刻的快照，**可能撕裂** |
| 停服后 `cp competitions.db` | 可行，但要求确实停干净、且 WAL 已 checkpoint |
| **`sqlite3.Connection.backup()`（本手册采用）** | **在线一致性快照，自动包含已提交的 WAL，单文件自包含** |

结论：**用 SQLite backup API 生成上传快照，不要直接 scp 线上库。**

### 4.2 本地生成一致性快照（Windows PowerShell）

先尽量停掉本地后端（如果有 uvicorn 在跑），减少写入：

```powershell
cd D:\agent-lab\KuAo-Platform\backend

# 如果本地后端正在运行，先停止它（Ctrl+C 或停止对应进程）
```

用 Python 标准库生成快照（**不需要安装 sqlite3.exe**）：

```powershell
.\venv\Scripts\python.exe -c @"
import sqlite3
src = sqlite3.connect('competitions.db')
dst = sqlite3.connect(r'workspace\competitions-upload.db')
src.backup(dst)
dst.execute('PRAGMA journal_mode = DELETE')  # 上传快照用单文件模式，不依赖 -wal
dst.commit()
dst.close()
src.close()
print('snapshot done')
"@
```

校验快照：

```powershell
.\venv\Scripts\python.exe -c @"
import sqlite3
c = sqlite3.connect(r'workspace\competitions-upload.db')
print('integrity:', c.execute('PRAGMA integrity_check').fetchone()[0])
for sql in [
    'SELECT COUNT(*) FROM competitions',
    'SELECT COUNT(*) FROM awards',
    'SELECT COUNT(*) FROM competition_candidates',
    'SELECT COUNT(*) FROM competitions WHERE LENGTH(full_process_guide) > 0',
    'SELECT COUNT(*) FROM ai_jobs',
]:
    print(sql, '->', c.execute(sql).fetchone()[0])
c.close()
"@
```

**期望值（2026-11-08 生产库基线）：**

| 指标 | 期望 |
|---|---|
| `integrity_check` | `ok` |
| competitions | **33** |
| awards | **87** |
| competition_candidates | **82** |
| 有攻略的赛事 | **7** |
| ai_jobs | **0** |

记录快照 SHA-256：

```powershell
Get-FileHash .\workspace\competitions-upload.db -Algorithm SHA256
```

> 注意：**快照 SHA 与线上 `competitions.db` 的 SHA 不一样是正常的**。backup API 会重写页布局，并把日志模式改为 DELETE。验收看 `integrity_check` + 行数，不要死磕 SHA 相等。

### 4.3 上传快照

```powershell
scp .\workspace\competitions-upload.db kuao@112.126.90.252:/tmp/competitions-upload.db
```

如果 `scp` 不可用，用 `rsync`（Windows 可用 WSL / Git Bash）：

```bash
rsync -avP workspace/competitions-upload.db kuao@112.126.90.252:/tmp/competitions-upload.db
```

服务器侧核对 SHA：

```bash
sha256sum /tmp/competitions-upload.db
```

与本地 `Get-FileHash` 的输出逐字符比对。

### 4.4 落到生产路径

```bash
# 如果之前已经启动过服务，先停
sudo systemctl stop kuao-backend 2>/dev/null || true

# 如果服务器上已有旧库，先留一份（首次部署可跳过）
sudo -u kuao -H cp /opt/kuao/backend/competitions.db \
  /opt/kuao/backend/competitions.db.bak-before-upload-$(date +%Y%m%d-%H%M%S) 2>/dev/null || true

# 放入新库
sudo -u kuao -H cp /tmp/competitions-upload.db /opt/kuao/backend/competitions.db
sudo -u kuao -H chmod 640 /opt/kuao/backend/competitions.db

# 关键：清掉可能残留的 WAL / SHM，避免旧日志覆盖新库
sudo -u kuao -H rm -f /opt/kuao/backend/competitions.db-wal \
  /opt/kuao/backend/competitions.db-shm

ls -l /opt/kuao/backend/competitions.db*
```

校验：

```bash
cd /opt/kuao/backend

sudo -u kuao -H sqlite3 competitions.db "PRAGMA integrity_check;"

sudo -u kuao -H sqlite3 competitions.db <<'SQL'
SELECT 'competitions', COUNT(*) FROM competitions
UNION ALL SELECT 'awards', COUNT(*) FROM awards
UNION ALL SELECT 'candidates', COUNT(*) FROM competition_candidates
UNION ALL SELECT 'guides', COUNT(*) FROM competitions
  WHERE LENGTH(full_process_guide) > 0
UNION ALL SELECT 'ai_jobs', COUNT(*) FROM ai_jobs;
SQL
```

> 此时数据库还是旧 schema（可能没有 `ai_jobs` 表 / `guide_*` 列）。上面最后两行如果报 `no such table/column`，**正常**，下一步迁移会补上；先确认前三个数字是 `33 / 87 / 82`。

### 4.5 执行生产库迁移

先只读体检（不动库）：

```bash
cd /opt/kuao/backend

sudo -u kuao -H ./venv/bin/python migrate_production.py --db-path competitions.db
```

确认输出里缺的列/表符合预期（`ai_jobs`、`guide_*` 等），再真正迁移：

```bash
sudo -u kuao -H ./venv/bin/python migrate_production.py --db-path competitions.db --apply
```

它会：

1. 用 SQLite backup API 生成 `competitions.db.bak`（已存在则轮转成 `.bak.<时间戳>`）；
2. 校验备份完整性 + 行数；
3. 调 `db.init_db()` 幂等补列/建表。

迁移后复验：

```bash
sudo -u kuao -H sqlite3 competitions.db ".tables"
sudo -u kuao -H sqlite3 competitions.db "PRAGMA table_info(competitions);" | grep -E 'guide_|image_url|contest_'
sudo -u kuao -H sqlite3 competitions.db "SELECT COUNT(*) FROM ai_jobs;"
sudo -u kuao -H sqlite3 competitions.db "PRAGMA integrity_check;"
sha256sum competitions.db
```

记录迁移后的新 SHA-256，写进交接文档。**迁移会改变文件字节，SHA 与本地不同是正常的。**

### 4.6 WAL 运维问答（务必看）

**Q1：服务器上要不要手动开 WAL？**
不用。`db.get_conn()` 每次连接都会执行 `PRAGMA journal_mode = WAL`，表结构迁移后自动生效。

**Q2：`competitions.db-wal` 越来越大怎么办？**
正常。SQLite 在约 1000 页时自动 checkpoint；也可以用：

```bash
sudo -u kuao -H sqlite3 /opt/kuao/backend/competitions.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

不要在写入高峰执行；它可能短暂阻塞写。

**Q3：恢复备份时为什么必须先删 `-wal/-shm`？**
旧 WAL 里可能还有「新库没有的事务」。如果只覆盖主库、保留旧 WAL，SQLite 会把旧事务回放到新库上，导致数据错乱。**恢复三步：停服务 → 删 `-wal/-shm` → 覆盖主库。**

**Q4：每日备份会不会丢 WAL 里的数据？**
不会。`backup_db.py` 用 `sqlite3.Connection.backup()`，读的是包含已提交 WAL 的一致性快照，并在备份文件上执行 `PRAGMA journal_mode=DELETE`，所以备份是单文件、可直接恢复。

**Q5：能不能把数据库放 OSS / NFS？**
不能。SQLite 依赖本地文件锁，网络盘会导致锁失效、库损坏。数据库必须放本地云盘。

### 4.7 上云后的同步策略（重要）

| 场景 | 正确做法 | 禁止 |
|---|---|---|
| 服务器成为主库后 | 所有 cron/写库脚本只写服务器 | ❌ 再把本地 `competitions.db` 上传覆盖 |
| 本地想拿最新数据开发 | 从服务器下载**备份文件**到本地测试库 | ❌ 反向覆盖服务器 |
| 发布新代码需要改表 | 先 `migrate_production.py` 只读体检，再 `--apply` | ❌ 手工 `ALTER TABLE` / `DROP` |
| 修数据 | 用脚本 + `--apply --allow-production`，自动备份 | ❌ 手工 `UPDATE` 生产库 |
| 每月演练 | 把最新备份复制到 `/tmp`，`integrity_check` + 行数核对 | ❌ 直接在生产库上试恢复 |

从服务器拉一份备份到本地（只读用途）：

```bash
# 服务器
sudo -u kuao -H ./venv/bin/python backup_db.py --db competitions.db --backup-dir /tmp/pull --keep-days 0
ls -l /tmp/pull/
```

```powershell
# 本地
scp kuao@112.126.90.252:/tmp/pull/competitions.db.bak-* .\workspace\prod-snapshot.db
```

### 4.8 数据库回滚

```bash
sudo systemctl stop kuao-backend

cd /opt/kuao/backend

# 选一个备份（每日 backups/ 或迁移生成的 competitions.db.bak）
ls -lt backups/ | head
# 或：ls -lt competitions.db.bak*

# 把 <备份文件名> 换成上面列出的真实文件名（每日备份通常带 -daily 后缀）
sudo -u kuao -H cp backups/<备份文件名> competitions.db
sudo -u kuao -H rm -f competitions.db-wal competitions.db-shm

sudo -u kuao -H sqlite3 competitions.db "PRAGMA integrity_check;"
sudo systemctl start kuao-backend
curl -sS http://127.0.0.1:8000/api/health
```

---

## 5. 后端服务化（systemd）

### 5.1 写 unit 文件

```bash
sudo tee /etc/systemd/system/kuao-backend.service >/dev/null <<'EOF'
[Unit]
Description=KuAo FastAPI backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kuao
Group=kuao
WorkingDirectory=/opt/kuao/backend
EnvironmentFile=/opt/kuao/backend/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/kuao/backend/venv/bin/uvicorn main:app \
  --host 127.0.0.1 --port 8000 --workers 1 \
  --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGINT

# 基础加固：只允许写 backend 目录
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/opt/kuao/backend
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
```

> **`--workers 1` 是硬要求。** SQLite 单写库 + 进程内 `ai_jobs` 线程池，多 worker 会放大并发、增加 `database is locked` 与重复生成风险。要扩多 worker，先迁 PostgreSQL + 外部队列。

### 5.2 启动与自检

```bash
systemctl daemon-reload
systemctl enable --now kuao-backend
systemctl status kuao-backend --no-pager

# 本机健康检查
curl -sS http://127.0.0.1:8000/api/health
# 期望：{"status":"ok","service":"agent-lab-api","version":"0.1.0"}
```

看日志：

```bash
journalctl -u kuao-backend -n 100 --no-pager
journalctl -u kuao-backend -f
```

启动失败最常见原因：

| 日志 | 原因 | 处理 |
|---|---|---|
| `检测到正在使用默认的开发 JWT_SECRET_KEY` | `.env` 没生效/没配 | 检查 `EnvironmentFile`、`.env` 权限与变量名 |
| `JWT_SECRET_KEY 长度不足` | 密钥太短 | `openssl rand -hex 32` 重新生成 |
| `Address already in use` | 8000 被占 | `ss -lntp \| grep 8000`，停掉旧进程 |
| `unable to open database file` | 目录/权限不对 | `chown -R kuao:kuao /opt/kuao/backend` |
| `ModuleNotFoundError` | venv 路径错 | 确认 `ExecStart` 用的是 `/opt/kuao/backend/venv/bin/uvicorn` |

---

## 6. 前端构建与 Nginx

### 6.1 本地构建（推荐）

Windows PowerShell：

```powershell
cd D:\agent-lab\KuAo-Platform\frontend
npm.cmd ci
npm.cmd run build
```

> PowerShell 里必须用 `npm.cmd`，`npm.ps1` 会被执行策略拦截。

产物在 `frontend\dist\`。打包上传：

```powershell
cd D:\agent-lab\KuAo-Platform\frontend
tar -czf dist.tar.gz -C dist .
scp .\dist.tar.gz kuao@112.126.90.252:/tmp/dist.tar.gz
```

服务器解压：

```bash
sudo -u kuao -H mkdir -p /opt/kuao/frontend/dist
sudo -u kuao -H tar -xzf /tmp/dist.tar.gz -C /opt/kuao/frontend/dist
sudo -u kuao -H chmod -R a+rX /opt/kuao/frontend/dist
ls -l /opt/kuao/frontend/dist/index.html
```

### 6.2 服务器构建（备选）

```bash
sudo -u kuao -H bash -lc 'cd /opt/kuao/frontend && npm ci && npm run build'
sudo -u kuao -H chmod -R a+rX /opt/kuao/frontend/dist
```

### 6.3 Nginx 站点

```bash
sudo tee /etc/nginx/sites-available/kuao >/dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name 112.126.90.252;   # 有域名后改成域名

    root /opt/kuao/frontend/dist;
    index index.html;

    client_max_body_size 2m;

    gzip on;
    gzip_comp_level 5;
    gzip_min_length 1024;
    gzip_types text/plain text/css application/json application/javascript
               application/xml image/svg+xml;

    # API 反向代理：保留 /api 前缀
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 10s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;   # AI 生成/检索可能 15–60s
    }

    # 带 hash 的静态资源长缓存
    location /assets/ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location = /index.html {
        add_header Cache-Control "no-cache";
    }

    # SPA History 路由兜底
    location / {
        try_files $uri $uri/ /index.html;
    }
}
EOF

ln -sf /etc/nginx/sites-available/kuao /etc/nginx/sites-enabled/kuao
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl reload nginx
systemctl status nginx --no-pager
```

### 6.4 验证

```bash
curl -I http://112.126.90.252/
curl -sS http://112.126.90.252/api/health
curl -I http://112.126.90.252/competition/1   # 应返回 index.html（SPA 兜底）
```

常见问题：

| 现象 | 原因 | 处理 |
|---|---|---|
| 403 Forbidden | Nginx 读不到 dist | `chmod -R a+rX /opt/kuao/frontend/dist`，检查 `/opt/kuao` 每级目录权限 |
| 404 刷新子路由 | 缺 `try_files` | 确认 `location /` 的 fallback |
| 502 Bad Gateway | uvicorn 没起 / 端口不对 | `systemctl status kuao-backend`、`curl 127.0.0.1:8000/api/health` |
| 接口 CORS 报错 | `CORS_ALLOW_ORIGINS` 不含当前来源 | 改 `.env` 后 `systemctl restart kuao-backend` |
| 页面能开、接口 404 | `/api` 前缀被改写 | `proxy_pass` 不要加尾部 `/` |

### 6.5 HTTPS（有域名再做）

Let's Encrypt 不给裸 IP 签发常规证书；**要么用域名，要么暂时只跑 HTTP**。不要给公网用户用自签名证书。

有域名时：

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d kuao.example.com
# 按提示选择 redirect（把 80 跳到 443）
systemctl list-timers | grep certbot
```

然后把 `.env` 改成：

```dotenv
CORS_ALLOW_ORIGINS=https://kuao.example.com
```

并重启：

```bash
systemctl restart kuao-backend
```

> **混合内容提醒**：库里部分封面是 `http://` 源（历史数据），HTTPS 页面会被浏览器拦截。前端已有渐变首字兜底，不会白屏；要显示原图需给图片做 HTTPS 代理/CDN，或换 https 源。不要为了图片关闭 HTTPS。

---

## 7. 自动化调度链（Crontab）

### 7.1 设计原则

| 原则 | 做法 |
|---|---|
| 时区 | 系统 `Asia/Shanghai`；Ubuntu cron **不支持 `CRON_TZ`** |
| 串行 | 每个任务一个 `flock -n`，同名任务不会叠加 |
| 顺序 | 备份 → 新闻 → 候选 → AI → 周度补全 → 清理 |
| 日志 | 按天一个文件，保留 7 天；`workspace/` 报告另算 |
| 失败可见 | 非 0 退出码留在日志里；建议加告警（7.7） |
| 不重复 | 赛氪每日守卫、AI 3 天失败冷却、`claim_ai_job` 幂等 |
| 不越权 | cron 永不带 `--force`；只走 `--apply --allow-production` 的脚本闸门 |

### 7.2 建运行目录

```bash
cd /opt/kuao/backend
sudo -u kuao -H mkdir -p logs backups locks workspace
sudo -u kuao -H chmod 750 logs locks workspace
sudo -u kuao -H chmod 700 backups
ls -ld logs backups locks workspace
```

### 7.3 挂 cron 前：手动逐条验收

> 每一条都先手动跑，确认退出码 `0`、日志无异常，再写进 crontab。

#### ① 备份

```bash
cd /opt/kuao/backend
sudo -u kuao -H ./venv/bin/python backup_db.py \
  --db competitions.db --backup-dir backups --keep-days 30 --label manual
echo "exit=$?"
```

期望：

```text
[备份完成] .../backups/competitions.db.bak-...（... 字节，integrity=ok）
[行数] competitions=33 / awards=87 / competition_candidates=82 / ai_jobs=0
[SHA-256] ...
exit=0
```

#### ② 新闻采集（先 dry-run，且用测试副本）

```bash
cd /opt/kuao/backend
sudo -u kuao -H cp competitions.db /tmp/test_crawl.db

sudo -u kuao -H ./venv/bin/python crawler_news.py \
  --list-url "<把这里换成真实的列表页 URL>" \
  --db-path /tmp/test_crawl.db \
  --dry-run --max-pages 1 --max-items 1
echo "exit=$?"
```

> `--list-url` 必须是目标高校新闻/通知的**列表页**（有“下一页”或分页参数的那种），不是单篇详情页。
> 第一次接入新站点时，先用 `--max-pages 1 --max-items 1 --dry-run` 验证解析，再放开。

#### ③ 候选发现（只读演练）

```bash
cd /opt/kuao/backend
sudo -u kuao -H ./venv/bin/python discover_saikr.py --db-path competitions.db
echo "exit=$?"
```

这会联网抓赛氪前 2 页并生成 `workspace/saikr_preview.json`，**不写库**。确认解析正常后，真实写入由 cron 执行。

#### ④ AI 慢刷（dry-run，不调模型）

```bash
cd /opt/kuao/backend
sudo -u kuao -H ./venv/bin/python daily_ai_worker.py --db-path competitions.db
echo "exit=$?"
```

期望：打印今天会选哪些赛事、预估 token，**不写库、不调模型**。

#### ⑤ 周度补全（可跳过首次演练）

`pipeline_update.py` 会联网 + 调模型，首次验收可以只跑 1 条：

```bash
cd /opt/kuao/backend
sudo -u kuao -H ./venv/bin/python pipeline_update.py --ids 9 --skip-notice --db-path competitions.db
echo "exit=$?"
```

#### ⑥ 清理命令（先 `-print` 预览，不删）

```bash
find /opt/kuao/backend/logs -type f -name '*.log*' -mtime +7 -print
find /opt/kuao/backend/backups -type f -name 'competitions.db.bak-*' -mtime +30 -print
```

确认列出的都是该删的，再把 `-print` 换成 `-delete`。

### 7.4 写入 crontab

```bash
sudo -u kuao -H crontab -l > /tmp/kuao-cron-backup-$(date +%F).txt 2>/dev/null || true
sudo -u kuao -H crontab -e
```

把下面整段粘进去（**替换 `<LIST_URL>`**）：

```cron
SHELL=/bin/bash
PATH=/opt/kuao/backend/venv/bin:/usr/local/bin:/usr/bin:/bin
LANG=C.UTF-8
MAILTO=""

# 0) 02:30 每日全量备份（SQLite backup API，WAL 安全；保留 30 天）
30 2 * * * cd /opt/kuao/backend && flock -n /opt/kuao/backend/locks/backup.lock ./venv/bin/python backup_db.py --db competitions.db --backup-dir backups --keep-days 30 --label daily >> logs/backup-$(date +\%F).log 2>&1

# 1) 03:00 每周一/周四 高校新闻获奖采集（写 awards / 占位赛事）
0 3 * * 1,4 cd /opt/kuao/backend && flock -n /opt/kuao/backend/locks/news.lock ./venv/bin/python crawler_news.py --list-url "<LIST_URL>" --db-path competitions.db --allow-production --max-pages 4 --max-items 10 --report workspace/news_batch_$(date +\%F).json >> logs/news-$(date +\%F).log 2>&1

# 2) 04:10 每日 赛氪候选发现（只写候选表；每日守卫防重复）
10 4 * * * cd /opt/kuao/backend && flock -n /opt/kuao/backend/locks/discover.lock ./venv/bin/python discover_saikr.py --db-path competitions.db --apply --allow-production >> logs/discover-$(date +\%F).log 2>&1

# 3) 04:40 每日 AI 慢刷（攻略 4 篇 + 灵感 2 条；token 预算熔断）
40 4 * * * cd /opt/kuao/backend && flock -n /opt/kuao/backend/locks/ai.lock ./venv/bin/python daily_ai_worker.py --db-path competitions.db --apply --allow-production --limit 4 --idea-limit 2 --budget-tokens 300000 >> logs/ai-$(date +\%F).log 2>&1

# 4) 05:20 每周日 存量补全（赛氪 + 通知页；限 20 条，5~10 秒限速）
20 5 * * 0 cd /opt/kuao/backend && flock -n /opt/kuao/backend/locks/pipeline.lock ./venv/bin/python pipeline_update.py --db-path competitions.db --apply --allow-production --limit 20 >> logs/pipeline-$(date +\%F).log 2>&1

# 5) 05:40 每日 日志清理（保留 7 天；只清 logs/）
40 5 * * * find /opt/kuao/backend/logs -type f -name '*.log*' -mtime +7 -delete >> /opt/kuao/backend/logs/cleanup.log 2>&1

# 6) 05:50 每周日 备份清理（每日备份 30 天；写前 .bak 14 天）
50 5 * * 0 find /opt/kuao/backend/backups -type f -name 'competitions.db.bak-*' -mtime +30 -delete >> /opt/kuao/backend/logs/cleanup.log 2>&1
50 5 * * 0 find /opt/kuao/backend -maxdepth 1 -type f -name 'competitions.db.bak-*' -mtime +14 -delete >> /opt/kuao/backend/logs/cleanup.log 2>&1
```

检查是否写入：

```bash
sudo -u kuao -H crontab -l
systemctl status cron --no-pager
journalctl -u cron -n 50 --no-pager
```

> **不要忘了替换 `<LIST_URL>`**。带着尖括号的 URL 会让新闻任务每天失败；建议先用 7.3 的 dry-run 验证真实 URL。

### 7.5 调度链时间表与依赖

| 时间 | 任务 | 写入 | 依赖/说明 |
|---|---|---|---|
| 02:30 | `backup_db.py` | `backups/` | 必须在所有写库之前 |
| 03:00（周一/四） | `crawler_news.py` | `awards`、占位 `competitions` | 可能新建占位赛事，AI 当晚会跳过无官网/省级 |
| 04:10 | `discover_saikr.py` | `competition_candidates` | 只写候选，不碰主表；每日守卫 |
| 04:40 | `daily_ai_worker.py` | `competitions.full_process_guide`、`insights`、`ideas`、`ai_jobs` | 吃当天候选/新闻结果；预算熔断 |
| 05:20（周日） | `pipeline_update.py` | 主表缺失字段 | 与 AI 错开；20 条上限 |
| 05:40 | 日志清理 | 删除 7 天前日志 | 只清 `logs/` |
| 05:50（周日） | 备份清理 | 删除过期备份 | 每日 30 天；写前 `.bak` 14 天 |

**不挂 cron 的任务：**

| 脚本 | 原因 |
|---|---|
| `discover_university.py` | 84 项目录年度更新，人工跑一次即可 |
| `review_candidates.py` | 人工审核，必须有人看名称/类别/封面 |
| `cleanup_orphan_ai.py` | 低频数据治理，人工 `--apply --allow-production` |
| `migrate_production.py` | 只在发布/迁移时执行 |
| `--force` | 任何 cron 都不允许；会绕过每日守卫/幂等闸门 |

### 7.6 日志与排障

```bash
cd /opt/kuao/backend

# 今天的任务日志
ls -lt logs/ | head
tail -n 100 logs/ai-$(date +%F).log
tail -n 100 logs/discover-$(date +%F).log
tail -n 100 logs/backup-$(date +%F).log

# cron 自身有没有执行
journalctl -u cron --since today --no-pager | tail -n 50

# 锁没抢到（任务被跳过）时，日志文件可能是空的；看是否有另一个同名任务在跑
ps -ef | grep -E 'crawler_news|discover_saikr|daily_ai_worker|pipeline_update' | grep -v grep
```

### 7.7 失败告警（可选，但建议）

`MAILTO=""` 不会发邮件。最简单的做法是加一个包装脚本，把退出码写进 `logs/job_status.log`，再用云监控/告警平台抓关键字：

```bash
sudo -u kuao -H mkdir -p /opt/kuao/backend/bin
sudo -u kuao -H tee /opt/kuao/backend/bin/run_job.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -u
name="$1"; shift
cd /opt/kuao/backend || exit 2
"$@" >> "logs/${name}-$(date +%F).log" 2>&1
rc=$?
echo "[$(date '+%F %T')] ${name} exit=${rc}" >> logs/job_status.log
if [ "$rc" -ne 0 ]; then
  # 可选：钉钉/企业微信/Slack webhook
  # curl -s -m 5 -H 'Content-Type: application/json' \
  #   -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"KuAo ${name} failed rc=${rc}\"}}" \
  #   "https://oapi.dingtalk.com/robot/send?access_token=..." >/dev/null || true
  :
fi
exit "$rc"
EOF
sudo -u kuao -H chmod 750 /opt/kuao/backend/bin/run_job.sh
```

然后把 cron 里的命令改成：

```cron
30 2 * * * /opt/kuao/backend/bin/run_job.sh backup ./venv/bin/python backup_db.py --db competitions.db --backup-dir backups --keep-days 30 --label daily
```

> 包装脚本已经把日志重定向到 `logs/<name>-<date>.log`，cron 行里就不要再写 `>>`，否则会重复。

### 7.8 常见坑

| 坑 | 现象 | 解决 |
|---|---|---|
| `%` 没转义 | cron 把 `%` 当换行，日志名/命令被截断 | 写成 `\%` |
| `CRON_TZ` | Ubuntu cron 不认，任务按 UTC 跑 | 改系统时区 `timedatectl set-timezone Asia/Shanghai` |
| PATH 不含 venv | `python: command not found` | 顶部设 `PATH`，或写绝对路径 `./venv/bin/python` |
| 没有 `cd` | 相对路径 `competitions.db`、`workspace/` 找不到 | 每条都 `cd /opt/kuao/backend && ...` |
| 锁文件目录不可写 | `flock: cannot open lock file` | `locks/` 归 `kuao` 且 750 |
| `flock -n` 抢不到锁 | 任务静默跳过 | 看 `ps` 是否上一轮还没跑完；必要时改 `-w 1800` 排队 |
| 日志清理误删 | `find` 写错目录 | 只对 `logs/` 用 `-name '*.log*'`；`backups/` 单独规则 |
| 时区差 8 小时 | 任务在凌晨 10 点跑 | `timedatectl` 确认 `Asia/Shanghai` |

### 7.9 可选：异地备份到阿里云 OSS

本地 `backups/` 只能防误删/坏库，防不了整机故障。生产环境建议再加一条 OSS 同步（只传备份文件，不回传）：

1. 用 RAM 子账号，只授予目标 bucket 的 `PutObject` / `ListObjects` 权限；不要把主账号 AK 写进 crontab。
2. 按阿里云官方文档安装 `ossutil`，配置独立 profile（示例参数以你使用的 ossutil 版本为准）：

```bash
ossutil config \
  -e oss-cn-beijing.aliyuncs.com \
  -i <RAM-AccessKeyId> \
  -k <RAM-AccessKeySecret>
```

3. 每天备份成功后上传（放在 02:30 备份之后）：

```cron
10 3 * * * /usr/local/bin/ossutil cp -r /opt/kuao/backend/backups/ oss://<bucket>/kuao/backups/ --update >> /opt/kuao/backend/logs/oss-$(date +\%F).log 2>&1
```

4. bucket 开版本控制 + 生命周期（如 30/90 天）；每季度至少下载一份备份做恢复演练。
5. OSS 上传失败不应影响本地备份：它是独立 cron 行，失败只写 `logs/oss-*.log`，不要串进 `backup_db.py` 的退出码。

---

## 8. 上线验收清单

### 8.1 服务与页面

```bash
curl -sS http://127.0.0.1:8000/api/health
curl -sS http://112.126.90.252/api/health
curl -I http://112.126.90.252/
curl -I http://112.126.90.252/competitions
curl -I http://112.126.90.252/competition/1
```

| 检查 | 期望 |
|---|---|
| `/api/health` | `{"status":"ok",...}` |
| `/` | 200，返回前端 HTML |
| `/competitions`（刷新） | 200，SPA fallback 生效 |
| `/competition/1`（刷新） | 200，不是 404 |
| 浏览器控制台 | 无 CORS / Mixed Content 报错（图片 http 会走兜底） |

### 8.2 功能

- [ ] 首页列表能加载，封面/渐变兜底正常；
- [ ] 注册/登录成功，刷新后登录态保持；
- [ ] 赛事详情能打开，攻略为空时显示「✨ 生成 AI 攻略」；
- [ ] 登录后点生成 → 202 → 轮询 → 攻略出现（约 15–60 秒）；
- [ ] `GET /api/competitions` 响应里没有 `full_process_guide`（列表已瘦身）；
- [ ] 备赛清单、AI 检索、反馈等原有功能抽检。

### 8.3 数据库

```bash
cd /opt/kuao/backend

sudo -u kuao -H sqlite3 competitions.db "PRAGMA integrity_check;"
sudo -u kuao -H sqlite3 competitions.db <<'SQL'
SELECT 'competitions', COUNT(*) FROM competitions
UNION ALL SELECT 'awards', COUNT(*) FROM awards
UNION ALL SELECT 'candidates', COUNT(*) FROM competition_candidates
UNION ALL SELECT 'guides', COUNT(*) FROM competitions
  WHERE LENGTH(full_process_guide) > 0
UNION ALL SELECT 'ai_jobs', COUNT(*) FROM ai_jobs
UNION ALL SELECT 'insights', COUNT(*) FROM insights
UNION ALL SELECT 'ideas', COUNT(*) FROM ideas;
SQL
```

期望：`33 / 87 / 82 / 7 / 0 / 3 / 4`（与本地快照一致；AI 跑过之后攻略数会增加）。

### 8.4 备份与恢复演练

```bash
cd /opt/kuao/backend

# 生成一份备份
sudo -u kuao -H ./venv/bin/python backup_db.py \
  --db competitions.db --backup-dir /tmp/restore-drill --keep-days 0

# 校验它（不需要停服）
ls -l /tmp/restore-drill/
sudo -u kuao -H sqlite3 /tmp/restore-drill/competitions.db.bak-* "PRAGMA integrity_check;"
sudo -u kuao -H sqlite3 /tmp/restore-drill/competitions.db.bak-* "SELECT COUNT(*) FROM competitions;"
```

### 8.5 安全

```bash
# 只应看到 22/80/443；8000 只能 127.0.0.1
ss -lntp | grep -E ':(22|80|443|8000)'

# .env 与库权限
stat -c '%a %U:%G %n' /opt/kuao/backend/.env /opt/kuao/backend/competitions.db

# 仓库里不应有真实 key
cd /opt/kuao
sudo -u kuao -H git grep -nE 'sk-[A-Za-z0-9]{16,}' || echo "no key in git"

# 确认 .env 没被跟踪
sudo -u kuao -H git status --short
```

### 8.6 Cron

- [ ] `crontab -l` 内容完整，`<LIST_URL>` 已替换；
- [ ] 手动逐条执行退出码 0；
- [ ] 第二天检查 `logs/backup-*.log`、`logs/news-*.log`（周一/四）、`logs/discover-*.log`、`logs/ai-*.log`；
- [ ] `backups/` 出现当日备份，`integrity=ok`；
- [ ] 候选表有增量，`pending` 不爆仓；
- [ ] `ai_jobs` 失败任务不超过预期，`error` 可读。

---

## 9. 回滚预案

### 9.1 代码回滚

```bash
cd /opt/kuao
sudo -u kuao -H git log --oneline -n 5
sudo -u kuao -H git checkout <上一个稳定 commit>

# 依赖有变化才重装
sudo -u kuao -H ./backend/venv/bin/pip install -r backend/requirements.txt

# 前端有变化才重新上传/构建 dist

systemctl restart kuao-backend
curl -sS http://127.0.0.1:8000/api/health
```

### 9.2 数据库回滚

见 4.8。要点：**停服务 → 删 `-wal/-shm` → 覆盖主库 → integrity → 启动**。

### 9.3 定时任务回滚

```bash
# 先备份当前 crontab
sudo -u kuao -H crontab -l > /tmp/kuao-cron-current.txt

# 编辑，把出问题的行注释掉（行首加 #）
sudo -u kuao -H crontab -e
```

不要用 `crontab -r` 清空全部任务，除非你确认要全部停掉。

### 9.4 整机回滚

阿里云控制台 → 快照 → 回滚磁盘。回滚前先确认快照时间点，并停止写入（`systemctl stop kuao-backend`、注释 cron）。

---

## 10. 运维速查

```bash
# 服务
systemctl status kuao-backend --no-pager
systemctl restart kuao-backend
journalctl -u kuao-backend -n 100 --no-pager

# 健康
curl -sS http://127.0.0.1:8000/api/health

# 数据库
cd /opt/kuao/backend
sudo -u kuao -H sqlite3 competitions.db "PRAGMA integrity_check;"
sudo -u kuao -H sqlite3 competitions.db "SELECT COUNT(*) FROM competitions;"

# 备份
sudo -u kuao -H ./venv/bin/python backup_db.py --db competitions.db --backup-dir backups --keep-days 30 --label manual

# 候选
sudo -u kuao -H ./venv/bin/python review_candidates.py --list --db-path competitions.db --limit 20

# AI 任务
sudo -u kuao -H sqlite3 competitions.db \
  "SELECT id,kind,status,attempts,substr(error,1,80),created_at FROM ai_jobs ORDER BY id DESC LIMIT 10;"

# Cron
sudo -u kuao -H crontab -l
journalctl -u cron --since today --no-pager | tail -n 50
```

### 故障排查表

| 现象 | 优先检查 | 处理 |
|---|---|---|
| 502 | uvicorn 是否活着 | `systemctl status`、`journalctl -u kuao-backend -n 100` |
| 页面 403 | dist 权限 / 父目录权限 | `namei -l /opt/kuao/frontend/dist/index.html`；`chmod -R a+rX` |
| 刷新 404 | Nginx `try_files` | 检查 `location /` |
| CORS | `.env` 来源 | 改 `CORS_ALLOW_ORIGINS` 后重启 |
| 登录 401 | JWT 密钥变了 | 重新登录；确认 `.env` 没被改 |
| `database is locked` | 两个写任务重叠 | 看 `ps`、`flock`；把 pipeline 错开到 06:00 后 |
| `no such table` | cwd / `--db-path` 错 | 所有命令先 `cd /opt/kuao/backend` |
| AI 一直 running | 15 分钟租约未到/进程被杀 | 看 `ai_jobs.error`；等租约回收或手工标 failed |
| AI 429 | 日预算/队列满 | 等次日零点，或调 `AI_GUIDE_DAILY_LIMIT` |
| 图片不显示（HTTPS） | http 混合内容 | 前端渐变兜底；要原图需 HTTPS 代理/CDN |
| cron 不执行 | 时区/PATH/`%`/权限 | 逐项看 7.8 |
| 备份失败 | 磁盘满/权限/库锁 | `df -h`、`ls -ld backups`、`PRAGMA integrity_check` |

---

## 附录 A：`.env` 完整模板

```dotenv
# ---------- 大模型 ----------
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash

# ---------- Embedding ----------
EMBEDDING_API_KEY=sk-...
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=bge-m3

# ---------- JWT ----------
JWT_SECRET_KEY=<openssl rand -hex 32>
JWT_EXPIRE_MINUTES=1440

# ---------- CORS ----------
CORS_ALLOW_ORIGINS=http://112.126.90.252

# ---------- AI 配额 ----------
AI_DAILY_QUOTA=30
AI_GUIDE_DAILY_LIMIT=8
AI_GUIDE_QUEUE_MAX=5
AI_GUIDE_FAIL_COOLDOWN_MIN=30
```

## 附录 B：数据库校验 SQL

```sql
-- 完整性
PRAGMA integrity_check;

-- 核心表行数
SELECT 'competitions' AS item, COUNT(*) AS n FROM competitions
UNION ALL SELECT 'awards', COUNT(*) FROM awards
UNION ALL SELECT 'candidates', COUNT(*) FROM competition_candidates
UNION ALL SELECT 'guides', COUNT(*) FROM competitions
  WHERE LENGTH(full_process_guide) > 0
UNION ALL SELECT 'ai_jobs', COUNT(*) FROM ai_jobs
UNION ALL SELECT 'insights', COUNT(*) FROM insights
UNION ALL SELECT 'ideas', COUNT(*) FROM ideas;

-- 候选状态分布
SELECT status, COUNT(*) FROM competition_candidates GROUP BY status;

-- 今日 AI 任务
SELECT id, kind, status, attempts, tokens, substr(error,1,120), created_at
FROM ai_jobs ORDER BY id DESC LIMIT 20;

-- 有攻略的赛事
SELECT id, name, guide_generated_at, guide_model
FROM competitions
WHERE LENGTH(full_process_guide) > 0
ORDER BY id;
```

## 附录 C：Crontab 一键复制

见 7.4。复制后务必：

1. 替换 `<LIST_URL>`；
2. `sudo -u kuao -H crontab -l` 核对；
3. 先手动跑 7.3 的每条命令。

## 附录 D：systemd / Nginx 文件位置

| 文件 | 路径 |
|---|---|
| 后端服务 | `/etc/systemd/system/kuao-backend.service` |
| Nginx 站点 | `/etc/nginx/sites-available/kuao` → `sites-enabled/kuao` |
| 应用配置 | `/opt/kuao/backend/.env`（600） |
| 生产库 | `/opt/kuao/backend/competitions.db`（640） |
| 每日备份 | `/opt/kuao/backend/backups/`（700） |
| 运行日志 | `/opt/kuao/backend/logs/`（750） |
| 运行报告 | `/opt/kuao/backend/workspace/`（750） |

## 附录 E：本地生成上传快照（PowerShell 汇总）

```powershell
cd D:\agent-lab\KuAo-Platform\backend

.\venv\Scripts\python.exe -c @"
import sqlite3
src = sqlite3.connect('competitions.db')
dst = sqlite3.connect(r'workspace\competitions-upload.db')
src.backup(dst)
dst.execute('PRAGMA journal_mode = DELETE')
dst.commit()
dst.close()
src.close()
print('snapshot done')
"@

.\venv\Scripts\python.exe -c @"
import sqlite3
c = sqlite3.connect(r'workspace\competitions-upload.db')
print('integrity:', c.execute('PRAGMA integrity_check').fetchone()[0])
print('competitions:', c.execute('SELECT COUNT(*) FROM competitions').fetchone()[0])
print('awards:', c.execute('SELECT COUNT(*) FROM awards').fetchone()[0])
print('candidates:', c.execute('SELECT COUNT(*) FROM competition_candidates').fetchone()[0])
print('guides:', c.execute('SELECT COUNT(*) FROM competitions WHERE LENGTH(full_process_guide) > 0').fetchone()[0])
print('ai_jobs:', c.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0])
c.close()
"@

Get-FileHash .\workspace\competitions-upload.db -Algorithm SHA256
scp .\workspace\competitions-upload.db kuao@112.126.90.252:/tmp/competitions-upload.db
```

---

## 附：部署完成后请回填交接文档

把以下信息写进 `CRAWLER_HANDOVER.md` 第十三节（或新增「上云记录」）：

| 项 | 值 |
|---|---|
| 发布 commit | `git -C /opt/kuao log -1 --format=%H` |
| 部署时间 | |
| 服务器 | 阿里云 Ubuntu `112.126.90.252` |
| 迁移后 DB SHA-256 | `sha256sum /opt/kuao/backend/competitions.db` |
| 迁移后行数 | competitions / awards / candidates / guides / ai_jobs |
| 备份路径 | `/opt/kuao/backend/backups/` |
| cron 生效时间 | |
| 首次验收人 | |
| 回滚 commit / 备份 | |
