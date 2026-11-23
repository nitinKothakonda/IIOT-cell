# test only: stands in for the three lab bridges against plc_sim.py
import threading, time
from pymodbus.client import ModbusTcpClient
def bridge(cmd_reg, st_reg, clear_after, total):
    c = ModbusTcpClient("127.0.0.1", port=5020); c.connect()
    while True:
        cmd = c.read_holding_registers(cmd_reg, count=1).registers[0]
        if cmd:
            c.write_register(st_reg, 1)
            if cmd_reg == 0: c.write_coil(0, True)
            time.sleep(clear_after)
            if clear_after: c.write_register(st_reg, 3)
            time.sleep(total - clear_after); c.write_register(st_reg, 2)
            while c.read_holding_registers(cmd_reg, count=1).registers[0]: time.sleep(.05)
            c.write_register(st_reg, 0)
        time.sleep(.05)
def vision():
    c = ModbusTcpClient("127.0.0.1", port=5020); c.connect()
    while True:
        if c.read_holding_registers(30, count=1).registers[0] == 7:
            time.sleep(.3); t = c.read_holding_registers(99, count=1).registers[0]
            c.write_register(40, t or 3)
            while c.read_holding_registers(30, count=1).registers[0] == 7: time.sleep(.05)
        time.sleep(.05)
for a in [(10, 11, 1.0, 2.0), (0, 1, 0.8, 2.0)]:
    threading.Thread(target=bridge, args=a, daemon=True).start()
threading.Thread(target=vision, daemon=True).start()
time.sleep(1000)
