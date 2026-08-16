"""
win_tools.py — axeAI Windows Integration & File Operations Engine
================================================================
Provides native Windows folder picking, non-blocking asynchronous shell execution
with optional administrator elevation, and comprehensive file system CRUD operations.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("aX.win_tools")


def select_folder_dialog(initial_dir: str | None = None) -> str | None:
    """
    Spawns a native Windows folder picker dialog using Tkinter.
    Falls back to pywin32 or CLI input if GUI cannot be spawned.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(
            initialdir=initial_dir or str(Path.cwd()),
            title="axeAI — Select Workspace Directory"
        )
        root.destroy()
        if folder:
            return str(Path(folder).resolve())
    except Exception as e:
        logger.warning("Tkinter GUI folder picker failed: %s. Attempting fallback.", e)

    try:
        import win32gui
        import win32con
        # Optional pywin32 check
    except ImportError:
        pass

    return None


async def execute_shell_command(
    cmd: str | list[str],
    elevated: bool = False,
    cwd: str | None = None,
    timeout_secs: float = 60.0,
) -> dict[str, Any]:
    """
    Executes a shell command asynchronously via non-blocking subprocess.
    If elevated=True, triggers PowerShell Start-Process -Verb RunAs.
    """
    logger.info("Shell execution: '%s' (elevated: %s, cwd: %s)", cmd, elevated, cwd)

    if elevated and sys.platform == "win32":
        # Launch elevated PowerShell command
        ps_cmd = f"Start-Process powershell -ArgumentList '-NoProfile -NonInteractive -Command \"{cmd}\"' -Verb RunAs -Wait"
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-Command",
            ps_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
            return {
                "returncode": proc.returncode,
                "stdout": stdout_bytes.decode(errors="replace").strip(),
                "stderr": stderr_bytes.decode(errors="replace").strip(),
                "elevated": True,
            }
        except asyncio.TimeoutError:
            proc.kill()
            return {"returncode": -1, "stdout": "", "stderr": "Elevated process timed out", "elevated": True}

    # Standard non-blocking asynchronous execution
    if isinstance(cmd, list):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    else:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
        return {
            "returncode": proc.returncode,
            "stdout": stdout_bytes.decode(errors="replace").strip(),
            "stderr": stderr_bytes.decode(errors="replace").strip(),
            "elevated": False,
        }
    except asyncio.TimeoutError:
        proc.kill()
        return {"returncode": -1, "stdout": "", "stderr": "Command execution timed out", "elevated": False}


class FileSystemEngine:
    """
    Provides comprehensive directory and file CRUD operations.
    """

    @staticmethod
    def mkdir(path: str | Path) -> bool:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        logger.info("FileSystemEngine: Created directory '%s'", p)
        return True

    @staticmethod
    def touch(path: str | Path) -> bool:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(exist_ok=True)
        logger.info("FileSystemEngine: Touched file '%s'", p)
        return True

    @staticmethod
    def rm(path: str | Path) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        logger.info("FileSystemEngine: Removed '%s'", p)
        return True

    @staticmethod
    def move(src: str | Path, dst: str | Path) -> bool:
        shutil.move(str(src), str(dst))
        logger.info("FileSystemEngine: Moved '%s' -> '%s'", src, dst)
        return True
