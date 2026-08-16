"""
a1.py — axeAI Orchestrator Agent (S)
======================================
The cognitive centre of the axeAI framework. High-level reasoning, planning,
and ultimate synthesis live here. comm_hub.py and aG.py are its instruments.

Responsibilities:
  - Decompose user prompts into structured agent plans via response_schema
  - Generate role-specific system instructions for each SubAgentNode
  - Own the three-phase fault-tolerance pipeline (backoff → circuit breaker
    → orchestrator redistribution)
  - Detect and approve/deny Board Room requests from sub-agents
  - Synthesise all sub-agent results into a coherent final response

Separation of Concerns:
  - a1.py OWNS task content and agent identity. It generates system_instructions.
  - comm_hub.py is a dumb router. It receives fully-formed specs and routes them.
  - context_hub.py is the memory. a1.py calls save/load; never accesses internals.

Three-Phase Fault-Tolerance Pipeline (Q13):
  Phase 1 — aG.py node: exponential backoff + jitter (max 3 retries)
  Phase 2 — comm_hub.py: circuit breaker (mark key dead, TaskGroup fail-fast)
  Phase 3 — a1.py: catch BroadcastFailure → rollback checkpoint → redistribute
             tasks from failed agents to surviving healthy agents → retry round.
             If retry also fails → raise OrchestratorFailure.
"""

import asyncio
import json
import logging
from typing import Any

from google import genai  # type: ignore
from google.genai import types  # type: ignore

from comm_hub import CommunicationHub, BroadcastFailure

logger = logging.getLogger("aX.a1")


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------

class OrchestratorFailure(Exception):
    """
    Raised when a1.py cannot recover from a BroadcastFailure even after
    task redistribution. Propagates up to a0.py for graceful shutdown.
    """


# ---------------------------------------------------------------------------
# Orchestrator System Prompt
# ---------------------------------------------------------------------------
# This is S's identity. It is passed as system_instruction to every
# orchestrator API call. It NEVER changes at runtime.

ORCHESTRATOR_SYSTEM_PROMPT = """
You are S — the Orchestrator of the axeAI multi-agent framework.

## Your Identity
You are an expert systems architect and strategic coordinator. You do not
execute tasks directly. Instead, you decompose complex problems into precise
sub-tasks and assign them to specialised sub-agent workers.

## Your Decomposition Philosophy
1. Identify the atomic units of work that can be parallelised without dependencies.
2. Assign each unit to a specialist role tailored exactly to that unit.
3. Write system instructions that are tightly scoped — agents must not be
   generalists. A "Senior Security Auditor" should audit only, not write code.
4. Calibrate the number of agents to the complexity of the task. Never spawn
   agents that have no real work to do.

## Your Board Room Authority
You have the power to APPROVE or DENY Board Room requests from sub-agents.
- APPROVE if: the request identifies a genuine ambiguity that would cause
  incorrect output without multi-agent consensus.
- DENY if: the request is speculative, the agent could resolve it independently,
  or the overhead of a debate outweighs the marginal benefit.
- When you approve, write a clear opening statement that frames the exact
  question to be debated so agents stay on topic.

## Your Synthesis Mandate
When compiling the final answer:
- Integrate all sub-agent outputs into a unified, coherent response.
- Resolve any remaining disagreements using your own judgment.
- Do not simply concatenate agent outputs. Synthesise them.
- The user sees only your final output — make it excellent.

## Output Discipline
Every structured output you produce must be valid JSON matching the schema
provided. Precision and schema compliance are non-negotiable.
"""

# ---------------------------------------------------------------------------
# Task Decomposition Response Schema
# ---------------------------------------------------------------------------
# Enforced via response_mime_type="application/json" + response_schema.
# a1.py's first LLM call must return exactly this structure.

