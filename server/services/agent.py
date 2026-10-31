"""Agent service — wraps the agent_loop for the server package.

Re-exports stream_agent_response from the local agent_loop module.
"""

from .agent_loop import stream_agent_response  # noqa: F401
