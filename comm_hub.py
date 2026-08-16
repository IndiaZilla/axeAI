"""
comm_hub.py — axeAI Communication Highway
==========================================
The pure message router. Knows about agents and transport, NOT about tasks.

Responsibilities:
  - Maintain the registry of active SubAgentNode instances
  - Assign sticky API keys via round-robin at spawn time (preserves cache)
  - Route tasks to agents concurrently with a Semaphore concurrency cap
  - Use asyncio.TaskGroup for fail-fast execution (Q13)
  - Implement the circuit breaker for dead API keys (linear cooldown timestamp)
  - Expose convene_board_room() for parallel and round-robin debate modes
  - Return compressed semantic summaries from Board Room (never raw transcript)

Separation of Concerns:
  - comm_hub.py is a DUMB HIGHWAY. It does NOT generate system instructions,
    decompose tasks, or know anything about the content of tasks.
  - a1.py (OrchestratorAgent) owns all task content and agent identity.
  - comm_hub.py receives fully-formed agent specs from a1.py and routes them.

Circuit Breaker Design (Linear Cooldown Timestamp):
  - When an agent raises AgentExecutionError, mark_key_dead() records:
      dead_key_cooldowns[key] = time.time() + HEALTH_CHECK_COOLDOWN_SECS
  - No background coroutine is spawned. No active polling.
  - The FIRST inbound request that arrives AFTER the timestamp has elapsed
    acts as the single passive probe. If it succeeds, the key is reinstated.
  - This is intentionally linear and non-exponential to keep the hub lean.
"""

import asyncio
import json
import logging
import time

from google import genai  # type: ignore
from google.genai import types  # type: ignore

from aG import SubAgentNode, AgentExecutionError

logger = logging.getLogger("aX.comm_hub")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEALTH_CHECK_COOLDOWN_SECS: int = 60
"""Seconds a dead key is quarantined before being eligible for re-probe."""


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------

class BroadcastFailure(Exception):
    """
    Raised by broadcast_tasks() when asyncio.TaskGroup fails.
    Carries the list of failed agent IDs and their exceptions
    so a1.py can make redistribution decisions.
    """
    def __init__(self, failed_agents: list[str], exception_group: ExceptionGroup):
        self.failed_agents = failed_agents
        self.exception_group = exception_group
        super().__init__(
            f"Broadcast round failed. Failed agents: {failed_agents}"
        )


# ---------------------------------------------------------------------------
# CommunicationHub
# ---------------------------------------------------------------------------

