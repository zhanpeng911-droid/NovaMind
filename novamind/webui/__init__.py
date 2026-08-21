"""NovaMind WebUI（FastAPI 后端 + pywebview 桌面窗口）。"""

from .server import app
from .app import run_gui

__all__ = ["app", "run_gui"]
