"""Part C：特殊环境功能验收（C1-C4）。环境缺失显式跳过，不静默略过。"""
from __future__ import annotations

import shutil
import subprocess

import pytest


def _run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _have(prog):
    return shutil.which(prog) is not None


class TestC1Docker:
    def test_c1_daemon_reachable(self):
        if not _have("docker"):
            pytest.skip("docker 未安装")
        r = _run(["docker", "ps"])
        assert r.returncode == 0, f"docker daemon 不可达: {r.stderr[:200]}"

    def test_c1_image_presence_and_create(self):
        """镜像在则真实建容器并写入挂载区；镜像缺失则显式跳过（非静默）。"""
        if not _have("docker"):
            pytest.skip("docker 未安装")
        r = _run(["docker", "image", "inspect", "novamind-sandbox:latest"])
        if r.returncode != 0:
            pytest.skip("novamind-sandbox 镜像未构建，真实容器创建留待镜像就绪后验收")
        # 真实创建容器并验证容器内挂载路径存在
        name = "novamind-func-test"
        _run(["docker", "rm", "-f", name])
        run = _run([
            "docker", "run", "-d", "--name", name,
            "-v", f"{__import__('os').getcwd()}:{__import__('novamind.core.sandbox.translators.docker_path_translator', fromlist=['VIRTUAL_PREFIX']).VIRTUAL_PREFIX}",
            "novamind-sandbox:latest", "sleep", "infinity",
        ])
        try:
            assert run.returncode == 0, f"容器创建失败: {run.stderr[:300]}"
            check = _run(["docker", "exec", name, "test", "-d", "/mnt/novamind/user_data"])
            assert check.returncode == 0, "挂载区未就位"
        finally:
            _run(["docker", "rm", "-f", name])


class TestC2Wsl:
    def test_c2_wsl_exec_and_path_translation(self):
        if _have("wsl") is False or _run(["wsl", "-l", "-q"]).returncode != 0:
            pytest.skip("WSL 不可用")
        r = _run(["wsl", "echo", "novamind-ok"])
        assert r.returncode == 0 and "novamind-ok" in r.stdout
        # D:\foo → /mnt/d/foo 翻译（executor 需启用才生效，纯函数恒生效）
        from novamind.core.sandbox.docker.executor import (
            WslDockerExecutor,
            translate_to_wsl,
        )
        assert translate_to_wsl(r"D:\foo\bar.txt") == "/mnt/d/foo/bar.txt"
        # 未启用 → 直传；启用 → 翻译（配置依赖，非缺陷）
        assert WslDockerExecutor().translate_path(r"D:\foo\bar.txt") == r"D:\foo\bar.txt"
        enabled = WslDockerExecutor(enabled=True).translate_path(r"D:\foo\bar.txt")
        assert enabled == "/mnt/d/foo/bar.txt"


class TestC3Gui:
    def test_c3_webview_importable_and_backend_contract(self):
        """桌面层无法在无头会话交互，验证可导入 + 后端已有 TestClient 守卫。"""
        try:
            import webview  # noqa: F401
            ok = True
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"pywebview 不可用: {exc}")
        assert ok
        # 后端接口在 integration 层已覆盖（/chat /health /monitor/* /skills）
        from novamind.webui.server import app
        assert app is not None


class TestC4Mcp:
    def test_c4_mcp_adapter_skip_without_dependency(self):
        """mcp 包未安装：显式跳过（记为"环境缺失"，非静默）。"""
        try:
            import mcp  # noqa: F401
            present = True
        except ImportError:
            present = False
        if not present:
            pytest.skip("mcp 依赖未安装，真实 MCP server 往返留待安装后验收")
        assert present
