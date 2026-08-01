# axeAI Agent Notes

## Version History
- **v2.0**: Added Multi-Round Loop Execution, Agent Status tags (WORKING/COMPLETED/REVIEW), S-managed planning/evaluation, and dedicated AgentRegistry lifecycle separation.
- **v1.0**: Core structural refactoring, prefix caching payload correction, 3-phase fault tolerance, and dumb router separation.

## Conversation Summary

This conversation clarified the intended architecture and the main design concerns for the framework.

### 1. Current control and data flow
- The user prompt enters through a0.py.
- a0.py boots the runtime and wires up the shared infrastructure, instantiating the AgentRegistry.
- a1.py acts as the orchestrator and decides how the task should be decomposed, dynamically spawning/registering agents via AgentRegistry.
- comm_hub.py routes tasks to registered sub-agents and handles concurrency, retries, and circuit-breaking. It is a dumb highway that queries AgentRegistry.
- aG.py executes the assigned task for each sub-agent and implements AgentRegistry.
- context_hub.py stores memory, including sub-agent history, scratchpads, checkpoints, and the global workforce state.

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

1. **Bootstrap Core & Key Wiring (`a0.py`)**:
   - Integrated dynamic `.env` loading and dual-chain configuration.
   - Refactored `boot_agent_x` to instantiate `AgentRegistry` first and inject it into `CommunicationHub` and `OrchestratorAgent`.
   - Wired bidirectional cross-references (`agent_registry.comm_hub = comm_hub`) to check key quarantine states.
   - Replaced default POSIX signal handles with safe thread-safe Windows fallbacks (`signal.signal`) to eliminate `NotImplementedError` crashes.

2. **Autonomous Multi-Round Orchestration (`a1.py`)**:
   - Implemented Orchestrator (S) loop checking up to `max_rounds=10`.
   - Structured round-based checks evaluating sub-agent statuses (`WORKING`, `COMPLETED`, `REVIEW`).
   - Integrated the 3-phase fault tolerance pipeline allowing task redistribution to remaining healthy nodes on failure.

3. **Lifecycle Separation & Node Execution (`aG.py`)**:
   - Created a standalone `AgentRegistry` to decouple worker creation from message routing.
   - Restructured the execution payload to enforce the prefix-caching contract: keeping static invariants (`agent_id`, `role`, schema) at the top of context block, and shifting volatile states (`scratchpad`, `history`) to the task block.
   - Enforced status updates and automatic `REVIEW` cycle triggers.

4. **Quarantine & Routing Highway (`comm_hub.py`)**:
   - Removed all agent factory dependencies, transforming `CommunicationHub` into a routing vehicle.
   - Implemented thread-safe semaphores, fast-failing task groups, and sticky API key allocations with linear passive cooldown quarantines.

5. **State & Transaction Preservation (`context_hub.py`)**:
   - Initialized a multi-tier memory store (`agent_workforce`, `sub_agent_histories`, `scratchpads`).
   - Added deepcopy-backed transactional safety checkpointing (`save_checkpoint`, `rollback_to_checkpoint`, `commit_checkpoint`) to secure states against broadcast dropouts.