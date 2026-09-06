"""
天翼云电脑
"""

import atexit
import datetime
import json
import os
import random
import sys
import threading
import time
from typing import Optional, Union

import ddddocr
from DrissionPage import ChromiumOptions, ChromiumPage

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

PRESET_MESSAGES = [
    "今天北京天气怎么样？（简短回答）",
    "给我讲一个冷笑话。（简短回答）",
    "来一首古诗。（简短回答）",
    "空腹可以吃饭吗？（简短回答）",
    "推荐一部人生必看电影。（简短回答）",
]


# ==========================================
# Cookie 持久化辅助函数
# ==========================================
def save_cookies(page: ChromiumPage, file_path: str) -> None:
    """获取当前页面的 Cookie 并持久化保存到本地文件。"""
    try:
        # 确保目录存在
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        cookies = page.cookies()
        if not cookies:
            print(f"[-] 抓取到的 Cookie 为空，已取消保存操作: {file_path}")
            return
        # 原因：确保获取到的 Cookie 具备业务层面的真实登录凭证，避免保存无用的访客 Cookie
        has_yl_token = False

        if isinstance(cookies, list):
            has_yl_token = any(cookie.get("name") == "YL-Token" for cookie in cookies)
        elif isinstance(cookies, dict):
            has_yl_token = "YL-Token" in cookies
        if not has_yl_token:
            print(f"[-] Cookie 中缺失关键凭证 'YL-Token'，已取消保存操作: {file_path}")
            return
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=4)
        print(f"[*] Cookie 已成功保存至: {file_path}")

    except Exception as e:
        print(f"[!] 保存 Cookie 失败: {e}")


def load_cookies(page: ChromiumPage, file_path: str) -> bool:
    """从本地文件读取 Cookie 并加载到浏览器中。"""
    if not os.path.exists(file_path):
        print(f"[-] 未发现本地 Cookie 缓存文件: {file_path}")
        return False
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        page.set.cookies(cookies)
        print(f"[*] 本地 Cookie 加载完成: {file_path}")
        return True
    except Exception as e:
        print(f"[!] 加载 Cookie 失败: {e}")
        return False


# ==========================================
# 浏览器初始化与核心功能函数
# ==========================================


