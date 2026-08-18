"""HMAS-1: a central LLM proposes a full mission plan once (last STEP is
FINISHED); each robot votes AGREE or DISAGREE once on that whole plan.
Unanimous AGREE executes the STEPs in order. DISAGREE or a new PLAN
discards the original plan and the fleet continues as a peer (DMAS) network.
"""
