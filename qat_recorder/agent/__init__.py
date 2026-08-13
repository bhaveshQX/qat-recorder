# -*- coding: utf-8 -*-
"""
Remote agent: run the recorder against an application on another machine.

Qat is built for one machine — it discovers the injected server's port through a
file on the local filesystem and the application dials back to 127.0.0.1. Rather
than tunnel around that, the agent runs the Qat client *on the VM*, beside the
application, and only our own protocol crosses the network.

What crosses is the Action IR, which has been the load-bearing abstraction since
Phase 1: small, versioned and already validated. The remote boundary lands
exactly where that seam already was.
"""

from qat_recorder.agent.protocol import (
    PROTOCOL_VERSION, AgentError, Busy, Command, SessionState,
)

__all__ = ["PROTOCOL_VERSION", "AgentError", "Busy", "Command", "SessionState"]
