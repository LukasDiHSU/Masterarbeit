"""Centralized (star / hub-and-spoke) multi-agent architecture.

A single :class:`MasterAgent` is the only agent with delegation tools. All
communication is routed through :mod:`agent_bus`'s central broker: robot
agents never talk to each other directly, they only answer requests coming
from the master and send their reply back through the broker.
"""
