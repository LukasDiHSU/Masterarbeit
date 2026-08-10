"""Conflict-based multi-agent architecture.

Peers work alone by default (plan / navigate / pick). Mesh negotiation opens
only when MCP events involve them (``box_missing``, ``conflict``, …), and only
toward the participant subset. Use ``mission_cli`` to assign solo missions or
inject conflicts.
"""
