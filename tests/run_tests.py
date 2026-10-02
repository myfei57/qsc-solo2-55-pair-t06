#!/usr/bin/env python3
"""无 pytest 环境下的全量测试运行器（仅依赖标准库）。

先注入 ``tests.pytest_shim`` 作为 ``pytest``，再按 conftest 的依赖关系实例化
``platform`` / ``online`` / ``clock`` 等夹具，逐个执行 ``tests/test_*.py`` 里的
``test_*`` 函数。退出码与常规测试运行器一致：0 全过，1 有失败。
"""

from __future__ import annotations

import importlib
import inspect
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import pytest_shim  # noqa: E402

sys.modules["pytest"] = pytest_shim

# conftest 在 shim 就位之后再导入。
conftest = importlib.import_module("conftest")


class Capsys:
    """最小化的 capsys：运行器在每个用例期间重定向 stdout/stderr 到内存。"""

    def __init__(self, out, err) -> None:
        import io

        self._out = out
        self._err = err

    def readouterr(self):
        class Result:
            def __init__(self, out_text: str, err_text: str) -> None:
                self.out = out_text
                self.err = err_text

        out_text, err_text = self._out.getvalue(), self._err.getvalue()
        self._out.seek(0)
        self._out.truncate(0)
        self._err.seek(0)
        self._err.truncate(0)
        return Result(out_text, err_text)


class Scope:
    """每个用例一个夹具作用域，按需构造、按依赖顺序解析。"""

    def __init__(self) -> None:
        self._cache: dict[str, object] = {}
        self._tmp: tempfile.TemporaryDirectory | None = None

    def resolve(self, name: str):
        if name in self._cache:
            return self._cache[name]
        if name == "tmp_path":
            if self._tmp is None:
                self._tmp = tempfile.TemporaryDirectory()
            value = Path(self._tmp.name)
        elif name == "capsys":
            import io

            value = Capsys(io.StringIO(), io.StringIO())
        else:
            factory = getattr(conftest, name)
            kwargs = {}
            for dep in inspect.signature(factory).parameters:
                kwargs[dep] = self.resolve(dep)
            value = factory(**kwargs)
        self._cache[name] = value
        return value

    def cleanup(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()


def run() -> int:
    failures = 0
    passed = 0
    names = sorted(path.stem for path in TESTS_DIR.glob("test_*.py"))
    for module_name in names:
        module = importlib.import_module(module_name)
        tests = [
            (name, obj)
            for name, obj in inspect.getmembers(module, inspect.isfunction)
            if name.startswith("test_") and obj.__module__ == module.__name__
        ]
        for name, test in tests:
            scope = Scope()
            try:
                kwargs = {dep: scope.resolve(dep) for dep in inspect.signature(test).parameters}
                if "capsys" in kwargs:
                    caps = kwargs["capsys"]
                    import contextlib

                    with contextlib.redirect_stdout(caps._out), contextlib.redirect_stderr(caps._err):
                        test(**kwargs)
                else:
                    test(**kwargs)
                passed += 1
                print(f". {module_name}.{name}")
            except Exception:  # noqa: BLE001 - 运行器要兜住任何用例异常
                failures += 1
                print(f"F {module_name}.{name}")
                traceback.print_exc()
            finally:
                scope.cleanup()
    print(f"\n{passed} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
