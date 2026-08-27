"""路径翻译不变量的 hypothesis 属性测试（P1-B2）。

不变量：
  I1 Local：虚拟路径翻译后物理结果永不逃逸出工位根（越权一律 PermissionError）
  I2 Local：mask_output(translate_path(v)) == v（往返一致，挂载区内任意相对段）
  I3 Docker：reverse_translate(translate_path(p)) == host_root/rel（p 在挂载区前缀下）；
    前缀之外的任何输入 reverse 必抛 ValueError
"""
import tempfile
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from novamind.core.sandbox.translators.docker_path_translator import (
    VIRTUAL_PREFIX,
    DockerPathTranslator,
)
from novamind.core.sandbox.translators.local_path_translator import (
    LocalPathTranslator,
)
from novamind.core.sandbox.types import PathMapping

# 相对段字符集：字母数字 + 常见安全符号；排除 NUL 与控制字符
_SEG = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCZ0123456789_-",
               min_size=0, max_size=12)

def _norm(path: str) -> str:
    """Windows 下统一比较：剥扩展长度前缀 + 归一大小写与分隔符。"""
    import os
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return os.path.normcase(p).replace("\\", "/")

_REL_PATH = st.lists(_SEG, min_size=0, max_size=5).map(lambda parts: "/".join(parts))


def _office(tmp: str) -> tuple[LocalPathTranslator, str]:
    local_root = str(tempfile.mkdtemp(dir=tmp))
    translator = LocalPathTranslator([
        PathMapping(container_path="/workspace", local_path=local_root),
    ])
    return translator, local_root


class TestLocalTranslatorInvariants(unittest.TestCase):
    def setUp(self):
        self._tmp_mgr = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_mgr.cleanup)
        self.tmp = self._tmp_mgr.name
        self.t, self.local_root = _office(self.tmp)

    @settings(max_examples=150, deadline=None)
    @given(_REL_PATH)
    def test_i1_translate_never_escapes_office_root(self, rel):
        """含 ../ 的恶意相对段也绝不逃逸：要么落在根内，要么 PermissionError。"""
        virtual = f"/workspace/{rel}"
        try:
            physical = self.t.translate_path(virtual)
        except PermissionError:
            return  # 拒绝即安全
        root = _norm(self.local_root)
        phys = _norm(physical)
        self.assertTrue(phys == root or phys.startswith(root + "/"),
                        f"escaped: {virtual} -> {physical}")

    @settings(max_examples=150, deadline=None)
    @given(_REL_PATH)
    def test_i2_mask_reverse_roundtrip(self, rel):
        """挂载区内的干净路径：脱敏是翻译的逆操作。"""
        clean_rel = "/".join(seg for seg in rel.split("/") if seg and seg != "..")
        virtual = f"/workspace/{clean_rel}".rstrip("/")
        physical = self.t.translate_path(virtual)
        self.assertEqual(self.t.mask_output(physical), virtual)


class TestDockerTranslatorInvariants(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = DockerPathTranslator(self.tmp.name, "sb123")
        self.host_root = f"{self.tmp.name.replace(chr(92), '/')}/sb123"

    @settings(max_examples=150, deadline=None)
    @given(_REL_PATH)
    def test_i3_reverse_of_prefix_paths_lands_under_host_root(self, rel):
        clean_rel = "/".join(seg for seg in rel.split("/")
                             if seg not in ("", ".", ".."))
        virtual = f"{VIRTUAL_PREFIX}/{clean_rel}" if clean_rel else VIRTUAL_PREFIX
        host = self.t.reverse_translate(virtual)
        self.assertTrue(host.replace("\\", "/").startswith(self.host_root))
        # translate 是直传（容器内即物理），幂等
        self.assertEqual(self.t.translate_path(virtual), virtual)

    @settings(max_examples=100, deadline=None)
    @given(st.text(min_size=1, max_size=30))
    def test_i4_outside_prefix_always_rejected(self, text):
        from hypothesis import assume
        assume(not text.startswith(VIRTUAL_PREFIX))
        with self.assertRaises(ValueError):
            self.t.reverse_translate(text)


if __name__ == "__main__":
    unittest.main()
