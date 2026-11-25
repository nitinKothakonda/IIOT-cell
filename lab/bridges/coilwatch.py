import time
from pymodbus.client import ModbusTcpClient as C
c = C('192.168.10.10', port=502); c.connect()
last = None; t0 = time.time()
while True:
    r = c.read_coils(0, count=2)
    v = None if r.isError() else (int(r.bits[0]), int(r.bits[1]))
    if v != last:
        print(f"{time.time()-t0:8.2f}s  Vac_On={v[0] if v else '?'}  Vac_Blow={v[1] if v else '?'}")
        last = v
    time.sleep(0.05)
