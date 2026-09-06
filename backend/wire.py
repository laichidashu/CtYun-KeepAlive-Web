# -*- coding: utf-8 -*-
"""
WebSocket 保活协议的加密应答与报文解析（对应 C# Encryption.cs + SendInfo.cs）。

保活校验流程：服务端下发以 "REDQ"(0x52454451) 开头的二进制帧；
客户端取 data[16:]，从中偏移 32 起取 129 字节大端 RSA 公钥 N、偏移 163 起取 3 字节指数 E，
用 RSA-OAEP(SHA-1, label="") 加密空消息，前面拼 4 字节小端 AuthMechanism(=1) 后原样回发。
"""
import hashlib
import os
import struct


def mgf1(seed: bytes, mask_len: int) -> bytes:
    """MGF1 掩码生成（SHA-1）。"""
    mask = bytearray()
    counter = 0
    while len(mask) < mask_len:
        block = seed + struct.pack(">I", counter)
        mask.extend(hashlib.sha1(block).digest())
        counter += 1
    return bytes(mask[:mask_len])


class Encryption:
    """保活校验应答器（一次连接一个实例，与 C# 一致）。"""

    def __init__(self):
        self.auth_mechanism = 1

    def execute(self, data: bytes) -> bytes:
        payload = data[16:]
        # 公钥 N：偏移 32 起 129 字节，大端无符号
        n = int.from_bytes(payload[32:161], "big")
        # 指数 E：偏移 163 起 3 字节大端
        e3 = payload[163:166]
        e = (e3[0] << 16) | (e3[1] << 8) | e3[2]
        return self._encrypt_and_wrap(n, e)

    def _encrypt_and_wrap(self, n: int, e: int) -> bytes:
        key_len = 128  # 1024 位 RSA
        h_len = 20     # SHA-1
        db_len = key_len - h_len - 1

        # OAEP 填充：DB = lHash || PS || 0x01 || M（M 为空）
        # 注意：与 C# 实现逐字节对齐 —— 分隔符 1 写在 db[db_len - 1 - label_len - 1]
        seed = bytearray(os.urandom(20))
        l_hash = hashlib.sha1(b"").digest()
        db = bytearray(l_hash + b"\x00" * db_len)
        db[db_len - 1 - 0 - 1] = 1

        # MGF1 掩码
        db_mask = mgf1(bytes(seed), db_len)
        for k in range(db_len):
            db[k] ^= db_mask[k]
        seed_mask = mgf1(bytes(db), h_len)
        for k in range(h_len):
            seed[k] ^= seed_mask[k]

        # EM = 00 || maskedSeed || maskedDB
        em = b"\x00" + bytes(seed) + bytes(db)
        m = int.from_bytes(em, "big")
        c = pow(m, e, n)
        result = c.to_bytes(key_len, "big")  # to_bytes 自动左侧补零

        # 报文封装：4 字节小端 AuthMechanism + 密文
        return struct.pack("<I", self.auth_mechanism) + result

    def recover_seed(self, decrypted_block: bytes) -> bytes:
        masked_seed = decrypted_block[1:21]
        masked_db = decrypted_block[21:]
        seed_mask = mgf1(masked_db, 20)
        return bytes(a ^ b for a, b in zip(masked_seed, seed_mask))


class SendInfo:
    """
    通用二进制报文：Type(2 字节 LE) + Length(4 字节 LE) + Data(N 字节)。
    FromBuffer 支持一帧内多个连续报文；ToBuffer(True) 额外写入两个 4 字节长度头。
    """

    def __init__(self, type_: int = 0, data: bytes = b""):
        self.type = type_
        self.data = data

    @property
    def size(self) -> int:
        return len(self.data)

    def to_buffer(self, is_build_msg: bool) -> bytes:
        msg_length = 8 if is_build_msg else 0
        data_length = len(self.data)
        buffer = bytearray(2 + 4 + msg_length + data_length)

        # Type (ushort LE)
        buffer[0] = self.type & 0xFF
        buffer[1] = (self.type >> 8) & 0xFF
        # Size (int LE)
        size = msg_length + data_length
        buffer[2] = size & 0xFF
        buffer[3] = (size >> 8) & 0xFF
        buffer[4] = (size >> 16) & 0xFF
        buffer[5] = (size >> 24) & 0xFF

        if is_build_msg:
            struct.pack_into("<I", buffer, 6, data_length)
            struct.pack_into("<I", buffer, 10, 8)

        if data_length > 0:
            buffer[6 + msg_length:6 + msg_length + data_length] = self.data
        return bytes(buffer)

    @staticmethod
    def from_buffer(buffer: bytes):
        results = []
        if not buffer:
            return results
        offset = 0
        while offset + 6 <= len(buffer):
            type_ = buffer[offset] | (buffer[offset + 1] << 8)          # ushort LE
            data_length = struct.unpack_from("<i", buffer, offset + 2)[0]  # int32 LE
            if data_length < 0 or offset + 6 + data_length > len(buffer):
                # 半包/非法长度：剩余字节整体放入最后一个条目（与 C# 一致）
                remaining = len(buffer) - offset
                if remaining > 0:
                    results.append(SendInfo(type_, buffer[offset:offset + remaining]))
                break
            results.append(SendInfo(type_, buffer[offset + 6:offset + 6 + data_length]))
            offset += 6 + data_length
        return results
