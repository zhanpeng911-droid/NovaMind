"""LocalRuntime — subprocess 裸执行 + Python 标准库文件操作。

吸收 Poirot `sandbox/runtimes/local_runtime.py`：只负责裸执行，不知路径翻译、不做安全检查。
融合 NovaMind 的超时（60s）与读文件输出截断（防爆 Token）。

INVARIANT:
- exec_command 用 subprocess.run(shell=True)，CalledProcessError → SandboxCommandError
- allow_host_bash=False 时 exec_command 直接 raise（零信任加固）
- 文件操作包装 FileNotFoundError → SandboxFileNotFoundError，PermissionError → SandboxPermissionError
- list_dir BFS 剪枝，防 node_modules 类大目录 DoS
- grep 内置 ReDoS 防护（拒绝超长 pattern + 嵌套量词）
- close no-op
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

from ..exceptions import (
    SandboxCommandError,
    SandboxFileNotFoundError,
    SandboxPermissionError,
    SandboxRuntimeError,
)
from ..types import GrepMatch

_EXEC_TIMEOUT_SECONDS = 60
_MAX_READ_CHARS = 10000  # NovaMind 输出截断：防超大文件撑爆 Token

# ReDoS 防护
_MAX_REGEX_PATTERN_LENGTH = 200
_NESTED_QUANTIFIER_RE = re.compile(r"\([^)]*[+*?][^)]*\)[+*?]")

# grep / glob 忽略项
IGNORE_PATTERNS = (".git", ".venv", "venv", "node_modules", "__pycache__", "*.pyc", ".pytest_cache")
DEFAULT_MAX_FILE_SIZE_BYTES = 1_000_000
DEFAULT_LINE_SUMMARY_LENGTH = 300


def _validate_regex_pattern(pattern: str) -> None:
    """ReDoS 防护：拒绝超长 pattern + 嵌套量词（如 (a+)+）。"""
    if len(pattern) > _MAX_REGEX_PATTERN_LENGTH:
        raise ValueError(f"regex pattern too long ({len(pattern)} > {_MAX_REGEX_PATTERN_LENGTH} chars)")
    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise ValueError(f"potential ReDoS pattern (nested quantifier): {pattern[:50]}")


def _is_path_ignored(file_path: Path, root: Path) -> bool:
    """检查路径的任一部分是否匹配 IGNORE_PATTERNS。"""
    try:
        rel = file_path.relative_to(root)
    except ValueError:
        return False
    for part in rel.parts:
        for pat in IGNORE_PATTERNS:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


class LocalRuntime:
    """LocalRuntime — subprocess 裸执行 + 标准库文件操作。"""

    def __init__(self, allow_host_bash: bool = True, exec_timeout: int = _EXEC_TIMEOUT_SECONDS) -> None:
        self._allow_host_bash = allow_host_bash
        self._exec_timeout = exec_timeout

    def exec_command(self, command: str) -> str:
        if not self._allow_host_bash:
            raise SandboxRuntimeError("host bash is disabled (allow_host_bash=false)")
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._exec_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxCommandError("command timed out", command=command, exit_code=None) from exc
        if result.returncode != 0:
            raise SandboxCommandError(
                f"command failed with exit code {result.returncode}",
                command=command,
                exit_code=result.returncode,
            )
        return result.stdout

    def read_file(self, path: str) -> str:
        try:
            content = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SandboxFileNotFoundError(f"file not found: {path}", path=path, operation="read") from exc
        except PermissionError as exc:
            raise SandboxPermissionError(f"permission denied: {path}", path=path, operation="read") from exc
        if len(content) > _MAX_READ_CHARS:
            return content[:_MAX_READ_CHARS] + "\n\n...[内容过长，已被安全截断]..."
        return content

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            if append:
                with p.open("a", encoding="utf-8") as f:
                    f.write(content)
            else:
                p.write_text(content, encoding="utf-8")
        except PermissionError as exc:
            raise SandboxPermissionError(f"permission denied: {path}", path=path, operation="write") from exc

    def list_dir(self, path: str, max_depth: int = 2, max_entries: int = 1000) -> list[str]:
        try:
            root = Path(path)
            if not root.exists():
                raise SandboxFileNotFoundError(f"dir not found: {path}", path=path, operation="list_dir")
            entries: list[str] = []
            self._scan_bfs(root, root, 1, max_depth, max_entries, entries)
            return sorted(entries)
        except PermissionError as exc:
            raise SandboxPermissionError(f"permission denied: {path}", path=path, operation="list_dir") from exc

    @staticmethod
    def _scan_bfs(root, current, depth, max_depth, max_entries, entries):
        if depth > max_depth or len(entries) >= max_entries:
            return
        try:
            with os.scandir(current) as it:
                for entry in sorted(it, key=lambda e: e.name):
                    if len(entries) >= max_entries:
                        return
                    rel = str(Path(entry.path).relative_to(root))
                    entries.append(rel)
                    if entry.is_dir() and depth < max_depth:
                        LocalRuntime._scan_bfs(root, Path(entry.path), depth + 1, max_depth, max_entries, entries)
        except (PermissionError, OSError):
            pass  # 跳过不可读目录

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        try:
            root = Path(path)
            matches: list[str] = []
            for item in root.rglob(pattern):
                if not include_dirs and item.is_dir():
                    continue
                matches.append(str(item.relative_to(root)))
                if len(matches) >= max_results:
                    return matches, True
            return matches, False
        except PermissionError as exc:
            raise SandboxPermissionError(f"permission denied: {path}", path=path, operation="glob") from exc

    def grep(self, path: str, pattern: str, *, glob: str | None = None, literal: bool = False, case_sensitive: bool = False, max_results: int = 100) -> tuple[list[GrepMatch], bool]:
        try:
            root = Path(path)
            flags = 0 if case_sensitive else re.IGNORECASE
            if literal:
                regex = re.compile(re.escape(pattern), flags)
            else:
                _validate_regex_pattern(pattern)
                regex = re.compile(pattern, flags)
            matches: list[GrepMatch] = []
            for file_path in root.rglob(glob or "*"):
                if file_path.is_dir():
                    continue
                if _is_path_ignored(file_path, root):
                    continue
                try:
                    stat = file_path.stat()
                except OSError:
                    continue
                if stat.st_size > DEFAULT_MAX_FILE_SIZE_BYTES:
                    continue
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                except (PermissionError, OSError):
                    continue
                for line_num, line in enumerate(content.splitlines(), start=1):
                    if regex.search(line):
                        matches.append(GrepMatch(str(file_path), line_num, line[:DEFAULT_LINE_SUMMARY_LENGTH]))
                        if len(matches) >= max_results:
                            return matches, True
            return matches, False
        except PermissionError as exc:
            raise SandboxPermissionError(f"permission denied: {path}", path=path, operation="grep") from exc

    def make_dir(self, path: str) -> None:
        try:
            os.makedirs(path, exist_ok=True)
        except PermissionError as exc:
            raise SandboxPermissionError(f"permission denied: {path}", path=path, operation="make_dir") from exc

    def list_dir_typed(self, path: str, max_entries: int = 1000) -> list[tuple[str, bool]]:
        root = Path(path)
        if not root.exists():
            raise SandboxFileNotFoundError(f"dir not found: {path}", path=path, operation="list_dir")
        try:
            out: list[tuple[str, bool]] = []
            with os.scandir(root) as it:
                for entry in sorted(it, key=lambda e: e.name):
                    if len(out) >= max_entries:
                        break
                    out.append((entry.name, entry.is_dir()))
            return out
        except PermissionError as exc:
            raise SandboxPermissionError(f"permission denied: {path}", path=path, operation="list_dir") from exc

    def download_file(self, path: str) -> bytes:
        try:
            return Path(path).read_bytes()
        except FileNotFoundError as exc:
            raise SandboxFileNotFoundError(f"file not found: {path}", path=path, operation="download") from exc

    def update_file(self, path: str, content: bytes) -> None:
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        except PermissionError as exc:
            raise SandboxPermissionError(f"permission denied: {path}", path=path, operation="update") from exc

    def close(self) -> None:
        pass
