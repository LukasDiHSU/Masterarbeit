"""HMAS-2 architecture: centralized planning with local robot feedback.

Paper mapping (Chen et al., "Scalable Multi-Robot Collaboration with Large
Language Models"): HMAS-2 is a hybrid variant of CMAS. A central LLM drafts
actions for the fleet; each robot has a local LLM that reviews its assigned
action and sends AGREE / DISAGREE feedback. On disagreement the central
agent re-plans. The loop ends only when every involved local agent agrees;
robots then execute the approved plan. Robots never talk to each other —
all traffic stays on the star broker.
"""
