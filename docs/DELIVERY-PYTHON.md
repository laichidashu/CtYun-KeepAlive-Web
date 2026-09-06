# 交付总览：后端 .NET → Python 全面替换

日期：2026-09-05 ｜ 状态：**已完成并本机实测通过**

## 背景

旧后端为 .NET 8 AOT 实现，但本机无 .NET SDK，只能静态交付无法运行验证。应用户要求，将后端**全面替换为纯 Python 实现**（仅标准库、零第三方依赖），前端与 API 契约保持不变。

## 交付物

### 新增 `backend/`（Python 后端，15 个模块，约 3300 行）
| 模块 | 对应 C# | 说明 |
|------|---------|------|
| server.py | Program.cs | 入口：路径/配置容错加载/后台线程/HTTP 服务 |
| httpd.py | Endpoints/* 5 组 | 30 个 REST 端点 + 静态托管 + SSE，响应字段与 .NET 版逐字一致 |
| keepalive.py | KeepAliveEngine/Restarter | 会话状态机、退避 {30,60,120,300,600}s、24h 重建、开机等待、心跳指标 |
| ctyun_api.py | CtYunApi.cs | 登录（挑战码+验证码 OCR+双 SHA256）、短信绑定、设备列表/连接、签名头 |
| wire.py | Encryption.cs + SendInfo.cs | REDQ 保活质询应答（RSA-OAEP/SHA1）、二进制报文解析 |
| wsclient.py | ClientWebSocket | 手写最小 RFC6455 客户端（wss/Origin/子协议/掩码/心跳） |
| jobs.py + cronx.py | JobService/CronExpression/CronScheduler | 任务 CRUD/历史/调度（20s tick）；cron 位图解析逐字移植 |
| scriptrunner.py | ScriptRunner/ProcessTree | 脚本运行（环境变量清单逐字一致）、超时、进程树回收 |
| redeem.py | RedeemService/Policy | 通道 A 兑换 + 三种调度策略 |
| envprobe.py / mutex.py / sessions.py / logs.py / store.py | 同名 C# | 环境自检/互斥/会话令牌/日志多播/原子配置存取 |

### 更新
- `Dockerfile` / `Dockerfile.slim`：Python 版镜像（完整版含 DrissionPage/ddddocr/requests + Chromium）
- `docker-compose.yml`：指向 Python 版构建
- `README.md`：全面重写（Python 版结构、部署、数据文件、安全说明）

### 未改动
- `CtYun/wwwroot/` 前端三件套：**零改动**，直接对接新后端
- `scripts/*.py` 任务脚本：零改动
- `CtYun/*.cs` 旧 .NET 源码：保留仅供参照（用户要求可随时删除）

## 验证结果（本机 Python 3.13 实测）

**API 全通过**：
- 登录（错误密码 200+success=false；正确密码发 token）、401 鉴权、改密后旧 token 全部吊销、登出失效
- 静态页 index.html/styles.css/app.js 200
- 账号列表（无密码泄漏，key/状态正确）、编辑账号
- 任务 CRUD + 校验（非法 cron/账号不存在/超时范围）+ nextRunAt 计算（"0 7 * * *" → 2026-09-05 07:00:00）
- 手动运行被环境自检拦截并给出修复命令（本机未装 DrissionPage/ddddocr）
- cron-preview（`*/10 * * * *` → 每 10 分钟 + 3 次触发时间）
- 设置 GET/PUT 持久化、兑换配置读写、monthly_days 策略判定（"今天是 5 号，不在每月兑换日 [1,15,-1] 中"）
- overview 联动（schedulerRunning=true——**.NET 版从未启动的调度器在 Python 版正常工作**）
- SSE：历史快照 + 实时推送（token 走查询参数）

**单元验证 8/8**：SendInfo 往返/build_msg 帧/多包一帧、REDQ 加密结构（132 字节、小端 AuthMechanism、密文<N）、cron DOM/DOW 组合语义、周日=7 归一化、边界值、非法表达式拒绝、严格大于。

## 过程中修复的 bug
1. store.py 尾部误留 `import logging_shim` → 启动即崩，已删
2. keepalive.py 重启调度循环缺 `import os` → 每 10s 刷异常日志，已修
3. socketserver 对客户端断连打堆栈 → 自定义 QuietThreadingHTTPServer 静默处理

## 遗留事项
- 积分余额仍恒为 -1（后端尚未接入真实积分接口，与 .NET 版一致）
- `ctyun_api` 对 desk.ctyun.cn 的实际登录链路无法在本机用真实账号验证（协议与加密已按 C# 逐字移植）
- 旧 .NET 源码（CtYun/*.cs）与两个 .NET Dockerfile（CtYun/Dockerfile*）已弃用，确认无用后可删除