class CommunicationHub:
    """
    The communication highway for the axeAI framework.

    Parameters
    ----------
    api_key_pool : list[str]
        All available Gemini API keys. Keys are assigned sticky (round-robin
        per spawn) so each SubAgentNode's cache continuity is preserved.
    context_hub : ContextHub
        Shared state database injected at construction time.
    max_concurrent : int
        Maximum simultaneous API calls across all agents. Enforced by Semaphore.
    board_room_rounds : int
        Number of debate rounds the Board Room runs before consensus.
    """

    def __init__(
        self,
        api_key_pool: list[str],
        context_hub,  # ContextHub — duck-typed to avoid circular import
        agent_registry,  # AgentRegistry — injected from a0.py
        max_concurrent: int = 5,
        board_room_rounds: int = 3,
    ):
        self.api_key_pool = api_key_pool
        self.context_hub = context_hub
        self.agent_registry = agent_registry
        # Scale concurrency dynamically according to key pool throughput (e.g. 5 concurrent per active key)
        self.max_concurrent = max(max_concurrent, len(api_key_pool) * 5)
        self.board_room_rounds = board_room_rounds

        # Circuit breaker: dead key → timestamp after which it may be re-probed
        self._dead_key_cooldowns: dict[str, float] = {}

        # Concurrency gate — auto-scaled dynamic semaphore
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

        logger.info(
            "CommunicationHub initialised | keys: %d | auto-scaled max_concurrent: %d | "
            "board_room_rounds: %d",
            len(api_key_pool), self.max_concurrent, board_room_rounds,
        )

    async def route_a2a_gibber_message(self, sender_id: str, target_agent_id: str, payload: dict) -> dict:
        """
        Routes dense tokenized gibberTalk message directly between agents in the registry
        without requiring Orchestrator S intervention for non-critical coordination.
        """
        from aG import gibber_encode, gibber_decode

        token_stream = gibber_encode(payload)
        logger.debug("comm_hub: A2A gibberTalk [%s -> %s] packed tokens: %s", sender_id, target_agent_id, token_stream)

        # Retrieve target agent node from registry
        try:
            target_node = self.agent_registry.get_agent(target_agent_id)
            # Route instruction to target agent directly
            decoded = gibber_decode(token_stream)
            prompt = f"[A2A MESSAGE from {sender_id}]\n{json.dumps(decoded)}"
            res = await target_node.execute(prompt)
            return {"status": "SUCCESS", "response": res}
        except Exception as e:
            logger.error("comm_hub: A2A gibberTalk routing error: %s", e)
            return {"status": "ERROR", "error": str(e)}

    # -----------------------------------------------------------------------
    # API Key Management (Sticky Round-Robin + Circuit Breaker)
    # -----------------------------------------------------------------------

    def is_key_dead(self, api_key: str) -> bool:
        """Public check for dead keys (polled by AgentRegistry)."""
        return self._is_key_dead(api_key)

    def _is_key_dead(self, api_key: str) -> bool:
        """
        Check if a key is currently in its cooldown quarantine window.
        Returns True if the key is dead AND the cooldown has NOT elapsed.
        If the cooldown HAS elapsed, the key is automatically reinstated
        (this call acts as the passive probe).
        """
        cooldown_until = self._dead_key_cooldowns.get(api_key)
        if cooldown_until is None:
            return False  # Key was never marked dead

        if time.time() >= cooldown_until:
            # Cooldown elapsed — reinstate the key (passive probe)
            del self._dead_key_cooldowns[api_key]
            logger.info(
                "Circuit breaker: Key %s... cooldown elapsed. "
                "Reinstated for rotation (passive probe).",
                api_key[:8],
            )
            return False

        return True  # Still in cooldown window

    def mark_key_dead(self, api_key: str) -> None:
        """
        Trip the circuit breaker for an API key.
        Sets a linear cooldown timestamp. No background coroutine is spawned.
        The key will be re-eligible for probing after HEALTH_CHECK_COOLDOWN_SECS.

        Called by broadcast_tasks() when it catches an AgentExecutionError.
        """
        cooldown_until = time.time() + HEALTH_CHECK_COOLDOWN_SECS
        self._dead_key_cooldowns[api_key] = cooldown_until
        logger.warning(
            "Circuit breaker TRIPPED: Key %s... marked dead. "
            "Re-probe eligible after %ds (at %.0f unix).",
            api_key[:8], HEALTH_CHECK_COOLDOWN_SECS, cooldown_until,
        )

    def get_active_agent_ids(self) -> list[str]:
        """
        Return the list of agent IDs whose assigned API key is NOT dead.
        Used by a1.py during task redistribution to find healthy survivors.
        """
        return [
            agent_id
            for agent_id, node in self.agent_registry.get_all_agents().items()
            if not self._is_key_dead(node._client.api_key
                                      if hasattr(node._client, "api_key")
                                      else "")
        ]

    async def _guarded_execute(
        self, agent, task_prompt: str
    ) -> tuple[str, dict]:
        """
        Execute an agent's task under the concurrency Semaphore.
        Returns (agent_id, result_dict) on success.
        Propagates AgentExecutionError on failure (TaskGroup catches it).
        """
        async with self._semaphore:
            result = await agent.execute(task_prompt)
            return (agent.agent_id, result)

    async def broadcast_tasks(
        self, tasks: list[dict]
    ) -> dict[str, dict]:
        """
        Fan out tasks to their assigned agents concurrently.

        Parameters
        ----------
        tasks : list[dict]
            Each entry must contain 'agent_id' and 'task' keys.
            The orchestrator is responsible for having already registered
            the agent in AgentRegistry.
            {
              "agent_id": str,
              "task":     str
            }

        Returns
        -------
        dict[str, dict]
            Mapping of agent_id → parsed response dict from aG.execute().

        Raises
        ------
        BroadcastFailure
            Raised if asyncio.TaskGroup catches any AgentExecutionError.
            Carries failed_agents list for a1.py's redistribution logic.
        RuntimeError
            If tasks list is empty.
        """
        if not tasks:
            raise RuntimeError("broadcast_tasks() called with an empty task list.")

        coroutines = []
        agent_ids_in_order = []

        for spec in tasks:
            agent_id = spec["agent_id"]
            node = self.agent_registry.get_agent(agent_id)
            coroutines.append(self._guarded_execute(node, spec["task"]))
            agent_ids_in_order.append(agent_id)

        results: dict[str, dict] = {}
        failed_agents: list[str] = []

        logger.info(
            "Broadcasting %d task(s) | max_concurrent: %d",
            len(tasks), self.max_concurrent,
        )

        # --- asyncio.TaskGroup: fail-fast structured concurrency ---
        try:
            async with asyncio.TaskGroup() as tg:
                task_handles = [tg.create_task(coro) for coro in coroutines]

        except* AgentExecutionError as exc_group:
            # One or more agents exhausted their retries.
            # Identify failed agents and trip the circuit breaker for each dead key.
            for exc in exc_group.exceptions:
                if isinstance(exc, AgentExecutionError):
                    failed_agents.append(exc.agent_id)
                    # Reconstruct the full key from the pool for circuit breaker
                    # (node stores only the prefix; find the matching full key)
                    dead_key = next(
                        (k for k in self.api_key_pool if k.startswith(exc.api_key_prefix)),
                        None,
                    )
                    if dead_key:
                        self.mark_key_dead(dead_key)

            logger.error(
                "Broadcast round FAILED. Failed agents: %s. "
                "Raising BroadcastFailure for orchestrator recovery.",
                failed_agents,
            )
            raise BroadcastFailure(
                failed_agents=failed_agents,
                exception_group=exc_group,
            )

        # Collect results from completed task handles
        for handle, agent_id in zip(task_handles, agent_ids_in_order):
            _agent_id, result = handle.result()
            results[_agent_id] = result

        logger.info(
            "Broadcast round complete. %d/%d agents succeeded.",
            len(results), len(tasks),
        )
        return results

    # -----------------------------------------------------------------------
    # Board Room (Parallel + Round-Robin Debate Modes)
    # -----------------------------------------------------------------------

    async def convene_board_room(
        self,
        agent_ids: list[str],
        topic: str,
        mode: str,                    # "parallel" | "round_robin"
        orchestrator_opening: str,    # S's context-setting statement
        requester_id: str,            # The agent who requested the board room
        synthesis_client: genai.Client,
        synthesis_model: str,
    ) -> str:
        """
        Convene a multi-agent debate and return a COMPRESSED SEMANTIC SUMMARY.

        The raw debate transcript is intentionally discarded after synthesis.
        Only the compressed summary is returned to a1.py to prevent overwhelming
        the orchestrator's synthesis context window.

        Parameters
        ----------
        agent_ids : list[str]
            IDs of all agents participating in the debate (including requester).
        topic : str
            The specific question or disagreement to be resolved.
        mode : str
            "parallel"   — all agents speak and receive simultaneously each round.
            "round_robin" — agents take turns; each sees all prior speakers' output.
        orchestrator_opening : str
            S's opening statement explaining why the board room was called.
        requester_id : str
            The agent that requested the debate; they speak first after S.
        synthesis_client : genai.Client
            The Gemini client owned by the OrchestratorAgent, used to compress
            the final transcript into a semantic summary.
        synthesis_model : str
            Gemini model used for the compression synthesis pass.

        Returns
        -------
        str
            A compressed semantic summary of the debate consensus.
            NOT the raw transcript.
        """
        # Validate participants
        valid_participants = [
            aid for aid in agent_ids if aid in self.agent_registry.get_all_agents()
        ]
        if not valid_participants:
            logger.error("Board Room: No valid participants found in %s.", agent_ids)
            return "Board Room failed: no active participants."

        logger.info(
            "Board Room convening | mode: %s | participants: %s | rounds: %d",
            mode, valid_participants, self.board_room_rounds,
        )

        # Build the ordered speaker list:
        # 1. Requester speaks first (after S's opening)
        # 2. Remaining agents in registration order
        ordered_speakers = [requester_id] + [
            aid for aid in valid_participants if aid != requester_id
        ]

        # Transcript accumulates all turns across all rounds
        # Format: list of {"speaker": agent_id | "S", "turn": str, "round": int}
        transcript: list[dict] = [
            {"speaker": "S", "turn": orchestrator_opening, "round": 0}
        ]

        for round_num in range(1, self.board_room_rounds + 1):
            logger.info("Board Room: Round %d/%d", round_num, self.board_room_rounds)

            if mode == "parallel":
                await self._board_room_parallel_round(
                    speakers=ordered_speakers,
                    topic=topic,
                    transcript=transcript,
                    round_num=round_num,
                )
            elif mode == "round_robin":
                await self._board_room_round_robin_round(
                    speakers=ordered_speakers,
                    topic=topic,
                    transcript=transcript,
                    round_num=round_num,
                )
            else:
                logger.error("Board Room: Unknown mode '%s'. Aborting.", mode)
                return f"Board Room aborted: unknown mode '{mode}'."

        # --- Compress the transcript into a semantic summary ---
        compressed_summary = await self._compress_board_room_transcript(
            transcript=transcript,
            topic=topic,
            synthesis_client=synthesis_client,
            synthesis_model=synthesis_model,
        )

        logger.info(
            "Board Room complete. Transcript (%d turns) compressed to summary.",
            len(transcript),
        )
        # Raw transcript is NOT returned — only the compressed summary
        return compressed_summary

    async def _board_room_parallel_round(
        self,
        speakers: list[str],
        topic: str,
        transcript: list[dict],
        round_num: int,
    ) -> None:
        """
        All agents receive the full current transcript simultaneously and
        respond in parallel via asyncio.gather. Each agent sees all prior turns.
        """
        context_for_agents = self._format_transcript_for_agents(transcript, topic)

        # Fan out in parallel — all agents receive identical context
        async def _one_speaker_parallel(agent_id: str) -> dict:
            node = self.agent_registry.get_agent(agent_id)
            result = await node.execute(
                task_prompt=context_for_agents + f"\n\nYour turn (Round {round_num}): "
                            "State your position or update your view based on the debate so far."
            )
            return {"speaker": agent_id, "turn": result.get("result", ""), "round": round_num}

        turns = await asyncio.gather(
            *[_one_speaker_parallel(aid) for aid in speakers],
            return_exceptions=True,
        )

        for turn in turns:
            if isinstance(turn, Exception):
                logger.warning("Board Room: A participant failed in parallel round: %s", turn)
            else:
                transcript.append(turn)

    async def _board_room_round_robin_round(
        self,
        speakers: list[str],
        topic: str,
        transcript: list[dict],
        round_num: int,
    ) -> None:
        """
        Agents speak in order. Each agent sees all prior speakers'
        output from this round before forming their response.
        """
        for agent_id in speakers:
            context_for_agent = self._format_transcript_for_agents(transcript, topic)
            node = self.agent_registry.get_agent(agent_id)

            result = await node.execute(
                task_prompt=context_for_agent + f"\n\nYour turn (Round {round_num}): "
                            "State your position or update your view based on all prior speakers."
            )

            turn = {
                "speaker": agent_id,
                "turn": result.get("result", ""),
                "round": round_num,
            }
            transcript.append(turn)
            logger.debug(
                "Board Room: %s spoke in round %d.", agent_id, round_num
            )

    def _format_transcript_for_agents(
        self, transcript: list[dict], topic: str
    ) -> str:
        """
        Format the running transcript into a readable string for agents.
        Passed as context for each Board Room turn.
        """
        lines = [f"BOARD ROOM DEBATE\nTopic: {topic}\n---"]
        for entry in transcript:
            speaker = entry["speaker"]
            turn_text = entry["turn"]
            round_label = f"[Round {entry['round']}]" if entry["round"] > 0 else "[Opening]"
            lines.append(f"{round_label} {speaker}: {turn_text}")
        return "\n".join(lines)

    async def _compress_board_room_transcript(
        self,
        transcript: list[dict],
        topic: str,
        synthesis_client: genai.Client,
        synthesis_model: str,
    ) -> str:
        """
        Use the OrchestratorAgent's Gemini client to compress the full
        board room transcript into a concise semantic summary.

        The summary should capture:
          - The consensus position (if reached)
          - Key disagreements that remain unresolved
          - The most important arguments raised
          - A recommended action for a1.py to take

        The raw transcript is NOT included in the return value.
        """
        transcript_text = self._format_transcript_for_agents(transcript, topic)

        compression_prompt = (
            f"You have just moderated a multi-agent Board Room debate.\n\n"
            f"{transcript_text}\n\n"
            f"---\n"
            f"Produce a COMPRESSED SEMANTIC SUMMARY of this debate. "
            f"Your summary must be concise (under 300 words) and include:\n"
            f"1. CONSENSUS: The agreed-upon position, if any.\n"
            f"2. DISSENT: Any unresolved disagreements.\n"
            f"3. KEY ARGUMENTS: The 2-3 most important points raised.\n"
            f"4. RECOMMENDATION: What the Orchestrator should do next.\n\n"
            f"Do NOT reproduce the raw transcript. Only the summary."
        )

        response = await synthesis_client.aio.models.generate_content(
            model=synthesis_model,
            contents=compression_prompt,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You are the synthesis engine for the axeAI Board Room. "
                    "Compress debate transcripts into concise semantic summaries."
                ),
            ),
        )

        summary = response.text or "Board Room synthesis returned no summary."
        logger.info("Board Room transcript compressed successfully.")
        return summary