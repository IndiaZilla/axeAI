# axeAI Agent Notes

## Version History
- **v1.1**: Expanded API Error Handling (503, 502, 500, 504, 429, socket errors), Cold-Start Boot Failover (`gemini-3.1-pro` -> `gemini-3.7-flash`), Dynamic Model Pool Delegation (`HEAVY_LOGIC`, `PARSING_FORMATTING`, `DEEP_REASONING_AUDIT`), WorkspaceManager Directory Mounting & Virtual Tree Indexing, Mediated File RBAC Permissions (`READ`, `SUGGEST`, `EDIT`), Outbound WebTool (`GET` with HTML sanitization & S-approved `POST`), and CLI tools.
- **v2.0 (Historical Loop)**: Added Multi-Round Loop Execution, Agent Status tags (WORKING/COMPLETED/REVIEW), S-managed planning/evaluation, and dedicated AgentRegistry lifecycle separation.
- **v1.0**: Core structural refactoring, prefix caching payload correction, 3-phase fault tolerance, and dumb router separation.

## Conversation Summary

This conversation clarified the intended architecture and the main design concerns for the framework.

### 1. Current control and data flow
- The user prompt enters through a0.py.
- a0.py boots the runtime and wires up the shared infrastructure, instantiating the AgentRegistry and WorkspaceManager.
- a1.py acts as the orchestrator and decides how the task should be decomposed, dynamically spawning/registering agents via AgentRegistry with model_pool allocations.
- comm_hub.py routes tasks to registered sub-agents and handles concurrency, retries, and circuit-breaking. It is a dumb highway that queries AgentRegistry.
- aG.py executes the assigned task for each sub-agent and implements AgentRegistry, mediated file requests, and web operations.
- context_hub.py stores memory, including sub-agent history, scratchpads, checkpoints, the global workforce state, WorkspaceManager, and RBAC PermissionMatrix.

### 2. Multi-cycle execution
- S loops up to max_rounds, reviewing sub-agent status (WORKING, COMPLETED, REVIEW) and deciding whether to relieve, redirect, or spawn new agents.
- On REVIEW status (automatic every N=5 cycles or on error), S evaluates the agent's history and intervenes.

### 3. Separation of concerns
- "The highway routes work, it does not build cars." Agent registry/lifecycle is completely separated from comm_hub.py.

## Design Principle

The framework follows this rule:

- “The highway routes work.”
- “The orchestrator decides what work exists.”
- "The registry builds and holds the workforce."
- “The memory layer preserves state.”
- “The agents execute the assigned work.”

## Summary of Work Done

1. **Phase 0 — Expanded API Error Handling (`aG.py`, `a0.py`)**:
   - Expanded retryable and recoverable error matching to intercept `503 Service Unavailable`, `502 Bad Gateway`, `500 Internal Server Error`, `504 Gateway Timeout`, and network connection reset exceptions.
   - Enforced exponential backoff + jitter before activating circuit breakers.

2. **Phase 1 — Orchestrator S Cold Start Protocol & Model Pool Delegation (`a1.py`, `a0.py`)**:
   - Initial cold-start bootstrap decomposes tasks using `gemini-3.1-pro` with instant failover to `gemini-3.7-flash`.
   - Subsequent operational reviews run on `gemini-3.7-flash` / `gemini-3.6-flash`.
   - Integrated `call_model_with_cascade()` helper with multi-tier model fallback.
   - Enabled `model_assignment` per sub-agent (`HEAVY_LOGIC`, `PARSING_FORMATTING`, `DEEP_REASONING_AUDIT`).

3. **Phase 2 — Local Directory Mount & Virtual File Tree (`context_hub.py`, `a0.py`)**:
   - Implemented `WorkspaceManager` with `workspace_config.json` alias tracking.
   - Built ASCII virtual tree indexer excluding `.git`, `node_modules`, `__pycache__`, and virtualenvs.
   - Added CLI subcommands: `python a0.py mount <alias> <path>`, `python a0.py tree`, and `python a0.py edit <file_path>`.

4. **Phase 3 — Fine-Grained Mediated File RBAC (`context_hub.py`, `a1.py`, `aG.py`)**:
   - Implemented `PermissionMatrix` enforcing `READ`, `SUGGEST` (git diffs), and `EDIT` access tokens.
   - Sub-agents emit `request_file_access` / `renounce_file_access` payloads mediated strictly by S.
   - Context is injected directly into agent scratchpads and revoked upon task completion.

5. **Phase 4 — Outbound HTTP Web Engine (`web_tool.py`, `a1.py`, `aG.py`)**:
   - Implemented `WebTool` providing asynchronous `GET` (with raw HTML -> Markdown sanitization) and `POST` (gated by Orchestrator approval).

6. **Roadmap (`future.md`)**:
   - Added comprehensive cutting-edge features report (MCP integration, A2A routing, Sandboxing, ABAC, Graph Memory, and OpenTelemetry).