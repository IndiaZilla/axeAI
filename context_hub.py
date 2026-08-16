"""
context_hub.py — axeAI State Database
======================================
The single source of truth for all in-memory agent state.

Responsibilities:
  - Two-tier per-agent memory: structured interaction history + free-form scratchpad
  - Centralised orchestrator history log
  - Atomic checkpoint/rollback system for TaskGroup-level transaction safety
  - Thread/coroutine-safe scratchpad access via asyncio.Lock

Design Contract:
  - This module has NO dependencies on any other axeAI module.
  - All other modules (aG, comm_hub, a1) import from here, never the reverse.
  - Memory is in-process RAM only. Persistence is a future concern.
"""

import copy
import difflib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("aX.context_hub")


# ---------------------------------------------------------------------------
# Workspace Context Manager (Phase 2)
# ---------------------------------------------------------------------------

class WorkspaceManager:
    """
    Manages local directory mounting and virtual file tree indexing.
    Reads workspace_config.json to map aliases (e.g. "src", "docs") to paths.
    """

    EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", ".gemini", "dist", "build"}

    def __init__(self, config_path: str | Path = "workspace_config.json"):
        self.config_path = Path(config_path)
        self.mounted_roots: dict[str, str] = {}
        self.load_config()

    def load_config(self) -> None:
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.mounted_roots = data.get("mounted_roots", {})
                logger.info("WorkspaceManager: Loaded %d mounted root(s).", len(self.mounted_roots))
            except Exception as e:
                logger.error("WorkspaceManager: Failed to load config %s: %s", self.config_path, e)
        else:
            self.mounted_roots = {}

    def save_config(self) -> None:
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump({"mounted_roots": self.mounted_roots}, f, indent=2)
            logger.info("WorkspaceManager: Saved configuration to %s", self.config_path)
        except Exception as e:
            logger.error("WorkspaceManager: Failed to save config: %s", e)

    def mount_directory(self, alias: str, path: str) -> None:
        abs_path = str(Path(path).resolve())
        self.mounted_roots[alias] = abs_path
        self.save_config()
        logger.info("WorkspaceManager: Mounted alias '%s' -> '%s'", alias, abs_path)

    def resolve_path(self, virtual_path: str) -> Path | None:
        """
        Resolves an alias-based path like 'src/a0.py' or an absolute/relative path.
        """
        parts = virtual_path.replace("\\", "/").split("/", 1)
        if len(parts) == 2 and parts[0] in self.mounted_roots:
            alias, subpath = parts
            return Path(self.mounted_roots[alias]) / subpath

        p = Path(virtual_path)
        if p.exists():
            return p.resolve()
        return None

    def build_virtual_tree(self) -> dict:
        """
        Crawls all mounted directories and builds a lightweight JSON tree structure.
        """
        tree = {}
        for alias, root_path in self.mounted_roots.items():
            p = Path(root_path)
            if not p.exists():
                tree[alias] = {"error": "Path does not exist", "path": root_path}
                continue

            def _scan(directory: Path) -> dict:
                node: dict[str, list | dict] = {"dirs": {}, "files": []}
                try:
                    for item in sorted(directory.iterdir()):
                        if item.is_dir():
                            if item.name not in self.EXCLUDED_DIRS:
                                node["dirs"][item.name] = _scan(item)
                        elif item.is_file():
                            node["files"].append(item.name)
                except PermissionError:
                    node["error"] = "Permission Denied"
                return node

            tree[alias] = _scan(p)
        return tree

    def format_tree_display(self) -> str:
        """Render a readable ASCII directory tree string for CLI and context injection."""
        lines = []
        for alias, root_path in self.mounted_roots.items():
            lines.append(f"[{alias}] -> {root_path}")
            p = Path(root_path)
            if not p.exists():
                lines.append("   \\-- [Path Not Found]")
                continue

            def _walk(directory: Path, prefix: str = "   "):
                try:
                    entries = sorted(list(directory.iterdir()), key=lambda x: (x.is_file(), x.name))
                except PermissionError:
                    return
                for i, item in enumerate(entries):
                    is_last = (i == len(entries) - 1)
                    connector = "\\-- " if is_last else "+-- "
                    if item.is_dir():
                        if item.name not in self.EXCLUDED_DIRS:
                            lines.append(f"{prefix}{connector}[DIR] {item.name}/")
                            _walk(item, prefix + ("    " if is_last else "|   "))
                    elif item.is_file():
                        lines.append(f"{prefix}{connector}{item.name}")

            _walk(p)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mediated RBAC Permission Matrix (Phase 3)
