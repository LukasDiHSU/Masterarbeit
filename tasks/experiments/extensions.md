# Extensions (later)

Keep these out of the core Easy/Medium/Hard grid:

1. **Tighter lidar cone** — `AGENT_SEM_LIDAR_FOV_DEG` below 360 so spawn does not see every class.
2. **Forced detection conflict** — camera and semantic lidar disagree on class.
3. **Nav abort mid-drive** — recovery; sense again, then the next centroid.
4. **Validator agent** — extra specialist that only checks the tour order.
