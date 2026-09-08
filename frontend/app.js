/* ==========================================================================
   CtYun KeepAlive 控制台 —— 前端逻辑（app.js）
   · 零框架 / 零依赖 / 无模块化（IIFE 包裹），可离线运行，file:// 亦可打开
   · 安全：所有服务端返回数据一律通过 textContent 写入，绝不拼接 innerHTML
   · 鉴权：所有 XHR 走 X-Auth-Token 请求头；SSE 因 EventSource 无法设头，走查询参数
   ========================================================================== */
(function () {
    'use strict';

    /* ======================================================================
       一、常量与全局状态
       ====================================================================== */

    var TOKEN_KEY = 'ctyun_token';
    var EXPIRES_KEY = 'ctyun_token_expires';

    /** 日志 DOM 中最多保留的行数，超出删除最旧的，防止内存泄漏。 */
    var LOG_MAX_LINES = 2000;
    /** 默认轮询间隔（秒），设置接口未返回时使用。 */
    var DEFAULT_POLL_SECONDS = 5;
    /** SSE 断线重连间隔与最大连续重试次数。 */
    var SSE_RETRY_MS = 3000;
    var SSE_MAX_RETRY = 5;
    /** Cron 预览输入防抖（毫秒）。 */
    var CRON_DEBOUNCE_MS = 350;

    /** 日志级别 → CSS 类映射：0=Info 1=Success 2=Warn 3=Error。 */
    var LOG_LEVEL_CLASS = ['log-info', 'log-success', 'log-warning', 'log-error'];

    /** 任务类型显示名。 */
    var JOB_TYPE_LABEL = { ai_chat: 'AI 对话', pc_hang: '云电脑挂机' };

    /** 云电脑状态中视为"在线"的关键字。 */
    var DESKTOP_ONLINE_KEYWORDS = ['保活', '运行', '就绪', '连接', '在线'];

    /**
     * 全局状态对象。所有字段均显式初始化，避免 undefined 传播。
     */
    var state = {
        token: '',              // 当前会话 token
        expiresAt: 0,           // 过期时间（Unix 秒）
        booted: false,          // 是否已初始化（防止重复 boot）
        activeTab: 'overview',

        accounts: [],           // GET /api/accounts
        jobs: [],               // GET /api/jobs
        history: [],            // GET /api/jobs/history
        tasksSummary: null,     // GET /api/tasks/summary
        tasksFilter: 'all',     // 平台任务筛选：all / todo / done
        platformTasks: {},      // 账号平台任务明细缓存：{user: {expanded, loading, ok, msg, tasks, at}}
        platformAll: null,      // 全部账号平台任务统计：{running, accounts, total, msg, at}
        updateInfo: null,       // GET /api/update/check 结果
        tasksAutofixTried: false, // 本次会话是否已自动补做过（防重复触发）
        overview: null,         // GET /api/overview
        settings: null,         // GET /api/settings
        redeemConfig: null,     // GET /api/redeem/config
        redeemPlan: null,       // GET /api/redeem/plan
        rewards: [],            // GET /api/redeem/rewards
        envCheck: null,         // GET /api/system/env-check

        pollTimer: null,
        pollSeconds: DEFAULT_POLL_SECONDS,
        expiryTimer: null,

        logStream: null,
        logPaused: false,
        logAutoScroll: true,
        logBuffer: [],          // 暂停期间缓存的日志
        logTotal: 0,
        sseRetry: 0,
        sseRetryTimer: null,
        cronTimer: null,

        pendingUser: '',        // 等待短信验证码的账号
        editingJobId: '',       // 正在编辑的任务 Id（空=新建）
        editingAccountKey: ''   // 正在编辑的账号 Key
    };

    /* ======================================================================
       二、通用工具：DOM 构建、格式化、XSS 防护
       ====================================================================== */

    /** 按 id 取元素。 */
    function $(id) {
        return document.getElementById(id);
    }

    /**
     * 创建元素。text 存在时通过 textContent 写入（天然免疫 XSS）。
     * @param {string} tag 标签名
     * @param {string} [className] class 字符串
     * @param {*} [text] 文本内容
     * @returns {HTMLElement}
     */
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) {
            node.className = className;
        }
        if (text !== undefined && text !== null && text !== '') {
            node.textContent = String(text);
        }
        return node;
    }

    /** 清空一个节点的所有子节点。 */
    function clearNode(node) {
        while (node.firstChild) {
            node.removeChild(node.firstChild);
        }
    }

    /**
     * 创建按钮并绑定点击回调（不使用内联 onclick，避免字符串拼接带来的注入面）。
     * @param {string} label 按钮文案
     * @param {string} className class 字符串
     * @param {Function} handler 点击回调
     * @returns {HTMLButtonElement}
     */
    function makeButton(label, className, handler) {
        var btn = el('button', className || 'btn-action', label);
        btn.type = 'button';
        if (handler) {
            btn.addEventListener('click', handler);
        }
        return btn;
    }

    /**
     * HTML 转义（XSS 兜底）。
     * 本项目所有服务端数据一律通过 textContent 写入；此函数仅用于
     * 少数必须以 HTML 字符串形式赋值的场景，转义后才允许写入。
     * @param {*} value 待转义的值
     * @returns {string}
     */
    function escapeHtml(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * 唯一允许写 innerHTML 的入口：入参必须先经 escapeHtml 转义。
     * @param {HTMLElement} node 目标节点
     * @param {*} unsafeText 原始（不可信）文本
     */
    function setEscapedHtml(node, unsafeText) {
        node.innerHTML = escapeHtml(unsafeText);
    }

    /** 数字补零到两位。 */
    function pad2(n) {
        return String(n).padStart(2, '0');
    }

    /** 数值转字符串，非法值回退为 fallback。 */
    function num(value, fallback) {
        var n = Number(value);
        if (isNaN(n)) {
            return String(fallback === undefined ? 0 : fallback);
        }
        return String(n);
    }

    /** 解析整数，非法值回退为 fallback。 */
    function parseIntOr(value, fallback) {
        var n = parseInt(value, 10);
        return isNaN(n) ? fallback : n;
    }

    /**
     * Unix 秒 → "YYYY-MM-DD HH:mm:ss"。0 / 空 / 非法 → "—"。
     * @param {number} unixSeconds Unix 秒
     * @returns {string}
     */
    function fmtDateTime(unixSeconds) {
        var v = Number(unixSeconds);
        if (!v || v <= 0) {
            return '—';
        }
        var d = new Date(v * 1000);
        if (isNaN(d.getTime())) {
            return '—';
        }
        return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
            ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
    }

    /** Date → "HH:mm:ss"。 */
    function fmtClock(date) {
        return pad2(date.getHours()) + ':' + pad2(date.getMinutes()) + ':' + pad2(date.getSeconds());
    }

    /**
     * 秒数 → "HH:MM:SS" 或 "N天 HH:MM:SS"。
     * @param {number} totalSeconds 总秒数
     * @returns {string}
     */
    function fmtDuration(totalSeconds) {
        var s = Math.max(0, Math.floor(Number(totalSeconds) || 0));
        var d = Math.floor(s / 86400);
        var h = Math.floor((s % 86400) / 3600);
        var m = Math.floor((s % 3600) / 60);
        var sec = s % 60;
        if (d > 0) {
            return d + '天 ' + pad2(h) + ':' + pad2(m) + ':' + pad2(sec);
        }
        return pad2(h) + ':' + pad2(m) + ':' + pad2(sec);
    }

    /**
     * 账号脱敏：11 位手机号保留前 3 后 4，其余按长度降级处理。
     * @param {string} user 原始账号
     * @returns {string}
     */
    function maskUser(user) {
        var s = String(user === null || user === undefined ? '' : user);
        if (!s) {
            return '—';
        }
        if (s.length >= 11) {
            return s.slice(0, 3) + '****' + s.slice(-4);
        }
        if (s.length >= 7) {
            return s.slice(0, 2) + '****' + s.slice(-2);
        }
        if (s.length >= 3) {
            return s.slice(0, 1) + '***' + s.slice(-1);
        }
        return '***';
    }

    /** 判断云电脑状态文本是否代表在线。 */
    function isDesktopOnline(status) {
        var s = String(status || '');
        for (var i = 0; i < DESKTOP_ONLINE_KEYWORDS.length; i++) {
            if (s.indexOf(DESKTOP_ONLINE_KEYWORDS[i]) >= 0) {
                return true;
            }
        }
        return false;
    }

    /** 构造徽章元素。 */
    function badge(text, kind) {
        return el('span', 'badge ' + (kind || 'muted'), text);
    }

    /** 构造"键-值"小格。 */
    function kvItem(key, value, valueClass) {
        var wrap = el('div', 'kv');
        wrap.appendChild(el('span', 'kv-k', key));
        wrap.appendChild(el('span', 'kv-v' + (valueClass ? ' ' + valueClass : ''), value));
        return wrap;
    }

    /** 构造指标卡。 */
    function metricCard(label, value, valueClass, sub) {
        var card = el('div', 'glass-card metric');
        card.appendChild(el('span', 'metric-label', label));
        card.appendChild(el('div', 'metric-value' + (valueClass ? ' ' + valueClass : ''), value));
        if (sub) {
            card.appendChild(el('span', 'metric-sub', sub));
        }
        return card;
    }

    /** 构造空态提示。 */
    function emptyState(message, isError) {
        return el('div', 'empty-state' + (isError ? ' error' : ''), message);
    }

    /** 事件委托：从事件目标向上查找匹配选择器的元素。 */
    function closestFrom(target, selector) {
        var node = target;
        while (node && node.nodeType === 1) {
            if (node.matches && node.matches(selector)) {
                return node;
            }
            node = node.parentNode;
        }
        return null;
    }

    /* ======================================================================
       三、鉴权与 HTTP 请求封装
       ====================================================================== */

    /** 从 localStorage 恢复会话。 */
    function loadSession() {
        state.token = localStorage.getItem(TOKEN_KEY) || '';
        var exp = localStorage.getItem(EXPIRES_KEY);
        state.expiresAt = exp ? Number(exp) : 0;
    }

    /** 保存会话（token + 过期时间）。 */
    function saveSession(token, expiresAt) {
        state.token = token || '';
        state.expiresAt = Number(expiresAt) || 0;
        localStorage.setItem(TOKEN_KEY, state.token);
        localStorage.setItem(EXPIRES_KEY, String(state.expiresAt));
    }

    /** 清除本地会话。 */
    function clearSession() {
        state.token = '';
        state.expiresAt = 0;
        localStorage.removeItem(TOKEN_KEY);
        localStorage.removeItem(EXPIRES_KEY);
    }

    /** 显示登录遮罩。 */
    function showLogin() {
        $('login-overlay').classList.remove('hidden');
        var pwd = $('admin-password');
        pwd.value = '';
        try {
            pwd.focus();
        } catch (e) {
            /* 忽略聚焦失败 */
        }
    }

    /** 隐藏登录遮罩。 */
    function hideLogin() {
        $('login-overlay').classList.add('hidden');
        $('login-error').textContent = '';
    }

    /**
     * 统一处理 401：清 token、停轮询、断 SSE、回登录页。
     */
    function handleUnauthorized() {
        clearSession();
        stopPolling();
        stopExpiryWatch();
        closeLogStream();
        closeAllModals();
        state.booted = false;
        resetPanels();
        showLogin();
    }

    /**
     * 带鉴权头的 fetch 封装。401 时统一登出。
     * @param {string} path 请求路径
     * @param {Object} [options] { method, body }
     * @returns {Promise<Response>}
     */
    async function request(path, options) {
        var opts = options || {};
        var headers = {};
        if (opts.headers) {
            for (var k in opts.headers) {
                if (Object.prototype.hasOwnProperty.call(opts.headers, k)) {
                    headers[k] = opts.headers[k];
                }
            }
        }
        headers['X-Auth-Token'] = state.token;
        if (opts.body !== undefined && opts.body !== null && !headers['Content-Type']) {
            headers['Content-Type'] = 'application/json';
        }

        var resp;
        try {
            resp = await fetch(path, {
                method: opts.method || 'GET',
                headers: headers,
                body: opts.body
            });
        } catch (err) {
            throw new Error('网络请求失败：' + err.message);
        }

        if (resp.status === 401) {
            handleUnauthorized();
            throw new Error('登录已失效，请重新登录');
        }
        return resp;
    }

    /**
     * 请求并解析 JSON。
     * @returns {Promise<any>} 解析后的对象 / 数组 / null
     */
    async function requestJson(path, options) {
        var resp = await request(path, options);
        var text = await resp.text();
        if (!resp.ok) {
            throw new Error('HTTP ' + resp.status + (text ? '：' + text.slice(0, 160) : ''));
        }
        if (!text) {
            return null;
        }
        try {
            return JSON.parse(text);
        } catch (e) {
            throw new Error('响应不是合法 JSON：' + text.slice(0, 120));
        }
    }

    /** POST JSON。 */
    function postJson(path, payload) {
        return requestJson(path, { method: 'POST', body: JSON.stringify(payload) });
    }

    /** PUT JSON。 */
    function putJson(path, payload) {
        return requestJson(path, { method: 'PUT', body: JSON.stringify(payload) });
    }

    /** DELETE。 */
    function delJson(path) {
        return requestJson(path, { method: 'DELETE' });
    }

    /** 显示 Toast。isError 为 true 时用红色。 */
    function notify(message, isError) {
        var toast = $('toast-notify');
        // 服务端返回的消息同样视为不可信文本，转义后再写入
        setEscapedHtml(toast, message);
        toast.style.background = isError ? 'rgba(239, 68, 68, 0.92)' : 'rgba(139, 92, 246, 0.92)';
        toast.classList.add('show');
        setTimeout(function () {
            toast.classList.remove('show');
        }, 3200);
    }

    /** 登录。 */
    async function doLogin() {
        var input = $('admin-password');
        var err = $('login-error');
        var btn = $('btn-login');
        var password = input.value;

        if (!password) {
            err.textContent = '请输入访问密码';
            input.focus();
            return;
        }

        btn.disabled = true;
        btn.textContent = '登录中...';
        err.textContent = '';

        try {
            var res = await postJson('/api/login', { password: password });
            if (res && res.success) {
                saveSession(res.token, res.expiresAt);
                input.value = '';
                hideLogin();
                notify('登录成功');
                boot();
            } else {
                err.textContent = (res && res.msg) ? res.msg : '密码错误';
            }
        } catch (e) {
            err.textContent = e.message;
        } finally {
            btn.disabled = false;
            btn.textContent = '进入系统';
        }
    }

    /** 退出登录。 */
    async function doLogout() {
        if (!window.confirm('确定要退出登录吗？')) {
            return;
        }
        try {
            if (state.token) {
                await postJson('/api/logout', {});
            }
        } catch (e) {
            // 登出失败也强制清理本地会话
        }
        clearSession();
        stopPolling();
        stopExpiryWatch();
        closeLogStream();
        closeAllModals();
        state.booted = false;
        resetPanels();
        showLogin();
        notify('已退出登录');
    }

    /** 启动登录过期看门狗：到点自动登出。 */
    function startExpiryWatch() {
        stopExpiryWatch();
        state.expiryTimer = setInterval(function () {
            if (!state.token) {
                return;
            }
            if (state.expiresAt > 0 && Date.now() / 1000 >= state.expiresAt) {
                notify('登录已过期，请重新登录', true);
                handleUnauthorized();
            }
        }, 5000);
    }

    function stopExpiryWatch() {
        if (state.expiryTimer) {
            clearInterval(state.expiryTimer);
            state.expiryTimer = null;
        }
    }

    /** 提交修改密码。 */
    async function submitChangePassword() {
        var oldPassword = $('old-password').value;
        var newPassword = $('new-password').value;
        var confirmNew = $('confirm-new-password').value;
        var btn = $('btn-password-submit');

        if (!oldPassword || !newPassword || !confirmNew) {
            notify('三个密码字段均不能为空', true);
            return;
        }
        if (newPassword !== confirmNew) {
            notify('两次输入的新密码不一致', true);
            return;
        }

        btn.disabled = true;
        btn.textContent = '提交中...';
        try {
            var res = await postJson('/api/change-password', {
                oldPassword: oldPassword,
                newPassword: newPassword
            });
            if (res && res.success) {
                notify('密码修改成功，请使用新密码重新登录');
                closeModal('modal-password');
                clearSession();
                stopPolling();
                stopExpiryWatch();
                closeLogStream();
                state.booted = false;
                resetPanels();
                showLogin();
            } else {
                notify((res && (res.msg || res.message)) || '原密码错误或修改失败', true);
            }
        } catch (e) {
            notify('修改失败：' + e.message, true);
        } finally {
            btn.disabled = false;
            btn.textContent = '确认修改';
            $('old-password').value = '';
            $('new-password').value = '';
            $('confirm-new-password').value = '';
        }
    }

    /* ======================================================================
       四、Tab 切换与轮询
       ====================================================================== */

    /** 各 Tab 对应的数据加载函数（函数声明提升，可在此处引用）。 */
    var TAB_LOADERS = {
        overview: loadOverview,
        accounts: loadAccounts,
        jobs: loadJobs,
        redeem: loadRedeemTab,
        logs: function () {
            return Promise.resolve();
        },
        settings: loadSettingsTab
    };

    /**
     * 切换 Tab：更新按钮/面板状态，并立即拉取该 Tab 数据。
     * @param {string} tab Tab 标识
     */
    function switchTab(tab) {
        state.activeTab = tab;

        var btns = document.querySelectorAll('#tab-bar .tab-btn');
        for (var i = 0; i < btns.length; i++) {
            if (btns[i].getAttribute('data-tab') === tab) {
                btns[i].classList.add('active');
            } else {
                btns[i].classList.remove('active');
            }
        }

        var panels = document.querySelectorAll('.tab-panel');
        for (var j = 0; j < panels.length; j++) {
            if (panels[j].id === 'panel-' + tab) {
                panels[j].classList.add('active');
            } else {
                panels[j].classList.remove('active');
            }
        }

        return refreshCurrentTab(true);
    }

    /** 刷新当前 Tab 数据。 */
    function refreshCurrentTab(toastOnError) {
        var loader = TAB_LOADERS[state.activeTab];
        if (!loader) {
            return Promise.resolve();
        }
        return Promise.resolve()
            .then(loader)
            .catch(function (err) {
                if (toastOnError) {
                    notify('加载失败：' + err.message, true);
                }
                console.error('[CtYun] 加载 ' + state.activeTab + ' 数据失败：', err);
            });
    }

    /** 启动定时轮询（仅总览 / 账号需要按 pollIntervalSeconds 轮询）。 */
    function startPolling() {
        stopPolling();
        var seconds = state.pollSeconds > 0 ? state.pollSeconds : DEFAULT_POLL_SECONDS;
        state.pollTimer = setInterval(function () {
            if (!state.token) {
                return;
            }
            if (state.activeTab === 'overview') {
                loadOverview().catch(function () { /* 静默 */ });
            } else if (state.activeTab === 'accounts') {
                loadAccounts().catch(function () { /* 静默 */ });
            }
        }, seconds * 1000);
    }

    function stopPolling() {
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
    }

    /** 应用启动：起看门狗、连 SSE、载入设置后按间隔轮询并渲染当前 Tab。 */
    function boot() {
        if (state.booted) {
            return;
        }
        state.booted = true;
        startExpiryWatch();
        startLogStream();
        // 先取设置（决定轮询间隔）；失败时 401 已在 request 层处理，其余错误用默认间隔兜底
        loadSettings().catch(function (err) {
            console.warn('[CtYun] 载入设置失败，使用默认轮询间隔', err);
        }).then(function () {
            startPolling();
            switchTab(state.activeTab || 'overview');
        });
    }

    /** 登出 / 401 后清空所有面板内容，避免残留上一次会话的数据。 */
    function resetPanels() {
        state.accounts = [];
        state.jobs = [];
        state.history = [];
        state.overview = null;
        state.settings = null;
        state.redeemConfig = null;
        state.redeemPlan = null;
        state.rewards = [];
        state.envCheck = null;
        state.tasksSummary = null;
        state.tasksAutofixTried = false;

        clearNode($('ov-metrics'));
        clearNode($('ov-schedule'));
        clearNode($('accounts-list'));
        clearNode($('jobs-list'));
        clearNode($('jobs-history-body'));
        clearNode($('tasks-summary-body'));
        clearNode($('redeem-plan'));
        clearNode($('settings-readonly'));
        clearNode($('env-check-summary'));
        clearNode($('env-check-list'));
        $('ov-last-update').textContent = '尚未加载';
        resetConsole();
    }

    /* ======================================================================
       五、总览 Tab
       ====================================================================== */

    /** 拉取总览指标。 */
    async function loadOverview() {
        var data = await requestJson('/api/overview');
        state.overview = data || {};
        renderOverview();
    }

    /** 渲染总览：指标卡 + 调度信息。 */
    function renderOverview() {
        var data = state.overview || {};

        // --- 指标卡 ---
        var metrics = $('ov-metrics');
        clearNode(metrics);
        metrics.appendChild(metricCard('账号总数', num(data.accountTotal)));
        metrics.appendChild(metricCard('运行中', num(data.accountRunning), 'ok', '共 ' + num(data.accountTotal) + ' 个账号'));
        metrics.appendChild(metricCard('保活总运行时长', fmtDuration(data.keepAliveUptimeSeconds), 'accent', '所有账号累计'));

        var rate = Number(data.keepAliveSuccessRate);
        var rateOk = !isNaN(rate) && rate >= 0;
        metrics.appendChild(metricCard(
            '保活成功率',
            rateOk ? rate.toFixed(1) + '%' : '暂无数据',
            rateOk ? (rate >= 95 ? 'ok' : (rate >= 80 ? 'warn' : 'err')) : 'muted',
            rateOk ? '全部账号心跳成功 / (成功+失败)' : '心跳尚未产生数据'
        ));

        metrics.appendChild(metricCard('今日任务成功', num(data.todayJobSuccess), 'ok'));
        metrics.appendChild(metricCard('今日任务失败', num(data.todayJobFailed), 'err'));

        var points = Number(data.pointsBalance);
        var pointsUnavailable = isNaN(points) || points < 0;
        metrics.appendChild(metricCard(
            '总积分余额',
            pointsUnavailable ? '不可用' : num(points),
            pointsUnavailable ? 'muted' : 'warn',
            pointsUnavailable ? '尚未查询积分，请到「账号」页点「刷新积分」' : ''
        ));

        // --- 调度信息 ---
        var schedule = $('ov-schedule');
        clearNode(schedule);
        var nextAt = Number(data.nextJobAt) || 0;
        schedule.appendChild(kvItem('下次任务时间', nextAt > 0 ? fmtDateTime(nextAt) : '暂无', nextAt > 0 ? '' : 'muted'));
        schedule.appendChild(kvItem('下次任务名称', data.nextJobName || '—', data.nextJobName ? '' : 'muted'));
        schedule.appendChild(kvItem('调度器状态', data.schedulerRunning ? '运行中' : '已停止', data.schedulerRunning ? 'ok' : 'err'));
        schedule.appendChild(kvItem('环境自检', data.envOk ? '全部通过' : '存在异常', data.envOk ? 'ok' : 'err'));

        $('ov-last-update').textContent = '最后更新：' + fmtClock(new Date());
    }

    /* ======================================================================
       六、账号 Tab
       ====================================================================== */

    /** 拉取账号列表。 */
    async function loadAccounts() {
        var data = await requestJson('/api/accounts');
        state.accounts = Array.isArray(data) ? data : [];
        // 进入账号页时若有账号从未查过积分，自动触发一次后台查询（5 分钟节流）
        var need = state.accounts.some(function (a) { return !a.pointsCheckedAt; });
        if (need && (!state._pointsAutoAt || Date.now() - state._pointsAutoAt > 300000)) {
            state._pointsAutoAt = Date.now();
            postJson('/api/accounts/refresh-points', {}).catch(function () { /* 静默 */ });
        }
        renderAccounts();
    }

    /** 渲染账号卡片列表。 */
    function renderAccounts() {
        var container = $('accounts-list');
        clearNode(container);

        if (state.accounts.length === 0) {
            container.appendChild(emptyState('暂无已配置账号，点击右上角「＋ 新增账号」添加并启动保活。'));
            return;
        }

        state.accounts.forEach(function (acc) {
            container.appendChild(buildAccountCard(acc));
        });
    }

    /**
     * 构建单个账号卡片。
     * @param {Object} acc AccountStatusDto
     * @returns {HTMLElement}
     */
    function buildAccountCard(acc) {
        var card = el('div', 'glass-card account-card');

        // --- 头部：名称 / 脱敏账号 / 状态徽章 ---
        var header = el('div', 'account-header');
        var nameGroup = el('div', 'account-name-group');
        nameGroup.appendChild(el('span', 'account-name', acc.name || '未命名'));
        nameGroup.appendChild(el('span', 'account-user', maskUser(acc.user)));
        header.appendChild(nameGroup);

        var isRunning = !!acc.isRunning;
        var statusText = acc.statusText || (isRunning ? '保活运行中' : '已停止');
        var dotClass = 'inactive';
        if (statusText === '等待验证码') {
            dotClass = 'pending';
        } else if (isRunning) {
            dotClass = 'active';
        }
        var badgeKind = dotClass === 'active' ? 'ok' : (dotClass === 'pending' ? 'warn' : 'muted');

        var statusBadge = el('div', 'badge ' + badgeKind);
        statusBadge.appendChild(el('span', 'status-dot ' + dotClass));
        statusBadge.appendChild(el('span', null, statusText));
        header.appendChild(statusBadge);
        card.appendChild(header);

        // --- 云电脑列表 ---
        var desktops = Array.isArray(acc.desktops) ? acc.desktops : [];
        if (desktops.length > 0) {
            var dl = el('div', 'desktop-list');
            desktops.forEach(function (d) {
                var item = el('div', 'desktop-item');
                var label = '🖥️ ' + (d.name || '未命名') + ' (' + (d.code || '-') + ')';
                if (d.desktopId) {
                    label += ' · ID ' + d.desktopId;
                }
                item.appendChild(el('span', null, label));
                item.appendChild(el('span', 'desktop-status ' + (isDesktopOnline(d.status) ? 'online' : 'offline'), d.status || '未知'));
                dl.appendChild(item);
            });
            card.appendChild(dl);
        } else {
            card.appendChild(el('div', 'env-detail', '未获取到可用云电脑设备'));
        }

        // --- 运行指标 ---
        var m = acc.metrics || {};
        var kv = el('div', 'kv-grid');
        kv.appendChild(kvItem('运行时长', fmtDuration(m.uptimeSeconds)));
        kv.appendChild(kvItem('心跳成功', num(m.heartbeatSuccess), 'ok'));
        kv.appendChild(kvItem('心跳失败', num(m.heartbeatFailed), Number(m.heartbeatFailed) > 0 ? 'err' : ''));
        var hbOk = Number(m.heartbeatSuccess) || 0;
        var hbBad = Number(m.heartbeatFailed) || 0;
        if (hbOk + hbBad > 0) {
            var rateVal = hbOk * 100.0 / (hbOk + hbBad);
            kv.appendChild(kvItem('保活成功率', rateVal.toFixed(1) + '%',
                rateVal >= 95 ? 'ok' : (rateVal >= 80 ? 'warn' : 'err')));
        }
        kv.appendChild(kvItem('连续失败', num(m.consecutiveFailures), Number(m.consecutiveFailures) > 0 ? 'warn' : ''));
        kv.appendChild(kvItem('下次重启', fmtDateTime(m.nextRestartAt), m.nextRestartAt ? '' : 'muted'));

        // --- 通用积分（缓存，点「刷新积分」更新） ---
        var pointsText;
        var pointsKind = 'warn';
        if (acc.pointsError) {
            pointsText = '查询失败';
            pointsKind = 'err';
        } else if (acc.pointsCheckedAt && acc.points !== null && acc.points !== undefined) {
            pointsText = num(acc.points);
        } else if (acc.pointsCheckedAt) {
            pointsText = '未读取到';
            pointsKind = 'muted';
        } else {
            pointsText = '未查询';
            pointsKind = 'muted';
        }
        kv.appendChild(kvItem('通用积分', pointsText, pointsKind));
        if (acc.pointsCheckedAt) {
            kv.appendChild(kvItem('积分查询时间', fmtDateTime(acc.pointsCheckedAt), 'muted'));
        }

        kv.appendChild(kvItem('最近错误', m.lastError || '无', m.lastError ? 'err' : 'muted'));
        card.appendChild(kv);

        // --- 操作按钮 ---
        var key = acc.key || acc.user || acc.name || '';
        var actions = el('div', 'account-actions');
        if (isRunning) {
            actions.appendChild(makeButton('🔴 停止保活', 'btn-action', function () {
                stopAccount(key, acc.name);
            }));
        } else if (statusText === '等待验证码') {
            actions.appendChild(makeButton('📩 输入验证码', 'btn-action', function () {
                openSmsStep(acc.user || key);
            }));
        } else {
            actions.appendChild(makeButton('⚡ 启动保活', 'btn-action', function () {
                startAccount(key, acc.name);
            }));
        }
        actions.appendChild(makeButton('✏️ 编辑', 'btn-action', function () {
            openEditAccountModal(acc);
        }));
        actions.appendChild(makeButton('🗑️ 删除', 'btn-action btn-delete', function () {
            deleteAccount(key, acc.name);
        }));
        card.appendChild(actions);

        return card;
    }

    /** 启动账号保活。 */
    async function startAccount(key, displayName) {
        try {
            var res = await postJson('/api/accounts/start', { name: key });
            if (res && res.success) {
                notify('账号 [' + (displayName || key) + '] 保活已启动');
                await loadAccounts();
            } else {
                notify((res && (res.msg || res.message)) || '启动失败', true);
            }
        } catch (e) {
            notify('启动失败：' + e.message, true);
        }
    }

    /** 停止账号保活。 */
    async function stopAccount(key, displayName) {
        try {
            var res = await postJson('/api/accounts/stop', { name: key });
            if (res && res.success) {
                notify('账号 [' + (displayName || key) + '] 保活已停止');
                await loadAccounts();
            } else {
                notify((res && (res.msg || res.message)) || '停止失败', true);
            }
        } catch (e) {
            notify('停止失败：' + e.message, true);
        }
    }

    /** 删除账号。 */
    async function deleteAccount(key, displayName) {
        if (!window.confirm('确认删除账号 [' + (displayName || key) + '] 吗？\n这会停止其保活并从配置文件中移除。')) {
            return;
        }
        try {
            var res = await delJson('/api/accounts/' + encodeURIComponent(key));
            if (res && res.success) {
                notify('账号 [' + (displayName || key) + '] 已删除');
                await loadAccounts();
            } else {
                notify((res && (res.msg || res.message)) || '删除失败', true);
            }
        } catch (e) {
            notify('删除失败：' + e.message, true);
        }
    }

    /** 打开编辑账号弹窗。 */
    function openEditAccountModal(acc) {
        state.editingAccountKey = acc.key || acc.user || '';
        $('me-key').textContent = state.editingAccountKey || '—';
        $('me-name').value = acc.name || '';
        $('me-password').value = '';
        openModal('modal-account-edit');
    }

    /** 提交编辑账号（name / password 留空表示不修改）。 */
    async function submitEditAccount() {
        var key = state.editingAccountKey;
        if (!key) {
            notify('账号主键缺失，无法保存', true);
            return;
        }
        var btn = $('btn-me-submit');
        btn.disabled = true;
        btn.textContent = '保存中...';
        try {
            var res = await putJson('/api/accounts', {
                key: key,
                name: $('me-name').value.trim(),
                password: $('me-password').value
            });
            if (res && res.success) {
                notify((res.msg || res.message) || '修改已保存');
                closeModal('modal-account-edit');
                await loadAccounts();
            } else {
                notify((res && (res.msg || res.message)) || '保存失败', true);
            }
        } catch (e) {
            notify('保存失败：' + e.message, true);
        } finally {
            btn.disabled = false;
            btn.textContent = '保存修改';
        }
    }

    /** 打开新增账号弹窗（第一步）。 */
    function openAddAccountModal() {
        resetAccountForm();
        openModal('modal-account');
    }

    /** 重置新增账号表单到第一步。 */
    function resetAccountForm() {
        $('ma-name').value = '';
        $('ma-user').value = '';
        $('ma-password').value = '';
        $('ma-deviceCode').value = '';
        $('ma-sms-code').value = '';
        $('ma-sms-user').textContent = '—';
        state.pendingUser = '';
        $('ma-step1').style.display = 'block';
        $('ma-step2').style.display = 'none';
    }

    /**
     * 切到短信验证码第二步。
     * @param {string} user 待验证的账号（手机号）
     */
    function openSmsStep(user) {
        state.pendingUser = user || '';
        $('ma-sms-user').textContent = user || '—';
        $('ma-sms-code').value = '';
        $('ma-step1').style.display = 'none';
        $('ma-step2').style.display = 'block';
        openModal('modal-account');
    }

    /** 提交新增账号（第一步：账号密码登录）。 */
    async function submitNewAccount() {
        var name = $('ma-name').value.trim();
        var user = $('ma-user').value.trim();
        var password = $('ma-password').value;
        var deviceCode = $('ma-deviceCode').value.trim();

        if (!user || !password) {
            notify('手机号和密码不能为空', true);
            return;
        }

        var btn = $('btn-ma-submit');
        btn.disabled = true;
        btn.textContent = '登录中...';
        try {
            var res = await postJson('/api/accounts', {
                name: name,
                user: user,
                password: password,
                deviceCode: deviceCode
            });
            if (res && res.status === 'Success') {
                notify((res.message || res.msg) || '账号添加成功，保活已启动');
                resetAccountForm();
                closeModal('modal-account');
                await loadAccounts();
            } else if (res && res.status === 'NeedSMS') {
                notify('设备未绑定，请输入短信验证码');
                openSmsStep(user);
                await loadAccounts();
            } else {
                notify((res && (res.message || res.msg)) || '添加失败', true);
            }
        } catch (e) {
            notify('添加失败：' + e.message, true);
        } finally {
            btn.disabled = false;
            btn.textContent = '立即登录并添加';
        }
    }

    /** 提交短信验证码（第二步：绑定设备）。 */
    async function submitSmsCode() {
        var code = $('ma-sms-code').value.trim();
        if (!code) {
            notify('请输入短信验证码', true);
            return;
        }
        if (!state.pendingUser) {
            notify('待验证账号已丢失，请重新添加账号', true);
            return;
        }

        var btn = $('btn-ma-sms-submit');
        btn.disabled = true;
        btn.textContent = '验证中...';
        try {
            var res = await postJson('/api/accounts/verify', {
                user: state.pendingUser,
                code: code
            });
            if (res && res.status === 'Success') {
                notify((res.message || res.msg) || '绑定成功，保活已启动');
                resetAccountForm();
                closeModal('modal-account');
                await loadAccounts();
            } else {
                notify((res && (res.message || res.msg)) || '绑定失败，验证码可能错误或已失效', true);
            }
        } catch (e) {
            notify('验证失败：' + e.message, true);
        } finally {
            btn.disabled = false;
            btn.textContent = '验证并绑定设备';
        }
    }

    /* ======================================================================
       七、定时任务 Tab
       ====================================================================== */

    /** 拉取任务列表 + 执行历史 + 账号（账号用于任务关联下拉）。 */
    async function loadJobs() {
        var results = await Promise.all([
            requestJson('/api/jobs'),
            requestJson('/api/jobs/history'),
            requestJson('/api/accounts'),
            requestJson('/api/tasks/summary').catch(function () { return null; })
        ]);
        state.jobs = Array.isArray(results[0]) ? results[0] : [];
        state.history = Array.isArray(results[1]) ? results[1] : [];
        state.accounts = Array.isArray(results[2]) ? results[2] : [];
        state.tasksSummary = results[3] || null;
        renderJobs();
        renderHistory();
        renderTasksSummary();
        maybeAutoFix();
    }

    /** 仅拉取平台任务完成情况。 */
    async function loadTasksSummary() {
        var data = await requestJson('/api/tasks/summary');
        state.tasksSummary = data || null;
        renderTasksSummary();
    }

    /** 自动补做开关（持久化到 localStorage，默认开启）。 */
    function autofixEnabled() {
        try {
            return localStorage.getItem('ctyun.tasksAutofix') !== '0';
        } catch (e) {
            return true;
        }
    }

    /** 汇总里是否存在今日未完成的 AI 对话任务。 */
    function hasMissingAiChat() {
        var s = state.tasksSummary;
        if (!s || !Array.isArray(s.accounts)) {
            return false;
        }
        return s.accounts.some(function (acc) {
            return Array.isArray(acc.aiChatMissing) && acc.aiChatMissing.length > 0;
        });
    }

    /** 汇总加载后：若开启自动补做且检测到未完成的 AI 对话，则触发一次。 */
    function maybeAutoFix() {
        if (state.tasksAutofixTried || !autofixEnabled() || !hasMissingAiChat()) {
            return;
        }
        state.tasksAutofixTried = true;
        runMissingAiChat(true);
    }

    /**
     * 触发补做今日未完成的 AI 对话任务。
     * @param {boolean} silent 自动触发时静默，仅在有实际触发时提示
     */
    async function runMissingAiChat(silent) {
        try {
            var res = await postJson('/api/tasks/run-missing', {});
            var triggered = (res && res.triggered) || [];
            var skipped = (res && res.skipped) || [];
            if (triggered.length > 0) {
                notify('已触发补做：' + triggered.join('、') +
                    (skipped.length > 0 ? '（跳过 ' + skipped.length + ' 个）' : ''));
            } else if (!silent) {
                notify(skipped.length > 0
                    ? '没有可补做的任务：' + skipped.map(function (s) { return s.job + '（' + s.reason + '）'; }).join('、')
                    : '所有账号的 AI 对话任务今日均已完成 ✓');
            }
            // 延迟刷新：让新触发的任务先落到汇总里
            setTimeout(function () {
                loadTasksSummary().catch(function () { /* ignore */ });
            }, 2500);
        } catch (e) {
            if (!silent) {
                notify('补做失败：' + e.message, true);
            }
        }
    }

    /** 更新平台任务面板顶部的完成情况统计文案。 */
    function updateTasksStats(stat) {
        var node = $('tasks-summary-stats');
        if (!node) {
            return;
        }
        if (!stat) {
            node.textContent = '';
            return;
        }
        var txt = '今日：已完成 ' + stat.done + ' · 未完成 ' + stat.todo +
            (stat.running > 0 ? ' · 运行中 ' + stat.running : '');
        var all = state.platformAll;
        if (all) {
            if (all.running) {
                txt += ' ｜ 平台任务：统计中…';
            } else if (Array.isArray(all.accounts) && all.accounts.length > 0) {
                var t = all.total || { done: 0, doing: 0, todo: 0 };
                txt += ' ｜ 平台任务：已完成 ' + t.done + ' · 进行中 ' + t.doing +
                    ' · 未完成 ' + t.todo;
            }
        }
        node.textContent = txt;
    }

    /**
     * 展开/收起某账号的平台任务明细。首次展开自动拉取；数据超过 5 分钟自动刷新。
     * @param {string} user 账号
     */
    function togglePlatformTasks(user) {
        var st = state.platformTasks[user];
        if (!st) {
            loadPlatformTasks(user);
            return;
        }
        st.expanded = !st.expanded;
        if (st.expanded && !st.loading && st.at && Date.now() - st.at > 5 * 60 * 1000) {
            loadPlatformTasks(user); // 数据过期，重新拉取
            return;
        }
        renderTasksSummary();
    }

    /** 实时登录平台拉取指定账号的全部积分任务完成情况。 */
    async function loadPlatformTasks(user) {
        state.platformTasks[user] = { expanded: true, loading: true, ok: false, msg: '', tasks: [] };
        renderTasksSummary();
        try {
            var res = await requestJson('/api/platform/tasks?user=' + encodeURIComponent(user));
            state.platformTasks[user] = {
                expanded: true,
                loading: false,
                ok: !!res.success,
                msg: res.msg || '',
                tasks: Array.isArray(res.tasks) ? res.tasks : [],
                at: Date.now()
            };
        } catch (e) {
            state.platformTasks[user] = {
                expanded: true, loading: false, ok: false,
                msg: e.message, tasks: []
            };
        }
        renderTasksSummary();
    }

    /** 渲染平台任务完成情况表。 */
    function renderTasksSummary() {
        var body = $('tasks-summary-body');
        if (!body) {
            return;
        }
        clearNode(body);
        var s = state.tasksSummary;
        if (!s || !Array.isArray(s.accounts)) {
            body.appendChild(el('tr', '', '')).appendChild(
                Object.assign(document.createElement('td'), { colSpan: 7 }));
            body.lastChild.lastChild.appendChild(emptyState('暂无数据'));
            updateTasksStats(null);
            return;
        }
        // 统计：已完成 / 未完成 / 运行中（按任务粒度，仅统计启用中的任务）
        var stat = { done: 0, todo: 0, running: 0 };
        s.accounts.forEach(function (acc) {
            (acc.tasks || []).forEach(function (t) {
                if (t.running) {
                    stat.running += 1;
                } else if (t.enabled && t.todaySuccess) {
                    stat.done += 1;
                } else if (t.enabled) {
                    stat.todo += 1;
                }
            });
        });
        updateTasksStats(stat);
        var filter = state.tasksFilter || 'all';
        var hasAny = false;
        s.accounts.forEach(function (acc) {
            var visible = (acc.tasks || []).filter(function (t) {
                if (filter === 'done') {
                    return !!t.todaySuccess;
                }
                if (filter === 'todo') {
                    return !t.todaySuccess;
                }
                return true;
            });
            var pst = state.platformTasks[acc.accountUser];
            var detailOpen = !!(pst && pst.expanded);
            if (visible.length === 0 && !detailOpen) {
                return;
            }
            hasAny = true;
            // 账号列：同账号首行显示，并附带平台任务明细开关
            var tdAcc = el('td', '', escapeHtml(acc.accountUser));
            tdAcc.rowSpan = Math.max(1, visible.length) + (detailOpen ? 1 : 0);
            var btnPt = el('button', 'btn-header', detailOpen ? '平台任务 ▴' : '平台任务 ▾');
            btnPt.style.cssText = 'font-size:12px;padding:2px 8px;margin-top:4px;';
            btnPt.addEventListener('click', function () {
                togglePlatformTasks(acc.accountUser);
            });
            tdAcc.appendChild(document.createElement('br'));
            tdAcc.appendChild(btnPt);
            // 平台任务统计（全量拉取后显示，无需展开）
            var pAll = state.platformAll;
            if (pAll) {
                var ps = platformStatOf(acc.accountUser);
                var line = el('div', 'section-subtitle', '');
                line.style.cssText = 'margin-top:4px;';
                if (pAll.running && !ps) {
                    line.textContent = '平台任务：统计中…';
                } else if (!ps) {
                    line.textContent = '';
                } else if (!ps.ok) {
                    line.textContent = '平台任务：' + (ps.msg || '拉取失败');
                } else {
                    var d = 0, g = 0, w = 0;
                    (ps.tasks || []).forEach(function (pt) {
                        if (pt.done === true) {
                            d += 1;
                        } else if (pt.progress != null && pt.progress > 0) {
                            g += 1;
                        } else {
                            w += 1;
                        }
                    });
                    line.textContent = '平台任务 ' + (ps.tasks || []).length + ' 项：✓' + d +
                        ' · 进行中 ' + g + ' · 未完成 ' + w;
                }
                if (line.textContent) {
                    tdAcc.appendChild(line);
                }
            }
            visible.forEach(function (t, idx) {
                var tr = el('tr', '');
                if (idx === 0) {
                    tr.appendChild(tdAcc);
                }
                tr.appendChild(el('td', '', t.jobName || '未命名任务'));
                tr.appendChild(el('td', '', JOB_TYPE_LABEL[t.jobType] || t.jobType || '未知'));
                // 今日状态徽章
                var tdStatus = el('td', '');
                var kind, text;
                if (t.running) {
                    kind = 'warn'; text = '运行中';
                } else if (!t.enabled) {
                    kind = 'muted'; text = '已停用';
                } else if (t.todaySuccess) {
                    kind = 'ok'; text = '今日已完成';
                } else if (t.todayRuns > 0) {
                    kind = 'err'; text = '今日失败 ×' + t.todayRuns;
                } else {
                    kind = 'err'; text = '今日未执行';
                }
                tdStatus.appendChild(badge(text, kind));
                tr.appendChild(tdStatus);
                // 今日最近一次
                var tdLast = el('td', '', t.todayRuns > 0
                    ? (t.todayLastAt || '') + ' ' + (t.todayLastSummary || '')
                    : '—');
                tdLast.title = t.todayLastSummary || '';
                tr.appendChild(tdLast);
                tr.appendChild(el('td', '', t.lastRunAt || '—'));
                tr.appendChild(el('td', '', t.nextRunAt || '—'));
                body.appendChild(tr);
            });
            if (visible.length === 0) {
                // 无任务但明细展开：占一行避免账号列悬空
                var tdNone = el('td', '', '');
                tdNone.colSpan = 6;
                tdNone.appendChild(emptyState('该账号暂无配置的定时任务'));
                var trNone = el('tr', '');
                trNone.appendChild(tdNone);
                body.appendChild(trNone);
            }
            // 平台任务明细行（实时登录平台拉取的全部积分任务）
            if (detailOpen) {
                var trD = el('tr', '');
                var tdD = el('td', '');
                tdD.colSpan = 6;
                if (pst.loading) {
                    tdD.appendChild(emptyState('正在登录平台并拉取任务列表，约需几秒…'));
                } else if (!pst.ok) {
                    tdD.appendChild(emptyState('拉取失败：' + (pst.msg || '未知错误'), true));
                } else if (pst.tasks.length === 0) {
                    tdD.appendChild(emptyState('平台未返回任何任务'));
                } else {
                    pst.tasks.forEach(function (pt) {
                        var line = el('div', '', '');
                        line.style.cssText = 'display:flex;align-items:center;gap:10px;padding:2px 0;';
                        var nm = el('span', '', pt.name || '未命名');
                        nm.style.cssText = 'min-width:180px;';
                        line.appendChild(nm);
                        var prog = (pt.progress != null ? pt.progress : '?') +
                            (pt.limit ? '/' + pt.limit : '');
                        line.appendChild(el('span', 'section-subtitle',
                            '进度 ' + prog + (pt.reward ? ' · ' + pt.reward + ' 积分' : '')));
                        var k2, t2;
                        if (pt.done === true) {
                            k2 = 'ok'; t2 = '已完成';
                        } else if (pt.progress != null && pt.progress > 0) {
                            k2 = 'warn'; t2 = '进行中'; // 有进度但未达标
                        } else {
                            k2 = 'err'; t2 = '未完成';
                        }
                        line.appendChild(badge(t2, k2));
                        tdD.appendChild(line);
                    });
                }
                trD.appendChild(tdD);
                body.appendChild(trD);
            }
        });
        if (!hasAny) {
            var tr = body.appendChild(el('tr', '', ''));
            var td = tr.appendChild(el('td', '', ''));
            td.colSpan = 7;
            td.appendChild(emptyState(
                filter === 'done' ? '当前筛选「已完成」下没有任务。'
                    : filter === 'todo' ? '当前筛选「未完成」下没有任务，全部完成 ✓'
                    : '还没有任务。在上方创建 AI 对话或云电脑挂机任务后，这里会显示每个账号的完成情况。'));
        }
    }

    /** 仅拉取执行历史。 */
    async function loadHistory() {
        var data = await requestJson('/api/jobs/history');
        state.history = Array.isArray(data) ? data : [];
        renderHistory();
    }

    /** 渲染任务卡片列表。 */
    function renderJobs() {
        var container = $('jobs-list');
        clearNode(container);

        if (state.jobs.length === 0) {
            container.appendChild(emptyState('暂无定时任务，点击「＋ 新建任务」创建 AI 对话或云电脑挂机任务。'));
            return;
        }

        state.jobs.forEach(function (job) {
            container.appendChild(buildJobCard(job));
        });
    }

    /**
     * 构建单个任务卡片。
     * @param {Object} job ScheduledJob
     * @returns {HTMLElement}
     */
    function buildJobCard(job) {
        var card = el('div', 'glass-card job-card');

        // --- 头部：名称 + 标签 ---
        var header = el('div', 'job-header');
        var leftGroup = el('div', 'account-name-group');
        leftGroup.appendChild(el('span', 'account-name', job.name || '未命名任务'));
        var tags = el('div', 'job-tags');
        tags.appendChild(badge(JOB_TYPE_LABEL[job.type] || job.type || '未知类型', job.type === 'pc_hang' ? 'info' : 'muted'));
        tags.appendChild(badge(job.enabled ? '已启用' : '已停用', job.enabled ? 'ok' : 'muted'));
        if (job.running) {
            tags.appendChild(badge('运行中', 'warn'));
        }
        leftGroup.appendChild(tags);
        header.appendChild(leftGroup);
        card.appendChild(header);

        // --- 任务属性 ---
        var kv = el('div', 'kv-grid');
        kv.appendChild(kvItem('关联账号', maskUser(job.accountUser), 'muted'));
        kv.appendChild(kvItem('Cron', job.cron || '—', 'muted'));
        kv.appendChild(kvItem('超时(分钟)', num(job.timeoutMinutes)));
        kv.appendChild(kvItem('挂机(秒)', job.type === 'pc_hang' ? num(job.hangSeconds) : '—', job.type === 'pc_hang' ? '' : 'muted'));
        kv.appendChild(kvItem('上次运行', job.lastRunAt || '—', 'muted'));
        kv.appendChild(kvItem('下次运行', job.nextRunAt || '—', 'muted'));
        var lastResult = job.lastResult || '';
        var resultClass = '';
        if (lastResult.indexOf('成功') >= 0) {
            resultClass = 'ok';
        } else if (lastResult.indexOf('失败') >= 0 || lastResult.indexOf('超时') >= 0) {
            resultClass = 'err';
        }
        kv.appendChild(kvItem('上次结果', lastResult || '—', lastResult ? resultClass : 'muted'));
        card.appendChild(kv);

        // --- 操作 ---
        var jobId = job.id || '';
        var actions = el('div', 'account-actions');
        actions.appendChild(makeButton('▶️ 手动执行', 'btn-action', function () {
            runJob(jobId, job.name);
        }));
        actions.appendChild(makeButton('⏹ 停止', 'btn-action', function () {
            stopJob(jobId, job.name);
        }));
        actions.appendChild(makeButton('✏️ 编辑', 'btn-action', function () {
            openJobModal(job);
        }));
        actions.appendChild(makeButton('🗑️ 删除', 'btn-action btn-delete', function () {
            deleteJob(jobId, job.name);
        }));
        card.appendChild(actions);

        return card;
    }

    /** 手动执行任务（异步，不等待完成）。 */
    async function runJob(id, name) {
        if (!id) {
            notify('任务 Id 缺失', true);
            return;
        }
        try {
            var res = await postJson('/api/jobs/run', { id: id });
            if (res && res.success) {
                notify('任务 [' + (name || id) + '] 已提交执行（异步，可在日志页查看进度）');
                await loadJobs();
            } else {
                notify((res && (res.msg || res.message)) || '执行失败', true);
            }
        } catch (e) {
            notify('执行失败：' + e.message, true);
        }
    }

    /** 停止任务。 */
    async function stopJob(id, name) {
        if (!id) {
            notify('任务 Id 缺失', true);
            return;
        }
        try {
            var res = await postJson('/api/jobs/stop', { id: id });
            if (res && res.success) {
                notify('任务 [' + (name || id) + '] 已请求停止');
                await loadJobs();
            } else {
                notify((res && (res.msg || res.message)) || '停止失败', true);
            }
        } catch (e) {
            notify('停止失败：' + e.message, true);
        }
    }

    /** 删除任务。 */
    async function deleteJob(id, name) {
        if (!window.confirm('确认删除任务 [' + (name || id) + '] 吗？')) {
            return;
        }
        try {
            var res = await delJson('/api/jobs/' + encodeURIComponent(id));
            if (res && res.success) {
                notify('任务 [' + (name || id) + '] 已删除');
                await loadJobs();
            } else {
                notify((res && (res.msg || res.message)) || '删除失败', true);
            }
        } catch (e) {
            notify('删除失败：' + e.message, true);
        }
    }

    /** 渲染执行历史表格。 */
    function renderHistory() {
        var tbody = $('jobs-history-body');
        clearNode(tbody);

        if (state.history.length === 0) {
            var tr = document.createElement('tr');
            var td = el('td', 'wrap', '暂无执行历史记录。');
            td.colSpan = 8;
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }

        state.history.forEach(function (r) {
            var tr = document.createElement('tr');
            tr.appendChild(el('td', null, r.jobName || '—'));
            tr.appendChild(el('td', null, JOB_TYPE_LABEL[r.jobType] || r.jobType || '—'));
            tr.appendChild(el('td', 'mono', fmtDateTime(r.startedAt)));
            tr.appendChild(el('td', 'mono', fmtDateTime(r.endedAt)));
            tr.appendChild(el('td', 'mono', fmtDuration(r.durationSeconds)));

            var exitTd = el('td', 'mono', num(r.exitCode));
            if (Number(r.exitCode) !== 0) {
                exitTd.style.color = 'var(--status-red)';
            }
            tr.appendChild(exitTd);

            var resultTd = document.createElement('td');
            if (r.timedOut) {
                resultTd.appendChild(badge('超时', 'warn'));
            } else if (r.success) {
                resultTd.appendChild(badge('成功', 'ok'));
            } else {
                resultTd.appendChild(badge('失败', 'err'));
            }
            tr.appendChild(resultTd);

            tr.appendChild(el('td', 'wrap', r.summary || '—'));
            tbody.appendChild(tr);
        });
    }

    /** 填充账号下拉（任务关联账号 / 兑换云电脑选择器共用数据源）。 */
    function fillAccountSelect(select, selected) {
        clearNode(select);

        if (state.accounts.length === 0) {
            var empty = el('option', null, '（暂无可用账号，请先在「账号」页添加）');
            empty.value = '';
            select.appendChild(empty);
            return;
        }

        state.accounts.forEach(function (acc) {
            var opt = el('option', null, (acc.name || '未命名') + '（' + maskUser(acc.user) + '）');
            opt.value = acc.key || acc.user || '';
            select.appendChild(opt);
        });

        if (selected) {
            select.value = selected;
        }
    }

    /** 打开任务编辑弹窗（job 为空表示新建）。 */
    function openJobModal(job) {
        state.editingJobId = (job && job.id) ? job.id : '';
        $('mj-title').textContent = job ? '编辑定时任务' : '新建定时任务';

        fillAccountSelect($('mj-accountUser'), job ? job.accountUser : '');
        if (!job && state.accounts.length === 1) {
            $('mj-accountUser').value = state.accounts[0].key || state.accounts[0].user || '';
        }

        $('mj-name').value = job ? (job.name || '') : '';
        $('mj-type').value = job ? (job.type || 'ai_chat') : 'ai_chat';
        $('mj-cron').value = job ? (job.cron || '') : '0 3,20 * * *';
        $('mj-timeoutMinutes').value = job ? num(job.timeoutMinutes, 15) : '15';
        $('mj-hangSeconds').value = job ? num(job.hangSeconds, 4800) : '4800';
        $('mj-enabled').checked = job ? !!job.enabled : true;
        $('mj-randomDaily').checked = job ? !!job.randomDaily : false;

        syncHangRowVisibility();
        renderCronPreview(null);
        openModal('modal-job');
        previewCron();
    }

    /** 挂机时长字段仅对 pc_hang 有意义。 */
    function syncHangRowVisibility() {
        var isPcHang = $('mj-type').value === 'pc_hang';
        $('mj-hang-row').style.display = isPcHang ? 'block' : 'none';
    }

    /** Cron 输入防抖后请求预览。 */
    function scheduleCronPreview() {
        if (state.cronTimer) {
            clearTimeout(state.cronTimer);
        }
        state.cronTimer = setTimeout(function () {
            state.cronTimer = null;
            previewCron();
        }, CRON_DEBOUNCE_MS);
    }

    /** 请求 cron 预览。 */
    async function previewCron() {
        var cron = $('mj-cron').value.trim();
        var box = $('mj-cron-preview');

        if (!cron) {
            renderCronPreview(null);
            return;
        }

        box.className = 'cron-preview';
        box.textContent = '校验中...';

        try {
            var res = await postJson('/api/jobs/cron-preview', { cron: cron });
            // 输入已变化则丢弃过期响应
            if ($('mj-cron').value.trim() !== cron) {
                return;
            }
            renderCronPreview(res || { valid: false, error: '响应为空' });
        } catch (e) {
            if ($('mj-cron').value.trim() !== cron) {
                return;
            }
            renderCronPreview({ valid: false, error: e.message, description: '', nextTimes: [] });
        }
    }

    /** 渲染 cron 预览结果。 */
    function renderCronPreview(preview) {
        var box = $('mj-cron-preview');
        clearNode(box);

        if (!preview) {
            box.className = 'cron-preview';
            box.textContent = '输入 cron 表达式后自动校验并预览最近 3 次触发时间。';
            return;
        }

        if (!preview.valid) {
            box.className = 'cron-preview invalid';
            box.appendChild(cpRow('错误', preview.error || '表达式无效'));
            return;
        }

        box.className = 'cron-preview valid';
        box.appendChild(cpRow('含义', preview.description || '—'));
        var times = Array.isArray(preview.nextTimes) ? preview.nextTimes : [];
        box.appendChild(cpRow('最近 3 次', times.length > 0 ? times.join('　|　') : '无'));
    }

    /** Cron 预览中的一行。 */
    function cpRow(key, value) {
        var row = el('div', 'cp-row');
        row.appendChild(el('span', 'cp-k', key));
        row.appendChild(el('span', 'cp-v', value));
        return row;
    }

    /** 保存（新增 / 更新）任务。 */
    async function saveJob() {
        var payload = {
            id: state.editingJobId,
            name: $('mj-name').value.trim(),
            type: $('mj-type').value,
            cron: $('mj-cron').value.trim(),
            enabled: $('mj-enabled').checked,
            randomDaily: $('mj-randomDaily').checked,
            accountUser: $('mj-accountUser').value,
            timeoutMinutes: parseIntOr($('mj-timeoutMinutes').value, 15),
            hangSeconds: parseIntOr($('mj-hangSeconds').value, 4800)
        };

        if (payload.type === 'pc_hang') {
            // 挂机任务的超时必须覆盖挂机时长，否则脚本再正常也会被判超时
            var needed = Math.ceil(payload.hangSeconds / 60) + 10;
            if (payload.timeoutMinutes < needed) {
                if (!confirm('云电脑挂机任务需要 ' + payload.hangSeconds + ' 秒（约 '
                    + Math.ceil(payload.hangSeconds / 60) + ' 分钟），但超时只有 '
                    + payload.timeoutMinutes + ' 分钟，任务会在挂机完成前被终止。\n\n'
                    + '是否自动把超时调整为 ' + needed + ' 分钟？')) {
                    return;
                }
                payload.timeoutMinutes = needed;
            }
        }

        if (!payload.name) {
            notify('任务名称不能为空', true);
            return;
        }
        if (!payload.cron) {
            notify('Cron 表达式不能为空', true);
            return;
        }
        if (!payload.accountUser) {
            notify('请选择关联账号', true);
            return;
        }
        if (payload.timeoutMinutes < 1 || payload.timeoutMinutes > 1440) {
            notify('超时时间必须在 1-1440 分钟之间', true);
            return;
        }

        var btn = $('btn-mj-submit');
        btn.disabled = true;
        btn.textContent = '保存中...';
        try {
            var res = state.editingJobId
                ? await putJson('/api/jobs', payload)
                : await postJson('/api/jobs', payload);
            if (res && res.success) {
                notify('任务已保存');
                closeModal('modal-job');
                await loadJobs();
            } else {
                notify((res && (res.msg || res.message)) || '保存失败', true);
            }
        } catch (e) {
            notify('保存失败：' + e.message, true);
        } finally {
            btn.disabled = false;
            btn.textContent = '保存任务';
        }
    }

    /* ======================================================================
       八、兑换 Tab
       ====================================================================== */

    /** 默认兑换配置（接口异常时的兜底）。 */
    function defaultRedeemConfig() {
        return {
            enabled: false,
            desktopId: '',
            prodId: 0,
            prodName: '',
            prodType: '',
            costPoints: 0,
            maxRedeemTimes: 0,
            lastRedeemDate: '',
            scheduleType: 'daily',
            intervalDays: 1,
            monthlyDays: []
        };
    }

    /** 拉取兑换配置 + 计划判定 + 账号列表。 */
    async function loadRedeemTab() {
        var results = await Promise.all([
            requestJson('/api/redeem/config'),
            requestJson('/api/redeem/plan'),
            requestJson('/api/accounts')
        ]);
        state.redeemConfig = results[0] || defaultRedeemConfig();
        state.redeemPlan = results[1] || {};
        state.accounts = Array.isArray(results[2]) ? results[2] : [];
        fillRedeemForm();
        renderRedeemPlan();
        fillDesktopPicker();
        // 自动识别：打开兑换页即拉取奖励列表，并按已保存的 prodId 回选下拉项
        loadRewards().then(syncRewardSelect).catch(function () { /* 静默 */ });
    }

    /** 用 state.redeemConfig 填充兑换表单。 */
    function fillRedeemForm() {
        var cfg = state.redeemConfig || defaultRedeemConfig();
        $('rd-enabled').checked = !!cfg.enabled;
        $('rd-desktopId').value = cfg.desktopId || '';
        $('rd-prodId').value = num(cfg.prodId, 0);
        $('rd-prodName').value = cfg.prodName || '';
        $('rd-prodType').value = cfg.prodType || '';
        $('rd-costPoints').value = num(cfg.costPoints, 0);
        $('rd-maxRedeemTimes').value = num(cfg.maxRedeemTimes, 0);
        $('rd-scheduleType').value = cfg.scheduleType || 'daily';
        $('rd-intervalDays').value = num(cfg.intervalDays, 1);
        var days = Array.isArray(cfg.monthlyDays) ? cfg.monthlyDays : [];
        $('rd-monthlyDays').value = days.join(',');
        syncScheduleRows();
    }

    /** 根据 scheduleType 显示/隐藏相关字段。 */
    function syncScheduleRows() {
        var type = $('rd-scheduleType').value;
        $('rd-interval-row').style.display = (type === 'interval_days') ? 'block' : 'none';
        $('rd-monthly-row').style.display = (type === 'monthly_days') ? 'block' : 'none';
    }

    /** 渲染兑换计划判定。 */
    function renderRedeemPlan() {
        var plan = state.redeemPlan || {};
        var container = $('redeem-plan');
        clearNode(container);

        var shouldTd = document.createElement('div');
        shouldTd.className = 'kv';
        shouldTd.appendChild(el('span', 'kv-k', '今日应兑换'));
        var shouldWrap = el('span', 'kv-v');
        shouldWrap.appendChild(badge(plan.shouldRedeem ? '是' : '否', plan.shouldRedeem ? 'ok' : 'muted'));
        shouldTd.appendChild(shouldWrap);
        container.appendChild(shouldTd);

        var points = Number(plan.points);
        var pointsUnavailable = isNaN(points) || points < 0;
        container.appendChild(kvItem('判定原因', plan.reason || '—', 'muted'));
        container.appendChild(kvItem('当前积分', pointsUnavailable ? '不可用' : num(points), pointsUnavailable ? 'muted' : 'warn'));

        var channelTd = document.createElement('div');
        channelTd.className = 'kv';
        channelTd.appendChild(el('span', 'kv-k', '通道A状态'));
        var channelWrap = el('span', 'kv-v');
        var channelState = plan.channelAState || 'Unknown';
        var channelKind = channelState === 'Ok' ? 'ok' : (channelState === 'LoginExpired' ? 'err' : 'warn');
        channelWrap.appendChild(badge(channelState, channelKind));
        channelTd.appendChild(channelWrap);
        container.appendChild(channelTd);

        container.appendChild(kvItem('上次兑换日期', plan.lastRedeemDate || '—', 'muted'));
        container.appendChild(kvItem('配置文件路径', plan.configPath || '—', 'muted'));
    }

    /** 填充云电脑选择器（值 = desktopId）。 */
    function fillDesktopPicker() {
        var select = $('rd-desktop-picker');
        clearNode(select);

        var placeholder = el('option', null, '（不选择）');
        placeholder.value = '';
        select.appendChild(placeholder);

        var found = false;
        state.accounts.forEach(function (acc) {
            var desktops = Array.isArray(acc.desktops) ? acc.desktops : [];
            desktops.forEach(function (d) {
                if (!d.desktopId) {
                    return;
                }
                found = true;
                var opt = el('option', null, (acc.name || '未命名') + ' / ' + (d.name || '未命名') + '（ID ' + d.desktopId + '）');
                opt.value = String(d.desktopId);
                select.appendChild(opt);
            });
        });

        if (!found) {
            var none = el('option', null, '（暂无已登录云电脑可选）');
            none.value = '';
            select.appendChild(none);
        }
    }

    /**
     * 拉取奖励列表。
     * 注意：该接口是多态的 —— 成功返回数组，失败返回 { success:false, msg }。
     */
    async function loadRewards() {
        var btn = $('btn-rewards-load');
        var select = $('rd-reward-select');
        btn.disabled = true;
        btn.textContent = '拉取中...';
        clearNode(select);

        try {
            var data = await requestJson('/api/redeem/rewards');

            var placeholder = el('option', null, '（请选择奖励）');
            placeholder.value = '';
            select.appendChild(placeholder);

            if (Array.isArray(data)) {
                state.rewards = data;
                data.forEach(function (r, index) {
                    var label = (r.prodName || '未命名') + ' · ' + num(r.costPoints) + ' 积分 · ID ' + num(r.prodId);
                    if (r.prodType) {
                        label += ' · 类型 ' + r.prodType;
                    }
                    var opt = el('option', null, label);
                    opt.value = String(index);
                    select.appendChild(opt);
                });
                notify('已拉取 ' + data.length + ' 项奖励');
            } else {
                // 多态失败分支：{ success:false, msg:"..." }
                state.rewards = [];
                var fail = el('option', null, '（拉取失败）');
                fail.value = '';
                select.appendChild(fail);
                notify((data && data.msg) ? data.msg : '奖励列表拉取失败：接口返回了非数组响应', true);
            }
        } catch (e) {
            state.rewards = [];
            var errOpt = el('option', null, '（拉取失败）');
            errOpt.value = '';
            select.appendChild(errOpt);
            notify('拉取奖励失败：' + e.message, true);
        } finally {
            btn.disabled = false;
            btn.textContent = '📥 拉取奖励列表';
        }
    }

    /** 选中奖励后自动填充商品字段。 */
    function applySelectedReward() {
        var index = parseInt($('rd-reward-select').value, 10);
        if (isNaN(index) || !state.rewards[index]) {
            return;
        }
        var reward = state.rewards[index];
        $('rd-prodId').value = num(reward.prodId, 0);
        $('rd-prodName').value = reward.prodName || '';
        $('rd-prodType').value = reward.prodType || '';
        $('rd-costPoints').value = num(reward.costPoints, 0);
        notify('已填充商品：' + (reward.prodName || '未命名'));
    }

    /** 已保存的 prodId 能在奖励列表中匹配时，自动回选下拉项。 */
    function syncRewardSelect() {
        var cfg = state.redeemConfig || {};
        var pid = Number(cfg.prodId);
        if (!pid) {
            return;
        }
        var select = $('rd-reward-select');
        for (var i = 0; i < state.rewards.length; i++) {
            if (Number(state.rewards[i].prodId) === pid) {
                select.value = String(i);
                return;
            }
        }
    }

    /** 从表单组装完整 RedeemConfig 对象（PUT 需要提交完整对象）。 */
    function buildRedeemConfigFromForm() {
        var monthlyRaw = $('rd-monthlyDays').value.split(/[,，\s]+/);
        var monthlyDays = [];
        monthlyRaw.forEach(function (item) {
            if (!item) {
                return;
            }
            var n = parseInt(item, 10);
            if (!isNaN(n)) {
                monthlyDays.push(n);
            }
        });

        return {
            enabled: $('rd-enabled').checked,
            desktopId: $('rd-desktopId').value.trim(),
            prodId: parseIntOr($('rd-prodId').value, 0),
            prodName: $('rd-prodName').value.trim(),
            prodType: $('rd-prodType').value.trim(),
            costPoints: parseIntOr($('rd-costPoints').value, 0),
            maxRedeemTimes: parseIntOr($('rd-maxRedeemTimes').value, 0),
            lastRedeemDate: (state.redeemConfig && state.redeemConfig.lastRedeemDate) || '',
            scheduleType: $('rd-scheduleType').value || 'daily',
            intervalDays: parseIntOr($('rd-intervalDays').value, 1),
            monthlyDays: monthlyDays
        };
    }

    /**
     * 保存兑换配置。
     * PUT 提交的是完整对象，为避免把服务端刚更新的 lastRedeemDate 覆盖成旧值，
     * 先重新 GET 一次配置，取其 lastRedeemDate 与表单字段合并。
     */
    async function saveRedeemConfig() {
        var btn = $('btn-redeem-save');
        btn.disabled = true;
        btn.textContent = '保存中...';

        var fresh = null;
        try {
            fresh = await requestJson('/api/redeem/config');
        } catch (e) {
            console.warn('[CtYun] 读取最新兑换配置失败，回退使用本地缓存的 lastRedeemDate', e);
        }

        var payload = buildRedeemConfigFromForm();
        if (fresh && typeof fresh.lastRedeemDate === 'string') {
            payload.lastRedeemDate = fresh.lastRedeemDate;
        }

        try {
            var res = await putJson('/api/redeem/config', payload);
            if (res && res.success) {
                notify('兑换配置已保存');
                await loadRedeemTab();
            } else {
                notify((res && (res.msg || res.message)) || '保存失败', true);
            }
        } catch (e) {
            notify('保存失败：' + e.message, true);
        } finally {
            btn.disabled = false;
            btn.textContent = '💾 保存配置';
        }
    }

    /** 立即执行兑换。 */
    async function executeRedeem() {
        if (!window.confirm('确认立即执行一次积分兑换吗？')) {
            return;
        }
        var btn = $('btn-redeem-execute');
        btn.disabled = true;
        btn.textContent = '执行中...';
        try {
            var res = await requestJson('/api/redeem/execute', { method: 'POST' });
            if (res && res.success) {
                notify((res.msg || res.message) || '兑换执行成功');
            } else {
                notify((res && (res.msg || res.message)) || '兑换执行失败', true);
            }
            await loadRedeemTab();
        } catch (e) {
            notify('兑换失败：' + e.message, true);
        } finally {
            btn.disabled = false;
            btn.textContent = '⚡ 立即执行兑换';
        }
    }

    /* ======================================================================
       九、日志 Tab（SSE）
       ====================================================================== */

    /** 建立 SSE 连接（token 走查询参数，EventSource 无法设置请求头）。 */
    function startLogStream() {
        closeLogStream();
        if (!state.token) {
            return;
        }
        state.sseRetry = 0;
        connectLogStream();
    }

    function connectLogStream() {
        if (!state.token) {
            return;
        }

        var url = '/api/logs?token=' + encodeURIComponent(state.token);
        var es = new EventSource(url);
        state.logStream = es;

        es.onopen = function () {
            state.sseRetry = 0;
            setLogStatus('已连接', 'ok');
        };

        es.onmessage = function (event) {
            state.sseRetry = 0;
            setLogStatus('已连接', 'ok');
            pushLog(parseLogEvent(event.data));
        };

        es.onerror = function () {
            setLogStatus('连接中断', 'err');
            try {
                es.close();
            } catch (e) {
                /* 忽略 */
            }
            if (state.logStream === es) {
                state.logStream = null;
            }
            if (!state.token) {
                return;
            }
            state.sseRetry += 1;
            if (state.sseRetry > SSE_MAX_RETRY) {
                setLogStatus('已断开（重试过多）', 'err');
                return;
            }
            state.sseRetryTimer = setTimeout(connectLogStream, SSE_RETRY_MS);
        };
    }

    /** 关闭 SSE 连接并清理重连定时器。 */
    function closeLogStream() {
        if (state.sseRetryTimer) {
            clearTimeout(state.sseRetryTimer);
            state.sseRetryTimer = null;
        }
        if (state.logStream) {
            try {
                state.logStream.close();
            } catch (e) {
                /* 忽略 */
            }
            state.logStream = null;
        }
        setLogStatus('未连接', 'muted');
    }

    /**
     * 解析 SSE 推送的日志载荷。
     * 正常为 { ts, level, source, line }；异常情况下退化为纯文本行。
     */
    function parseLogEvent(data) {
        try {
            var obj = JSON.parse(data);
            if (obj && typeof obj === 'object') {
                return {
                    ts: Number(obj.ts) || 0,
                    level: Number(obj.level) || 0,
                    source: String(obj.source || ''),
                    line: String(obj.line === undefined || obj.line === null ? data : obj.line)
                };
            }
        } catch (e) {
            /* 非 JSON，按纯文本处理 */
        }
        return { ts: Date.now(), level: 0, source: '', line: String(data) };
    }

    /** 推送一条日志；暂停期间写入缓冲区（有上限，防内存泄漏）。 */
    function pushLog(entry) {
        state.logTotal += 1;
        if (state.logPaused) {
            state.logBuffer.push(entry);
            if (state.logBuffer.length > LOG_MAX_LINES) {
                state.logBuffer.splice(0, state.logBuffer.length - LOG_MAX_LINES);
            }
            updateLogCounter();
            return;
        }
        appendLogLine(entry);
    }

    /** 追加一行日志到 DOM，并限制最大行数。 */
    function appendLogLine(entry) {
        var box = $('console-logs');
        box.appendChild(logLine(entry.line, entry.level));
        while (box.childNodes.length > LOG_MAX_LINES) {
            box.removeChild(box.firstChild);
        }
        updateLogCounter();
        if (state.logAutoScroll) {
            box.scrollTop = box.scrollHeight;
        }
    }

    /**
     * 构造日志行元素。
     * @param {string} text 行文本
     * @param {number} level 0=Info 1=Success 2=Warn 3=Error
     * @returns {HTMLElement}
     */
    function logLine(text, level) {
        var cls = LOG_LEVEL_CLASS[Number(level)] || 'log-info';
        return el('div', 'log-line ' + cls, text);
    }

    /** 更新日志状态徽章。 */
    function setLogStatus(text, kind) {
        var node = $('log-status');
        node.textContent = text;
        node.className = 'badge ' + (kind || 'muted');
    }

    /** 更新日志行数统计。 */
    function updateLogCounter() {
        var box = $('console-logs');
        var shown = box.childNodes.length;
        var buffered = state.logBuffer.length;
        var text = '显示 ' + shown + ' / 累计 ' + state.logTotal + ' 行';
        if (state.logPaused && buffered > 0) {
            text += '（暂停中，缓存 ' + buffered + ' 行）';
        } else if (state.logPaused) {
            text += '（已暂停）';
        }
        $('log-counter').textContent = text;
    }

    /** 切换暂停 / 继续。 */
    function toggleLogPause() {
        state.logPaused = !state.logPaused;
        var btn = $('btn-log-pause');
        if (state.logPaused) {
            btn.textContent = '▶️ 继续滚动';
            notify('日志已暂停，新日志将缓存，恢复后补入');
            updateLogCounter();
        } else {
            btn.textContent = '⏸ 暂停滚动';
            flushLogBuffer();
        }
    }

    /** 恢复滚动时把缓冲区一次性补入（超出上限只保留最新的）。 */
    function flushLogBuffer() {
        var pending = state.logBuffer.slice();
        state.logBuffer = [];
        if (pending.length > LOG_MAX_LINES) {
            pending = pending.slice(pending.length - LOG_MAX_LINES);
        }
        var box = $('console-logs');
        pending.forEach(function (entry) {
            box.appendChild(logLine(entry.line, entry.level));
        });
        while (box.childNodes.length > LOG_MAX_LINES) {
            box.removeChild(box.firstChild);
        }
        if (state.logAutoScroll) {
            box.scrollTop = box.scrollHeight;
        }
        updateLogCounter();
    }

    /** 清空日志屏幕与缓冲区。 */
    function clearLogs() {
        var box = $('console-logs');
        clearNode(box);
        state.logBuffer = [];
        state.logTotal = 0;
        box.appendChild(logLine('[系统] 控制台已清空。', 0));
        updateLogCounter();
    }

    /** 重置日志区到初始状态。 */
    function resetConsole() {
        var box = $('console-logs');
        clearNode(box);
        state.logBuffer = [];
        state.logTotal = 0;
        state.logPaused = false;
        state.logAutoScroll = true;
        $('btn-log-pause').textContent = '⏸ 暂停滚动';
        $('auto-scroll-toggle').checked = true;
        box.appendChild(logLine('[系统] 控制面板已就绪，正在连接日志流...', 0));
        updateLogCounter();
    }

    /* ======================================================================
       十、设置 Tab
       ====================================================================== */

    /** 载入设置（boot 时调用：用于确定轮询间隔）。 */
    async function loadSettings() {
        var data = await requestJson('/api/settings');
        state.settings = data || {};
        var seconds = parseIntOr(state.settings.pollIntervalSeconds, DEFAULT_POLL_SECONDS);
        if (seconds > 0) {
            state.pollSeconds = seconds;
        }
        if (state.activeTab === 'settings') {
            fillSettingsForm();
        }
    }

    // ---------- 版本与自动更新 ----------

    /** 渲染版本信息（kv-grid）。 */
    function renderUpdateInfo(res, extra) {
        var box = $('update-info');
        if (!box || !res) {
            return;
        }
        clearNode(box);
        box.appendChild(kvItem('本地版本', (res.local || '—').toString().slice(0, 12), 'muted'));
        box.appendChild(kvItem('远端版本', (res.remote || (res.ok ? '无' : '—')).toString().slice(0, 12),
            res.hasUpdate ? 'warn' : 'muted'));
        if (res.date) {
            box.appendChild(kvItem('远端提交时间', res.date, 'muted'));
        }
        if (res.message) {
            box.appendChild(kvItem('提交说明', res.message, 'muted'));
        }
        box.appendChild(kvItem('状态',
            res.hasUpdate ? '有新版本可用' : (res.ok ? '已是最新版本' : '检查失败'),
            res.hasUpdate ? 'warn' : (res.ok ? 'ok' : 'err')));
        if (extra) {
            var p = $('update-progress');
            if (p) {
                p.textContent = extra;
            }
        }
    }

    /** 检查更新。silent=true 时不弹提示（用于打开设置页自动检查）。 */
    async function checkUpdate(silent) {
        var p = $('update-progress');
        if (p) {
            p.textContent = '正在检查更新…';
        }
        try {
            var res = await requestJson('/api/update/check');
            state.updateInfo = res || {};
            renderUpdateInfo(state.updateInfo, res && res.ok
                ? (res.hasUpdate ? '发现新版本，点击「更新到最新版并重启」' : '已是最新版本')
                : '检查失败：' + ((res && res.error) || '未知错误'));
            if (!silent) {
                if (res && res.ok) {
                    notify(res.hasUpdate ? '发现新版本，可点击更新' : '已是最新版本 ✓');
                } else {
                    notify('检查更新失败：' + ((res && res.error) || '未知错误'), true);
                }
            }
        } catch (e) {
            notify('检查更新失败：' + e.message, true);
        }
    }

    /** 立即更新并重启服务（更新完成后自动重载页面）。 */
    async function runUpdate() {
        if (!confirm('将下载 GitHub 最新版本覆盖代码文件（账号/任务/兑换配置等数据会保留），并重启服务。确定继续？')) {
            return;
        }
        var p = $('update-progress');
        if (p) {
            p.textContent = '正在下载并更新，请耐心等待…';
        }
        try {
            var res = await postJson('/api/update/run', {});
            if (!res || !res.ok) {
                notify('更新失败：' + ((res && res.error) || '未知错误'), true);
                if (p) {
                    p.textContent = '更新失败：' + ((res && res.error) || '');
                }
                return;
            }
            if (!res.updated) {
                notify('已经是最新版本，无需更新 ✓');
                if (p) {
                    p.textContent = '已是最新版本。';
                }
                return;
            }
            var changed = (res.changed || []).length;
            notify('更新成功（' + changed + ' 个文件），服务正在重启…');
            if (p) {
                p.textContent = '更新成功（' + changed + ' 个文件），服务正在重启，页面将自动刷新…';
            }
            // 等待服务重启完成后自动刷新页面
            waitForServerAndReload();
        } catch (e) {
            // 更新触发重启时连接会被断开，属于正常现象，走等待重启流程
            waitForServerAndReload();
        }
    }

    /** 轮询服务可用性，恢复后自动刷新页面。 */
    function waitForServerAndReload() {
        var tries = 0;
        var timer = setInterval(async function () {
            tries += 1;
            try {
                await requestJson('/api/update/check');
                clearInterval(timer);
                window.location.reload();
            } catch (e) {
                if (tries > 30) {
                    clearInterval(timer);
                    notify('服务重启超时，请手动刷新页面或重新启动服务', true);
                }
            }
        }, 3000);
    }

    // ---------- 全部账号平台任务统计 ----------

    /** 启动全量统计，并轮询直到完成。 */
    async function loadAllPlatformTasks() {
        if (state.platformAll && state.platformAll.running) {
            return;
        }
        state.platformAll = { running: true, accounts: [], total: { done: 0, doing: 0, todo: 0 }, msg: '' };
        renderTasksSummary();
        try {
            await postJson('/api/platform/tasks/all', {});
        } catch (e) {
            state.platformAll.running = false;
            notify('启动平台任务统计失败：' + e.message, true);
            renderTasksSummary();
            return;
        }
        var tries = 0;
        var timer = setInterval(async function () {
            tries += 1;
            try {
                var snap = await requestJson('/api/platform/tasks/all');
                state.platformAll = {
                    running: !!snap.running,
                    accounts: snap.accounts || [],
                    total: snap.total || { done: 0, doing: 0, todo: 0 },
                    msg: snap.msg || '',
                    at: Date.now()
                };
                renderTasksSummary();
                if (!snap.running || tries > 60) {
                    clearInterval(timer);
                    state.platformAll.running = false;
                    renderTasksSummary();
                }
            } catch (e) {
                clearInterval(timer);
                state.platformAll.running = false;
                renderTasksSummary();
            }
        }, 3000);
    }

    /** 取某账号的全量平台任务统计结果（未完成则返回 null）。 */
    function platformStatOf(user) {
        var all = state.platformAll;
        if (!all || !Array.isArray(all.accounts)) {
            return null;
        }
        for (var i = 0; i < all.accounts.length; i++) {
            if (all.accounts[i].user === user) {
                return all.accounts[i];
            }
        }
        return null;
    }

    /** 设置 Tab：载入设置 + 环境自检。 */
    async function loadSettingsTab() {
        var data = await requestJson('/api/settings');
        state.settings = data || {};
        fillSettingsForm();
        await loadEnvCheck();
        checkUpdate(true).catch(function () { /* ignore */ });
    }

    /** 用 state.settings 填充设置表单（可编辑 12 项 + 只读诊断信息）。 */
    function fillSettingsForm() {
        var s = state.settings || {};
        $('set-keepAliveSeconds').value = num(s.keepAliveSeconds, 60);
        $('set-sessionRestartMinutes').value = num(s.sessionRestartMinutes, 1440);
        $('set-sessionTokenHours').value = num(s.sessionTokenHours, 12);
        $('set-pythonExecutable').value = s.pythonExecutable || '';
        $('set-scriptsDir').value = s.scriptsDir || '';
        $('set-aiChatTimeoutMinutes').value = num(s.aiChatTimeoutMinutes, 15);
        $('set-pcHangTimeoutMinutes').value = num(s.pcHangTimeoutMinutes, 100);
        $('set-pcHangSeconds').value = num(s.pcHangSeconds, 4800);
        $('set-bootWaitRounds').value = num(s.bootWaitRounds, 3);
        $('set-bootWaitSecondsPerRound').value = num(s.bootWaitSecondsPerRound, 60);
        $('set-browserMutexMode').value = s.browserMutexMode || 'Global';
        $('set-pollIntervalSeconds').value = num(s.pollIntervalSeconds, DEFAULT_POLL_SECONDS);
        $('set-feishuWebhook').value = s.feishuWebhook || '';
        $('set-feishuSecret').value = s.feishuSecret || '';
        $('set-feishuAppId').value = s.feishuAppId || '';
        $('set-feishuAppSecret').value = s.feishuAppSecret || '';
        $('set-feishuChatId').value = s.feishuChatId || '';

        // --- 只读诊断信息 ---
        var ro = $('settings-readonly');
        clearNode(ro);
        ro.appendChild(readonlyItem('dataDir（数据目录）', s.dataDir));
        ro.appendChild(readonlyItem('accountsPath（账号配置）', s.accountsPath));
        ro.appendChild(readonlyItem('redeemConfigPath（兑换配置）', s.redeemConfigPath));
        ro.appendChild(readonlyItem('jobsPath（任务配置）', s.jobsPath));
        ro.appendChild(readonlyItem('scriptsResolvedDir（脚本目录实际路径）', s.scriptsResolvedDir));
        ro.appendChild(readonlyItem('isContainer（容器环境）', s.isContainer ? 'true' : 'false'));
    }

    /** 只读诊断项。 */
    function readonlyItem(key, value) {
        var item = el('div', 'readonly-item');
        item.appendChild(el('span', 'ro-k', key));
        item.appendChild(el('span', 'ro-v', value === undefined || value === null || value === '' ? '—' : String(value)));
        return item;
    }

    /** 保存设置（提交 SettingsUpdateRequest 的 12 个字段）。 */
    async function saveSettings() {
        var payload = {
            keepAliveSeconds: parseIntOr($('set-keepAliveSeconds').value, 60),
            sessionRestartMinutes: parseIntOr($('set-sessionRestartMinutes').value, 1440),
            sessionTokenHours: parseIntOr($('set-sessionTokenHours').value, 12),
            pythonExecutable: $('set-pythonExecutable').value.trim(),
            scriptsDir: $('set-scriptsDir').value.trim(),
            aiChatTimeoutMinutes: parseIntOr($('set-aiChatTimeoutMinutes').value, 15),
            pcHangTimeoutMinutes: parseIntOr($('set-pcHangTimeoutMinutes').value, 100),
            pcHangSeconds: parseIntOr($('set-pcHangSeconds').value, 4800),
            bootWaitRounds: parseIntOr($('set-bootWaitRounds').value, 3),
            bootWaitSecondsPerRound: parseIntOr($('set-bootWaitSecondsPerRound').value, 60),
            browserMutexMode: $('set-browserMutexMode').value || 'Global',
            pollIntervalSeconds: parseIntOr($('set-pollIntervalSeconds').value, DEFAULT_POLL_SECONDS),
            feishuWebhook: $('set-feishuWebhook').value.trim(),
            feishuSecret: $('set-feishuSecret').value.trim(),
            feishuAppId: $('set-feishuAppId').value.trim(),
            feishuAppSecret: $('set-feishuAppSecret').value.trim(),
            feishuChatId: $('set-feishuChatId').value.trim()
        };

        if (payload.keepAliveSeconds < 1) {
            notify('保活心跳间隔必须大于 0', true);
            return;
        }
        if (payload.pollIntervalSeconds < 1) {
            notify('轮询间隔必须大于 0', true);
            return;
        }

        var btn = $('btn-settings-save');
        btn.disabled = true;
        btn.textContent = '保存中...';
        try {
            var res = await putJson('/api/settings', payload);
            if (res && res.success) {
                notify('设置已保存');
                state.pollSeconds = payload.pollIntervalSeconds;
                await loadSettings();
                startPolling();
            } else {
                notify((res && (res.msg || res.message)) || '保存失败', true);
            }
        } catch (e) {
            notify('保存失败：' + e.message, true);
        } finally {
            btn.disabled = false;
            btn.textContent = '💾 保存设置';
        }
    }

    /** 拉取环境自检结果。 */
    async function loadEnvCheck() {
        var data = await requestJson('/api/system/env-check');
        state.envCheck = data || { allOk: false, items: [] };
        renderEnvCheck();
        updateEnvInstallBtn();
    }

    /** 根据自检状态更新「自动安装」按钮外观。 */
    function updateEnvInstallBtn() {
        var btn = $('btn-env-install');
        if (!btn || state._envPoll) {
            return; // 轮询中由轮询器接管按钮状态
        }
        var installing = state.envCheck && state.envCheck.installing;
        btn.disabled = !!installing;
        btn.textContent = installing ? '⏳ 依赖安装中...' : '⬇️ 自动安装缺失依赖';
    }

    /** 一键安装缺失依赖（后台执行，进度实时进系统日志）。 */
    async function startEnvInstall() {
        var btn = $('btn-env-install');
        btn.disabled = true;
        btn.textContent = '启动中...';
        try {
            var res = await postJson('/api/system/env-install', {});
            if (!res.started) {
                if ((res.msg || '').indexOf('齐全') !== -1) {
                    notify('依赖已齐全，无需安装');
                    btn.textContent = '✅ 依赖齐全';
                    return;
                }
                notify(res.msg || '无法启动安装', true);
                btn.disabled = false;
                btn.textContent = '⬇️ 自动安装缺失依赖';
                return;
            }
            notify('安装已启动（' + (res.missing || []).join(', ') + '），进度见「实时系统日志」');
            btn.textContent = '⏳ 依赖安装中...';
            pollEnvInstall();
        } catch (e) {
            notify('启动安装失败：' + e.message, true);
            btn.disabled = false;
            btn.textContent = '⬇️ 自动安装缺失依赖';
        }
    }

    /** 安装期间每 3 秒轮询自检，结束后自动刷新结果。 */
    function pollEnvInstall() {
        if (state._envPoll) {
            clearInterval(state._envPoll);
        }
        state._envPoll = setInterval(async function () {
            try {
                var data = await requestJson('/api/system/env-check');
                state.envCheck = data || state.envCheck;
                renderEnvCheck();
                if (!data.installing) {
                    clearInterval(state._envPoll);
                    state._envPoll = null;
                    var btn = $('btn-env-install');
                    btn.disabled = false;
                    btn.textContent = '⬇️ 自动安装缺失依赖';
                    notify(data.allOk ? '依赖安装完成，环境自检全部通过 ✓' : '安装任务已结束，自检仍有异常项，请查看列表', !data.allOk);
                }
            } catch (e) { /* 静默，下轮重试 */ }
        }, 3000);
    }

    /** 渲染环境自检结果。 */
    function renderEnvCheck() {
        var result = state.envCheck || { allOk: false, items: [] };

        var summary = $('env-check-summary');
        clearNode(summary);

        var okWrap = el('div', 'kv');
        okWrap.appendChild(el('span', 'kv-k', '总体状态'));
        var okValue = el('span', 'kv-v');
        okValue.appendChild(badge(result.allOk ? '全部通过' : '存在异常', result.allOk ? 'ok' : 'err'));
        okWrap.appendChild(okValue);
        summary.appendChild(okWrap);

        summary.appendChild(kvItem('Python 路径', result.pythonPath || '—', 'muted'));
        summary.appendChild(kvItem('Python 版本', result.pythonVersion || '—', 'muted'));
        summary.appendChild(kvItem('检查时间', fmtDateTime(result.checkedAt), 'muted'));

        var list = $('env-check-list');
        clearNode(list);

        var items = Array.isArray(result.items) ? result.items : [];
        if (items.length === 0) {
            list.appendChild(emptyState('暂无环境自检数据。'));
            return;
        }

        items.forEach(function (item) {
            list.appendChild(buildEnvItem(item));
        });
    }

    /**
     * 构建环境自检条目；失败项提供一键复制 fixCommand。
     * @param {Object} item EnvironmentCheckItem
     * @returns {HTMLElement}
     */
    function buildEnvItem(item) {
        var row = el('div', 'env-item' + (item.ok ? '' : ' bad'));

        var main = el('div', 'env-main');
        var titleLine = el('div');
        titleLine.appendChild(el('span', 'env-name', item.displayName || item.name || '未命名检查项'));
        titleLine.appendChild(document.createTextNode(' '));
        titleLine.appendChild(badge(item.ok ? '正常' : '异常', item.ok ? 'ok' : 'err'));
        main.appendChild(titleLine);
        main.appendChild(el('div', 'env-detail', item.detail || '—'));

        if (!item.ok && item.fixCommand) {
            var fixRow = el('div', 'env-fix');
            fixRow.appendChild(el('code', null, item.fixCommand));
            fixRow.appendChild(makeButton('📋 复制修复命令', 'btn-action flex-none', function () {
                copyToClipboard(item.fixCommand);
            }));
            main.appendChild(fixRow);
        }

        row.appendChild(main);
        return row;
    }

    /**
     * 复制文本到剪贴板。优先使用 Clipboard API，
     * 非安全上下文（如通过 LAN IP 访问）下回退到 execCommand。
     * @param {string} text 待复制文本
     * @returns {Promise<boolean>}
     */
    function copyToClipboard(text) {
        if (!text) {
            notify('没有可复制的内容', true);
            return Promise.resolve(false);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text).then(function () {
                notify('已复制到剪贴板');
                return true;
            }).catch(function () {
                return Promise.resolve(fallbackCopy(text));
            });
        }
        return Promise.resolve(fallbackCopy(text));
    }

    /** execCommand 复制回退方案。 */
    function fallbackCopy(text) {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', 'readonly');
        ta.style.position = 'fixed';
        ta.style.top = '-1000px';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        var ok = false;
        try {
            ok = document.execCommand('copy');
        } catch (e) {
            ok = false;
        }
        document.body.removeChild(ta);
        notify(ok ? '已复制到剪贴板' : '复制失败，请手动选中复制', !ok);
        return ok;
    }

    /* ======================================================================
       十一、模态框控制
       ====================================================================== */

    /** 打开指定模态框。 */
    function openModal(id) {
        $(id).classList.add('open');
    }

    /** 关闭指定模态框。 */
    function closeModal(id) {
        $(id).classList.remove('open');
    }

    /** 关闭所有模态框。 */
    function closeAllModals() {
        var overlays = document.querySelectorAll('.modal-overlay');
        for (var i = 0; i < overlays.length; i++) {
            overlays[i].classList.remove('open');
        }
    }

    /** 打开修改密码弹窗。 */
    function openPasswordModal() {
        $('old-password').value = '';
        $('new-password').value = '';
        $('confirm-new-password').value = '';
        openModal('modal-password');
    }

    /* ======================================================================
       十二、事件绑定与初始化
       ====================================================================== */

    /** 绑定所有 DOM 事件。 */
    function bindEvents() {
        // --- 登录 / 头部 ---
        $('btn-login').addEventListener('click', doLogin);
        $('admin-password').addEventListener('keydown', function (event) {
            if (event.key === 'Enter') {
                doLogin();
            }
        });
        $('btn-logout').addEventListener('click', doLogout);
        $('btn-refresh').addEventListener('click', function () {
            refreshCurrentTab(true);
        });
        $('btn-change-password').addEventListener('click', openPasswordModal);
        $('btn-change-password-2').addEventListener('click', openPasswordModal);

        // --- Tab 切换（事件委托） ---
        $('tab-bar').addEventListener('click', function (event) {
            var btn = closestFrom(event.target, '.tab-btn');
            if (!btn) {
                return;
            }
            switchTab(btn.getAttribute('data-tab'));
        });

        // --- 账号 Tab ---
        $('btn-account-add').addEventListener('click', openAddAccountModal);

        /** 手动刷新全部账号积分（后台逐账号查询，进度见实时日志）。 */
        async function refreshPoints() {
            var btn = $('btn-points-refresh');
            btn.disabled = true;
            btn.textContent = '查询中...';
            try {
                var res = await postJson('/api/accounts/refresh-points', {});
                notify(res.msg || (res.started ? '积分刷新已启动' : '无法启动刷新'), !res.started);
                if (res.started) {
                    // 轮询账号列表直到后端刷新结束
                    var tries = 0;
                    var timer = setInterval(async function () {
                        tries += 1;
                        try {
                            var refreshing = await requestJson('/api/accounts/refresh-status');
                            if (!refreshing.refreshing || tries > 100) {
                                clearInterval(timer);
                                btn.disabled = false;
                                btn.textContent = '💰 刷新积分';
                                await loadAccounts();
                                renderAccounts();
                                notify('积分刷新完成');
                            }
                        } catch (e) { /* 静默 */ }
                    }, 3000);
                } else {
                    btn.disabled = false;
                    btn.textContent = '💰 刷新积分';
                }
            } catch (e) {
                notify('刷新积分失败：' + e.message, true);
                btn.disabled = false;
                btn.textContent = '💰 刷新积分';
            }
        }
        $('btn-points-refresh').addEventListener('click', refreshPoints);
        $('btn-accounts-refresh').addEventListener('click', function () {
            loadAccounts().catch(function (e) {
                notify('刷新失败：' + e.message, true);
            });
        });
        $('btn-ma-submit').addEventListener('click', submitNewAccount);
        $('btn-ma-cancel').addEventListener('click', function () {
            resetAccountForm();
            closeModal('modal-account');
        });
        $('btn-ma-sms-submit').addEventListener('click', submitSmsCode);
        $('btn-ma-sms-cancel').addEventListener('click', function () {
            resetAccountForm();
            closeModal('modal-account');
        });
        $('btn-me-submit').addEventListener('click', submitEditAccount);
        $('btn-me-cancel').addEventListener('click', function () {
            closeModal('modal-account-edit');
        });

        // --- 定时任务 Tab ---
        $('btn-job-add').addEventListener('click', function () {
            openJobModal(null);
        });
        $('btn-jobs-refresh').addEventListener('click', function () {
            loadJobs().catch(function (e) {
                notify('刷新失败：' + e.message, true);
            });
        });
        $('btn-history-refresh').addEventListener('click', function () {
            loadHistory().catch(function (e) {
                notify('刷新历史失败：' + e.message, true);
            });
        });
        $('btn-tasks-refresh').addEventListener('click', function () {
            loadTasksSummary().catch(function (e) {
                notify('刷新平台任务情况失败：' + e.message, true);
            });
        });
        $('btn-tasks-autofix').addEventListener('click', function () {
            runMissingAiChat(false);
        });
        // 完成/未完成筛选按钮
        Array.prototype.forEach.call(
            document.querySelectorAll('#tasks-filter-group [data-tfilter]'),
            function (btn) {
                btn.addEventListener('click', function () {
                    state.tasksFilter = this.getAttribute('data-tfilter') || 'all';
                    renderTasksSummary();
                });
            });
        $('btn-tasks-platform-all').addEventListener('click', function () {
            loadAllPlatformTasks().catch(function (e) {
                notify('拉取平台任务失败：' + e.message, true);
            });
        });
        // 版本与自动更新
        $('btn-update-check').addEventListener('click', function () {
            checkUpdate(false);
        });
        $('btn-update-run').addEventListener('click', function () {
            runUpdate();
        });
        $('chk-tasks-autofix').addEventListener('change', function () {
            try {
                localStorage.setItem('ctyun.tasksAutofix', this.checked ? '1' : '0');
            } catch (e) { /* ignore */ }
        });
        $('mj-type').addEventListener('change', syncHangRowVisibility);
        $('mj-cron').addEventListener('input', scheduleCronPreview);
        $('btn-mj-submit').addEventListener('click', saveJob);
        $('btn-mj-cancel').addEventListener('click', function () {
            closeModal('modal-job');
        });

        // --- 兑换 Tab ---
        $('btn-redeem-plan-refresh').addEventListener('click', function () {
            loadRedeemTab().catch(function (e) {
                notify('刷新失败：' + e.message, true);
            });
        });
        $('btn-rewards-load').addEventListener('click', loadRewards);
        $('rd-reward-select').addEventListener('change', applySelectedReward);
        $('rd-scheduleType').addEventListener('change', syncScheduleRows);
        $('rd-desktop-picker').addEventListener('change', function () {
            var value = $('rd-desktop-picker').value;
            if (value) {
                $('rd-desktopId').value = value;
            }
        });
        $('btn-redeem-save').addEventListener('click', saveRedeemConfig);
        $('btn-redeem-execute').addEventListener('click', executeRedeem);

        // --- 日志 Tab ---
        $('btn-log-pause').addEventListener('click', toggleLogPause);
        $('btn-log-clear').addEventListener('click', clearLogs);
        $('auto-scroll-toggle').addEventListener('change', function () {
            state.logAutoScroll = $('auto-scroll-toggle').checked;
            if (state.logAutoScroll) {
                var box = $('console-logs');
                box.scrollTop = box.scrollHeight;
            }
        });

        // --- 设置 Tab ---
        $('btn-settings-refresh').addEventListener('click', function () {
            loadSettingsTab().catch(function (e) {
                notify('载入失败：' + e.message, true);
            });
        });
        $('btn-settings-save').addEventListener('click', saveSettings);

        /** 飞书表单当前值（测试/探测共用）。 */
        function feishuFormConfig() {
            return {
                feishuWebhook: $('set-feishuWebhook').value.trim(),
                feishuSecret: $('set-feishuSecret').value.trim(),
                feishuAppId: $('set-feishuAppId').value.trim(),
                feishuAppSecret: $('set-feishuAppSecret').value.trim(),
                feishuChatId: $('set-feishuChatId').value.trim()
            };
        }

        /** 发送飞书测试消息（用表单当前值，未保存也能测）。 */
        async function testFeishu() {
            var btn = $('btn-feishu-test');
            btn.disabled = true;
            btn.textContent = '发送中...';
            try {
                var res = await postJson('/api/feishu/test', feishuFormConfig());
                if (res && res.success) {
                    if (res.usedChatId && !$('set-feishuChatId').value.trim()) {
                        $('set-feishuChatId').value = res.usedChatId;
                        notify('测试消息已发送，目标群 chat_id 已自动回填，请记得保存');
                    } else {
                        notify('飞书测试消息已发送，请到群里查看');
                    }
                } else {
                    notify('飞书测试失败：' + ((res && res.msg) || '未知原因'), true);
                }
            } catch (e) {
                notify('飞书测试失败：' + e.message, true);
            } finally {
                btn.disabled = false;
                btn.textContent = '📨 发送飞书测试消息';
            }
        }

        /** 探测应用机器人所在的群聊。 */
        async function probeFeishuChats() {
            var btn = $('btn-feishu-chats');
            btn.disabled = true;
            btn.textContent = '探测中...';
            try {
                var res = await postJson('/api/feishu/chats', feishuFormConfig());
                if (!res || !res.success) {
                    notify('探测失败：' + ((res && res.msg) || '未知原因'), true);
                    return;
                }
                var chats = res.chats || [];
                if (!chats.length) {
                    notify('机器人未加入任何群聊，请先把应用机器人拉进目标群', true);
                    return;
                }
                var lines = chats.map(function (c) { return c.name + '（' + c.chat_id + '）'; });
                if (chats.length === 1) {
                    $('set-feishuChatId').value = chats[0].chat_id;
                    notify('已探测到群「' + chats[0].name + '」，chat_id 已回填，请记得保存');
                } else {
                    notify('探测到 ' + chats.length + ' 个群：' + lines.join('；') + '，请把要用的 chat_id 填入输入框', false);
                }
            } catch (e) {
                notify('探测失败：' + e.message, true);
            } finally {
                btn.disabled = false;
                btn.textContent = '🔍 探测群聊';
            }
        }
        $('btn-feishu-test').addEventListener('click', testFeishu);
        $('btn-feishu-chats').addEventListener('click', probeFeishuChats);
        $('btn-env-check').addEventListener('click', function () {
            loadEnvCheck().catch(function (e) {
                notify('环境自检失败：' + e.message, true);
            });
        });
        $('btn-env-install').addEventListener('click', function () {
            startEnvInstall();
        });

        // --- 修改密码弹窗 ---
        $('btn-password-submit').addEventListener('click', submitChangePassword);
        $('btn-password-cancel').addEventListener('click', function () {
            closeModal('modal-password');
        });

        // --- 模态框：点击遮罩空白处关闭；Esc 关闭 ---
        var overlays = document.querySelectorAll('.modal-overlay');
        for (var i = 0; i < overlays.length; i++) {
            overlays[i].addEventListener('click', function (event) {
                if (event.target === event.currentTarget) {
                    event.currentTarget.classList.remove('open');
                }
            });
        }
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                closeAllModals();
            }
        });
    }

    /** 入口。 */
    function init() {
        bindEvents();
        resetConsole();
        loadSession();

        if (state.token) {
            // 本地有 token，先假定有效进入界面；若已过期或失效，
            // 首个请求返回 401 或过期看门狗会把它踢回登录页。
            hideLogin();
            if (state.expiresAt > 0 && Date.now() / 1000 >= state.expiresAt) {
                handleUnauthorized();
                return;
            }
            boot();
        } else {
            showLogin();
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
