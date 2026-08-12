"""Decentralized architecture with a shared, turn-based message pool
(blackboard).

Agents discuss in fixed turn order until every turn-taker's latest post
starts with ``AGREE``; the server then enters an execute phase. ``DONE``
ends the round only during execute. Agents may call
``start_discussion_round`` to reopen discussion for a replan.
"""
