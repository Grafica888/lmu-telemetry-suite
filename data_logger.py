import sys
import time
import os
import sqlite3
import datetime

# Fügen Sie das heruntergeladene Modul zum Pfad hinzu
sys.path.append(os.path.join(os.path.dirname(__file__), 'pyRfactor2SharedMemory'))

from sharedMemoryAPI import SimInfoAPI

DB_FILE = "lmu_telemetry.db"

class DataLogger:
    def __init__(self):
        self._init_db()
        self.is_recording = False
        self.current_run_id = None
        self.start_time = 0
        self.buffer = []  # Um Datenpunkte zwischenzuspeichern und Batch-Inserts auszuführen
        
    def _init_db(self):
        """Initialisiert die SQLite-Datenbank und die Tabellen."""
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Tabelle für aufgenommene Runs
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_name TEXT,
                vehicle_class TEXT,
                track_name TEXT,
                timestamp DATETIME
            )
        ''')
        
        # Tabelle für die Telemetriedaten eines Runs
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS telemetry_data (
                run_id INTEGER,
                time_elapsed REAL,
                gear INTEGER,
                rpm REAL,
                torque REAL,
                speed_kmh REAL,
                throttle REAL,
                FOREIGN KEY(run_id) REFERENCES runs(id)
            )
        ''')
        
        # Auto-Upgrade Schema for Handling Analytics
        new_columns = [
            ("lat_g", "REAL DEFAULT 0"),
            ("lon_g", "REAL DEFAULT 0"),
            ("steering_angle", "REAL DEFAULT 0"),
            ("lap_distance", "REAL DEFAULT 0"),
            ("sector", "INTEGER DEFAULT 0")
        ]
        
        for col_name, col_type in new_columns:
            try:
                cursor.execute(f"ALTER TABLE telemetry_data ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass # Column already exists
                
        try:
            cursor.execute("ALTER TABLE runs ADD COLUMN run_type TEXT DEFAULT 'DRAG'")
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE runs ADD COLUMN notes TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()
        
    def start_recording(self, vehicle_name, vehicle_class, track_name, run_type="DRAG"):
        """Startet eine neue Aufzeichnung."""
        self.is_recording = True
        self.start_time = time.time()
        self.buffer = []
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            timestamp_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute(
                "INSERT INTO runs (vehicle_name, vehicle_class, track_name, timestamp, run_type) VALUES (?, ?, ?, ?, ?)",
                (vehicle_name, vehicle_class, track_name, timestamp_str, run_type)
            )
            self.current_run_id = cursor.lastrowid
            conn.commit()
            conn.close()
            print(f"\n[Logger] Starter Aufzeichnung -- Run ID: {self.current_run_id} | Type: {run_type} | Fahrzeug: {vehicle_name}")
        except Exception as e:
            print(f"\n[Datenbankfehler in start_recording]: {e}")
            self.is_recording = False
            self.current_run_id = None
    def stop_recording(self):
        """Stoppt die Aufzeichnung und speichert verbleibende Daten aus dem Buffer."""
        if not self.is_recording:
            return
            
        self.is_recording = False
        self._flush_buffer()
        print(f"\n[Logger] Aufzeichnung beendet -- Run ID: {self.current_run_id}")
        self.current_run_id = None

    def log_data_point(self, gear, rpm, torque, speed_kmh, throttle, lat_g=0.0, lon_g=0.0, steering_angle=0.0, lap_distance=0.0, sector=0):
        """Fügt einen neuen Datenpunkt zum aktuellen Run hinzu."""
        if not self.is_recording:
            return
            
        time_elapsed = time.time() - self.start_time
        self.buffer.append((self.current_run_id, time_elapsed, gear, rpm, torque, speed_kmh, throttle, lat_g, lon_g, steering_angle, lap_distance, sector))
        
        # Flush Buffer alle 50 Datenpunkte, um RAM zu schonen und Schreiboperationen zu bündeln
        if len(self.buffer) >= 50:
            self._flush_buffer()

    def _flush_buffer(self):
        """Schreibt den Buffer in die Datenbank."""
        if not self.buffer:
            return
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.executemany(
                "INSERT INTO telemetry_data (run_id, time_elapsed, gear, rpm, torque, speed_kmh, throttle, lat_g, lon_g, steering_angle, lap_distance, sector) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self.buffer
            )
            conn.commit()
            conn.close()
            self.buffer = []
        except Exception as e:
            print(f"\n[Datenbankfehler in _flush_buffer]: {e}")
            self.buffer = []

def _get_lmu_version():
    try:
        import psutil
        import win32api
        exe_path = None
        for p in psutil.process_iter(['name', 'exe']):
            name = p.info.get('name')
            if name and (name.lower().startswith('le mans ultimate') or name.lower().startswith('rfactor2')):
                exe_path = p.info.get('exe')
                break
                
        if exe_path:
            info_ver = win32api.GetFileVersionInfo(exe_path, '\\')
            ms = info_ver['FileVersionMS']
            ls = info_ver['FileVersionLS']
            return f" [v{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}]"
    except Exception:
        pass
    return ""

def main():
    import socket
    global _single_instance_socket
    try:
        _single_instance_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _single_instance_socket.bind(("127.0.0.1", 54321))
    except socket.error:
        print("[INFO] Eine Instanz des Data Loggers läuft bereits im Hintergrund. Beende doppelte Ausführung.")
        sys.exit(0)

    print("LMU / rFactor 2 Data Logger")
    print("Suche nach LMU / rFactor 2 Prozess...")

    try:
        info = SimInfoAPI()
    except Exception as e:
        print(f"Fehler beim Initialisieren der Shared Memory API: {e}")
        return

    logger = DataLogger()

    print("Verbinde mit Shared Memory...")
    print("Log-Algorithmus: Wenn Drosselklappe (Throttle) > 95% und Geschwindigkeit < 40 km/h, startet eine Messung.")
    print("Die Aufzeichnung stoppt, wenn du vom Gas gehst oder bremst.")
    print("WICHTIG: Erfordert nun den Start via Streamlit GUI (Start Button)!")

    import os
    STATE_FILE = "logger_state.txt"
    
    def read_state():
        if not os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "w") as f: f.write("IDLE")
            except: pass
            return "IDLE"
        try:
            with open(STATE_FILE, "r") as f: return f.read().strip()
        except:
            return "IDLE"

    def write_state(s):
        try:
            with open(STATE_FILE, "w") as f: f.write(s)
        except: pass

    state_check_counter = 0
    current_cmd_state = read_state()

    try:
        while True:
            state_check_counter += 1
            if state_check_counter >= 10:
                current_cmd_state = read_state()
                state_check_counter = 0

            if info.isRF2running() and info.isSharedMemoryAvailable() and info.isOnTrack():
                telemetry = info.playersVehicleTelemetry()
                scoring = info.playersVehicleScoring()
                
                rpm = telemetry.mEngineRPM
                
                # Le Mans Ultimate Nullen das Engine Torqure ('mEngineTorque') leider aus,
                # um die BoP-Motormappings geheim zu halten!
                # Daher nutzen wir wieder die rohe Beschleunigung an den Hinterrädern als Referenz.
                # Wir greifen auf die longitudinale Beschleunigung (mLocalAccel.z) zurück!
                accel_z = -telemetry.mLocalAccel.z # Negativ, weil Z in rF2 nach hinten zeigt
                
                # Wir loggen die Beschleunigung als "Torque" Proxy, da F = m*a und Drehmoment proportional zu Kraft ist.
                torque_proxy = accel_z * 1000 # Skaliert auf einen realistischen Wert für die Skala (z.B. Massenfaktor)
                
                gear = telemetry.mGear
                speed = telemetry.mLocalVel.z
                speed_kmh = abs(speed) * 3.6
                throttle = telemetry.mUnfilteredThrottle
                brake = telemetry.mUnfilteredBrake
                
                # Handling & Grip Analyzer Werte:
                lat_g = telemetry.mLocalAccel.x / 9.81
                lon_g = -telemetry.mLocalAccel.z / 9.81 # Z points backwards
                steering_angle = telemetry.mUnfilteredSteering
                lap_distance = scoring.mLapDist
                sector = scoring.mSector
                
                if current_cmd_state == "IDLE" or current_cmd_state == "FINISHED":
                    if logger.is_recording:
                        logger.stop_recording()
                    sys.stdout.write(f"\r[Warte auf GUI] State: {current_cmd_state} | Geh ins Dashboard und druecke START!     ")
                    sys.stdout.flush()
                
                elif current_cmd_state.startswith("ARMED"):
                    if not logger.is_recording:
                        is_handling = "HANDLING" in current_cmd_state
                        
                        trigger = False
                        if is_handling:
                            # Handling Trigger: Fährt schneller als 15 km/h
                            trigger = speed_kmh > 15
                        else:
                            # Drag Trigger: Aus dem Stand beschleunigen (auch bei partiellem Gas)
                            trigger = throttle > 0.05 and speed_kmh < 40 and gear >= 1 and brake < 0.1
                            
                        if trigger:
                            lmu_version = _get_lmu_version()
                            ts_str = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M')
                            try:
                                raw_v_name = bytes(scoring.mVehicleName).partition(b'\0')[0].decode('utf-8', 'ignore')
                                clean_name = raw_v_name.replace(' ', '_').replace('-', '_')
                                v_name = f"{clean_name}{lmu_version.replace(' ', '_')}"
                                v_class = bytes(scoring.mVehicleClass).partition(b'\0')[0].decode('utf-8', 'ignore')
                                t_name = bytes(info.Rf2Scor.mScoringInfo.mTrackName).partition(b'\0')[0].decode('utf-8', 'ignore')
                            except:
                                v_name = f"Unknown_Vehicle{lmu_version.replace(' ', '_')}"
                                v_class = "Unknown Class"
                                t_name = "Unknown Track"
                                
                            run_type = current_cmd_state.replace("ARMED_", "")
                            logger.start_recording(v_name, v_class, t_name, run_type)
                            next_state = f"RECORDING_{run_type}"
                            write_state(next_state)
                            current_cmd_state = next_state
                        else:
                            sys.stdout.write(f"\r[{current_cmd_state} - Wartend] Thr: {throttle*100:3.0f}% | Vel: {speed_kmh:5.1f} km/h | Gear: {gear}    ")
                            sys.stdout.flush()
                    else:
                        run_type = current_cmd_state.replace("ARMED_", "")
                        next_state = f"RECORDING_{run_type}"
                        write_state(next_state)
                        current_cmd_state = next_state
                        
                elif current_cmd_state.startswith("RECORDING"):
                    if logger.is_recording:
                        logger.log_data_point(gear, rpm, torque_proxy, speed_kmh, throttle, lat_g, lon_g, steering_angle, lap_distance, sector)
                        
                        sys.stdout.write(f"\r[{current_cmd_state}] RPM: {rpm:6.0f} | Speed: {speed_kmh:5.1f} km/h | LatG: {lat_g:5.2f} | LonG: {lon_g:5.2f} ")
                        sys.stdout.flush()
                        
                        is_handling = "HANDLING" in current_cmd_state
                        
                        # Stop-Trigger nur bei DRAG (wenn Fahrer vom Gas geht/bremst)
                        if not is_handling:
                            if throttle < 0.05 or brake > 0.1:
                                logger.stop_recording()
                                write_state("FINISHED")
                                current_cmd_state = "FINISHED"
                        # Handling stoppt nur über das GUI
                    else:
                        write_state("IDLE")
                        current_cmd_state = "IDLE"
                
            else:
                if logger.is_recording:
                     logger.stop_recording()
                sys.stdout.write("\rWarte auf Spiel / aktive Session...                    ")
                sys.stdout.flush()

            time.sleep(0.02) # ~50 Hz Update-Rate (alle 20ms) -> Feine Datenauflösung für Berechnungen

    except KeyboardInterrupt:
        if logger.is_recording:
            logger.stop_recording()
        print("\nLogger beendet.")

if __name__ == "__main__":
    main()
