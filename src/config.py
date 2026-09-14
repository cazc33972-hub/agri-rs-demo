"""读取 config.yaml，提供点号路径访问。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Cfg:
    """薄封装，支持 cfg.get('a.b.c', default) 这种点号路径取值。"""

    def __init__(self, data: dict):
        self._data = data

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return default if node is None else node

    def require(self, path: str) -> Any:
        value = self.get(path, None)
        if value is None:
            raise KeyError("配置缺少必填项: " + path)
        return value

    def resolve(self, path: str, default: str | None = None) -> Path:
        """把配置里的相对路径解析成项目内绝对路径。"""
        raw = self.get(path, default)
        if raw is None:
            raise KeyError("配置缺少路径: " + path)
        p = Path(str(raw))
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def raw(self) -> dict:
        return self._data


def load_config(path: str | Path = "config/config.yaml") -> Cfg:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    with open(p, "r", encoding="utf-8") as fh:
        return Cfg(yaml.safe_load(fh))
