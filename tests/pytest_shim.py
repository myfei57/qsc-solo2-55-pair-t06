"""测试入口垫片：没有 pytest 时用标准库跑全部用例。

交付运行期零第三方依赖；验证环境若装了 pytest 仍由 pytest 原生接管。未安装时，
本垫片提供项目测试实际用到的那一小撮 ``pytest`` 名字（``raises``、
``approx``、``fixture``、``mark``），并把使用夹具的 ``conftest`` 风格模块改造为
标准库可运行的形式，从而 ``python3 -m unittest discover -s tests`` 即可全量验证。
"""

from __future__ import annotations

import contextlib
import math
import unittest

_SENTINEL = object()


class raises(contextlib.AbstractContextManager):
    """最小化的 ``pytest.raises``：断言块内抛出指定异常。"""

    def __init__(self, expected: type[BaseException]) -> None:
        self._expected = expected
        self.value: BaseException | None = None

    def __enter__(self) -> "raises":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            raise AssertionError(f"DID NOT RAISE {self._expected.__name__}")
        if not issubclass(exc_type, self._expected):
            return False
        self.value = exc
        return True


class approx:
    """最小化的 ``pytest.approx``：默认 1e-6 相对/绝对容差。"""

    def __init__(self, expected: float, rel: float | None = None, abs: float | None = None) -> None:
        self.expected = float(expected)
        self.rel = 1e-6 if rel is None else rel
        self.abs = 1e-6 if abs is None else abs

    def __eq__(self, other: object) -> bool:
        try:
            value = float(other)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return NotImplemented
        return math.isclose(value, self.expected, rel_tol=self.rel, abs_tol=self.abs)

    def __repr__(self) -> str:
        return f"approx({self.expected})"


class _Marker:
    def __getattr__(self, name: str):
        def decorator(obj=_SENTINEL, *args, **kwargs):
            if callable(obj):
                return obj

            def wrap(func):
                return func

            return wrap

        return decorator


class mark:
    skip = staticmethod(lambda reason=None: (lambda func: func))


def fixture(func=None, **kwargs):
    """把夹具函数原样暴露；垫片运行器在每个用例前解析同名夹具。"""

    if func is None:
        def wrapper(fn):
            fn._is_fixture = True
            return fn

        return wrapper
    func._is_fixture = True
    return func


Fixture = fixture


class FixtureTestCase(unittest.TestCase):
    """支持按形参名注入 conftest 夹具的用例基类。"""

    fixtures: dict[str, object] = {}

    def _callTestMethod(self, method) -> None:
        import inspect

        signature = inspect.signature(method)
        kwargs = {}
        for name in signature.parameters:
            if name in self.fixtures:
                kwargs[name] = self._resolve_fixture(name)
        method(**kwargs)

    def _resolve_fixture(self, name: str):
        import inspect
        import tempfile
        from pathlib import Path

        if name == "tmp_path":
            if not hasattr(self, "_tmp_dir"):
                self._tmp_dir = tempfile.TemporaryDirectory()
                self.addCleanup(self._tmp_dir.cleanup)
            return Path(self._tmp_dir.name)
        factory = self.fixtures[name]
        params = inspect.signature(factory).parameters
        kwargs = {}
        for dep in params:
            kwargs[dep] = self._resolve_fixture(dep)
        value = factory(**kwargs)
        if hasattr(value, "__enter__"):
            pass
        return value
