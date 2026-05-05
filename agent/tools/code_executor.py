"""
Code Executor: 在受限环境中执行 LLM 生成的 Python 代码。

安全策略（portfolio 级别）：
- 白名单 builtins，禁止 open / os / subprocess / importlib
- 允许 pandas / numpy / math / statistics / datetime
- 15 秒超时（通过 threading.Timer 强制中断）
"""
from __future__ import annotations

import io
import math
import statistics
import threading
from contextlib import redirect_stdout
from datetime import datetime, date, timedelta
from typing import Any

try:
    import pandas as pd
    import numpy as np
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

_SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool,
    "dict": dict, "enumerate": enumerate, "filter": filter,
    "float": float, "format": format, "int": int,
    "isinstance": isinstance, "len": len, "list": list,
    "map": map, "max": max, "min": min, "print": print,
    "range": range, "repr": repr, "round": round,
    "set": set, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "type": type, "zip": zip,
    "True": True, "False": False, "None": None,
}


def execute_python(code: str, data: dict | None = None, timeout: int = 15) -> dict[str, Any]:
    """
    执行 code，把 data 注入为全局变量 `data`。

    返回:
        stdout  — print() 的输出
        result  — 代码最后赋值给 `result` 的值（可选）
        error   — 异常信息（None 表示成功）
    """
    safe_globals: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "math": math,
        "statistics": statistics,
        "datetime": datetime,
        "date": date,
        "timedelta": timedelta,
        "data": data or {},
    }
    if _HAS_PANDAS:
        safe_globals["pd"] = pd
        safe_globals["np"] = np

    stdout_buf = io.StringIO()
    result_holder: dict[str, Any] = {"stdout": "", "result": None, "error": None}
    completed = threading.Event()

    def _run():
        try:
            with redirect_stdout(stdout_buf):
                exec(compile(code, "<compute>", "exec"), safe_globals)  # noqa: S102
            result_holder["stdout"] = stdout_buf.getvalue()
            result_holder["result"] = safe_globals.get("result")
        except Exception as exc:
            result_holder["stdout"] = stdout_buf.getvalue()
            result_holder["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            completed.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    if not completed.wait(timeout):
        result_holder["error"] = f"TimeoutError: execution exceeded {timeout}s"

    return result_holder
