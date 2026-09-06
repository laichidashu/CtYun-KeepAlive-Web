# CtYun KeepAlive - 天翼云电脑与云手机多账号网页保活管理器

`CtYun KeepAlive` 是一款专门为**天翼云电脑（CtYun Desktop）**和**天翼云手机（CtYun Phone）**设计的多账号保活工具。该项目通过模拟客户端连接协议、维护底层 WebSocket 链路并动态响应保活质询，有效防止设备因长时间无操作被系统自动关闭或休眠。

在此基础上，项目已整合为一套**一体化 Web 管理平台**：除多账号保活外，还提供定时任务（AI 对话 / 云电脑挂机，驱动 Python 脚本）、积分自动兑换、运行环境自检、实时日志推送与图形化控制台。

**后端为纯 Python 实现（`backend/`，仅标准库、零第三方依赖）**，单命令即可运行；**前端为原生 HTML/CSS/JS（`frontend/`，零框架、零 CDN）**，由后端直接托管，整套系统可完全离线运行，**无需 Docker、无需 .NET**。

> 历史版本使用 .NET 8 AOT 实现，现已全面替换为 Python 后端；数据文件（`accounts.json` / `jobs.json` / `jobs_history.json` / `redeem_config.json`）格式与旧版完全互通。

<img width="3792" height="1952" alt="image" src="https://github.com/user-attachments/assets/f7eb407d-da8c-43d3-b2d5-de44631fa03b" />

---

## ✨ 功能一览

### 1. 多账号保活
- 📱 **云手机与云电脑双支持**：根据天翼云接口返回的 `OsType` 自动判断设备类型，对云手机自动适配 `pm.ctyun.cn` 的 WebSocket 握手 Origin。
- 👥 **多账号并发保活**：每账号一条会话线程，每台设备一条 WebSocket worker，账号互不干扰。
- 🔄 **24h 强制重建**：保活会话默认每 `sessionRestartMinutes`（默认 1440 = 24 小时）主动重建一次，`0` 表示关闭。
- 🩹 **四级自愈链路**：连接瞬断按 `{5, 10, 20, 40, 60}s` 自适应退避重连 → 连续失败自动重新 `connect` 刷新设备连接信息（应对平台轮换网关/证书）→ 整会话重建（重新登录）→ 全部失败才告警。
- 🔌 **自动开机（防关机）**：会话启动对未运行的云电脑**主动发送开机指令**（`vdCommand=powerOn` + 专用接口兜底），配合 `bootWaitRounds`（默认 10）× 60s 轮询等待；云电脑被平台关机后可自动拉起，无需人工干预。
- 🔍 **真实状态校验（防"假保活"）**：WebSocket 心跳正常 ≠ 平台认定桌面在用。每 5 个周期核对一次平台侧 `useStatusText`，连续异常立即重建会话（含自动开机）——在平台约 60 分钟的关机宽限期内完成自愈。
- ⏳ **开机等待**：设备未开机时最多等待 `bootWaitRounds` 轮 × `bootWaitSecondsPerRound` 秒（默认 10 × 60s）。
- 📊 **运行指标**：启动时间、运行时长、心跳成功/失败数、连续失败次数、重连次数、下次重试/强制重启时间、最近错误。
- 🔐 **保活质询应答**：内置 RSA-OAEP(SHA-1) 应答器，正确响应服务端 `REDQ` 保活校验帧（`backend/wire.py`）。

### 2. Web 控制台（6 个面板）
| 面板 | 说明 |
|------|------|
| **总览** | 账号总数/运行中、累计保活时长、今日任务成功/失败数、下次任务时间与名称、环境自检状态、调度器运行态 |
| **账号** | 添加（含短信验证码绑定）/ 启动 / 停止 / 删除 / 改名改密，查看每台设备的状态与云电脑 ID |
| **定时任务** | 任务的增删改查、cron 预览、手动执行、停止、执行历史 |
| **兑换** | 兑换配置读写、奖励列表拉取、兑换计划判定、立即执行兑换 |
| **日志** | 实时日志终端（SSE 推送），按级别着色 |
| **设置** | 保活周期、各类超时、Python 路径、脚本目录、互斥模式、轮询间隔等 |

### 3. 定时任务
- ⏰ **标准 5 段 cron 表达式**，支持 `*` / `,` / `-` / `/`，面板提供**校验 + 中文描述 + 未来 3 次触发时间预览**（不支持 `L`/`?`/`@宏` 等）。
- 🤖 **两种任务类型**：
  - `ai_chat`：AI 对话任务，驱动 `scripts/ai_chat_task.py`
  - `pc_hang`：云电脑挂机任务，驱动 `scripts/pc_hang_task.py`（同时承载积分兑换的 Python 通道 B）
- ▶️ **手动执行**：立即触发一次（异步返回，不等待完成）。
- 🧾 **执行历史**：开始/结束时间、耗时、退出码、成功/超时标记与日志摘要，保留最近 **50** 条。
- 🔒 **浏览器互斥**：`browserMutexMode` = `Global`（默认）时 AI 对话与云电脑挂机互斥；`PerType` 按类型各一把锁、可并行。被拒绝的任务立即失败不排队。

