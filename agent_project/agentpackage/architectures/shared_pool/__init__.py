"""Decentralized architecture with a shared, turn-based message pool
(blackboard).

There is no master and no point-to-point addressing. Every agent -- and the
human user -- connects to the same :mod:`message_pool` server, sees every
message ever posted (new joiners get the full history replayed to them),
and can post a new message that is broadcast to everyone, including the
poster. On top of that broadcast layer, the pool server enforces a fixed
round-robin speaking order (``config.POOL_TURN_ORDER``): only the agent
whose turn it is may post, and a message from the user always restarts the
round. As soon as any agent posts a message containing the word ``DONE``,
the round ends. What each agent *says* on its turn is still entirely its
own decision -- only *when* it may speak is centrally enforced, by the pool
itself rather than by any one privileged agent.
"""