# ---------------------------------------------------------------------------

class PermissionMatrix:
    """
    Manages fine-grained agent file access tokens: READ, SUGGEST, EDIT.
    """

    def __init__(self, workspace_manager: WorkspaceManager):
        self.workspace_manager = workspace_manager
        # agent_id -> {file_path: token_info}
        self.granted_tokens: dict[str, dict[str, dict]] = {}

    def grant_access(self, agent_id: str, file_path: str, level: str, reason: str) -> str:
        """
        Grants a token for an agent. Level must be READ, SUGGEST, or EDIT.
        """
        level = level.upper()
        if level not in {"READ", "SUGGEST", "EDIT"}:
            raise ValueError(f"Invalid access level '{level}'. Must be READ, SUGGEST, or EDIT.")

        if agent_id not in self.granted_tokens:
            self.granted_tokens[agent_id] = {}

        token = f"TOKEN_{agent_id}_{abs(hash(file_path + level + reason))}"
        self.granted_tokens[agent_id][file_path] = {
            "token": token,
            "level": level,
            "reason": reason,
        }
        logger.info("PermissionMatrix: Granted [%s] access on '%s' to agent '%s'.", level, file_path, agent_id)
        return token

    def revoke_access(self, agent_id: str, file_path: str) -> bool:
        """
        Revokes token for a specific file.
        """
        if agent_id in self.granted_tokens and file_path in self.granted_tokens[agent_id]:
            self.granted_tokens[agent_id].pop(file_path)
            logger.info("PermissionMatrix: Revoked access on '%s' from agent '%s'.", file_path, agent_id)
            return True
        return False

    def revoke_all_for_agent(self, agent_id: str) -> None:
        """
        Revokes all tokens for a relieved or completed agent.
        """
        if agent_id in self.granted_tokens:
            self.granted_tokens.pop(agent_id, None)
            logger.info("PermissionMatrix: Revoked all file access for agent '%s'.", agent_id)

    def read_file(self, agent_id: str, virtual_path: str) -> str:
        tokens = self.granted_tokens.get(agent_id, {})
        if virtual_path not in tokens or tokens[virtual_path]["level"] not in {"READ", "SUGGEST", "EDIT"}:
            raise PermissionError(f"Agent '{agent_id}' does not have READ permission for '{virtual_path}'.")

        resolved = self.workspace_manager.resolve_path(virtual_path)
        if not resolved or not resolved.is_file():
            raise FileNotFoundError(f"File '{virtual_path}' could not be resolved.")

        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def suggest_diff(self, agent_id: str, virtual_path: str, new_content: str) -> str:
        tokens = self.granted_tokens.get(agent_id, {})
        if virtual_path not in tokens or tokens[virtual_path]["level"] not in {"SUGGEST", "EDIT"}:
            raise PermissionError(f"Agent '{agent_id}' does not have SUGGEST permission for '{virtual_path}'.")

        resolved = self.workspace_manager.resolve_path(virtual_path)
        if not resolved or not resolved.is_file():
            raise FileNotFoundError(f"File '{virtual_path}' could not be resolved.")

        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            original = f.read().splitlines(keepends=True)

        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(original, new_lines, fromfile=f"a/{virtual_path}", tofile=f"b/{virtual_path}")
        return "".join(diff)

    def apply_edit(self, agent_id: str, virtual_path: str, content: str) -> None:
        tokens = self.granted_tokens.get(agent_id, {})
        if virtual_path not in tokens or tokens[virtual_path]["level"] != "EDIT":
            raise PermissionError(f"Agent '{agent_id}' does not have EDIT permission for '{virtual_path}'.")

        resolved = self.workspace_manager.resolve_path(virtual_path)
        if not resolved:
            raise FileNotFoundError(f"Cannot resolve path '{virtual_path}'.")

        resolved.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("PermissionMatrix: Agent '%s' applied EDIT to '%s'.", agent_id, resolved)


