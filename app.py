import webbrowser
from flask import Flask, jsonify, request, send_from_directory, Response
from flask_cors import CORS
import serial
import time
import threading
import csv
import io
import os
print("CURRENT DIR:", os.getcwd())

serial_lock = threading.Lock()
data_lock = threading.Lock()

saved_port = None
saved_baud = None

new_sample_ready = False

app = Flask(__name__)
CORS(app)

@app.route('/')
def serve_ui():
    return send_from_directory('.', 'index.html')

sampling_rate_sec = 1
last_log_time = 0

sample_counter = 0

ser = None

last_values = {
    "pressure": [0.0] * 5,
    "temperature": [0.0] * 5
}
last_update_time = 0

# ✅ DATA LOG STORAGE
data_log = []

is_running = False          # controls acquisition visibility + logging
frozen_values = None        # stores last STOP snapshot


# ---------------- SERVE FRONTEND ----------------
import sys
import os

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


# ---------------- CONNECT ----------------
@app.route('/connect', methods=['POST'])
def connect():
    global ser, last_update_time, last_values, saved_port, saved_baud

    data = request.json

    try:
        port = data.get("com_port")
        baud = int(data.get("baud_rate"))

        with serial_lock:
            if ser and ser.is_open:
                ser.close()
                time.sleep(1)

            ser = serial.Serial(port, baud, timeout=1)

            saved_port = port      # 🔥 remember last successful COM port
            saved_baud = baud      # 🔥 remember last successful baud

        time.sleep(2)
        ser.reset_input_buffer()
        last_update_time = time.time()

        last_values = {
    "pressure": [0.0] * 5,
    "temperature": [0.0] * 5
}
    
        last_update_time = time.time()

        return jsonify({"status": "connected"})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ---------------- CONTROL ----------------
@app.route('/start', methods=['POST'])
def start():
    global is_running, sampling_rate_sec, last_log_time, frozen_values

    data = request.json
    sampling_rate_sec = int(data.get("sampling_rate", 1))

    is_running = True
    frozen_values = None
    last_log_time = 0   # 🔥 fresh sampling cycle starts immediately

    return jsonify({"status": "started"})

@app.route('/set_sampling_rate', methods=['POST'])
def set_sampling_rate():
    global sampling_rate_sec, last_log_time

    data = request.json
    new_rate = int(data.get("sampling_rate", 1))

    if new_rate not in [1, 5, 10, 30, 60, 120]:
        return jsonify({"status": "error", "message": "Invalid sampling rate"})

    sampling_rate_sec = new_rate
    last_log_time = time.time()   # restart timer from this moment

    print(f"✅ Sampling rate changed live to {sampling_rate_sec} sec")

    return jsonify({
        "status": "ok",
        "sampling_rate": sampling_rate_sec
    })

@app.route('/stop', methods=['POST'])
def stop():
    global is_running, frozen_values, last_values

    is_running = False

    frozen_values = {
        "pressure": last_values["pressure"].copy(),
        "temperature": last_values["temperature"].copy()
    }

    return jsonify({"status": "stopped"})

@app.route('/reset', methods=['POST'])
def reset():
    global is_running, data_log, sample_counter, last_log_time
    global last_values, last_update_time, frozen_values

    is_running = False
    frozen_values = None

    with data_lock:
        data_log.clear()
    sample_counter = 0
    last_log_time = 0

    last_values = {
        "pressure": [0.0] * 5,
        "temperature": [0.0] * 5
    }

    last_update_time = 0

    return jsonify({"status": "reset"})


# ---------------- SERIAL READ ----------------
def read_serial():
    global ser, last_values, last_update_time, data_log

    with serial_lock:
        if ser is None or not ser.is_open:
            return

    try:
        with serial_lock:
            line = ser.readline().decode('utf-8', errors='ignore').strip()

        if not line:
            return

        parts = line.replace(',', ' ').split()

        if len(parts) < 10:
            return

        values = [float(x) for x in parts[:10]]

        # split into pressure + temperature
        p_values = values[:5]
        t_values = values[5:]

        if not all(0 <= v <= 5 for v in p_values + t_values):
            return

        last_values = {
            "pressure": [round(v, 3) for v in p_values],
            "temperature": [round(v, 3) for v in t_values]
        }

        last_update_time = time.time()

        # ✅ ONLY LOG WHEN RUNNING (VERY IMPORTANT)
        global last_log_time

        if is_running:
            now = time.time()

            if now - last_log_time >= sampling_rate_sec:
                last_log_time = now

                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

                global sample_counter, new_sample_ready
                sample_counter += 1
                new_sample_ready = True

                with data_lock:
                    data_log.append({
                        "sample_id": sample_counter,
                        "timestamp": timestamp,
                        "pressure": last_values["pressure"].copy(),
                        "temperature": last_values["temperature"].copy()
                    })

                    if len(data_log) > 10000:
                        data_log.pop(0)

        print("UPDATED:",
              "P:", last_values["pressure"],
              "T:", last_values["temperature"])
        
    except Exception as e:
        print("Serial error:", e)

        # 🔴 ADD THIS BLOCK (USB unplug detection)
        try:
            if ser and ser.is_open:
                ser.close()
        except:
            pass

        # force backend to report disconnect
        last_update_time = 0

