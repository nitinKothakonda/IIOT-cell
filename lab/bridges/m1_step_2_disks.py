#!/usr/bin/env python3
"""M1 Pro step test — two pick levels from the yellow rack.

    py m1_step.py          choose the sequence at the prompt

Disk 1 is the top level: the fork can lift straight out because nothing
is above it.

Disk 2 is below level 1's shelf, so it cannot lift clear from inside the
rack. It lifts just enough to unseat the disk, retracts in -Y to get the
fork out, and only then lifts to transit height.

    *** THE FOUR VALUES MARKED "TEACH" ARE PLACEHOLDERS ***
    They are currently set to disk 1 values, which is SAFE (nothing
    descends lower than already proven) but WRONG (it will not reach
    disk 2). Replace them with values captured by m1_teach.py before
    expecting the level 2 sequence to work.
"""

import socket, time

M1_IP = "192.168.10.40"
SPEED = 30

dash = socket.create_connection((M1_IP, 29999), timeout=10)
move = socket.create_connection((M1_IP, 30003), timeout=60)


def send(sock, cmd):
    sock.sendall((cmd + "\n").encode())
    r = sock.recv(1024).decode().strip()
    print(f"    {cmd} -> {r}")
    return r


def status():
    print("  --- status ---")
    send(dash, "RobotMode()")
    send(dash, "GetErrorID()")


def recover():
    print("  --- clearing error ---")
    send(dash, "ClearError()")
    time.sleep(0.5)
    send(dash, "EnableRobot()")
    time.sleep(2)
    send(dash, f"SpeedFactor({SPEED})")


# ---------------- disk 1, top level — proven ----------------

HOME = (314.38,  41.65, 157.12,  95.88)
P1   = (314.38,  41.65,  70.53,  95.88)
P2   = (314.13, 180.89,  70.53,  95.88)
P3   = (314.13, 180.89, 111.93,  95.88)
P4   = ( 69.49, 389.61, 110.40, 184.58)
P5   = ( 69.49, 389.61,  75.62, 184.58)
P6   = (203.78, 340.41,  75.62, 179.87)
P7   = (203.78, 340.41, 151.12, 179.87)   # withdraw
# -------------------------------------------------------------

# ---------------- disk 2, lower level ----------------
Z_DISK2       =  32.68      # taught: fork under disk 2
Z_SMALL_LIFT  =  49.28      # taught: +16.6mm, unseats the disk, ~21mm below
                            #         where the fork sits for disk 1
Y_RETRACT     =  41.65      # assumed: entry Y — CONFIRM it clears backing out
Z_LIFT_CLEAR  = 111.93      # assumed: matches disk 1 transit, P4 is at 110.40

Q1 = (314.38,  41.65, Z_DISK2,      107.54)   # descend to disk 2
Q2 = (314.13, 180.89, Z_DISK2,      107.54)   # fork slides under, +Y
Q3 = (314.13, 180.89, Z_SMALL_LIFT, 107.54)   # small lift only
Q4 = (314.38, Y_RETRACT, Z_SMALL_LIFT, 107.54)  # retract -Y, still low
Q5 = (314.38, Y_RETRACT, Z_LIFT_CLEAR, 107.54)  # now safe to lift
# -------------------------------------------------------------


SEQ_DISK1 = [
    ("home",                 HOME),
    ("descend to disk 1",    P1),
    ("traverse low",         P2),
    ("lift clear",           P3),
    ("over conveyor",        P4),
    ("descend to carrier",   P5),
    ("rotate",               P6),
    ("withdraw",             P7),
    ("home",                 HOME),
]

SEQ_DISK2 = [
    ("home",                 HOME),
    ("descend to disk 2",    Q1),
    ("traverse low",         Q2),
    ("small lift",           Q3),
    ("retract from rack",    Q4),
    ("lift clear",           Q5),
    ("over conveyor",        P4),
    ("descend to carrier",   P5),
    ("rotate",               P6),
    ("withdraw",             P7),
    ("home",                 HOME),
]


send(dash, "ClearError()")
send(dash, "EnableRobot()")
time.sleep(2)
send(dash, f"SpeedFactor({SPEED})")

choice = input("\nrun — 1 = disk 1 only, 2 = disk 2 only, ENTER = both in sequence: ").strip()
if choice == "1":
    STEPS = SEQ_DISK1
elif choice == "2":
    STEPS = SEQ_DISK2
else:
    # both, dropping the duplicate home where the two sequences meet
    STEPS = SEQ_DISK1 + SEQ_DISK2[1:]
    print("\n*** disk 1 then disk 2 — motion test only ***")
    print("*** in the real cycle the conveyor and Pro 600 run between them ***")

print("\ncommands: ENTER = next move | s = status | c = clear error | q = quit\n")

i = 0
while i < len(STEPS):
    label, pose = STEPS[i]
    key = input(f"\n[{i}] ENTER to move -> {label}  (s/c/q): ").strip().lower()

    if key == "q":
        break
    if key == "s":
        status()
        continue
    if key == "c":
        recover()
        continue1

    x, y, z, r = pose
    send(move, f"MovJ({x},{y},{z},{r})")
    send(move, "Sync()")
    status()
    i += 1

print("\ndone")