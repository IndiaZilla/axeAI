"""
compute_sandbox.py — axeAI Compute Engine & Multi-Language Sandbox
==================================================================
Provides in-memory exact symbolic/numeric mathematical evaluation (via SymPy/NumPy)
and a multi-language script execution sandbox (.py, .ps1, .bat, .js).
"""

import asyncio
import logging
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import sympy as sp

from win_tools import execute_shell_command

logger = logging.getLogger("aX.compute_sandbox")


class MathEngine:
    """
    In-memory exact symbolic and numeric mathematical calculator.
    """

    @staticmethod
    def evaluate_expression(expr_str: str) -> dict[str, Any]:
        """
        Parses and evaluates a mathematical expression symbolically and numerically.
        Supports algebra, calculus, integration, derivatives, and matrix operations.
        """
        try:
            parsed = sp.sympify(expr_str)
            simplified = sp.simplify(parsed)
            try:
                numeric_val = float(simplified.evalf())
            except Exception:
                numeric_val = None

            return {
                "success": True,
                "input": expr_str,
                "symbolic": str(simplified),
                "latex": sp.latex(simplified),
                "numeric": numeric_val,
            }
        except Exception as e:
            logger.error("MathEngine evaluation error for '%s': %s", expr_str, e)
            return {"success": False, "input": expr_str, "error": str(e)}

    @staticmethod
    def calculate_zscore_anomalies(data: list[float], threshold: float = 2.5) -> dict[str, Any]:
        """
        Detects anomalies using NumPy z-score calculation.
        """
        try:
            arr = np.array(data, dtype=np.float64)
            if len(arr) == 0:
                return {"success": False, "error": "Empty dataset"}

            mean = float(np.mean(arr))
            std = float(np.std(arr))
            if std == 0:
                z_scores = np.zeros_like(arr)
            else:
                z_scores = (arr - mean) / std

            anomalies = [
                {"index": int(i), "value": float(arr[i]), "z_score": float(z_scores[i])}
                for i in range(len(arr))
                if abs(z_scores[i]) >= threshold
            ]

            return {
                "success": True,
                "mean": mean,
                "std": std,
                "anomalies_detected": len(anomalies),
                "anomalies": anomalies,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class CodeSandbox:
    """
    Unified multi-language script execution runner (.py, .ps1, .bat, .js).
    """

    SUPPORTED_EXTENSIONS = {".py", ".ps1", ".bat", ".cmd", ".js"}

    @classmethod
    async def execute_script(
        cls,
        code_or_file: str,
        ext: str = ".py",
        cwd: str | None = None,
        timeout_secs: float = 30.0,
    ) -> dict[str, Any]:
        """
        Executes a script file or raw code snippet in an isolated non-blocking subprocess.
        """
        p = Path(code_or_file)
        temp_file = None

        if p.exists() and p.is_file():
            target_path = str(p.resolve())
            extension = p.suffix.lower()
        else:
            extension = ext if ext.startswith(".") else f".{ext}"
            temp_file = tempfile.NamedTemporaryFile("w", suffix=extension, delete=False, encoding="utf-8")
            temp_file.write(code_or_file)
            temp_file.flush()
            temp_file.close()
            target_path = temp_file.name

        try:
            if extension == ".py":
                cmd = [sys.executable, target_path]
            elif extension in {".ps1"}:
                cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", target_path]
            elif extension in {".bat", ".cmd"}:
                cmd = ["cmd.exe", "/c", target_path]
            elif extension == ".js":
                cmd = ["node", target_path]
            else:
                return {"success": False, "error": f"Unsupported script extension: {extension}"}

            res = await execute_shell_command(cmd, cwd=cwd, timeout_secs=timeout_secs)
            res["success"] = (res["returncode"] == 0)
            return res
        finally:
            if temp_file and os.path.exists(target_path):
                try:
                    os.unlink(target_path)
                except Exception:
                    pass