DECOMPOSITION_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "agents": types.Schema(
            type=types.Type.ARRAY,
            description=(
                "The list of sub-agent specs to execute in parallel. "
                "Each agent gets a unique role, tightly-scoped task, and model assignment."
            ),
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "agent_id": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Unique snake_case identifier. Format: "
                            "<role_slug>_<index> e.g. 'code_writer_1'."
                        ),
                    ),
                    "role": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Human-readable role title. Specific and senior. "
                            "e.g. 'Senior Python Developer', 'Security Auditor'."
                        ),
                    ),
                    "model_assignment": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Allocated model from the pool: 'HEAVY_LOGIC' (3.7-flash), "
                            "'PARSING_FORMATTING' (3.1-flash-lite), or 'DEEP_REASONING_AUDIT' (3.1-pro)."
                        ),
                    ),
                    "system_instruction": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Full role-definition prompt for this agent. Must define: "
                            "the agent's expertise, output constraints, and what "
                            "it must NOT do (out-of-scope work)."
                        ),
                    ),
                    "task": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "The specific, atomic task this agent must execute. "
                            "Be precise. Ambiguity leads to hallucination."
                        ),
                    ),
                },
                required=["agent_id", "role", "system_instruction", "task"],
            ),
        ),
        "reasoning": types.Schema(
            type=types.Type.STRING,
            description=(
                "S's internal reasoning for this decomposition. "
                "Explains why these specific agents and tasks were chosen."
            ),
        ),
    },
    required=["agents", "reasoning"],
)

# ---------------------------------------------------------------------------
# Board Room Approval Response Schema
# ---------------------------------------------------------------------------

BOARD_ROOM_APPROVAL_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "approved": types.Schema(
            type=types.Type.BOOLEAN,
            description="True if the Board Room request is approved, False if denied.",
        ),
        "reason": types.Schema(
            type=types.Type.STRING,
            description="S's reasoning for the approval or denial decision.",
        ),
        "opening_statement": types.Schema(
            type=types.Type.STRING,
            description=(
                "If approved: S's opening statement that frames the debate topic. "
                "If denied: empty string."
            ),
        ),
    },
    required=["approved", "reason", "opening_statement"],
)


# ---------------------------------------------------------------------------
# OrchestratorAgent
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Round Review Response Schema
# ---------------------------------------------------------------------------
# Enforced via response_mime_type="application/json" + response_schema.
# Used by S at the end of each round to evaluate all active agent work.

ROUND_REVIEW_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "relieved_agents": types.Schema(
            type=types.Type.ARRAY,
            description="List of agent IDs to relieve (task completed successfully).",
            items=types.Schema(type=types.Type.STRING)
        ),
        "redirections": types.Schema(
            type=types.Type.ARRAY,
            description="Active agents to redirect or instruct to continue working.",
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "agent_id": types.Schema(type=types.Type.STRING),
                    "comment": types.Schema(type=types.Type.STRING, description="Feedback explaining what is incomplete, incorrect, or the next objective."),
                    "next_task": types.Schema(type=types.Type.STRING, description="The updated prompt or next sub-task for the agent.")
                },
                required=["agent_id", "comment", "next_task"]
            )
        ),
        "new_agents": types.Schema(
            type=types.Type.ARRAY,
            description="New agents to dynamically spawn and register.",
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "agent_id": types.Schema(type=types.Type.STRING, description="Unique snake_case identifier."),
                    "role": types.Schema(type=types.Type.STRING, description="Specialist role title."),
                    "model_assignment": types.Schema(type=types.Type.STRING, description="Model category ('HEAVY_LOGIC', 'PARSING_FORMATTING', 'DEEP_REASONING_AUDIT')."),
                    "system_instruction": types.Schema(type=types.Type.STRING, description="Full persona/system instructions."),
                    "task": types.Schema(type=types.Type.STRING, description="Initial sub-task assigned to the new agent.")
                },
                required=["agent_id", "role", "system_instruction", "task"]
            )
        ),
        "overall_complete": types.Schema(
            type=types.Type.BOOLEAN,
            description="Set to true if the entire user goal is accomplished and no further rounds are needed."
        ),
        "reasoning": types.Schema(
            type=types.Type.STRING,
            description="S's reasoning detailing the progress of the workforce and next steps."
        )
    },
    required=["relieved_agents", "redirections", "new_agents", "overall_complete", "reasoning"]
)


# ---------------------------------------------------------------------------
# OrchestratorAgent
# ---------------------------------------------------------------------------

