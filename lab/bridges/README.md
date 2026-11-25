# Lab bridge and motion scripts

These are the cell owner's scripts for Dr. Ameri's two-arm wafer cell, used with permission, copied from the lab folder and kept here for reference. They start the three bridges (`vision_bridge.py`, `pro600_bridge.py`, `m1_bridge.py`), their motion helpers, and a few test tools (`explore.py`, `m1_step_2_disks.py`).

`coilwatch.py` is ours: it prints every change of the Vac_On and Vac_Blow coils with a timestamp.

Local change: in `pro600_bridge.py`, `BLOW_TIME` was raised from 0.1 s to 1.5 s because the wafer stayed on the suction cup after release. The cell owner's original is not in this repo.
