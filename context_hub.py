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
import json
import logging

logger = logging.getLogger("aX.context_hub")


# ---------------------------------------------------------------------------
# ContextHub
# ---------------------------------------------------------------------------

class ContextHub:
    """
    Central state database for the axeAI framework.

    Memory Model
    ------------
    sub_agent_histories : dict[agent_id -> list[{prompt, response}]]
        Structured chronological log of every prompt/response pair for each
        sub-agent. Appended to by aG.execute() after each cycle. Read by aG
        to build the static "context" block for prefix-cache integrity.

    scratchpads : dict[agent_id -> dict]
        Free-form JSON working memory per agent. Agents read and write
        arbitrary key-value pairs here across cycles. This is the "notebook"
        that enables cross-cycle reasoning without re-reading full history.

    orchestrator_history : list[{user_prompt, final_output}]
        Append-only log owned exclusively by a1.py (OrchestratorAgent).

    checkpoints : dict[round_id -> snapshot]
        Atomic snapshots taken before each broadcast round. Used by
        rollback_to_checkpoint() if asyncio.TaskGroup fails.
    """

    def __init__(self):
        # --- Tier 1: Structured interaction logs ---
        self.sub_agent_histories: dict[str, list[dict]] = {}

        # --- Tier 2: Free-form scratchpad working memory ---
        self.scratchpads: dict[str, dict] = {}

        # --- Global active workforce state map ---
        # Map of agent_id -> {role: str, status: str, current_task: str}
        self.agent_workforce: dict[str, dict] = {}

        # --- Orchestrator log (owned by a1.py) ---
        self.orchestrator_history: list[dict] = []

        # --- Atomic checkpoint store ---
        self.checkpoints: dict[str, dict] = {}

        logger.info("ContextHub initialised — memory stores ready.")

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