def find_browser() -> str:
    """定位可用的 Chromium 内核浏览器：环境变量 > Chrome > Edge。"""
    env_path = os.getenv("CTYUN_BROWSER_PATH", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    # 注意：expandvars 不支持含括号的变量名（如 ProgramFiles(x86)），需显式读取
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    la = os.environ.get("LocalAppData", os.path.expanduser(r"~\AppData\Local"))
    candidates = [
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(la, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def init_browser_options() -> ChromiumOptions:
    """初始化并配置 Chromium 浏览器的启动参数。"""
    options = ChromiumOptions()
    browser_path = find_browser()
    if browser_path:
        options.set_browser_path(browser_path)
    options.set_argument("--no-sandbox")
    options.set_argument("--disable-gpu")
    options.set_argument("--disable-dev-shm-usage")
    options.headless()
    return options


def fill_credentials(page: ChromiumPage, username: str, password: str) -> None:
    """在页面上填写账号与密码信息。先显式等待登录表单渲染（CAS 跳转可能较慢）。"""
    print("正在等待登录表单渲染...")
    if not page.wait.ele_displayed('css:input[type="text"]', timeout=15):
        raise RuntimeError("登录表单 15 秒内未渲染出来（CAS 跳转慢或页面异常），等待重试。")

    print("正在输入账号信息...")
    account_input = page.ele('css:input[type="text"]')
    account_input.clear()
    account_input.input(username)

    print("正在输入密码信息...")
    password_input = page.ele('css:input[type="password"]')
    password_input.clear()
    password_input.input(password)


def handle_captcha(page: ChromiumPage) -> None:
    """检测页面是否存在图形验证码容器，若存在则提取图片并填充识别结果。"""
    print("正在检测图形验证码容器...")
    captcha_container = page.ele("css:.fgt-capt-ct", timeout=2)

    if not captcha_container:
        print("当前无需处理图形验证码。")
        return

    print("检测到图形验证码，开始提取并识别...")
    pic_ele = captcha_container.ele("css:img")
    pic_bytes = pic_ele.get_screenshot(as_bytes=True)

    ocr_result = get_bytes_numeric_captcha(pic_bytes)
    print(f"OCR 识别结果为: {ocr_result}")

    input_ele = captcha_container.ele('css:input[placeholder="输入图形验证码"]')
    input_ele.clear()
    input_ele.input(ocr_result)


def analyze_login_response(response_body: Union[dict, str, None]) -> int:
    """分析登录接口的返回体，提取并映射为内部状态码。"""
    if not response_body or not isinstance(response_body, dict):
        return 0

    code = response_body.get("code")
    msg = response_body.get("msg", "")

    if code == 51040 and "用户名或密码错误" in msg:
        return 1
    elif code == 51030:
        return 2
    elif code == 51040 and "图形验证码" in msg:
        return 3
    return -1


def save_screenshot(page: ChromiumPage) -> None:
    # Windows 文件名不允许冒号，时间戳中的 ":" 必须替换，否则截图静默失败
    file_name = f"{os.getenv('APP_USER')}_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
    if os.getenv("RUNNING_IN_DOCKER") == "true":
        path = "/app/data"
    else:
        path = "./"
    page.get_screenshot(path=path, name=file_name, full_page=True)


def execute_login_with_listener(
    page: ChromiumPage,
    target_url: str,
    username: str,
    password: str,
) -> Optional[bool]:
    """执行完整的账号密码登录流程。"""
    print("\n--- 开始账密登录流程 ---")
    print(f"访问登录页面: {target_url}")
    page.get(target_url)
    page.wait.load_start()

    fill_credentials(page, username, password)

    handle_captcha(page)

    if not page.wait.ele_displayed("css:button.lgm-submit-ct", timeout=5):
        raise RuntimeError("页面未渲染出登录按钮")

    # 确认显示后，重新提取元素对象
    login_button = page.ele("css:button.lgm-submit-ct")

    page.listen.start("api/auth/iam/login")
    login_button.click()

    print("已点击登录，等待接口返回...")
    packet = page.listen.wait(timeout=5)
    page.listen.stop()

    if not packet:
        raise RuntimeError("未捕获到登录接口数据包，检查是否已重定向。")

    status_code = analyze_login_response(packet.response.body)

    if status_code == 0:
        print("登录成功")
        return True
    elif status_code == 1:
        print("登录失败：用户名或密码错误。")
        return False
    elif status_code in [2, 3]:
        print(f"登录受阻（状态码 {status_code}），准备重试...")
        time.sleep(1)
        raise RuntimeError("登录受阻，准备重试。")
    else:
        print(f"未知响应: {packet.response.body}")
        return False


def display_user_info(page: ChromiumPage) -> None:
    """
    提取并输出当前登录的用户信息（手机号掩码）。
    仅作信息展示，非关键步骤：失败只警告，不抛异常、不中断任务。
    """
    user_selector = "css:div.username span.txt"

    try:
        if page.wait.ele_displayed(user_selector, timeout=5):
            username_text = (page.ele(user_selector).text or "").strip()
            if username_text:
                print(f"[*] 登录成功，当前登录用户: {username_text}")
                return
        print("[!] 暂未获取到用户信息（页面可能未完全渲染），不影响任务，继续对话流程。")
    except Exception as e:
        print(f"[!] 获取用户信息时出现异常（不影响任务）: [{type(e).__name__}] {e}")


def dismiss_popups(page: ChromiumPage) -> None:
    """关闭可能遮挡页面的营销弹窗（如 VIP 升级提示，会拦截发送按钮点击）。
    此类弹窗每次刷新都会弹出且无关闭按钮，直接用 JS 移除弹窗/遮罩 DOM 节点最可靠。"""
    js = """
    const sels = '[class*="mask"],[class*="modal"],[class*="dialog"],[class*="popup"],[class*="overlay"],[class*=" Modal"],[class*="Modal"]';
    let n = 0;
    document.querySelectorAll(sels).forEach(e => {
        const r = e.getBoundingClientRect();
        if (r.width > 100 && r.height > 100) { e.remove(); n++; }
    });
    return n;
    """
    try:
        removed = page.run_js(js)
        if removed:
            print(f"[*] 已通过 JS 移除 {removed} 个弹窗/遮罩节点。")
            time.sleep(1)
        else:
            print("[*] 未检测到弹窗节点。")
    except Exception as e:
        print(f"[!] JS 移除弹窗失败（继续）: [{type(e).__name__}] {e}")


def get_logged_in_user(page: ChromiumPage) -> str:
    """读取当前登录用户的掩码手机号（如 189****8613）；读取失败返回空串。"""
    try:
        if page.wait.ele_displayed("css:div.username span.txt", timeout=5):
            return (page.ele("css:div.username span.txt").text or "").strip()
    except Exception:
        pass
    return ""


def clear_browser_session(page: ChromiumPage) -> None:
    """清除浏览器残留会话（Cookie + 本地存储），用于切换账号。"""
    try:
        page.run_cdp("Network.clearBrowserCookies")
        page.run_js("try{localStorage.clear();sessionStorage.clear();}catch(e){}")
        print("[*] 已清除浏览器残留 Cookie 与本地存储。")
    except Exception as e:
        print(f"[!] 清除浏览器会话失败: [{type(e).__name__}] {e}")


def user_matches(page: ChromiumPage, my_username: str) -> bool:
    """校验当前登录用户是否为本任务账号（页面显示掩码：前3+****+后4）。"""
    u = get_logged_in_user(page)
    if not u:
        return False
    expected_mask = my_username[:3] + "****" + my_username[-4:]
    return u == my_username or u == expected_mask


def chat_and_earn_points(page: ChromiumPage) -> None:
    """在登录成功后，跳转至聊天页面发送预置话语，并通过 DOM 提取稳定文字。"""
    chat_url = "https://eaichat.ctyun.cn/chat/#/aichat"

    if page.url != chat_url:
        print(f"\n正在跳转至 AI 聊天页面: {chat_url}")
        page.get(chat_url)

    print("等待聊天输入框加载...")
    input_selector = "css:div.input-box.input-wrap"

    if not page.wait.ele_displayed(input_selector, timeout=10):
        raise RuntimeError("未找到聊天输入框")

    # 营销弹窗（VIP升级提示等）每次刷新都会弹出并拦截发送点击，直接移除其 DOM 节点
    dismiss_popups(page)

    # 在确认核心页面元素加载完毕后，立刻提取并输出用户信息
    display_user_info(page)

    input_box = page.ele(input_selector)
    message = random.choice(PRESET_MESSAGES)
    print(f"准备发送信息: {message}")
    input_box.input(message)
    print("等待发送按钮变为可用...")
    send_selector = "css:div.send-button"
    time.sleep(5)

    if page.wait.ele_displayed(send_selector, timeout=5):
        send_button = page.ele(send_selector)
        time.sleep(1)
        send_button.click()
        time.sleep(3)

        # 校验消息是否真正发出：被弹窗拦截时输入框内容不会被清空
        if (page.ele(input_selector).text or "").strip():
            print("[!] 点击发送后输入框未清空，疑似弹窗拦截，移除弹窗后重试...")
            dismiss_popups(page)
            try:
                page.ele(send_selector).click()
            except Exception:
                pass
            time.sleep(2)
            if (page.ele(input_selector).text or "").strip():
                # 点击仍无效，直接在输入框内回车发送
                print("[!] 点击仍未生效，改用回车键发送...")
                try:
                    page.actions.key_down("Enter").key_up("Enter")
                except Exception as e:
                    print(f"[!] 回车发送失败: [{type(e).__name__}] {e}")
                time.sleep(2)

        print("信息已发送，正在等待 AI 回复生成...\n")

        # 轮询等待回复气泡出现并产生内容（预算 60 秒）
        # 注意：eles() 自带默认 10 秒等待，必须显式传短超时，否则轮询被放大成 10 分钟
        latest_reply = None
        for _ in range(30):
            replies = page.eles("css:div.markdown-content", timeout=1)
            if replies and (replies[-1].text or "").strip():
                latest_reply = replies[-1]
                break
            time.sleep(1)
        if latest_reply is None:
            save_screenshot(page)
            raise RuntimeError("[!] 等待 60 秒仍未获得 AI 回复内容，已保存现场截图。")

        # 等待回复稳定：每轮重新定位最后一个回复元素（避免 DOM 重渲染使旧元素失效），文本连续 3 秒不变视为生成完毕
        previous_text = latest_reply.text
        stable_count = 0
        for _ in range(90):
            replies = page.eles("css:div.markdown-content", timeout=1)
            current_text = replies[-1].text if replies else ""
            if current_text and current_text == previous_text:
                stable_count += 1
            else:
                stable_count = 0
                previous_text = current_text
            if stable_count >= 3:
                break
            time.sleep(1)

        if not (previous_text or "").strip():
            raise RuntimeError("[!] 未得到回复。")
        print("=== AI 助手回复 ===")
        print(previous_text)
        print("\n===================\n")
        print("[*] 积分任务完成。")
    else:
        raise RuntimeError("[!] 未找到发送按钮。")


# ==========================================
# 主流程控制
# ==========================================


def main() -> None:
    login_url = (
        "https://desk.ctyun.cn/cloudB/dy/iam/api/auth/iam/cas/login?"
        "service=https%3A%2F%2Feaichat.ctyun.cn%3A443%2Fchat%2F%23%2Faichat&consent=false"
    )
    chat_url = "https://eaichat.ctyun.cn/chat/#/aichat"

    my_username = os.getenv("APP_USER")
    my_password = os.getenv("APP_PASSWORD")

    if not my_username or not my_password:
        print("错误：未检测到 APP_USER 或 APP_PASSWORD 环境变量。")
        sys.exit(1)

    # 动态构造 Cookie 文件路径，包含手机号
    # 格式：/app/data/ctyun_cookies_xxx_.json
    if os.getenv("RUNNING_IN_DOCKER") == "true":
        cookie_file = f"/app/data/ctyun_cookies_{my_username}_.json"
    else:
        cookie_file = f"./ctyun_cookies_{my_username}_.json"

    browser_options = init_browser_options()
    page = ChromiumPage(addr_or_opts=browser_options)
    # 页面加载超时 60 秒：防止站点响应慢时 page.get 长时间阻塞（默认可达 300 秒）
    try:
        page.set.timeouts(page_load=60)
    except Exception as e:
        print(f"[!] 设置页面加载超时失败（不影响继续）: [{type(e).__name__}] {e}")
    atexit.register(page.quit)
    attempt = 0
    max_retries = 3
    while attempt < max_retries:
        print(f"--- 对话尝试: {attempt + 1}/{max_retries} ---")
        try:
            is_logged_in = False

            # === 会话归属校验：无头浏览器配置文件可能残留其他账号的会话 ===
            # 此前只检查"聊天页能打开"，导致残留主卡会话时所有任务都顶着主卡身份聊天，
            # 而平台每账号每天只奖励一次 → 其他账号永远拿不到积分。
            print(f"正在建立域名上下文环境，准备使用账号 {my_username} 的缓存...")
            page.get(chat_url)
            time.sleep(1)

            if page.wait.ele_displayed("css:div.input-box.input-wrap", timeout=5):
                if user_matches(page, my_username):
                    print("[*] 会话仍有效且属于本账号，免登录继续任务！")
                    is_logged_in = True
                else:
                    print("[!] 检测到残留会话但不是本账号（%s），清除后重新登录..."
                          % (get_logged_in_user(page) or "未知用户"))
                    clear_browser_session(page)
                    page.get(chat_url)
                    time.sleep(1)

            if not is_logged_in and attempt <= 0:
                if load_cookies(page, cookie_file):
                    print("正在验证 Cookie 是否有效且属于本账号...")
                    page.get(chat_url)
                    if (page.wait.ele_displayed(
                            "css:div.input-box.input-wrap", timeout=5)
                            and user_matches(page, my_username)):
                        print(f"[*] 账号 {my_username} 免密登录成功！")
                        is_logged_in = True
                    else:
                        print("[-] Cookie 无效或不属于本账号，清除后准备账密登录...")
                        clear_browser_session(page)

            # === 登录流程 (如果 Cookie 无效) ===
            if not is_logged_in:
                is_success = execute_login_with_listener(
                    page, login_url, my_username, my_password
                )
                if is_success:
                    # 登录成功后保存到对应手机号的文件中
                    time.sleep(5)
                    save_cookies(page, cookie_file)
                    is_logged_in = True
                else:
                    print("[!] 自动化登录未能成功执行。")
                    sys.exit(1)

            # === 执行互动获取积分 ===
            if is_logged_in:
                chat_and_earn_points(page)
                print("\n对话任务已完成")
            break

        except Exception as e:
            attempt += 1
            print(f"[!] 执行过程中发生异常: [{type(e).__name__}] {e}")
            try:
                save_screenshot(page)
                print("[*] 已保存异常现场截图，便于排查。")
            except Exception:
                pass
            time.sleep(5)
    else:
        # 重试耗尽仍失败：非零退出，让调度器如实记录失败并触发告警
        print(f"[!] 连续 {max_retries} 次尝试均失败，任务终止。")
        sys.exit(1)


# ==========================================
# OCR 模块封装
# ==========================================


class NumericOcrSolver:
    """使用单例模式封装的数字 OCR 识别器。"""

    _instance: Optional["NumericOcrSolver"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "NumericOcrSolver":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_engine()
        return cls._instance

    def _init_engine(self) -> None:
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.ocr.set_ranges(0)

    def solve(self, image_data: bytes) -> str:
        try:
            return self.ocr.classification(image_data)
        except Exception as e:
            return f"Error: {str(e)}"


def get_bytes_numeric_captcha(image_bytes: bytes) -> str:
    solver = NumericOcrSolver()
    return solver.solve(image_bytes)


if __name__ == "__main__":
    main()
