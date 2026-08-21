"""DockerRuntime — Docker 容器内裸执行（骨架）。

吸收 Poirot `sandbox/runtimes/docker_runtime.py` 的接口语义，但用 docker CLI（docker exec）
实现，避免额外依赖 agent-sandbox。文件操作委托 exec_command 用容器内 shell 命令完成。

说明：真实运行需要 Docker 环境；本类保证接口与 LocalRuntime 一致，便于 provider 切换。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..exceptions import SandboxCommandError, SandboxRuntimeError
from ..types import GrepMatch


class DockerRuntime:
    """DockerRuntime — 容器内裸执行，docker exec 委托。"""

    def __init__(self, container_name: str, docker_cmd: str = "docker") -> None:
        self._container = container_name
        self._docker_cmd = docker_cmd

    def exec_command(self, command: str) -> str:
        """docker exec <container> sh -c '<command>'。"""
        try:
            result = subprocess.run(
                [self._docker_cmd, "exec", self._container, "sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except FileNotFoundError as exc:
            raise SandboxRuntimeError(f"docker not found: {self._docker_cmd}") from exc
        if result.returncode != 0:
            raise SandboxCommandError(
                f"command failed with exit code {result.returncode}: {result.stderr.strip()}",
                command=command,
                exit_code=result.returncode,
            )
        return result.stdout

    def read_file(self, path: str) -> str:
        return self.exec_command(f"cat {shlex.quote(path)}")

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        op = ">>" if append else ">"
        self.exec_command(f"mkdir -p {shlex.quote(str(Path(path).parent))}")
        self.exec_command(f"printf '%s' {shlex.quote(content)} {op} {shlex.quote(path)}")

    def list_dir(self, path: str, max_depth: int = 2, max_entries: int = 1000) -> list[str]:
        output = self.exec_command(f"find {shlex.quote(path)} -maxdepth {max_depth} -mindepth 1 2>/dev/null | head -n {max_entries}")
        base = path.rstrip("/")
        result = []
        for line in output.splitlines():
            if line.startswith(base + "/"):
                result.append(line[len(base) + 1:])
            elif line == base:
                result.append(".")
        return sorted(result)

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        output = self.exec_command(f"find {shlex.quote(path)} -path {shlex.quote(pattern)} 2>/dev/null | head -n {max_results}")
        base = path.rstrip("/")
        results = []
        for line in output.splitlines():
            if line.startswith(base + "/"):
                results.append(line[len(base) + 1:])
        return results, False

    def grep(self, path: str, pattern: str, *, glob: str | None = None, literal: bool = False, case_sensitive: bool = False, max_results: int = 100) -> tuple[list[GrepMatch], bool]:
        flags = "" if case_sensitive else " -i"
        fixed = " -F" if literal else ""
        scope = shlex.quote(str(Path(path) / (glob or "*")))
        output = self.exec_command(f"grep -rn{flags}{fixed} {shlex.quote(pattern)} {scope} 2>/dev/null | head -n {max_results}")
        matches = []
        for line in output.splitlines():
            if ":" not in line:
                continue
            file_part, _, rest = line.partition(":")
            if ":" in rest:
                line_num, _, text = rest.partition(":")
                try:
                    matches.append(GrepMatch(file_part, int(line_num), text.strip()[:300]))
                except ValueError:
                    continue
        return matches, False

    def download_file(self, path: str) -> bytes:
        """docker cp 容器内文件到宿主机临时文件，读取后清理。"""
        tmp_dir = tempfile.mkdtemp(prefix="novamind-dl-")
        tmp_file = os.path.join(tmp_dir, "download.bin")
        try:
            try:
                subprocess.run(
                    [self._docker_cmd, "cp", f"{self._container}:{path}", tmp_file],
                    check=True,
                    capture_output=True,
                    text=False,
                )
            except FileNotFoundError as exc:
                raise SandboxRuntimeError(f"docker not found: {self._docker_cmd}") from exc
            except subprocess.CalledProcessError as exc:
                raise SandboxCommandError(
                    f"docker cp failed with exit code {exc.returncode}: "
                    f"{(exc.stderr or b'').decode('utf-8', errors='replace').strip()}",
                    command=f"docker cp {self._container}:{path}",
                    exit_code=exc.returncode,
                ) from exc
            with open(tmp_file, "rb") as f:
                return f.read()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def update_file(self, path: str, content: bytes) -> None:
        self.write_file(path, content.decode("utf-8", errors="replace"), append=False)

    def close(self) -> None:
        pass
