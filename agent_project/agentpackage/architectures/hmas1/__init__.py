"""HMAS-1: a central LLM proposes a full mission plan once (last STEP is
FINISHED); each robot votes AGREE or DISAGREE once on that whole plan.
Unanimous AGREE executes the STEPs in order. After each original work STEP
every executor reports STEP_OK or STEP_FAILED; any STEP_FAILED (or a
DISAGREE / new PLAN at vote time) drops the original plan and the fleet
continues as a peer (DMAS) network.
"""
