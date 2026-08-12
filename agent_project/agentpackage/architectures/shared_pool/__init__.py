"""Decentralized architecture with a shared message pool (blackboard).

Agents discuss in fixed turn order until every turn-taker's latest post
starts with ``AGREE``; the server then wakes **all** agents to execute the
agreed plan in parallel. ``DONE`` ends the round only during execute.
Agents may call ``start_discussion_round`` to reopen discussion for a replan.
"""
