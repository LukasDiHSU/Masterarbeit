"""HMAS-1 architecture: one central priming plan + turn-based robot dialogue.

Paper mapping (Chen et al.): HMAS-1 is a hybrid variant of DMAS. The central
planner sends exactly one initial plan (to everyone or to one robot) with no
confirmation step; robots then discuss in turn order until ``EXECUTE``.
There is no free mesh chat — turn-taking is orchestrated over the star broker.
"""
