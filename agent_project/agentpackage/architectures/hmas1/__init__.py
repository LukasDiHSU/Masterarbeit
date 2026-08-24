"""HMAS-1: a central LLM proposes a full multi-step mission plan once
(last STEP is FINISHED). Robots vote AGREE or DISAGREE once on that whole
plan. Unanimous AGREE executes STEPs in order; after each work STEP the
executor reports STEP_OK or STEP_FAILED. DISAGREE or STEP_FAILED discards
the original plan and the fleet continues as DMAS peers.
"""