### 4. 积分自动兑换（双通道）
- **通道 A（后端内置）**：直连 `selforder` 接口拉取奖励列表 / 积分进度并 `placeOrder` 下单，无需浏览器。
- **通道 B（Python 脚本）**：由「云电脑挂机」任务在脚本内完成兑换，作为通道 A 登录态失效（code=40010）时的降级方案。
- 📅 **调度策略**：`daily`（每日）/ `interval_days`（每隔 N 天）/ `monthly_days`（每月指定日，`-1` 表示月末）。

### 5. 环境自检
一键探测 Python 解释器、`DrissionPage` / `ddddocr` / `requests` 三个依赖、两个脚本文件是否存在，以及（建议性）Chromium。每一项都给出可复制的修复命令。

### 6. 日志实时推送（SSE）+ 本地文件日志
前端通过 Server-Sent Events 实时同步后端日志（`GET /api/logs?token=...`）；后端保留最近 `logHistorySize`（默认 500）条环形历史，新订阅者先收历史再收增量。
同时**持久化到本地文件**：`logs/app-YYYY-MM-DD.log`（按天滚动，带 `[INFO/OK/WARN/ERROR]` 级别标签，自动清理 30 天前的旧日志），重启/断开页面均不丢日志。

### 7. 可靠性细节（实测踩坑沉淀）
- 🧩 **验证码本地 OCR**：ddddocr + 灰度/多阈值二值化/4× 放大管线（合规率 12/12），外部 OCR 接口仅作兜底，不受代理波动影响；登录失败自适应重试（每次重新取验证码）。
- 🪟 **营销弹窗穿透**：AI 对话页的 VIP 升级弹窗会拦截发送点击，脚本自动识别并以 JS 移除弹窗/遮罩节点，发送后校验输入框清空、回车键兜底重发。
- 🪪 **AI 对话会话归属校验**：无头浏览器配置文件会残留上次登录的账号会话。脚本校验页面登录用户与任务账号一致（掩码比对），不一致自动清除 Cookie/本地存储并重新登录——杜绝"顶替其他账号聊天导致积分入错账"。
- 📸 **失败现场留证**：任务异常自动截图（Windows 安全文件名），调度器按退出码如实记录成败。

---

## 🚀 快速开始（无需 Docker）

### 方式一：双击启动（Windows）
双击项目根目录的 **`启动服务.bat`**，按 `Ctrl+C` 停止。

启动脚本会自动完成三步，全部就绪后才开始运行服务：
1. **定位解释器**：项目内置 `.venv` → 系统 `python` → `py -3` 启动器；未安装 Python 时给出下载指引（**安装时务必勾选 "Add python.exe to PATH"**）
2. **创建虚拟环境**：无 `.venv` 时自动创建，依赖隔离不污染系统 Python
3. **环境自检 + 自动装依赖**：运行 `backend/bootstrap.py` 检查 `requests / pillow / DrissionPage / ddddocr`，缺失项自动 `pip` 安装（默认源失败自动换清华镜像），安装复检通过才启动服务

### 方式二：命令行启动（Windows / Linux / macOS 通用）
```bash
# 需要 Python 3.10+（零第三方依赖）
python backend/server.py
# 可选环境变量：CTYUN_DATA_DIR（数据目录，默认工程根）、PORT（默认 8080）
```

打开 `http://localhost:8080`，默认访问密码 `admin`（登录后请立即在「设置」里修改）。

> 若要使用定时任务 / 积分兑换，还需任务脚本的运行环境：`pip install -r requirements.txt`（即 `DrissionPage ddddocr requests`；浏览器任务另需 Chromium）。登录后在「设置 → 环境自检」可查看缺失项与修复命令。

> 首次使用可复制示例配置快速起步：`accounts.example.json` → `accounts.json`、`jobs.example.json` → `jobs.json`、`redeem_config.example.json` → `redeem_config.json`（或直接登录 Web 控制台在界面上添加账号/任务）。

---

## 🔒 隐私与数据安全

- 所有账号、密码、Cookie、设备码**仅保存在本地数据目录**，通过 `.gitignore` 排除，**不会进入 Git 仓库**。
- 日志中的手机号一律掩码显示（如 `138****8888`）；本地日志文件同样脱敏。
- 请勿将 `accounts.json`、`ctyun_cookies_*.json`、`devices/` 等运行数据分享或提交到公开仓库。
- 本项目仅供学习交流，请遵守天翼云服务条款，合理使用。

---

## ⚙️ 配置说明

所有数据都落在数据目录（容器内 `/app/data`，本地默认为工程根目录）：

