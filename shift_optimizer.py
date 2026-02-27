import sqlite3
import pandas as pd
import numpy as np
from scipy.interpolate import interp1d

class ShiftOptimizer:
    def __init__(self, db_path="lmu_telemetry.db"):
        self.db_path = db_path

    def get_torque_curve_from_run(self, run_id, gear_ratios, final_drive, mass_kg=1200.0, wheel_radius_m=0.33, c_w_a=1.5, rho=1.225):
        """
        Liest die Telemetriedaten eines bestimmten Runs und berechnet
        eine interpolierte/extrapolierte Drehmomentkurve in echten Nm unter
        Berücksichtigung von Masse, Luft- und Rollwiderstand.
        """
        conn = sqlite3.connect(self.db_path)
        # Hole alle Daten mit offener Drosselklappe (Volllast) über 2000 RPM
        query = f"SELECT rpm, torque, gear, speed_kmh FROM telemetry_data WHERE run_id = {run_id} AND throttle > 0.95 AND rpm > 2000 ORDER BY rpm ASC"
        df = pd.read_sql_query(query, conn)
        conn.close()

        if df.empty:
            return None

        # Filtern von ungültigen Daten (z.B. Schalt-Löcher mit negativer Beschleunigung/Torque)
        df = df[df['torque'] > 0].copy()
        
        def calc_engine_torque(row):
            g = int(row['gear'])
            if 1 <= g <= len(gear_ratios):
                ratio = gear_ratios[g - 1]
            else:
                ratio = gear_ratios[-1]
                
            # 'torque' ist aktuell accel_z * 1000
            a = row['torque'] / 1000.0 # Beschleunigung in m/s^2
            
            # Netto-Zugkraft am Rad (F = m * a)
            # Wir verzichten hier auf die Addition von Luftwiderstand (F_drag). 
            # Warum? Der Algorithmus nimmt weiter unten das 95%-Quantil aller Datenpunkte. 
            # Bei gleicher RPM hat der niedrigste Gang die höchste Beschleunigung (weil kaum Luftwiderstand).
            # Das Quantil zieht sich also ohnehin automatisch die "reinen" Motorwerte aus Gang 1 & 2.
            # Luftwiderstand würde bei Ungenauigkeiten in hohen Gängen die Kurve rechnerisch "explodieren" lassen.
            F_wheel_net = mass_kg * a
            
            # Rad-Drehmoment
            T_wheel_Nm = F_wheel_net * wheel_radius_m
            
            # Motor-Drehmoment Proxy
            T_engine_Nm = T_wheel_Nm / (ratio * final_drive)
            return T_engine_Nm
        df['engine_torque'] = df.apply(calc_engine_torque, axis=1)
        
        # Runden der RPM auf 50er Schritte zur sauberen Gruppierung
        df['rpm_rounded'] = (df['rpm'] / 50).round() * 50
        
        # Bilde die obere Hüllkurve (Um Hänger durch Auskuppeln/Schaltvorgänge zu ignorieren)
        # quantile(0.95) fängt das reale Peak-Drehmoment robuster ab als max()
        curve = df.groupby('rpm_rounded')['engine_torque'].quantile(0.95).reset_index()
        
        # Daten glätten & interpolieren/extrapolieren, um Lücken in Gängen zu füllen
        rpms = curve['rpm_rounded'].values
        torques = curve['engine_torque'].values
        
        if len(rpms) > 3:
            # Fit polynomial 3. Grades für die Hüllkurve
            # Das füllt alle Lücken und erlaubt uns, auch bei extrem niedrigen/hohen Drehzahlen Schaltpunkte zu berechnen!
            z = np.polyfit(rpms, torques, 3)
            p = np.poly1d(z)
            
            # Neue RPM Achse von 2000 bis Max RPM + 500 (für reichlich Überlappung)
            min_rpm = max(2000, rpms.min() - 1000)
            max_rpm = rpms.max() + 500
            new_rpms = np.arange(min_rpm, max_rpm, 50)
            
            smoothed_curve = pd.DataFrame({
                'rpm_rounded': new_rpms,
                'torque_smoothed': p(new_rpms)
            })
            return smoothed_curve
        else:
            return curve.rename(columns={'engine_torque': 'torque_smoothed'})

    def calculate_ideal_shift_points(self, torque_curve_df, gear_ratios, final_drive, wheel_radius_m=0.33):
        """
        Berechnet die idealen Schaltpunkte basierend auf der Zugkraftkurve.
        
        :param torque_curve_df: DataFrame mit 'rpm_rounded' und 'torque_smoothed' in Nm
        :param gear_ratios: Liste von Übersetzungen, z.B. [2.89, 2.10, 1.64, 1.31, 1.09, 0.93]
        :param final_drive: Achsübersetzung, z.B. 3.42
        :return: Liste mit Schaltempfehlungen (Gang N -> N+1: at RPM)
        """
        rpms = torque_curve_df['rpm_rounded'].values
        torques = torque_curve_df['torque_smoothed'].values
        
        # Interpolationsfunktion für das Drehmoment, um Werte exakt abzufragen
        torque_func = interp1d(rpms, torques, kind='cubic', fill_value="extrapolate")
        
        min_rpm = max(rpms.min(), 3000)
        max_rpm = rpms.max()
        
        # Feineres RPM-Array für präzise Berechnungen
        fine_rpms = np.arange(min_rpm, max_rpm, 10)
        
        wheel_forces = []
        for ratio in gear_ratios:
            # F_wheel = T_engine * Ratio * Final_Drive / Radius
            # Radzugkraft (ohne Abzug von Luftwiderstand, d.h. Bruttokraft)
            wt = torque_func(fine_rpms) * ratio * final_drive / wheel_radius_m
            wheel_forces.append(wt)
            
        shift_points = []
        
        # Schnittpunkte zwischen Gang N und N+1 finden
        for i in range(len(gear_ratios) - 1):
            wf_current_gear = wheel_forces[i]
            
            # Bei gleicher Geschwindigkeit (km/h) fällt die RPM im nächsten Gang ab.
            ratio_next = gear_ratios[i+1]
            ratio_current = gear_ratios[i]
            
            rpm_drop_factor = ratio_next / ratio_current
            rpms_in_next_gear = fine_rpms * rpm_drop_factor
            wf_next_gear = torque_func(rpms_in_next_gear) * ratio_next * final_drive / wheel_radius_m
            
            shift_rpm = max_rpm
            for j in range(len(fine_rpms)-1, 0, -1):
                if fine_rpms[j] >= max_rpm:
                    shift_rpm = max_rpm
                    continue
                    
                # Weil der Luftwiderstand in beiden Gängen bei derselben Geschwindigkeit exakt gleich groß ist, 
                # genügt der Vergleich der Brutto-Radzugkräfte (wf_current vs wf_next):
                if wf_current_gear[j] > wf_next_gear[j]:
                    shift_rpm = fine_rpms[j]
                    break
                    
            if shift_rpm == fine_rpms[0] or shift_rpm > max_rpm - 50:
                 shift_rpm = max_rpm
                 
            shift_points.append({
                'from_gear': i + 1,
                'to_gear': i + 2,
                'shift_rpm': shift_rpm,
                'rpm_drop_to': shift_rpm * rpm_drop_factor
            })
            
        return shift_points, fine_rpms, wheel_forces

    def get_auto_gear_ratios(self, run_id):
        """
        Automatische Erkennung der Getriebeübersetzung basierend auf R = Speed / RPM.
        Speichert die Durchschnittswerte pro Gang in der Datenbank.
        """
        conn = sqlite3.connect(self.db_path)
        
        # Fahrzeugnamen ermitteln
        df_run = pd.read_sql_query(f"SELECT vehicle_name FROM runs WHERE id = {run_id}", conn)
        if df_run.empty:
            conn.close()
            return None
            
        vehicle_name = df_run.iloc[0]['vehicle_name']
        
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS vehicle_gear_ratios (
                    vehicle_name TEXT,
                    gear INTEGER,
                    ratio_r REAL,
                    PRIMARY KEY (vehicle_name, gear)
                )
            ''')
        except sqlite3.OperationalError:
            pass
            
        # Zuerst alle jemals gefahrenen Gänge für dieses Auto auswerten
        query = """
            SELECT t.gear, t.speed_kmh, t.rpm, t.torque 
            FROM telemetry_data t
            JOIN runs r ON t.run_id = r.id
            WHERE r.vehicle_name = ? 
              AND t.throttle > 0.9 
              AND t.rpm > 3000 
              AND t.speed_kmh > 10
        """
        df_tele = pd.read_sql_query(query, conn, params=(vehicle_name,))
        
        if not df_tele.empty:
            df_tele = df_tele[df_tele['torque'] > 0].copy()
            if not df_tele.empty:
                df_tele['r_val'] = df_tele['speed_kmh'] / df_tele['rpm']
                # Berechne den Median (Durchschnittswert, stabil gegen Ausreißer) und ignoriere Gänge mit wenig Haltepunkten (Glitches)
                detected = df_tele.groupby('gear')['r_val'].agg(['median', 'count']).reset_index()
                detected = detected[(detected['count'] > 20) & (detected['gear'] > 0)]
                
                # In Datenbank abspeichern
                conn.execute("DELETE FROM vehicle_gear_ratios WHERE vehicle_name = ?", (vehicle_name,))
                for _, row in detected.iterrows():
                    conn.execute("INSERT OR REPLACE INTO vehicle_gear_ratios (vehicle_name, gear, ratio_r) VALUES (?, ?, ?)", 
                                 (vehicle_name, int(row['gear']), row['median']))
                conn.commit()
                
        # Lese aktuellen Stand aus der DB
        df_existing = pd.read_sql_query("SELECT gear, ratio_r FROM vehicle_gear_ratios WHERE vehicle_name = ? ORDER BY gear ASC", conn, params=(vehicle_name,))
        conn.close()
        
        if df_existing.empty:
            return None
            
        return df_existing['ratio_r'].tolist()

def test():
    print("Testing Shift Optimizer Algorithm...")
    # Beispieldaten erzeugen, falls keine DB existiert
    df = pd.DataFrame({
        'rpm_rounded': np.arange(3000, 8500, 100),
        'torque_smoothed': 400 - ((np.arange(3000, 8500, 100) - 6000) / 100)**2 * 0.5  # Parabolische Kurve, Peak bei 6000
    })
    
    optimizer = ShiftOptimizer(db_path="lmu_telemetry.db")
    gear_ratios = [2.5, 1.9, 1.5, 1.2, 1.0, 0.85]
    final_drive = 3.4
    
    points, rpms, wts = optimizer.calculate_ideal_shift_points(df, gear_ratios, final_drive)
    
    for p in points:
        print(f"Schalte Gang {p['from_gear']} -> {p['to_gear']} bei {p['shift_rpm']:.0f} RPM (Fällt auf {p['rpm_drop_to']:.0f} RPM)")

if __name__ == "__main__":
    test()