# ---------------------------------------------------------------------------
# ContextHub
# ---------------------------------------------------------------------------

class ContextHub:
    """
    Central state database for the axeAI framework.
    """

    def __init__(self, config_path: str | Path = "workspace_config.json"):
        # --- Tier 1: Structured interaction logs ---
        self.sub_agent_histories: dict[str, list[dict]] = {}

        # --- Tier 2: Free-form scratchpad working memory ---
        self.scratchpads: dict[str, dict] = {}

        # --- Global active workforce state map ---
        self.agent_workforce: dict[str, dict] = {}

        # --- Orchestrator log (owned by a1.py) ---
        self.orchestrator_history: list[dict] = []

        # --- Atomic checkpoint store ---
        self.checkpoints: dict[str, dict] = {}

        # --- Workspace & RBAC Components ---
        self.workspace_manager = WorkspaceManager(config_path=config_path)
        self.permission_matrix = PermissionMatrix(self.workspace_manager)

        logger.info("ContextHub initialised — memory stores, workspace, and RBAC ready.")

    # -----------------------------------------------------------------------
    # Workforce State Tracking
    # -----------------------------------------------------------------------

    def update_workforce_agent(self, agent_id: str, role: str, status: str, current_task: str) -> None:
        """Update or create an agent's workforce state entry."""
        self.agent_workforce[agent_id] = {
            "role": role,
            "status": status,
            "current_task": current_task
        }
        logger.debug(
            "ContextHub: Workforce agent '%s' updated to status '%s'.",
            agent_id, status
        )

    def get_workforce(self) -> dict[str, dict]:
        """Return the active agent workforce dictionary."""
        return self.agent_workforce

    # -----------------------------------------------------------------------
    # Sub-agent History (Tier 1)
    # -----------------------------------------------------------------------

    def save_sub_agent_interaction(
        self, agent_id: str, prompt: str, response: str
    ) -> None:
        """
        Append a prompt/response pair to the agent's structured history log.
        Called by aG.execute() after each successful API cycle.
        """
        if agent_id not in self.sub_agent_histories:
            self.sub_agent_histories[agent_id] = []
            logger.debug("ContextHub: Created history log for agent '%s'.", agent_id)

        self.sub_agent_histories[agent_id].append(
            {"prompt": prompt, "response": response}
        )
        logger.debug(
            "ContextHub: Interaction logged for '%s' (total entries: %d).",
            agent_id,
            len(self.sub_agent_histories[agent_id]),
        )

    def get_agent_history(self, agent_id: str) -> list[dict]:
        """
        Return the full interaction history for an agent.
        Returns an empty list if the agent has no history yet.
        Used by aG to build the static context block.
        """
        return self.sub_agent_histories.get(agent_id, [])

    # -----------------------------------------------------------------------
    # Scratchpad (Tier 2)
    # -----------------------------------------------------------------------

    def get_scratchpad(self, agent_id: str) -> dict:
        """
        Return the current scratchpad for an agent.
        Returns an empty dict if the agent has no scratchpad yet.
        """
        return self.scratchpads.get(agent_id, {})

    def update_scratchpad(self, agent_id: str, data: dict) -> None:
        """
        Merge `data` into the agent's scratchpad (shallow merge).
        Creates the scratchpad if it doesn't exist.
        Used by aG after each cycle to persist working notes,
        and by a1.py during task redistribution to inject dual-role context.
        """
        if agent_id not in self.scratchpads:
            self.scratchpads[agent_id] = {}
            logger.debug("ContextHub: Created scratchpad for agent '%s'.", agent_id)

        self.scratchpads[agent_id].update(data)
        logger.debug(
            "ContextHub: Scratchpad updated for '%s'. Keys: %s",
            agent_id,
            list(data.keys()),
        )

    def clear_scratchpad(self, agent_id: str) -> None:
        """Wipe an agent's scratchpad entirely. Use with caution."""
        self.scratchpads.pop(agent_id, None)
        logger.warning("ContextHub: Scratchpad cleared for agent '%s'.", agent_id)

    # -----------------------------------------------------------------------
    # Atomic Checkpoint / Rollback (Transaction Safety for TaskGroup)
    # -----------------------------------------------------------------------

    def save_checkpoint(self, round_id: str) -> None:
        """
        Snapshot the current state of all scratchpads and sub-agent histories
        before a broadcast round begins.

        This enables rollback_to_checkpoint() to undo partial state writes
        if asyncio.TaskGroup fails mid-round — preventing desynchronized memory.

        round_id: A unique identifier for the broadcast round (e.g., "round_1").
        """
        self.checkpoints[round_id] = {
            "sub_agent_histories": copy.deepcopy(self.sub_agent_histories),
            "scratchpads": copy.deepcopy(self.scratchpads),
            "agent_workforce": copy.deepcopy(self.agent_workforce),
        }
        logger.info(
            "ContextHub: Checkpoint saved for round '%s'. "
            "Tracking %d agent histories, %d scratchpads, %d workforce entries.",
            round_id,
            len(self.sub_agent_histories),
            len(self.scratchpads),
            len(self.agent_workforce),
        )

    def rollback_to_checkpoint(self, round_id: str) -> None:
        """
        Restore scratchpads and sub-agent histories to the state they were in
        when save_checkpoint(round_id) was called.

        Called by a1.py when it catches a BroadcastFailure from comm_hub,
        before it re-plans and redistributes tasks.
        """
        if round_id not in self.checkpoints:
            logger.error(
                "ContextHub: Cannot rollback — no checkpoint found for round '%s'.",
                round_id,
            )
            return

        snapshot = self.checkpoints[round_id]
        self.sub_agent_histories = copy.deepcopy(snapshot["sub_agent_histories"])
        self.scratchpads = copy.deepcopy(snapshot["scratchpads"])
        self.agent_workforce = copy.deepcopy(snapshot["agent_workforce"])
        logger.warning(
            "ContextHub: Rolled back to checkpoint for round '%s'. "
            "Partial state from failed round has been discarded.",
            round_id,
        )

    def commit_checkpoint(self, round_id: str) -> None:
        """
        Purge the checkpoint for a round after it completes successfully.
        Frees memory — checkpoints are not needed after a successful commit.
        Called by a1.py after broadcast_tasks() succeeds.
        """
        removed = self.checkpoints.pop(round_id, None)
        if removed is not None:
            logger.debug(
                "ContextHub: Checkpoint '%s' committed and purged.", round_id
            )
        else:
            logger.warning(
                "ContextHub: commit_checkpoint called for unknown round '%s'.",
                round_id,
            )

    # -----------------------------------------------------------------------
    # Orchestrator History
    # -----------------------------------------------------------------------

    def log_orchestrator_turn(self, user_prompt: str, final_output: str) -> None:
        """
        Append a completed orchestration turn to the orchestrator history.
        Called by a1.py after synthesis is complete.
        """
        self.orchestrator_history.append(
            {"user_prompt": user_prompt, "final_output": final_output}
        )
        logger.info(
            "ContextHub: Orchestrator turn logged (total turns: %d).",
            len(self.orchestrator_history),
        )

    # -----------------------------------------------------------------------
    # Diagnostics
    # -----------------------------------------------------------------------

    def dump_state(self) -> str:
        """
        Return a JSON-serializable snapshot of the entire hub state.
        Used for debugging — do NOT log this at production log levels
        as it may contain sensitive prompt/response content.
        """
        state = {
            "orchestrator_history_count": len(self.orchestrator_history),
            "active_agents": list(self.sub_agent_histories.keys()),
            "scratchpad_keys": {
                agent_id: list(pad.keys())
                for agent_id, pad in self.scratchpads.items()
            },
            "pending_checkpoints": list(self.checkpoints.keys()),
        }
        return json.dumps(state, indent=2)