| 文件 | 作用 |
|------|------|
| `accounts.json` | 主配置：账号列表 + 全部运行参数 + Web 访问密码（`adminPassword`） |
| `jobs.json` | 定时任务定义 |
| `jobs_history.json` | 任务执行历史（最近 50 条） |
| `redeem_config.json` | 积分兑换配置（C#/Python 通道与脚本共用，字段名与 `pc_hang_task.py` 逐字一致） |
| `.devicecode_<user>` | 设备码兜底文件（非 TTY 下脚本读取，避免 input() 抛 EOFError） |
| `devices/` | 设备码持久化目录（`web_` 前缀 32 位随机串） |
| `ctyun_restart_at` | 延迟重启信号文件（脚本写入 → 后端消费 → 全量重启保活） |

所有配置写入均为**原子写**：先序列化到内存 → 备份 `.bak` → 写 `.tmp` → 原子替换；读取失败自动回落 `.bak`。

### 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `APP_USER` / `APP_PASSWORD` | 空 | 初始账号（仅在 `accounts.json` 完全缺失时生效） |
| `APP_NAME` | 空 | 初始账号备注名 |
| `DEVICECODE` | 空 | 设备码，留空自动生成并持久化 |
| `CTYUN_DATA_DIR` | 容器 `/app/data` / 本地工程根 | 数据目录 |
| `CTYUN_CONFIG` | 空 | 单独指定 accounts.json 路径 |
| `PORT` | 8080 | Web 监听端口（监听 0.0.0.0） |
| `TZ` | — | 时区；cron 按本地时间判定 |

---

## 🗂️ 项目结构

```
├── backend/               # Python 后端（纯标准库）
│   ├── server.py          # 入口：路径/配置/后台线程/HTTP 服务
│   ├── httpd.py           # 30 个 REST 端点 + 静态托管 + SSE
│   ├── keepalive.py       # 保活引擎（会话状态机/退避/心跳）+ 重启调度
│   ├── ctyun_api.py       # 天翼云客户端（登录/验证码 OCR/短信/设备/签名）
│   ├── wire.py            # WebSocket 二进制协议 + RSA-OAEP(SHA1) 保活应答
│   ├── wsclient.py        # 最小 RFC6455 WebSocket 客户端（标准库实现）
│   ├── jobs.py            # 定时任务服务 + cron 调度器
│   ├── cronx.py           # 5 段 cron 解析器
│   ├── scriptrunner.py    # Python 任务脚本运行器（超时/进程树回收）
│   ├── redeem.py          # 积分兑换（通道 A）+ 调度策略
│   ├── envprobe.py        # 环境自检
│   ├── mutex.py           # 浏览器任务互斥（Global / PerType）
│   ├── sessions.py        # Web 管理会话令牌
│   ├── logs.py            # 日志多播器（环形历史 + SSE 订阅）
│   └── store.py           # 路径解析 / 原子配置存取 / 全局状态
├── frontend/              # 前端（index.html / styles.css / app.js，零框架）
├── scripts/               # 任务脚本（ai_chat_task.py / pc_hang_task.py）
├── docs/                  # 文档（DELIVERY-PYTHON.md 为最新交付说明）
├── logs/                  # 运行时数据：按天滚动的本地日志（app-YYYY-MM-DD.log）
├── accounts.example.json  # 示例配置：账号与运行参数（复制为 accounts.json 使用）
├── jobs.example.json      # 示例配置：定时任务
├── redeem_config.example.json  # 示例配置：积分兑换
├── accounts.json          # 运行时数据：账号 + 全部配置（首次启动自动生成，已 gitignore）
├── devices/               # 运行时数据：设备码（已 gitignore）
├── 启动服务.bat            # Windows 一键启动
└── .gitignore             # 敏感数据与运行产物全部排除
```

---

## 🔑 访问与安全

- 默认访问密码 `admin`（`accounts.json` 的 `adminPassword` 字段）。**没有环境变量可以覆盖它**；首次登录后请在「设置」面板修改。
- 改密会立即吊销所有已签发的会话令牌。
- Web 会话令牌有效期 `sessionTokenHours`（默认 12 小时），最多 32 个并发会话。
- 请勿将 8080 端口直接暴露公网，建议置于反向代理 + 访问控制之后。

---

## ⚠️ 已知限制

1. `selforder` 接口鉴权与页面结构依赖线上环境，无法离线验证；通道 A 登录态失效时请启用「云电脑挂机」任务走通道 B。
2. 定时任务脚本需要 `DrissionPage`/`ddddocr`/`requests`/`pillow` 与 Chromium——完整版镜像已内置，本地裸跑请自行安装。
3. 平台页面结构（弹窗、按钮、验证码样式）可能随版本更新变化，届时需对应调整脚本选择器。
4. 保活与自动化操作请遵守天翼云服务条款，仅建议在小规模个人账号上使用。

---

## 🙏 致谢

本项目的实现借鉴了以下开源项目，感谢原作者的分享：

- [leleji/CtYun](https://github.com/leleji/CtYun) —— 天翼云客户端接口与保活协议的参考实现；
- [keaidang/CtYun-KeepAlive-Web](https://github.com/keaidang/CtYun-KeepAlive-Web) —— 多账号网页保活管理器的整体思路与 Web 架构参考。
