"""
aG.py — axeAI SubAgent Node
=============================
Each SubAgentNode is a stateless-per-call, stateful-per-lifecycle worker.

Responsibilities:
  - Receive a task dict from CommunicationHub (via a1.py's plan)
  - Build the canonical prefix-cache input payload (static context on top,
    dynamic task at the bottom) and serialise it with json.dumps()
  - Call the Gemini API asynchronously with exponential backoff + jitter
  - Parse the structured JSON response (result + optional board_room_request)
  - Persist the interaction to ContextHub (history log + scratchpad update)
  - Raise AgentExecutionError after retry exhaustion for circuit-breaker pickup

╔══════════════════════════════════════════════════════════════╗
║          PREFIX CACHE CONTRACT — DO NOT VIOLATE              ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Every call to execute() MUST produce this structure:        ║
║                                                              ║
║  {                                                           ║
║    "context": {          <-- STATIC TOP                      ║
║      "output_format": ...,   ← never changes                 ║
║      "env_context":  ...,    ← never changes                 ║
║      "unv_context":  ...,    ← never changes                 ║
║      "agent_id":     ...,    ← assigned once at spawn        ║
║      "role":         ...     ← assigned once at spawn        ║
║    },                                                        ║
║    "task": {             <-- DYNAMIC BOTTOM                  ║
║      "scratchpad":   ...,    ← changes every cycle           ║
║      "history":      [...],  ← grows every cycle             ║
║      "task_prompt":  "..."   ← changes every call            ║
║    }                                                         ║
║  }                                                           ║
║                                                              ║
║  WHY: scratchpad and history change on EVERY cycle.          ║
║  Placing them in "context" would make the prefix unique      ║
║  every call, destroying all cache hits and burning tokens.   ║
║                                                              ║
║  Only TRUE INVARIANTS (set once at spawn, never mutated)     ║
║  belong in the "context" block.                              ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import math
import random

from google import genai  # type: ignore
from google.genai import types  # type: ignore

logger = logging.getLogger("aX.aG")


# ---------------------------------------------------------------------------
# Agent Registry Helper
# ---------------------------------------------------------------------------

class AgentRegistry:
    """
    Dedicated registry helper class for SubAgentNode instances.
    Instantiated in a0.py and injected into OrchestratorAgent (S).
    S uses it to dynamically build/register agents, and comm_hub queries it.
    """
    def __init__(self, context_hub, api_key_pool: list[str]):
        self.context_hub = context_hub
        self.api_key_pool = api_key_pool
        self.registry: dict[str, "SubAgentNode"] = {}
        self._key_index = 0
        self.comm_hub = None  # Injected in a0.py after instantiation

    def register_agent(
        self,
        agent_id: str,
        role: str,
        system_instruction: str,
        model_name: str = "gemini-2.5-flash-lite"
    ) -> "SubAgentNode":
        """Spawn and register a new SubAgentNode. Reuses existing agent if ID matches."""
        if agent_id in self.registry:
            logger.debug(
                "AgentRegistry: Reusing registered agent '%s'.", agent_id
            )
            return self.registry[agent_id]

        # Select a sticky key from the pool, skipping any dead keys
        attempts = 0
        pool_size = len(self.api_key_pool)
        assigned_key = None
        while attempts < pool_size:
            candidate_key = self.api_key_pool[self._key_index % pool_size]
            self._key_index += 1
            attempts += 1
            if self.comm_hub and self.comm_hub.is_key_dead(candidate_key):
                continue
            assigned_key = candidate_key
            break

        if not assigned_key:
            logger.warning("AgentRegistry: All keys in pool are dead! Falling back to primary.")
            assigned_key = self.api_key_pool[0]

        agent = SubAgentNode(
            agent_id=agent_id,
            role=role,
            system_instruction=system_instruction,
            api_key=assigned_key,
            context_hub=self.context_hub,
            model_name=model_name
        )
        self.registry[agent_id] = agent
        logger.info("AgentRegistry: Registered agent '%s' with key prefix %s...", agent_id, assigned_key[:8])
        return agent

    def terminate_agent(self, agent_id: str) -> bool:
        """
        Teardown a SubAgentNode instance on demand, freeing memory and resources.
        """
        if agent_id in self.registry:
            node = self.registry.pop(agent_id)
            # Revoke RBAC permissions and clean context memory
            self.context_hub.permission_matrix.revoke_all_for_agent(agent_id)
            logger.info("AgentRegistry: Terminated agent node '%s' and released resources.", agent_id)
            return True
        return False

    def get_agent(self, agent_id: str) -> "SubAgentNode":
        """Retrieve a registered agent by ID. Raises KeyError if not found."""
        if agent_id not in self.registry:
            raise KeyError(f"Agent '{agent_id}' is not registered in AgentRegistry.")
        return self.registry[agent_id]

    def get_all_agents(self) -> dict[str, "SubAgentNode"]:
        """Return the dictionary of all registered agents."""
        return self.registry


# ---------------------------------------------------------------------------
# gibberTalk Token Optimization Protocol (Phase 2)
# ---------------------------------------------------------------------------

def gibber_encode(payload: dict) -> str:
    """
    Compresses an inter-agent message into a dense, minified tokenized payload.
    Eliminates conversational intros, markdown padding, and whitespace.
    """
    try:
        minified = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return minified
    except Exception as e:
        logger.error("gibber_encode failed: %s", e)
        return str(payload)


def gibber_decode(token_str: str) -> dict:
    """
    Decodes a dense gibberTalk token string back into structured agent dictionary.
    """
    try:
        return json.loads(token_str)
    except Exception:
        return {"raw": token_str}


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------

class AgentExecutionError(Exception):
    """
    Raised by SubAgentNode.execute() after all retry attempts are exhausted.
    Carries enough metadata for comm_hub's circuit breaker to act on.
    """
    def __init__(self, agent_id: str, api_key_prefix: str, cause: Exception):
        self.agent_id = agent_id
        self.api_key_prefix = api_key_prefix  # first 8 chars only, for safe logging
        self.cause = cause
        super().__init__(
            f"Agent '{agent_id}' failed after all retries. "
            f"Key prefix: {api_key_prefix}. Cause: {cause}"
        )


# ---------------------------------------------------------------------------
# SubAgent Response Schema
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# SubAgent Response Schema
# ---------------------------------------------------------------------------

SUB_AGENT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {
            "type": "string",
            "description": "The main output of the sub-agent for its assigned task.",
        },
        "confidence": {
            "type": "number",
            "description": "A self-assessed confidence score between 0.0 and 1.0.",
        },
        "status": {
            "type": "object",
            "description": (
                "Mandatory. Declare your current status. Choose WORKING if you are "
                "in progress, COMPLETED if you have fully accomplished your task, "
                "or REVIEW if you are stuck or need feedback/review from S."
            ),
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["WORKING", "COMPLETED", "REVIEW"]
                },
                "message": {
                    "type": "string",
                    "description": "Short explanation of your active work, completion reasoning, or review description."
                }
            },
            "required": ["type", "message"]
        },
        "file_access_request": {
            "type": "object",
            "description": "Optional request to read, suggest diffs, or edit workspace files.",
            "properties": {
                "action": {"type": "string", "enum": ["request_file_access", "renounce_file_access"]},
                "files": {"type": "array", "items": {"type": "string"}},
                "level": {"type": "string", "enum": ["READ", "SUGGEST", "EDIT"]},
                "reason": {"type": "string"},
                "edits": {
                    "type": "object",
                    "description": "Optional mapping of file_path -> new_content when performing EDIT or SUGGEST."
                }
            },
            "required": ["action", "files"]
        },
        "compute_request": {
            "type": "object",
            "description": "Optional request to run exact Math/SymPy calculations or execute code/scripts in sandbox.",
            "properties": {
                "type": {"type": "string", "enum": ["math_expression", "zscore_anomaly", "execute_script", "shell_command"]},
                "expression": {"type": "string"},
                "dataset": {"type": "array", "items": {"type": "number"}},
                "script_content": {"type": "string"},
                "script_ext": {"type": "string"},
                "command": {"type": "string"},
                "elevated": {"type": "boolean"}
            }
        },
        "a2a_message": {
            "type": "object",
            "description": "Optional direct gibberTalk message/delegation to another agent in the registry.",
            "properties": {
                "target_agent_id": {"type": "string"},
                "payload": {"type": "object"}
            }
        },
        "scratchpad_updates": {
            "type": "object",
            "description": "Key-value pairs to persist in this agent's scratchpad.",
        },
        "board_room_request": {
            "type": "object",
            "description": "Optional request for a board room debate.",
            "properties": {
                "reason": {"type": "string"},
                "mode": {"type": "string", "enum": ["parallel", "round_robin"]},
            },
            "required": ["reason", "mode"],
        },
    },
    "required": ["result", "confidence", "status"],
}


# ---------------------------------------------------------------------------
# Retry Configuration & Error Handling (Phase 0)
# ---------------------------------------------------------------------------

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_BASE: float = 2.0   # seconds
RETRY_JITTER_MAX: float = 0.5     # seconds — random added to each wait

# Expanded retryable error keywords covering 503, 502, 500, 504, 429, timeouts
RETRYABLE_ERROR_PATTERNS = [
    "429", "503", "502", "500", "504",
    "resource_exhausted", "unavailable", "service unavailable",
    "bad gateway", "internal server error", "gateway timeout",
    "connection reset", "connection refused", "timeout", "timed out"
]


# ---------------------------------------------------------------------------
# SubAgentNode
# ---------------------------------------------------------------------------

class SubAgentNode:
    """
    A single worker node in the axeAI framework.

    Each instance is created by CommunicationHub when a1.py assigns a
    new role + task pair. The instance is then cached in comm_hub's
    active_connections so it can be reused across broadcast rounds.

    Parameters
    ----------
    agent_id : str
        Unique identifier. Format: "<role_slug>_<index>" e.g. "code_writer_1".
    role : str
        Human-readable role name. e.g. "Senior Python Developer".
    system_instruction : str
        Full role-definition prompt generated by a1.py. Placed in the Gemini
        API's system_instruction field (cached natively by the API).
    api_key : str
        Sticky API key assigned at spawn time by CommunicationHub (round-robin).
        Never rotated during this node's lifetime to preserve cache continuity.
    context_hub : ContextHub
        Shared state database. Injected at construction time.
    model_name : str
        Gemini model identifier. Defaults to "gemini-2.5-flash-lite".
    """

    def __init__(
        self,
        agent_id: str,
        role: str,
        system_instruction: str,
        api_key: str,
        context_hub,  # ContextHub — avoid circular import by using duck typing
        model_name: str = "gemini-2.5-flash-lite",
    ):
        self.agent_id = agent_id
        self.role = role
        self.system_instruction = system_instruction
        self.context_hub = context_hub
        self.model_name = model_name
        self.cycle_count = 0  # Track execution cycles for review triggers

        # Store only the first 8 chars of the key for safe log references
        self._api_key_prefix = api_key[:8] if api_key else "NONE"

        # Initialise the Gemini async client — sticky to this node's lifetime
        self._client = genai.Client(api_key=api_key)

        logger.info(
            "SubAgentNode '%s' spawned | role: '%s' | model: %s | key: %s...",
            agent_id, role, model_name, self._api_key_prefix,
        )

    # -----------------------------------------------------------------------
    # Prefix-Cache Input Builder
    # -----------------------------------------------------------------------

    def _build_serialised_prompt(self, task_prompt: str) -> str:
        """
        Build and serialise the canonical prefix-cache payload.

        ┌─────────────────────────────────────────────────────────┐
        │  STATIC TOP  — byte-identical across ALL cycles         │
        │  for this agent instance. Gemini caches this prefix.    │
        │                                                         │
        │  Contains ONLY true invariants:                         │
        │    - output_format  : response contract (never changes) │
        │    - env_context    : framework/env metadata            │
        │    - unv_context    : universal axeAI principles        │
        │    - agent_id       : set once at spawn                 │
        │    - role           : set once at spawn                 │
        ├─────────────────────────────────────────────────────────┤
        │  DYNAMIC BOTTOM — changes every cycle                   │
        │                                                         │
        │  Contains volatile state:                               │
        │    - scratchpad  : agent's working memory (mutates)     │
        │    - history     : interaction log (grows each turn)    │
        │    - task_prompt : the current assignment               │
        └─────────────────────────────────────────────────────────┘

        CRITICAL: scratchpad and history MUST stay in the "task" block.
        Moving them to "context" would invalidate the cache on every call
        because their content changes — defeating the entire strategy.
        """
        # Set review status requirements if we are at N-cycle review (e.g. every 5 cycles)
        is_review_cycle = (self.cycle_count + 1) % 5 == 0
        review_instruction = ""
        if is_review_cycle:
            review_instruction = (
                " Note: This is a periodic REVIEW cycle. You MUST set status.type "
                "to 'REVIEW' and explain your progress or challenges in status.message. "
                "Include a brief summary of the last 2 interactions if relevant."
            )

        static_context = {
            "output_format": (
                "Respond with a valid JSON object matching the schema: "
                "{result: string, confidence: float 0-1, "
                "status: {type: 'WORKING'|'COMPLETED'|'REVIEW', message: string}, "
                "scratchpad_updates?: object, board_room_request?: "
                "{reason: string, mode: 'parallel'|'round_robin'}}"
            ),
            "env_context": (
                "You are a node in the axeAI multi-agent framework. "
                "Your Orchestrator (S) has decomposed a complex task and "
                "assigned you a specific sub-task. Execute it with precision. "
                "Do not deviate from your assigned role."
                + review_instruction
            ),
            "unv_context": (
                "Universal principles: (1) Accuracy over speed. "
                "(2) If you are uncertain, express it in confidence score. "
                "(3) Use scratchpad_updates to persist findings for future cycles. "
                "(4) Only request a board_room if genuinely blocked by ambiguity "
                "that requires multi-agent consensus."
            ),
            "agent_id": self.agent_id,
            "role": self.role,
        }

        # --- DYNAMIC BLOCK (bottom) — changes every cycle ---
        dynamic_task = {
            "scratchpad": self.context_hub.get_scratchpad(self.agent_id),
            "history": self.context_hub.get_agent_history(self.agent_id),
            "task_prompt": task_prompt,
        }

        payload = {
            "context": static_context,  # <-- STATIC TOP  (cache hits here)
            "task": dynamic_task,       # <-- DYNAMIC BOTTOM (tail tokens)
        }

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # -----------------------------------------------------------------------
    # Core Execute Method (with Exponential Backoff + Jitter)
    # -----------------------------------------------------------------------

    async def execute(self, task_prompt: str) -> dict:
        """
        Run one execution cycle for this sub-agent.
        """
        self.cycle_count += 1
        serialised_prompt = self._build_serialised_prompt(task_prompt)

        logger.info(
            "[%s] execute() — cycle %d — task preview: '%s...'",
            self.agent_id,
            self.cycle_count,
            task_prompt[:60],
        )

        last_exception: Exception | None = None

        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            try:
                logger.debug(
                    "[%s] API call attempt %d/%d (model: %s).",
                    self.agent_id, attempt, RETRY_MAX_ATTEMPTS, self.model_name
                )

                response = await self._client.aio.models.generate_content(
                    model=self.model_name,
                    contents=serialised_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=self.system_instruction,
                        response_mime_type="application/json",
                    ),
                )

                response_text = response.text or ""
                if not response_text.strip():
                    raise ValueError("Empty response returned from Gemini API.")

                # --- Parse structured JSON response ---
                try:
                    parsed = json.loads(response_text)
                except json.JSONDecodeError as parse_err:
                    raise ValueError(
                        f"Response was not valid JSON: {parse_err}\n"
                        f"Raw response: {response_text[:200]}"
                    ) from parse_err

                # --- Persist scratchpad updates if agent provided them ---
                scratchpad_updates = parsed.get("scratchpad_updates")
                if scratchpad_updates and isinstance(scratchpad_updates, dict):
                    self.context_hub.update_scratchpad(
                        self.agent_id, scratchpad_updates
                    )

                # --- Log the interaction to history ---
                self.context_hub.save_sub_agent_interaction(
                    agent_id=self.agent_id,
                    prompt=task_prompt,
                    response=parsed.get("result", response_text),
                )

                logger.info(
                    "[%s] execute() complete | confidence: %.2f | "
                    "board_room_request: %s",
                    self.agent_id,
                    parsed.get("confidence", 0.0),
                    "YES" if parsed.get("board_room_request") else "no",
                )

                return parsed

            except Exception as exc:
                last_exception = exc
                err_msg = str(exc).lower()
                is_retryable = any(pattern in err_msg for pattern in RETRYABLE_ERROR_PATTERNS)

                if attempt == RETRY_MAX_ATTEMPTS or not is_retryable:
                    # All retries exhausted or non-retryable fatal error
                    logger.error(
                        "[%s] Execution failed (attempt %d/%d, retryable=%s): %s. "
                        "Raising AgentExecutionError for circuit breaker.",
                        self.agent_id, attempt, RETRY_MAX_ATTEMPTS, is_retryable, exc,
                    )
                    raise AgentExecutionError(
                        agent_id=self.agent_id,
                        api_key_prefix=self._api_key_prefix,
                        cause=exc,
                    ) from exc

                # --- Exponential backoff with jitter ---
                wait_time = (RETRY_BACKOFF_BASE ** attempt) + random.uniform(
                    0, RETRY_JITTER_MAX
                )
                logger.warning(
                    "[%s] Attempt %d/%d encountered recoverable error: %s. Retrying in %.2fs...",
                    self.agent_id, attempt, RETRY_MAX_ATTEMPTS, exc, wait_time,
                )
                await asyncio.sleep(wait_time)

        # Should be unreachable, but satisfies type checker
        raise AgentExecutionError(
            agent_id=self.agent_id,
            api_key_prefix=self._api_key_prefix,
            cause=last_exception or RuntimeError("Unknown failure"),
        )