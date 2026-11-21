# Wafer IIoT cell (MFG598 Group 3)

Two-arm wafer handling: robot and camera events -> MQTT -> PostgreSQL -> OEE dashboard. Currently running on a fake source; real arm and camera readers come next.

Flow: edge/wafer_sim.py -> Mosquitto (MQTT) -> cloud/ingest.py -> PostgreSQL -> dashboard/app.py (http://localhost:5000)

## Run it on one laptop (Windows/Mac/Linux)
1. Install Mosquitto and PostgreSQL. Create the broker users: mosquitto_passwd -c broker/passwd edge  then  mosquitto_passwd broker/passwd cloud
2. Broker: broker/mq.conf is the config (create broker/passwd first as shown inside it). Start: mosquitto -c broker/mq.conf
3. Database: createdb wafer ; psql wafer -f cloud/schema.sql
4. Copy cloud/.env.example to cloud/.env and set DATABASE_URL, MQTT_HOST, MQTT_USER=cloud, MQTT_PASS, TOPIC_PREFIX=mfg598/group3/wafer
5. pip install -r cloud/requirements.txt -r dashboard/requirements.txt -r edge/requirements.txt
6. Terminal 1: cd cloud && python ingest.py
   Terminal 2: cd dashboard && python app.py
   Terminal 3: cd edge && python wafer_sim.py --user edge --password <pw> --speed 5
   Add --burst to make defects spike and trigger the quality_low alert.

## Monday
Replace read_event() in edge/wafer_sim.py with the real arm/camera reader. The message format (wafer_id, arm, station, cycle_s, label, confidence, result) stays the same, so ingest and dashboard need no changes.
