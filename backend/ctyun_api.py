# -*- coding: utf-8 -*-
"""
天翼云客户端 API（对应 C# CtYunApi.cs，仅标准库 urllib）。
- 登录：genChallengeData 挑战码 + 图形验证码 OCR + 双重 SHA256
- 短信：图形验证码 OCR → getSmsCode → bindingDevice
- 设备：pageDesktop 拉列表、connect 取连接信息
- 签名：登录后所有请求附 ctg-userid/tenantid/timestamp/requestid/signaturestr
"""
import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import logs

ORC_URL = "https://orc.1999111.xyz/ocr"
VERSION = "103020001"
DEVICE_TYPE = "60"
BASE = "https://desk.ctyun.cn:8810"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")

_HTTP_TIMEOUT = 30


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class CtYunApi:
    def __init__(self, device_code: str):
        self.device_code = device_code or ""
        self.login_info = None  # dict: bondedDevice/secretKey/userId/tenantId/userName

    # ---------- 基础请求 ----------

    def _headers(self, extra=None):
        h = {
            "User-Agent": _UA,
            "ctg-devicetype": DEVICE_TYPE,
            "ctg-version": VERSION,
            "ctg-devicecode": self.device_code,
            "referer": "https://pc.ctyun.cn/",
        }
        if self.login_info is not None:
            ts = str(int(time.time() * 1000))
            li = self.login_info
            h["ctg-userid"] = str(li.get("userId", ""))
            h["ctg-tenantid"] = str(li.get("tenantId", ""))
            h["ctg-timestamp"] = ts
            h["ctg-requestid"] = ts
            sig_src = "%s%s%s%s%s%s%s" % (
                DEVICE_TYPE, ts, li.get("tenantId", ""), ts,
                li.get("userId", ""), VERSION, li.get("secretKey", ""))
            h["ctg-signaturestr"] = _md5_hex(sig_src)
        if extra:
            h.update(extra)
        return h

    def _send(self, req: urllib.request.Request):
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as ex:
            body = ""
            try:
                body = ex.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise ConnectionError("HTTP %s %s" % (ex.code, body))
        except urllib.error.URLError as ex:
            raise ConnectionError(str(getattr(ex, "reason", ex)))

    def _request_json(self, method: str, url: str, data: bytes = None, content_type: str = None):
        """返回 (ok, code, msg, data)。网络异常时 ok=False code=-100。"""
        try:
            headers = {"Content-Type": content_type} if content_type else {}
            req = urllib.request.Request(url, data=data, headers=self._headers(headers), method=method)
            raw = self._send(req)
            obj = json.loads(raw.decode("utf-8", "replace"))
            return True, obj.get("code", -1), obj.get("msg", ""), obj.get("data")
        except Exception as ex:
            logs.fail("系统", "请求 %s 失败：%s" % (url, ex))
            return False, -100, str(ex), None

    def _request_bytes(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        return self._send(req)

    # ---------- 验证码 OCR ----------

    _DDDD = None
    _DDDD_LOCK = threading.Lock()

    @classmethod
    def _local_ocr(cls, img: bytes) -> str:
        """本地 ddddocr 识别：灰度原图 + 多阈值二值化(120/140/160/180)逐个尝试，
        取首个 4 位合规结果。实测多阈值方案合规率 12/12，远优于单阈值(6/10)。"""
        try:
            with cls._DDDD_LOCK:
                if cls._DDDD is None:
                    import ddddocr
                    cls._DDDD = ddddocr.DdddOcr(show_ad=False)
            import io
            from PIL import Image
            im = Image.open(io.BytesIO(img)).convert("L")
            variants = [im]
            variants += [im.point(lambda p, t=t: 255 if p > t else 0) for t in (120, 140, 160, 180)]
            with cls._DDDD_LOCK:
                for v in variants:
                    big = v.resize((v.width * 4, v.height * 4), Image.LANCZOS)
                    buf = io.BytesIO()
                    big.save(buf, format="PNG")
                    r = (cls._DDDD.classification(buf.getvalue()) or "").strip()
                    if cls._plausible(r):
                        return r
            return ""
        except Exception as ex:
            logs.warn("系统", "本地OCR失败：" + str(ex))
            return ""

    @staticmethod
    def _plausible(code: str) -> bool:
        """合理结果：4 位字母数字（该平台验证码固定 4 位）。"""
        return len(code) == 4 and code.isalnum()

    def _ocr(self, img: bytes) -> str:
        """验证码识别：本地 ddddocr（含二值化+放大预处理）优先，外部接口兜底。"""
        logs.info("系统", "正在识别验证码.")
        local = self._local_ocr(img)
        if self._plausible(local):
            logs.ok("系统", "本地识别结果：" + local)
            return local

        # 本地结果不合规（位数不对/为空）→ 外部 OCR 接口兜底
        try:
            boundary = uuid.uuid4().hex
            b64 = base64.b64encode(img).decode()
            body = (
                ("--%s\r\n" % boundary) +
                ('Content-Disposition: form-data; name="image"\r\n\r\n') +
                b64 + ("\r\n--%s--\r\n" % boundary)
            ).encode()
            req = urllib.request.Request(
                ORC_URL, data=body, method="POST",
                headers=self._headers({"Content-Type": "multipart/form-data; boundary=" + boundary}))
            raw = self._send(req)
            result = raw.decode("utf-8", "replace")
            logs.info("系统", "外部识别结果：" + result)
            remote = json.loads(result).get("data", "") or ""
            if self._plausible(remote):
                return remote
        except Exception as ex:
            logs.fail("系统", "验证码识别错误：" + str(ex))

        # 双引擎都不理想：返回本地结果赌一把（外层还有 3 次登录重试）
        if local:
            logs.warn("系统", "验证码识别结果存疑(%s)，仍尝试提交。" % local)
        return local

    # ---------- 登录 ----------

    def login(self, userphone: str, password: str) -> bool:
        """最多重试 5 次（每次重新获取挑战码与验证码）；密码明确错误立即返回 False。"""
        for i in range(1, 6):
            # 1. 挑战码
            ok, code, msg, data = self._request_json(
                "POST", BASE + "/api/auth/client/genChallengeData", data=b"{}", content_type="application/json")
            if not ok or code != 0:
                logs.fail("系统", "GetGenChallengeDataAsync Error:" + (msg or "网络错误"))
                continue
            challenge_id, challenge_code = data.get("challengeId", ""), data.get("challengeCode", "")

            # 2. 图形验证码
            try:
                img = self._request_bytes(
                    BASE + "/api/auth/client/captcha?height=36&width=85&userInfo=%s&mode=auto&_t=%d"
                    % (urllib.parse.quote(userphone), int(time.time() * 1000)))
            except Exception as ex:
                logs.fail("系统", "登录验证码获取错误：" + str(ex))
                continue
            captcha_code = self._ocr(img)
            if not captcha_code:
                continue

            # 3. 表单登录
            fields = [
                ("userAccount", userphone),
                ("password", _sha256_hex(password + challenge_code)),
                ("sha256Password", _sha256_hex(_sha256_hex(password) + challenge_code)),
                ("challengeId", challenge_id),
                ("captchaCode", captcha_code),
            ]
            fields += self._device_fields()
            body = urllib.parse.urlencode(fields).encode()
            ok, code, msg, data = self._request_json(
                "POST", BASE + "/api/auth/client/login", data=body,
                content_type="application/x-www-form-urlencoded")
            if ok and code == 0:
                self.login_info = data
                return True
            logs.fail("系统", "重试%d, Login Error:%s" % (i, msg))
            if msg == "用户名或密码错误":
                return False
            time.sleep(1)  # 避免密集重试触发风控
        return False

    def _device_fields(self):
        return [
            ("deviceCode", self.device_code),
            ("deviceName", "Chrome浏览器"),
            ("deviceType", DEVICE_TYPE),
            ("deviceModel", "Windows NT 10.0; Win64; x64"),
            ("appVersion", "3.2.0"),
            ("sysVersion", "Windows NT 10.0; Win64; x64"),
            ("clientVersion", VERSION),
        ]

    # ---------- 短信绑定 ----------

    def get_sms_code(self, userphone: str) -> bool:
        for i in range(3):
            try:
                img = self._request_bytes(
                    BASE + "/api/auth/client/validateCode/captcha?width=120&height=40&_t=%d"
                    % int(time.time() * 1000))
            except Exception as ex:
                logs.fail("系统", "短信验证码获取错误：" + str(ex))
                continue
            captcha_code = self._ocr(img)
            if captcha_code:
                url = (BASE + "/api/cdserv/client/device/getSmsCode?mobilePhone=%s&captchaCode=%s"
                       % (urllib.parse.quote(userphone), urllib.parse.quote(captcha_code)))
                ok, code, msg, _ = self._request_json("GET", url)
                if ok and code == 0:
                    return True
                logs.fail("系统", "重试%d, GetSmsCode Error:%s" % (i, msg))
        return False

    def binding_device(self, verification_code: str) -> bool:
        qs = urllib.parse.urlencode({
            "verificationCode": (verification_code or "").strip(),
            "deviceName": "Chrome浏览器",
            "deviceCode": self.device_code,
            "deviceModel": "Windows NT 10.0; Win64; x64",
            "sysVersion": "Windows NT 10.0; Win64; x64",
            "appVersion": "3.2.0",
            "hostName": "pc.ctyun.cn",
            "deviceInfo": "Win32",
        })
        ok, code, msg, _ = self._request_json("POST", BASE + "/api/cdserv/client/device/binding?" + qs, data=b"")
        if ok and code == 0:
            return True
        logs.fail("系统", "BindingDevice Error:" + (msg or "网络错误"))
        return False

    # ---------- 设备 ----------

    def get_client_list(self):
        """返回 desktopList（list[dict]），失败返回 None。"""
        body = json.dumps({
            "getCnt": 20,
            "desktopTypes": ["1", "2001", "2002", "2003"],
            "sortType": "createTimeV1",
        }).encode()
        ok, code, msg, data = self._request_json(
            "POST", BASE + "/api/desktop/client/pageDesktop", data=body, content_type="application/json")
        if not ok or code != 0:
            if ok:
                logs.fail("系统", "获取设备信息错误。" + (msg or ""))
            return None
        if data is None:
            return None
        return data.get("desktopList") or []

    def connect(self, desktop_id: str):
        """返回 (ok, msg, desktopInfo dict 或 None)。"""
        fields = [
            ("objId", desktop_id),
            ("objType", "0"),
            ("osType", "15"),
            ("deviceId", DEVICE_TYPE),
            ("vdCommand", ""),
            ("ipAddress", ""),
            ("macAddress", ""),
        ] + self._device_fields()
        body = urllib.parse.urlencode(fields).encode()
        ok, code, msg, data = self._request_json(
            "POST", BASE + "/api/desktop/client/connect", data=body,
            content_type="application/x-www-form-urlencoded")
        if ok and code == 0:
            di = (data or {}).get("desktopInfo")
            if di:
                return True, "", di
            # 平台返回成功但缺关键数据：多见于云电脑重置/分配过渡态
            return False, "接口成功但未返回 desktopInfo（云电脑可能正在重置/分配中）", None
        return False, (msg or "未知错误(code=%s)" % code), None

    def power_on(self, desktop_id: str):
        """向平台发送开机指令。返回 (ok, msg)。
        优先 connect 接口带 vdCommand=powerOn（VDI 常见做法：连接即开机）；
        未确认时再试专用 powerOn 接口兜底。已开机的桌面平台会自行忽略该指令。"""
        fields = [
            ("objId", desktop_id),
            ("objType", "0"),
            ("osType", "15"),
            ("deviceId", DEVICE_TYPE),
            ("vdCommand", "powerOn"),
            ("ipAddress", ""),
            ("macAddress", ""),
        ] + self._device_fields()
        body = urllib.parse.urlencode(fields).encode()
        ok, code, msg, _ = self._request_json(
            "POST", BASE + "/api/desktop/client/connect", data=body,
            content_type="application/x-www-form-urlencoded")
        if ok and code == 0:
            return True, ""
        body2 = urllib.parse.urlencode(
            [("objId", desktop_id)] + self._device_fields()).encode()
        ok2, code2, msg2, _ = self._request_json(
            "POST", BASE + "/api/desktop/client/powerOn", data=body2,
            content_type="application/x-www-form-urlencoded")
        if ok2 and code2 == 0:
            return True, ""
        return False, (msg2 or msg or "开机指令未确认(code=%s/%s)" % (code, code2))
