"""
a0.py — axeAI Control Plane & Bootstrap
=========================================
The single entry point for the axeAI framework.

Responsibilities:
  - Load environment variables (dotenv-first, os.environ fallback)
  - Configure the global logging infrastructure (console + rotating file)
  - Build the API key pool and validate at least one key exists
  - Instantiate ContextHub and CommunicationHub
  - Instantiate and run the OrchestratorAgent
  - Register SIGINT / SIGTERM handlers for graceful async shutdown

Boot sequence:
  1. load_dotenv()           — populate env from ~/.env
  2. _configure_logging()    — set up console + rotating file handler
  3. _build_key_pool()       — collect and validate GEMINI_API_KEY_1..N
  4. _boot_agent_x()         — wire all modules and start the event loop
"""

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Step 1 — Load .env before any os.environ access
# ---------------------------------------------------------------------------
# Try user home directory first (~/.env), then project root (.env).
# os.environ values already set in the shell take precedence over .env
# (load_dotenv does not overwrite existing env vars by default).

_HOME_ENV = Path.home() / ".env"
_LOCAL_ENV = Path(__file__).parent / ".env"

if _HOME_ENV.exists():
    load_dotenv(dotenv_path=_HOME_ENV)
    _env_source = str(_HOME_ENV)
elif _LOCAL_ENV.exists():
    load_dotenv(dotenv_path=_LOCAL_ENV)
    _env_source = str(_LOCAL_ENV)
else:
    _env_source = "os.environ only (no .env file found)"


# ---------------------------------------------------------------------------
# Step 2 — Configure Logging
# ---------------------------------------------------------------------------
# AX_LOG_LEVEL env var sets the global level (DEBUG / INFO / WARNING / ERROR).
# Default: INFO.
# Output: coloured console (StreamHandler) + rotating file (aX.log, 5 MB × 3).

_LOG_LEVEL_NAME = os.environ.get("AX_LOG_LEVEL", "INFO").upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)

_LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _configure_logging() -> None:
    """Configure root logger with console and rotating file handlers."""
    root_logger = logging.getLogger()
    root_logger.setLevel(_LOG_LEVEL)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(_LOG_LEVEL)
    console_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    # Rotating file handler — 5 MB max, keep 3 backups
    log_path = Path(__file__).parent / "aX.log"
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_path,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)  # file always captures DEBUG+
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


_configure_logging()
logger = logging.getLogger("aX.a0")


# ---------------------------------------------------------------------------
# Step 3 — Build API Key Pool
# ---------------------------------------------------------------------------
# Reads GEMINI_API_KEY_1, GEMINI_API_KEY_2, ..., GEMINI_API_KEY_N.
# Stops at the first missing key. Minimum 1 key required.

def _build_key_pool() -> list[str]:
    """
    Collect all configured GEMINI_API_KEY_<N> env vars into a list.
    Raises SystemExit if no valid keys are found.
    """
    pool = []
    index = 1
    while True:
        key = os.environ.get(f"GEMINI_API_KEY_{index}")
        if not key:
            break
        pool.append(key)
        index += 1

    if not pool:
        logger.critical(
            "No API keys found. Set GEMINI_API_KEY_1 (and optionally "
            "GEMINI_API_KEY_2, GEMINI_API_KEY_3, ...) in ~/.env or as "
            "environment variables. Aborting."
        )
        sys.exit(1)

    logger.info("API key pool loaded: %d key(s) available.", len(pool))
    return pool


# ---------------------------------------------------------------------------
# Framework Configuration Constants
# ---------------------------------------------------------------------------
# These can be overridden via environment variables.

MAX_CONCURRENT: int = int(os.environ.get("AX_MAX_CONCURRENT", "5"))
"""Maximum number of sub-agent API calls that may run simultaneously."""

BOARD_ROOM_DEBATE_ROUNDS: int = int(os.environ.get("AX_BOARDROOM_ROUNDS", "3"))
"""Number of debate rounds the Board Room runs before producing consensus."""

ORCHESTRATOR_MODEL: str = os.environ.get(
    "AX_ORCHESTRATOR_MODEL", "gemini-2.5-flash"
)
"""Gemini model used by the OrchestratorAgent (a1.py)."""

SUB_AGENT_MODEL: str = os.environ.get(
    "AX_SUB_AGENT_MODEL", "gemini-2.5-flash-lite"
)
"""Gemini model used by all SubAgentNodes (aG.py)."""


# ---------------------------------------------------------------------------
# Step 4 — Async Boot & Graceful Shutdown
# ---------------------------------------------------------------------------

