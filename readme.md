





{This project has been licensed under the MIT licence. Check LICENCE for details.}

Hey developer — yes, YOU. This project is actively in development. The ideas and architecture are my handiwork, though I happily admit it has not been polished. Expect rough edges and some edge case errors; but it works, and your feedback helps.

Hello, fellow developer! Welcome to axeAI!

axeAI:

a rchitecture for e-
x treme
e xploitation of
A rtificial
I ntelligence


axeAI is an architecture that helps address common bottlenecks (context drift, lack of focus, communication breakdowns, hallucination loops, etc.) in systems that run multiple agentic AIs in parallel.

Supported model backends (configurable): Google AI Studio (default), with preconfigured usage of `gemini-3.6-flash` and `gemini-3.5-flash-lite` for cost/performance tradeoffs. The codebase is written to be provider-agnostic and extensible.

MAJOR FEATURES (current):

- **Orchestrator (`S`)**: Top-level coordinator implemented in `a1.py`. Splits tasks into roles and manages worker agents.
- **JSON-enforced prompt structure**: Agents communicate using strict JSON schemas to reduce ambiguity and make parsing deterministic.
- **Dynamic context via AI Scratchpad**: Agents can write/read a scratchpad to cache context and avoid prompt bloat.
- **Context-caching support**: Automated prompt and context caching to reduce token usage and latency.
- **Parallel Debate (Board Room)**: Agents can request a moderated multi-agent discussion to reconcile differing answers.
- **Multiple Review Protocol**: Periodic orchestrator reviews (every N iterations by default) plus on-demand reviews triggered by agents.

What's new in v1.1:

- **Windows integration**: Added Windows-specific helpers and tooling (`win_tools.py`) and guidance for running on Windows hosts.
- **gibberTalk A2A protocol**: Lightweight agent-to-agent messaging layer for direct, low-latency inter-agent communication.
- **Dynamic model pool**: Runtime model pool selection and cascading fallbacks to balance cost, latency, and capability.
- **Compute sandbox**: Local compute sandbox for running isolated workloads and experiments (`compute_sandbox.py`).
- **Workspace config & helpers**: Added `workspace_config.json` plus convenience scripts and service wiring for cross-platform use.

Other notable changes in this update:

- Added `web_tool.py` for outbound HTTP interactions (sanitized GET -> Markdown + gated POSTs).
- Added `agents.md` and `future.md` for design notes and roadmap.
- Small ergonomics and API stability improvements across the orchestrator and comms layer.

HOW TO RUN (quick):

1) Install dependencies from `requirements.txt`.
2) Run the bootstrapper:

```powershell
python a0.py
```

On Windows, use PowerShell or an appropriate Python virtual environment; `win_tools.py` contains helpers for common Windows integrations.

HOW IT WORKS (high level):

user --"task prompt"--> a0 (system bootstrap)
    -> a1.py (orchestrator `S`) assigns roles and builds prompts
    -> comm_hub.py routes messages between agents
    -> context_hub.py stores scratchpads and cached context
    -> aG.py executes worker-level behavior

S (via `a1.py`) issues structured roles and prompts; `a0.py` boots agents (aG instances) and wires them through `comm_hub.py` and `context_hub.py`.

FUTURE FEATURES:

- Provider pluggable backends and local LLM integrations.
- Graph memory and ABAC-style access control for richer context and safer file mediation.
- Better developer UX, CLI tools, and packaged Docker images for reproducible runs.

Notes:

- This repo is experimental. Expect changes. Open issues for bugs and feature requests.
- Contributions welcome — see `agents.md` and `future.md` for design context.

Enjoy exploring, and tell me what you'd like improved next.


Currently, it supports Google AI Studio API keys, and uses the 'gemini-3.6-flash' and 'gemini-3.5-flash-lite' models.

3.6-flash is smart and not a wallet burner(58.7% on SWE‑Bench Pro, 49% on DeepSWE, 63.9% on MLE‑Bench, 83% on OSWorld‑Verified, and 1421 Elo on GDPval‑AA v2 with 17% fewer tokens on Artificial Analysis Index, up to 65% fewer output tokens on DeepSWE tasks, and a lower output token cost: $7.50 per million tokens (down from $9)..), 

while Flash-lite also has a good reasoning but more importantly much cheaper($0.30 per 1M input tokens, and $2.50 per 1M output tokens), making it an optimum choice for the agents.

{Sorry if this is turning into a bit of a Gemini API ad; I'm just justifiying my choice of models.}

It will be expanded to support configurable AI models and various providers, along with local processing.
[Check: FUTURE FEATURES]


MAJOR FEATURES(current):

-> Organised multi-level chain-of-command structure
    Has a top level orchestrator agent('S') that takes instruction directly from the user and divides it between all the agents.
-> JSON enforced prompt structuring
    Forces the AI to respond in a fixed JSON structure for increased efficiency     
-> Dynamic Context via 'AI Scratchpad'
    Allows the AI to note down context in a 'scratchpad', letting it focus on completing its goals without encurring context drift.
-> Context-cacheing support
    The prompt structure supports automatic context-cacheing/prompt-cacheing(reducing costs and latency).
-> Parallel Debate mode(Board Room)
    Allows any agent to request 'S' to trigger a 'Board Room' discussion where all the agents can discuss and debate.
-> Multiple Review Protocol
    Features a review by the orchestrator every 5 iteration cycles but also allows the worker to trigger it on-demand.

{there are other current features too, please explore the code for the same; they will soon be featured here}


HOW TO RUN:
step 1) Install the repo
step 2) Run a0.py in the respective terminal of your OS(eg for windows, run python a0.py) 

HOW IT WORKS:

its easier for me to explain via a simple flowchart/diagram.

                                                                    |---> a1.py(file for 'S', the orchestr)
user ----"task prompt"----> a0(underlying system) ---- initialise --|---> comm_hub.py(the communication highway for the agents)
                                                                    |---> context_hub.py(the context storage for the agents) 
                                                                
S(via a1) ---- "roles and prompts" ----> a0 ---- "bootup agents" ----> aG

{note 2 self: add rest later}


{note 2 self: add the missing stuffs}