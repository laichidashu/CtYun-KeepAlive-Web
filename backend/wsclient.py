# -*- coding: utf-8 -*-
"""
最小 RFC 6455 WebSocket 客户端（仅标准库，满足保活连接所需子集）。
- wss://（TLS，SNI）与 ws://
- 请求头：Origin、Sec-WebSocket-Protocol: binary
- 发送：文本 / 二进制帧（客户端帧必须掩码）
- 接收：自动处理 ping→pong、分片重组、close；带超时的 recv（用于周期重连）
"""
import base64
import hashlib
import os
import socket
import ssl
import struct

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    pass


class WSClosed(WebSocketError):
    pass


class WSTimeout(WebSocketError):
    pass


class WSClient:
    def __init__(self):
        self._sock = None

    # ---------- 连接 ----------

    def connect(self, url: str, origin: str = "", subprotocol: str = "", connect_timeout: float = 15.0):
        """url 形如 wss://host[:port]/path。失败抛 WebSocketError。"""
        if not url.lower().startswith(("wss://", "ws://")):
            raise WebSocketError("仅支持 ws/wss 地址：" + url)
        secure = url.lower().startswith("wss://")
        rest = url[len("wss://" if secure else "ws://"):]
        slash = rest.find("/")
        hostport = rest[:slash] if slash >= 0 else rest
        path = rest[slash:] if slash >= 0 else "/"

        host, port = hostport, (443 if secure else 80)
        if ":" in hostport:
            host, _, port_s = hostport.partition(":")
            port = int(port_s or (443 if secure else 80))

        raw = socket.create_connection((host, port), timeout=connect_timeout)
        raw.settimeout(connect_timeout)
        if secure:
            ctx = ssl.create_default_context()
            self._sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            self._sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        headers = [
            "GET %s HTTP/1.1" % path,
            "Host: %s" % hostport,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        if origin:
            headers.append("Origin: %s" % origin)
        if subprotocol:
            headers.append("Sec-WebSocket-Protocol: %s" % subprotocol)
        self._sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())

        # 读响应头（逐字节直到 \r\n\r\n，避免吃掉帧数据）
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                self.close()
                raise WebSocketError("握手时连接被关闭")
            resp += chunk
            if len(resp) > 65536:
                self.close()
                raise WebSocketError("握手响应过大")

        head = resp.split(b"\r\n\r\n", 1)
        status_line = head[0].decode("latin-1", "replace")
        if " 101 " not in status_line:
            self.close()
            raise WebSocketError("握手失败：" + status_line.split("\r\n")[0])
        headers_map = {}
        for line in head[0].decode("latin-1", "replace").split("\r\n")[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers_map[k.strip().lower()] = v.strip()
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if headers_map.get("sec-websocket-accept", "") != expected:
            self.close()
            raise WebSocketError("Sec-WebSocket-Accept 校验失败")

        # 若响应携带了多余字节（极少见），缓存起来
        self._leftover = head[1] if len(head) > 1 else b""

    # ---------- 发送 ----------

    def _send_frame(self, opcode: int, payload: bytes):
        if self._sock is None:
            raise WSClosed("连接已关闭")
        mask = os.urandom(4)
        header = bytearray()
        header.append(0x80 | opcode)  # FIN=1
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def send_text(self, text: str):
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes):
        self._send_frame(OP_BINARY, data)

    # ---------- 接收 ----------

    def _recv_exact(self, n: int, deadline) -> bytes:
        """读到恰好 n 字节；带全局截止时间。"""
        buf = bytearray()
        while len(buf) < n:
            remaining = None if deadline is None else deadline - __import__("time").monotonic()
            if remaining is not None and remaining <= 0:
                raise WSTimeout("接收超时（周期结束）")
            self._sock.settimeout(2.0 if remaining is None else min(2.0, remaining))
            try:
                chunk = self._sock.recv(n - len(buf))
            except socket.timeout:
                continue
            except (ssl.SSLError, OSError) as ex:
                if isinstance(ex, ssl.SSLError) and "timed out" in str(ex):
                    continue
                raise WSClosed("连接中断：%s" % ex)
            if not chunk:
                raise WSClosed("连接被对端关闭")
            buf.extend(chunk)
        return bytes(buf)

    def recv_message(self, deadline=None):
        """
        收取一条完整消息（自动处理控制帧与分片）。
        返回 (opcode, payload)。deadline 为 time.monotonic() 时刻，超时抛 WSTimeout。
        """
        fragments = []
        frag_opcode = 0
        while True:
            hdr = self._recv_exact(2, deadline)
            fin = hdr[0] & 0x80
            opcode = hdr[0] & 0x0F
            masked = hdr[1] & 0x80
            length = hdr[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2, deadline))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8, deadline))[0]
            mask = self._recv_exact(4, deadline) if masked else None
            payload = self._recv_exact(length, deadline) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self._send_frame(OP_CLOSE, b"")
                except Exception:
                    pass
                raise WSClosed("对端发送了关闭帧")

            if opcode == OP_CONT:
                if not fragments:
                    raise WebSocketError("意外的 continuation 帧")
                fragments.append(payload)
            else:
                if fin:
                    return opcode, payload
                fragments = [payload]
                frag_opcode = opcode

            if fragments and fin:
                return frag_opcode, b"".join(fragments)

    # ---------- 关闭 ----------

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))  # close 帧（掩码）
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