async def boot_agent_x(user_prompt: str) -> str:
    """
    Main async entry point. Wires all modules and runs one orchestration cycle.

    Parameters
    ----------
    user_prompt : str
        The top-level task description from the user.

    Returns
    -------
    str
        The final synthesised output from the OrchestratorAgent.
    """
    # --- Import here (not at module top) to respect dependency order ---
    from context_hub import ContextHub
    from aG import AgentRegistry
    from comm_hub import CommunicationHub
    from a1 import OrchestratorAgent

    logger.info("=" * 60)
    logger.info("axeAI — Boot sequence initiated.")
    logger.info("Env source : %s", _env_source)
    logger.info("Log level  : %s", _LOG_LEVEL_NAME)
    logger.info("Max concurrent sub-agents : %d", MAX_CONCURRENT)
    logger.info("Board Room debate rounds  : %d", BOARD_ROOM_DEBATE_ROUNDS)
    logger.info("Orchestrator model : %s", ORCHESTRATOR_MODEL)
    logger.info("Sub-agent model    : %s", SUB_AGENT_MODEL)
    logger.info("=" * 60)

    api_key_pool = _build_key_pool()

    # Instantiate the state database (no network calls, no dependencies)
    context_hub = ContextHub()
    logger.info("ContextHub online.")

    # Instantiate the AgentRegistry (lifecycle & creation helper)
    agent_registry = AgentRegistry(context_hub=context_hub, api_key_pool=api_key_pool)
    logger.info("AgentRegistry online.")

    # Instantiate the communication hub (pure router, no LLM calls)
    comm_hub = CommunicationHub(
        api_key_pool=api_key_pool,
        context_hub=context_hub,
        agent_registry=agent_registry,
        max_concurrent=MAX_CONCURRENT,
        board_room_rounds=BOARD_ROOM_DEBATE_ROUNDS,
    )
    logger.info("CommunicationHub online.")

    # Link the comm_hub back to agent_registry so it can check circuit breaker state
    agent_registry.comm_hub = comm_hub

    # Instantiate the orchestrator (owns LLM, task decomposition, synthesis)
    orchestrator = OrchestratorAgent(
        api_key=api_key_pool[0],  # Orchestrator always uses the primary key
        comm_hub=comm_hub,
        context_hub=context_hub,
        agent_registry=agent_registry,
        model_name=ORCHESTRATOR_MODEL,
        sub_agent_model=SUB_AGENT_MODEL,
    )
    logger.info("OrchestratorAgent (S) online.")
    logger.info("axeAI — Boot complete. Starting orchestration run.")
    logger.info("=" * 60)

    # Run the orchestration cycle
    result = await orchestrator.run(user_prompt)

    logger.info("=" * 60)
    logger.info("axeAI — Orchestration complete.")
    logger.info("=" * 60)

    return result


def _register_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """
    Register OS signal handlers for graceful shutdown.
    On SIGINT (Ctrl+C) or SIGTERM, cancel all running tasks and stop the loop.
    Windows does not support loop.add_signal_handler; skip it or use standard signal on Windows.
    """
    def _shutdown(sig_name: str) -> None:
        logger.warning("Signal %s received — initiating graceful shutdown.", sig_name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    if sys.platform != "win32":
        try:
            loop.add_signal_handler(signal.SIGINT, lambda: _shutdown("SIGINT"))
            loop.add_signal_handler(signal.SIGTERM, lambda: _shutdown("SIGTERM"))
        except NotImplementedError:
            pass
    else:
        # On Windows, we can use standard signal library handler for SIGINT
        def win_handler(sig, frame):
            logger.warning("SIGINT received on Windows — initiating graceful shutdown.")
            # Schedule cancellation of all tasks on the loop
            for task in asyncio.all_tasks(loop):
                loop.call_soon_threadsafe(task.cancel)
        try:
            signal.signal(signal.SIGINT, win_handler)
        except ValueError:
            # signal.signal must be run in main thread, ignore if run elsewhere
            pass


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Collect the user prompt from command-line args or prompt interactively
    if len(sys.argv) > 1:
        _user_prompt = " ".join(sys.argv[1:])
    else:
        _user_prompt = input("axeAI > Enter your task: ").strip()
        if not _user_prompt:
            logger.error("No task provided. Exiting.")
            sys.exit(1)

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    _register_signal_handlers(_loop)

    try:
        _final_output = _loop.run_until_complete(boot_agent_x(_user_prompt))
        print("\n" + "=" * 60)
        print("axeAI RESULT:")
        print("=" * 60)
        print(_final_output)
    except asyncio.CancelledError:
        logger.info("axeAI — Shutdown complete (tasks cancelled).")
    except Exception as exc:
        logger.critical("axeAI — Fatal error during boot: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        _loop.close()
        logger.info("axeAI — Event loop closed.")