class OrchestratorAgent:
    """
    The strategic coordinator of the axeAI framework.

    Parameters
    ----------
    api_key : str
        The primary API key used by S. Always the first key in the pool.
    comm_hub : CommunicationHub
        The routing layer.
    context_hub : ContextHub
        The state database.
    agent_registry : AgentRegistry
        The registry helper managing node lifecycle.
    boot_model_name : str
        Initial cold-start model (e.g. gemini-3.1-pro).
    default_model_name : str
        Operational loop default model (e.g. gemini-3.7-flash).
    sub_agent_model : str
        Default sub-agent fallback model name.
    model_pool : dict
        Mapping of role archetypes to recommended models.
    """

    def __init__(
        self,
        api_key: str,
        comm_hub: CommunicationHub,
        context_hub,  # ContextHub — duck-typed
        agent_registry,  # AgentRegistry — duck-typed
        boot_model_name: str = "gemini-3.1-pro",
        default_model_name: str = "gemini-3.7-flash",
        sub_agent_model: str = "gemini-3.1-flash-lite",
        model_pool: dict | None = None,
    ):
        self.comm_hub = comm_hub
        self.context_hub = context_hub
        self.agent_registry = agent_registry
        self.boot_model_name = boot_model_name
        self.model_name = default_model_name
        self.sub_agent_model = sub_agent_model
        self.model_pool = model_pool or {
            "HEAVY_LOGIC": "gemini-3.7-flash",
            "PARSING_FORMATTING": "gemini-3.1-flash-lite",
            "DEEP_REASONING_AUDIT": "gemini-3.1-pro",
        }

        # S has its own sticky Gemini client
        self._client = genai.Client(api_key=api_key)

        logger.info(
            "OrchestratorAgent (S) initialised | boot_model: %s | default_model: %s | sub_agent_model: %s",
            boot_model_name, default_model_name, sub_agent_model,
        )

    # -----------------------------------------------------------------------
    # Main Multi-Round Orchestration Run
    # -----------------------------------------------------------------------

    async def run(self, user_prompt: str, max_rounds: int = 10) -> str:
        """
        Execute an autonomous multi-round orchestration loop.
        """
        logger.info("=" * 60)
        logger.info("[S] Starting multi-round run | User prompt: %s...", user_prompt[:80])

        current_round = 1
        overall_complete = False
        all_agent_results: dict[str, dict] = {}
        board_room_summaries: dict[str, str] = {}

        # Round 1 Setup: Decompose the task into initial sub-agents using cold-start model
        logger.info("[S] Round 1: Cold-start task decomposition (model: %s)...", self.boot_model_name)
        initial_agents = await self._decompose_task(user_prompt)

        if not initial_agents:
            raise OrchestratorFailure("Initial decomposition produced no agents.")

        # Register and configure initial agents with model delegation
        for spec in initial_agents:
            agent_id = spec["agent_id"]
            model_category = spec.get("model_assignment", "PARSING_FORMATTING")
            assigned_model = self.model_pool.get(model_category, self.sub_agent_model)

            self.agent_registry.register_agent(
                agent_id=agent_id,
                role=spec["role"],
                system_instruction=spec["system_instruction"],
                model_name=assigned_model
            )
            self.context_hub.update_workforce_agent(
                agent_id=agent_id,
                role=spec["role"],
                status="WORKING",
                current_task=spec["task"]
            )

        # Main Multi-Round Loop (Switch S default model for operational review)
        while current_round <= max_rounds and not overall_complete:
            round_id = f"round_{current_round}"
            logger.info("=" * 50)
            logger.info("[S] Executing Execution Round %d / %d", current_round, max_rounds)

            # Get current active workforce assignments
            workforce = self.context_hub.get_workforce()
            active_tasks = []
            for aid, state in workforce.items():
                if state["status"] in ["WORKING", "REVIEW"]:
                    active_tasks.append({
                        "agent_id": aid,
                        "task": state["current_task"]
                    })

            if not active_tasks:
                logger.info("[S] No active tasks. Terminating loop.")
                break

            # Broadcast tasks using TaskGroup and Semaphore under 3-phase fault tolerance
            logger.info("[S] Broadcasting tasks for %d active agent(s)...", len(active_tasks))
            self.context_hub.save_checkpoint(round_id)
            try:
                results = await self._broadcast_with_recovery(
                    agent_specs=active_tasks,
                    user_prompt=user_prompt,
                    round_id=round_id
                )
                self.context_hub.commit_checkpoint(round_id)
            except BroadcastFailure as bf:
                self.context_hub.rollback_to_checkpoint(round_id)
                logger.error("[S] Broadcast recovery failed in round %d.", current_round)
                raise OrchestratorFailure(f"Recovery failed in round {current_round}") from bf

            # Merge results into global tracking map
            all_agent_results.update(results)

            # Process sub-agent status tag updates, file access requests, and web requests
            for aid, res in results.items():
                agent_status = res.get("status", {})
                status_type = agent_status.get("type", "WORKING")
                status_msg = agent_status.get("message", "")
                role = workforce[aid]["role"]
                task = workforce[aid]["current_task"]
                self.context_hub.update_workforce_agent(aid, role, status_type, task)
                logger.info("[%s] Declared status: %s — Msg: %s", aid, status_type, status_msg)

                # Process Mediated RBAC File Requests (Phase 3)
                file_req = res.get("file_access_request")
                if file_req and isinstance(file_req, dict):
                    action = file_req.get("action")
                    files = file_req.get("files", [])
                    level = file_req.get("level", "READ")
                    reason = file_req.get("reason", "")
                    edits = file_req.get("edits", {})

                    if action == "request_file_access":
                        for target_file in files:
                            try:
                                token = self.context_hub.permission_matrix.grant_access(
                                    agent_id=aid, file_path=target_file, level=level, reason=reason
                                )
                                file_content = ""
                                if level == "READ":
                                    file_content = self.context_hub.permission_matrix.read_file(aid, target_file)
                                elif level == "SUGGEST" and target_file in edits:
                                    diff = self.context_hub.permission_matrix.suggest_diff(aid, target_file, edits[target_file])
                                    file_content = f"Diff generated:\n{diff}"
                                elif level == "EDIT" and target_file in edits:
                                    self.context_hub.permission_matrix.apply_edit(aid, target_file, edits[target_file])
                                    file_content = "Edit successfully applied."

                                self.context_hub.update_scratchpad(
                                    aid, {f"file_context_{target_file}": {"token": token, "level": level, "content": file_content}}
                                )
                                logger.info("[S] Mediated File Access GRANTED to '%s' for '%s' [%s].", aid, target_file, level)
                            except Exception as file_err:
                                logger.warning("[S] Mediated File Access FAILED for '%s': %s", aid, file_err)
                                self.context_hub.update_scratchpad(
                                    aid, {f"file_error_{target_file}": str(file_err)}
                                )
                    elif action == "renounce_file_access":
                        for target_file in files:
                            self.context_hub.permission_matrix.revoke_access(aid, target_file)
                            # Purge from scratchpad to conserve context
                            scratchpad = self.context_hub.get_scratchpad(aid)
                            scratchpad.pop(f"file_context_{target_file}", None)

                # Process In-Memory Compute Sandbox & MathEngine Requests (Phase 4)
                compute_req = res.get("compute_request")
                if compute_req and isinstance(compute_req, dict):
                    req_type = compute_req.get("type")
                    try:
                        from compute_sandbox import MathEngine, CodeSandbox
                        from win_tools import execute_shell_command

                        if req_type == "math_expression":
                            math_res = MathEngine.evaluate_expression(compute_req.get("expression", "0"))
                            self.context_hub.update_scratchpad(aid, {"math_calc_result": math_res})
                            logger.info("[S] MathEngine computed expression for '%s': %s", aid, math_res)
                        elif req_type == "zscore_anomaly":
                            data = compute_req.get("dataset", [])
                            anomaly_res = MathEngine.calculate_zscore_anomalies(data)
                            self.context_hub.update_scratchpad(aid, {"zscore_anomaly_result": anomaly_res})
                            logger.info("[S] MathEngine anomaly analysis for '%s': %d anomalies", aid, anomaly_res.get("anomalies_detected", 0))
                        elif req_type == "execute_script":
                            script_content = compute_req.get("script_content", "")
                            ext = compute_req.get("script_ext", ".py")
                            run_res = await CodeSandbox.execute_script(script_content, ext=ext)
                            self.context_hub.update_scratchpad(aid, {"script_execution_result": run_res})
                            logger.info("[S] CodeSandbox executed script for '%s' (success: %s)", aid, run_res.get("success"))
                        elif req_type == "shell_command":
                            sh_cmd = compute_req.get("command", "")
                            elevated = compute_req.get("elevated", False)
                            sh_res = await execute_shell_command(sh_cmd, elevated=elevated)
                            self.context_hub.update_scratchpad(aid, {"shell_command_result": sh_res})
                            logger.info("[S] Shell command executed for '%s' (returncode: %s)", aid, sh_res.get("returncode"))
                    except Exception as compute_err:
                        logger.error("[S] Compute request execution failed for '%s': %s", aid, compute_err)
                        self.context_hub.update_scratchpad(aid, {"compute_error": str(compute_err)})

                # Process Direct A2A gibberTalk Messages (Phase 2)
                a2a_req = res.get("a2a_message")
                if a2a_req and isinstance(a2a_req, dict):
                    target_id = a2a_req.get("target_agent_id")
                    payload = a2a_req.get("payload", {})
                    if target_id:
                        a2a_res = await self.comm_hub.route_a2a_gibber_message(
                            sender_id=aid, target_agent_id=target_id, payload=payload
                        )
                        self.context_hub.update_scratchpad(aid, {f"a2a_response_{target_id}": a2a_res})
                        logger.info("[S] Direct A2A gibberTalk message routed: %s -> %s", aid, target_id)

                # Process Outbound Web Requests (Phase 4)
                web_req = res.get("web_request")
                if web_req and isinstance(web_req, dict):
                    method = web_req.get("method", "GET")
                    url = web_req.get("url", "")
                    data = web_req.get("data", "")
                    web_reason = web_req.get("reason", "")
                    try:
                        from web_tool import WebTool
                        wt = WebTool()
                        if method == "GET":
                            web_res = await wt.http_get(url)
                            self.context_hub.update_scratchpad(
                                aid, {f"web_result_{url}": web_res[:4000]}
                            )
                        elif method == "POST":
                            logger.info("[S] S Approved Web POST to '%s' (reason: %s)", url, web_reason)
                            web_res = await wt.http_post(url, data=data)
                            self.context_hub.update_scratchpad(
                                aid, {f"web_result_{url}": web_res[:2000]}
                            )
                        await wt.close()
                    except Exception as web_err:
                        logger.warning("[S] Web request failed for '%s': %s", aid, web_err)
                        self.context_hub.update_scratchpad(
                            aid, {f"web_error_{url}": str(web_err)}
                        )

            # Handle Board Room requests from active agents
            logger.info("[S] Evaluating Board Room requests...")
            summaries = await self._handle_board_room_requests(results)
            board_room_summaries.update(summaries)

            # S-managed Review and Planning pass
            logger.info("[S] Evaluating workforce progress...")
            evaluation = await self._review_round(user_prompt, round_id, all_agent_results)

            overall_complete = evaluation.get("overall_complete", False)
            logger.info("[S] Evaluation reasoning: %s", evaluation.get("reasoning", ""))
            logger.info("[S] Overall complete status: %s", overall_complete)

            # Apply relieved agent statuses
            for aid in evaluation.get("relieved_agents", []):
                if aid in workforce:
                    self.context_hub.update_workforce_agent(
                        aid, workforce[aid]["role"], "RELIEVED", workforce[aid]["current_task"]
                    )
                    self.context_hub.permission_matrix.revoke_all_for_agent(aid)
                    logger.info("[S] Relieved agent '%s'.", aid)

            # Apply redirections / task updates
            for redir in evaluation.get("redirections", []):
                aid = redir["agent_id"]
                comment = redir["comment"]
                next_task = redir["next_task"]
                if aid in workforce:
                    self.context_hub.update_scratchpad(aid, {"S_feedback": comment})
                    self.context_hub.update_workforce_agent(
                        aid, workforce[aid]["role"], "WORKING", next_task
                    )
                    logger.info("[S] Redirected agent '%s' with new task: %s", aid, next_task[:60])

            # Spawn new agents
            for new_spec in evaluation.get("new_agents", []):
                aid = new_spec["agent_id"]
                model_category = new_spec.get("model_assignment", "PARSING_FORMATTING")
                assigned_model = self.model_pool.get(model_category, self.sub_agent_model)

                self.agent_registry.register_agent(
                    agent_id=aid,
                    role=new_spec["role"],
                    system_instruction=new_spec["system_instruction"],
                    model_name=assigned_model
                )
                self.context_hub.update_workforce_agent(
                    agent_id=aid,
                    role=new_spec["role"],
                    status="WORKING",
                    current_task=new_spec["task"]
                )
                logger.info("[S] Dynamically spawned new agent '%s' (%s) [model: %s]", aid, new_spec["role"], assigned_model)

            current_round += 1

        # Final Synthesis
        logger.info("[S] Compiling final synthesised answer...")
        final_output = await self._synthesise(
            user_prompt=user_prompt,
            agent_results=all_agent_results,
            board_room_summaries=board_room_summaries
        )

        self.context_hub.log_orchestrator_turn(user_prompt, final_output)
        logger.info("[S] Multi-round orchestration run complete.")
        return final_output

    # -----------------------------------------------------------------------
    # Phase 1: Task Decomposition
    # -----------------------------------------------------------------------

    async def _decompose_task(self, user_prompt: str) -> list[dict]:
        """
        Call the Gemini API to decompose the user prompt into an agent plan.
        Uses cold-start boot model with instant fallback cascade.
        """
        decomposition_prompt = (
            f"User Task:\n{user_prompt}\n\n"
            f"Decompose this task into parallelisable sub-tasks. "
            f"Create specialised agents to handle each sub-task. "
            f"Assign an appropriate model_assignment from ['HEAVY_LOGIC', 'PARSING_FORMATTING', 'DEEP_REASONING_AUDIT']. "
            f"Each agent must have a tightly-scoped role and unambiguous task. "
            f"Produce the agent plan as a structured JSON response."
        )

        from a0 import call_model_with_cascade

        config = types.GenerateContentConfig(
            system_instruction=ORCHESTRATOR_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=DECOMPOSITION_SCHEMA,
        )

        response, used_model = await call_model_with_cascade(
            client=self._client,
            model_name=self.boot_model_name,
            contents=decomposition_prompt,
            config=config,
            fallback_cascade=[self.boot_model_name, self.model_name, "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"]
        )

        logger.info("[S] Decomposition generated using model: %s", used_model)

        try:
            plan = json.loads(response.text)
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error("[S] Decomposition JSON parse failed: %s", e)
            raise OrchestratorFailure(f"Decomposition returned invalid JSON: {e}") from e

        agents = plan.get("agents", [])
        return agents

    # -----------------------------------------------------------------------
    # Phase 2: Broadcast with Three-Phase Fault Tolerance Recovery
    # -----------------------------------------------------------------------

    async def _broadcast_with_recovery(
        self,
        agent_specs: list[dict],
        user_prompt: str,
        round_id: str,
    ) -> dict[str, dict]:
        """
        Broadcast tasks with the three-phase fault-tolerance pipeline.
        """
        try:
            results = await self.comm_hub.broadcast_tasks(agent_specs)
            return results
        except BroadcastFailure as bf:
            logger.warning(
                "[S] Phase 3 Recovery: BroadcastFailure caught. "
                "Failed agents: %s. Attempting redistribution.",
                bf.failed_agents,
            )

            # Rollback state to pre-round checkpoint (undo partial writes)
            self.context_hub.rollback_to_checkpoint(round_id)

            # Retrieve details of the failed tasks
            workforce = self.context_hub.get_workforce()
            failed_tasks = {}
            for aid in bf.failed_agents:
                failed_tasks[aid] = {
                    "agent_id": aid,
                    "role": workforce[aid]["role"],
                    "task": workforce[aid]["current_task"]
                }

            # Get surviving agents with live keys
            active_agent_ids = self.comm_hub.get_active_agent_ids()
            surviving_specs = [
                spec for spec in agent_specs
                if spec["agent_id"] in active_agent_ids
                and spec["agent_id"] not in bf.failed_agents
            ]

            if not surviving_specs:
                logger.error("[S] No surviving agents available for redistribution.")
                raise OrchestratorFailure("Broadcast failure with no survivors for redistribution.") from bf

            # Redistribute failed tasks to survivors
            redistributed_specs = await self._redistribute_tasks(
                failed_tasks=failed_tasks,
                surviving_specs=surviving_specs,
                user_prompt=user_prompt,
            )

            logger.info(
                "[S] Redistribution complete. Re-broadcasting %d task(s) to survivors.",
                len(failed_tasks)
            )

            try:
                recovery_results = await self.comm_hub.broadcast_tasks(redistributed_specs)
                return recovery_results
            except BroadcastFailure as bf2:
                logger.error("[S] Recovery broadcast also failed. Raising OrchestratorFailure.")
                raise OrchestratorFailure("Recovery broadcast failed after redistribution.") from bf2

    async def _redistribute_tasks(
        self,
        failed_tasks: dict[str, dict],
        surviving_specs: list[dict],
        user_prompt: str,
    ) -> list[dict]:
        """
        Assign the tasks of failed agents to surviving agents.
        """
        augmented_specs = []
        workforce = self.context_hub.get_workforce()

        for i, (failed_agent_id, failed_spec) in enumerate(failed_tasks.items()):
            target_spec = surviving_specs[i % len(surviving_specs)]
            target_agent_id = target_spec["agent_id"]

            logger.info("[S] Redistributing '%s' task → '%s'", failed_agent_id, target_agent_id)

            # Inject dual-role context into the survivor's scratchpad
            self.context_hub.update_scratchpad(
                target_agent_id,
                {
                    "dual_role_mandate": {
                        "absorbed_from": failed_agent_id,
                        "absorbed_role": failed_spec["role"],
                        "absorbed_task": failed_spec["task"],
                        "note": (
                            "You have absorbed a secondary task from a failed agent. "
                            "Complete your primary task first, then address the "
                            "secondary task. Maintain your primary JSON output schema."
                        ),
                    }
                },
            )

            combined_task = (
                f"[PRIMARY TASK]\n{target_spec['task']}\n\n"
                f"[SECONDARY TASK — absorbed from failed agent '{failed_agent_id}' "
                f"(role: {failed_spec['role']})]\n{failed_spec['task']}\n\n"
                f"Complete both tasks. Your JSON response should address both."
            )

            augmented_spec = {**target_spec, "task": combined_task}
            augmented_specs.append(augmented_spec)

            # Update workforce state
            role = workforce[target_agent_id]["role"]
            self.context_hub.update_workforce_agent(target_agent_id, role, "WORKING", combined_task)

        return augmented_specs

    # -----------------------------------------------------------------------
    # S-Managed Evaluation Pass
    # -----------------------------------------------------------------------

    async def _review_round(
        self,
        user_prompt: str,
        round_id: str,
        results: dict[str, dict]
    ) -> dict:
        """
        Review current status of sub-agents and plan next actions.
        """
        workforce = self.context_hub.get_workforce()
        workforce_summary = json.dumps(workforce, indent=2)
        results_summary = json.dumps(
            {aid: {
                "result": res.get("result", ""),
                "status": res.get("status", {}),
                "confidence": res.get("confidence", 0.0)
            } for aid, res in results.items()},
            indent=2
        )

        review_prompt = (
            f"User Overall Request:\n{user_prompt}\n\n"
            f"Current Round: {round_id}\n\n"
            f"Active Workforce State:\n{workforce_summary}\n\n"
            f"Latest Agent Execution Results:\n{results_summary}\n\n"
            f"Review the progress. Decide:\n"
            f"1. Which agents have finished their task and should be relieved.\n"
            f"2. Which agents need to continue working (provide comment + next_task updates).\n"
            f"3. Do we need to spawn new agents to handle pending items?\n"
            f"4. Is the entire user request completed successfully?"
        )

        response = await self._client.aio.models.generate_content(
            model=self.model_name,
            contents=review_prompt,
            config=types.GenerateContentConfig(
                system_instruction=ORCHESTRATOR_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=ROUND_REVIEW_SCHEMA,
            ),
        )

        try:
            return json.loads(response.text)
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error("[S] Round review JSON parse failed: %s", e)
            return {
                "relieved_agents": [],
                "redirections": [],
                "new_agents": [],
                "overall_complete": False,
                "reasoning": f"Review evaluation JSON parse error: {e}"
            }

    # -----------------------------------------------------------------------
    # Phase 3: Board Room Request Handling
    # -----------------------------------------------------------------------

    async def _handle_board_room_requests(
        self, results: dict[str, dict]
    ) -> dict[str, str]:
        """
        Scan all agent results for board_room_request fields.
        """
        board_room_summaries: dict[str, str] = {}

        for agent_id, result in results.items():
            request = result.get("board_room_request")
            if not request:
                continue

            reason = request.get("reason", "No reason given.")
            mode = request.get("mode", "round_robin")

            logger.info(
                "[S] Board Room request from '%s': '%s...' | mode: %s",
                agent_id, reason[:80], mode,
            )

            # Ask S to approve or deny
            approval = await self._evaluate_board_room_request(
                requester_id=agent_id,
                reason=reason,
                mode=mode,
                current_results=results,
            )

            if not approval["approved"]:
                logger.info(
                    "[S] Board Room DENIED for '%s': %s",
                    agent_id, approval["reason"],
                )
                self.context_hub.update_scratchpad(
                    agent_id,
                    {
                        "board_room_denial": {
                            "reason": approval["reason"],
                            "note": "Proceed independently. S has denied the Board Room.",
                        }
                    },
                )
                continue

            logger.info(
                "[S] Board Room APPROVED for '%s': %s",
                agent_id, approval["reason"],
            )

            # Retrieve active connections list from AgentRegistry
            participant_ids = list(self.agent_registry.get_all_agents().keys())

            # Convene the board room
            summary = await self.comm_hub.convene_board_room(
                agent_ids=participant_ids,
                topic=reason,
                mode=mode,
                orchestrator_opening=approval["opening_statement"],
                requester_id=agent_id,
                synthesis_client=self._client,
                synthesis_model=self.model_name,
            )

            board_room_summaries[agent_id] = summary
            logger.info(
                "[S] Board Room for '%s' complete. Summary captured.", agent_id
            )

        return board_room_summaries

    async def _evaluate_board_room_request(
        self,
        requester_id: str,
        reason: str,
        mode: str,
        current_results: dict[str, dict],
    ) -> dict:
        """
        S evaluates whether a Board Room request merits approval.
        """
        results_summary = json.dumps(
            {aid: r.get("result", "")[:200] for aid, r in current_results.items()},
            indent=2,
        )

        evaluation_prompt = (
            f"A sub-agent has requested a Board Room debate.\n\n"
            f"Requesting Agent: {requester_id}\n"
            f"Requested Mode: {mode}\n"
            f"Reason: {reason}\n\n"
            f"Current agent results summary:\n{results_summary}\n\n"
            f"Decide: APPROVE or DENY this Board Room request."
        )

        response = await self._client.aio.models.generate_content(
            model=self.model_name,
            contents=evaluation_prompt,
            config=types.GenerateContentConfig(
                system_instruction=ORCHESTRATOR_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=BOARD_ROOM_APPROVAL_SCHEMA,
            ),
        )

        try:
            return json.loads(response.text)
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error("[S] Board Room approval JSON parse failed: %s", e)
            return {"approved": False, "reason": f"Parse error: {e}", "opening_statement": ""}

    # -----------------------------------------------------------------------
    # Phase 4: Synthesis
    # -----------------------------------------------------------------------

    async def _synthesise(
        self,
        user_prompt: str,
        agent_results: dict[str, dict],
        board_room_summaries: dict[str, str],
    ) -> str:
        """
        Compile all sub-agent outputs and Board Room summaries into a single response.
        """
        agent_outputs_section = json.dumps(
            {
                agent_id: {
                    "result": res.get("result", ""),
                    "confidence": res.get("confidence", 0.0),
                }
                for agent_id, res in agent_results.items()
            },
            indent=2,
        )

        board_room_section = ""
        if board_room_summaries:
            board_room_section = (
                "\n\nBoard Room Consensus Summaries:\n"
                + json.dumps(board_room_summaries, indent=2)
            )

        synthesis_prompt = (
            f"Original User Request:\n{user_prompt}\n\n"
            f"Sub-Agent Results:\n{agent_outputs_section}"
            f"{board_room_section}\n\n"
            f"Synthesise all of the above into a single, excellent, coherent "
            f"final response for the user. Resolve any conflicts."
        )

        response = await self._client.aio.models.generate_content(
            model=self.model_name,
            contents=synthesis_prompt,
            config=types.GenerateContentConfig(
                system_instruction=ORCHESTRATOR_SYSTEM_PROMPT,
            ),
        )

        final_output = response.text or "Synthesis returned no output."
        logger.info("[S] Synthesis complete.")
        return final_output