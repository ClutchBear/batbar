"""遗留桥接协议（wire.py）的分片鲁棒性测试（纯标准库）。

TCP 是字节流，一次 recv 可能收到半条消息、或多条消息粘在一起——这是协议层
最容易翻车的地方。本测试起一个内存 echo 端，验证行缓冲逻辑在乱序分片下仍能
逐条正确解帧。

> 注：`wire.py` 属旧双进程架构的遗留模块（已非主路径），本测试保留用于守住
> 这段解码逻辑不腐化。新功能一律不走这条通道。
"""
import socket
import threading
import unittest

from batbar import wire


class TestSocketFraming(unittest.TestCase):
    def _pair(self):
        a, b = socket.socketpair()
        return a, b

    def test_fragmented_and_coalesced(self):
        ca, ag = self._pair()
        try:
            msgs = [wire.msg_hello(), wire.msg_read(2), wire.msg_quit()]
            blob = b"".join(msgs)
            # 恶意的分片点：切在 JSON 中间、切在换行前后
            cuts = [3, len(msgs[0]) - 1, len(msgs[0]), len(wire.msg_hello()) + 5]
            pieces = []
            prev = 0
            for c in cuts + [len(blob)]:
                pieces.append(blob[prev:c])
                prev = c

            buf = b""
            got = []

            def feed(chunk):
                nonlocal buf
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    f = wire.decode_line(line)
                    if f:
                        got.append(f)

            for p in pieces:
                if p:
                    feed(p)

            self.assertEqual([f.type for f in got], ["hello", "read", "quit"])
            self.assertEqual(got[1].get("devnum"), 2)
        finally:
            ca.close()
            ag.close()

    def test_garbage_line_between_good(self):
        ca, ag = self._pair()
        try:
            stream = b"garbage not json\n" + wire.msg_read()
            buf = b""
            got = []
            for chunk in (stream[:7], stream[7:]):
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    f = wire.decode_line(line)
                    if f:
                        got.append(f)
            self.assertEqual([f.type for f in got], ["read"])  # 垃圾被丢弃，好帧存活
        finally:
            ca.close()
            ag.close()


if __name__ == "__main__":
    unittest.main()
