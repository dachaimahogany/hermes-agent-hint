"""pytest 装载器：以「包」方式加载本仓库根目录下的 agent-hint 插件。

目录名本身可作为包名，但上游加载器走的是文件装载（自带命名空间），这里用
importlib 复刻同样的装载方式，保证相对导入（from . import _settings）可用。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load(name, path, submodule_search_locations=None):
    spec = importlib.util.spec_from_file_location(
        name, str(path), submodule_search_locations=submodule_search_locations)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def ah_plugin():
    return _load("ah_plugin", PLUGIN_DIR / "__init__.py", [str(PLUGIN_DIR)])