def serial_loop():
    while True:
        if ser and ser.is_open:
            read_serial()
        time.sleep(0.05)

def auto_reconnect():
    global ser, saved_port, saved_baud, last_update_time

    while True:
        try:
            if saved_port is None:
                time.sleep(2)
                continue

            with serial_lock:
                if ser is None or not ser.is_open:
                    ser = serial.Serial(saved_port, saved_baud, timeout=1)
                    time.sleep(2)
                    ser.reset_input_buffer()

                    last_update_time = time.time()   # 🔥 ADD EXACTLY HERE

                    print("🔁 Auto reconnected to device")

        except:
            pass

        time.sleep(2)

threading.Thread(target=serial_loop, daemon=True).start()
threading.Thread(target=auto_reconnect, daemon=True).start()

# ---------------- DATA API ----------------
def calculate_psi(voltage):
    return (4 * voltage) + 5


@app.route('/data')
def get_data():
    global last_values, last_update_time, frozen_values, new_sample_ready

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    sample_created = new_sample_ready
    new_sample_ready = False

    if ser is None or not ser.is_open:
        return jsonify({"status": "disconnected"})

    age = time.time() - last_update_time

    if age > 2:
        return jsonify({
            "status": "no_data",
            "pressure": "NO SIGNAL",
            "temperature": "NO SIGNAL",
            **{f"p{i}_volt": "" for i in range(1, 6)},
            **{f"p{i}_psi": "" for i in range(1, 6)},
            **{f"t{i}_volt": "" for i in range(1, 6)},
            **{f"t{i}_temp": "" for i in range(1, 6)}
        })
    
    global frozen_values

    if not is_running and frozen_values is not None:

        response = {
            "status": "stopped",
            "pressure": "STOPPED",
            "temperature": "STOPPED",
            "timestamp": timestamp,
            "sample_id": -1
        }

        for i in range(5):
            p_v = frozen_values["pressure"][i]
            psi = calculate_psi(p_v)

            t_v = frozen_values["temperature"][i]
            temp = (8 * t_v) + 10

            response[f"p{i+1}_volt"] = round(p_v, 3)
            response[f"p{i+1}_psi"] = round(psi, 2)

            response[f"t{i+1}_volt"] = round(t_v, 3)
            response[f"t{i+1}_temp"] = round(temp, 2)

        return jsonify(response)

    with data_lock:
        current_sample_id = sample_counter if len(data_log) > 0 else -1

        response = {
        "status": "live",
        "pressure": "LIVE",
        "temperature": "LIVE",
        "timestamp": timestamp,
        "sample_id": current_sample_id,
        "new_sample": sample_created
    }

    for i in range(5):
        # PRESSURE
        p_v = last_values["pressure"][i]
        psi = calculate_psi(p_v)

        response[f"p{i+1}_volt"] = round(p_v, 3)
        response[f"p{i+1}_psi"] = round(psi, 2)

        # TEMPERATURE
        t_v = last_values["temperature"][i]
        temp = (8 * t_v) + 10   # your chosen mapping

        response[f"t{i+1}_volt"] = round(t_v, 3)
        response[f"t{i+1}_temp"] = round(temp, 2)

    return jsonify(response)


# ---------------- CSV DOWNLOAD ----------------
@app.route('/download_csv')
def download_csv():
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
    "Timestamp",
    "Sample_ID",

    "P1_Volt","P2_Volt","P3_Volt","P4_Volt","P5_Volt",
    "P1_Psi","P2_Psi","P3_Psi","P4_Psi","P5_Psi",

    "T1_Volt","T2_Volt","T3_Volt","T4_Volt","T5_Volt",
    "T1_DegreeC","T2_DegreeC","T3_DegreeC","T4_DegreeC","T5_DegreeC"
])

    with data_lock:
        log_copy = data_log.copy()

    for entry in log_copy:
        temps = entry["temperature"]
        
        writer.writerow(
            [entry["timestamp"], entry["sample_id"]] +
            [f"{v:.3f}" for v in entry["pressure"]] +
            [f"{(4*v)+5:.2f}" for v in entry["pressure"]] +
            [f"{v:.3f}" for v in entry["temperature"]] +
            [f"{(8*v)+10:.2f}" for v in entry["temperature"]]
        )

    output.seek(0)

    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=sensor_data.csv"}
    )


# ---------------- RUN ----------------
import sys
import os

def resource_path(relative_path):
    """ Get absolute path for PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

print("ROUTES:")
for rule in app.url_map.iter_rules():
    print(rule)
    
if __name__ == "__main__":
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
