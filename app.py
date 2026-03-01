import os
import time
import streamlit as st
import sqlite3
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import importlib
from scipy.interpolate import interp1d
from scipy.spatial import ConvexHull
import shift_optimizer
importlib.reload(shift_optimizer)
from shift_optimizer import ShiftOptimizer

st.set_page_config(page_title="LMU Analyzer", layout="wide", page_icon="🏎️")

def init_state_db():
    try:
        conn = sqlite3.connect("lmu_telemetry.db", timeout=5.0)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS logger_state (id INTEGER PRIMARY KEY, state TEXT)")
        cursor.execute("INSERT OR IGNORE INTO logger_state (id, state) VALUES (1, 'IDLE')")
        conn.commit()
        conn.close()
    except Exception:
        pass

init_state_db()

def get_logger_state():
    try:
        conn = sqlite3.connect("lmu_telemetry.db", timeout=5.0)
        cursor = conn.cursor()
        cursor.execute("SELECT state FROM logger_state WHERE id=1")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else "IDLE"
    except Exception:
        return "IDLE"

def set_logger_state(state):
    try:
        conn = sqlite3.connect("lmu_telemetry.db", timeout=5.0)
        cursor = conn.cursor()
        cursor.execute("UPDATE logger_state SET state=? WHERE id=1", (state,))
        conn.commit()
        conn.close()
    except Exception:
        pass

DB_PATH = "lmu_telemetry.db"

def load_runs():
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query("SELECT * FROM runs ORDER BY timestamp DESC", conn)
        conn.close()
        if not df.empty and 'run_type' not in df.columns:
            df['run_type'] = 'DRAG'
            
        if not df.empty and 'notes' not in df.columns:
            try:
                conn_tmp = sqlite3.connect(DB_PATH)
                conn_tmp.cursor().execute("ALTER TABLE runs ADD COLUMN notes TEXT DEFAULT ''")
                conn_tmp.commit()
                conn_tmp.close()
            except sqlite3.OperationalError:
                pass
            df['notes'] = ''
            
        if df.empty:
            return pd.DataFrame(columns=['id', 'vehicle_name', 'vehicle_class', 'track_name', 'timestamp', 'run_type', 'notes'])
            
        return df
    except Exception:
        return pd.DataFrame(columns=['id', 'vehicle_name', 'vehicle_class', 'track_name', 'timestamp', 'run_type', 'notes'])

def load_telemetry(run_id):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(f"SELECT * FROM telemetry_data WHERE run_id = {run_id}", conn)
    conn.close()
    return df

def analyze_run_quality(df):
    if df.empty:
        return df, 50.0, False
        
    # 1. Cleaning & Trimming
    start_mask = (df['speed_kmh'] > 60) & (df['throttle'] > 0.8)
    if start_mask.any():
        start_idx = start_mask.idxmax()
        df_clean = df.loc[start_idx:].copy()
    else:
        df_clean = df.copy()
        
    end_mask = df_clean['speed_kmh'] < 10
    if end_mask.any():
        end_idx = end_mask.idxmax()
        df_clean = df_clean.loc[:end_idx].copy()
        
    if len(df_clean) < 10:
        df_clean = df.copy()
        
    # A. Fake Peaks & Crash Detection
    if 'lat_g' in df_clean.columns and 'lon_g' in df_clean.columns:
        df_clean['lat_g_smooth'] = df_clean['lat_g'].rolling(10, min_periods=1).mean()
        df_clean['lon_g_smooth'] = df_clean['lon_g'].rolling(10, min_periods=1).mean()
        
        max_safe_g = 4.0
        max_lat = df_clean['lat_g_smooth'].abs().max()
        max_lon = df_clean['lon_g_smooth'].abs().max()
        crash_detected = (max_lat > max_safe_g) or (max_lon > max_safe_g)
        
        # Clip absurd peaks to avoid fake scores if we proceed
        df_clean['lat_g_smooth'] = df_clean['lat_g_smooth'].clip(-max_safe_g, max_safe_g)
        df_clean['lon_g_smooth'] = df_clean['lon_g_smooth'].clip(-max_safe_g, max_safe_g)
    else:
        crash_detected = False

    # 3. Stability Metrics (CSI)
    stability_score = 50.0
    confidence_ratio = 50.0
    counter_steer_bonus = 0.0
    unrecoverable_spin_penalty = 0.0
    spin_count = 0
    max_yaw_accel = 0.0
    
    if 'lat_g' in df_clean.columns and 'steering_angle' in df_clean.columns:
        # Calculate derived metrics
        # Yaw rate approximation from lat_g and speed
        df_clean['yaw_rate'] = df_clean.apply(lambda row: (row['lat_g'] * 9.81) / (row['speed_kmh'] / 3.6) if row['speed_kmh'] > 10 else 0, axis=1)
        df_clean['yaw_accel'] = df_clean['yaw_rate'].diff().abs() / df_clean['time_elapsed'].diff()
        
        # Approximate Slip Angle: very rough proxy using steering vs actual lateral G curve
        # A simple proxy: when steering angle changes faster than lat_g changes, or steering is opposite
        
        corners = df_clean[df_clean['lat_g'].abs() > 0.5]
        if not corners.empty:
            steering_noise = corners['steering_angle'].diff().abs().mean()
            # Factor heuristic: steering_noise of 0.05 is bad, 0.005 is good.
            stability_score = max(0.0, 100.0 - (steering_noise * 1000.0))
            
            max_yaw_accel = corners['yaw_accel'].max()
            
        hard_corners = df_clean[df_clean['lat_g'].abs() > 0.8]
        if not hard_corners.empty:
            peak_g = hard_corners['lat_g'].abs().max()
            avg_g = hard_corners['lat_g'].abs().mean()
            if peak_g > 0:
                confidence_ratio = (avg_g / peak_g) * 100.0
                
        # Counter-steer detection
        # lat_g is e.g., positive for left corner, negative for right corner
        # steering is e.g., positive for left, negative for right
        # We detect counter steer when lat_g and steering have opposite signs and both are somewhat significant
        df_clean['is_counter_steering'] = (df_clean['lat_g'] * df_clean['steering_angle'] < 0) & (df_clean['lat_g'].abs() > 0.5) & (df_clean['steering_angle'].abs() > 0.05)
        
        counter_steer_events = df_clean[df_clean['is_counter_steering']]
        
        # For each counter steer event, check if recovered
        # We define an event grouped by sequential frames
        if not counter_steer_events.empty:
            # We will use simple heuristics: scan the time after the event
            # If speed drops > 30% without brake, or yaw accel explodes = unrecoverable
            # Else recovered = bonus
            indices = counter_steer_events.index.tolist()
            # Group contiguous indices
            event_starts = []
            current_event = [indices[0]]
            for i in range(1, len(indices)):
                if indices[i] == indices[i-1] + 1:
                    current_event.append(indices[i])
                else:
                    event_starts.append(current_event[0])
                    current_event = [indices[i]]
            event_starts.append(current_event[0])
            
            for start_idx in event_starts:
                start_time = df_clean.loc[start_idx, 'time_elapsed']
                window = df_clean[(df_clean['time_elapsed'] > start_time) & (df_clean['time_elapsed'] <= start_time + 2.0)]
                
                if not window.empty:
                    max_yaw = window['yaw_rate'].abs().max()
                    start_speed = df_clean.loc[start_idx, 'speed_kmh']
                    min_speed = window['speed_kmh'].min()
                    max_brake = window['throttle'].min() if 'throttle' in window.columns else 0 # proxy check if they didn't just brake
                    # Or we just check if speed dropped 30%
                    
                    if max_yaw > 2.0 or (min_speed < start_speed * 0.7 and 'throttle' in window.columns and window['brake'].max() < 0.2 if 'brake' in window.columns else False):
                        unrecoverable_spin_penalty += 10.0
                        spin_count += 1
                    else:
                        counter_steer_bonus += 2.0

        # Hard slip angle proxy detection (Spins)
        # Fast rotation + speed loss
        potential_spins = df_clean[(df_clean['yaw_rate'].abs() > 2.5) & (df_clean['speed_kmh'].diff() < -10)]
        spin_count += len(potential_spins) // 10 # very rough grouping

    # Base CSI
    csi = (stability_score * 0.6) + (confidence_ratio * 0.4)
    
    # Apply Counter-steer modifiers
    csi += min(15.0, counter_steer_bonus)  # Cap bonus at 15
    csi -= unrecoverable_spin_penalty
    
    # Critical failure penalty
    csi -= (spin_count * 5.0)
    
    # Yaw Accel Penalty
    if max_yaw_accel > 5.0:
        csi -= min(15.0, (max_yaw_accel - 5.0) * 2.0)
        
    csi = max(0.0, min(100.0, csi))
    
    # Over-Rev filter penalty
    max_rpm = 9000
    if 'rpm' in df_clean.columns:
        over_rev_count = (df_clean['rpm'] > max_rpm).sum()
        if over_rev_count > 10: # > 0.2s over rev
            csi -= 10.0
            csi = max(0.0, csi)
            
    return df_clean, csi, crash_detected

def get_run_options(df):
    notes_str = df['notes'].apply(lambda x: f" | 📝 {x}" if pd.notna(x) and str(x).strip() != "" else "")
    return df['id'].astype(str) + " - [" + df['run_type'] + "] " + df['vehicle_name'] + " (" + df['timestamp'] + ")" + notes_str

runs_df = load_runs()

st.title("🏎️ LMU Performance & Telemetry Analyzer")

tab_rec, tab1, tab2, tab_shift, tab_handling, tab_scoring, tab3 = st.tabs(["🔴 Live Aufzeichnung", "⚙️ Shift Point", "⏱️ Drag Benchmarker", "🚀 Shift Analyzer", "🏎️ Handling & Grip", "🏆 Scoring", "🗑️ Logs"])

with tab_rec:
    # Platzhalter für dynamische Inhalte
    status_placeholder = st.empty()
    st.markdown("---")
    
    # 2. UI-Komponenten & Aufbau
    col_mode, col_light, col_monitor = st.columns([1.5, 1, 1.5])
    
    with col_mode:
        st.subheader("🎛️ Modus-Selektor & Steuerung")
        
        state_for_buttons = get_logger_state()
        
        # DRAG RUN
        st.markdown("#### 🏁 Drag Run Recording")
        st.caption("Optimiert für Beschleunigung (Drehmoment, 0-100, Vmax). Trigger: Vorwärtsbewegung aus dem Stand.")
        d_col1, d_col2 = st.columns(2)
        with d_col1:
            if st.button("🚀 DRAG START", width='stretch', type="primary", disabled=state_for_buttons.startswith("ARMED") or state_for_buttons.startswith("RECORDING")):
                set_logger_state("ARMED_DRAG")
                st.rerun()
        with d_col2:
            if st.button("⏹️ DRAG STOPP", width='stretch', disabled=not (state_for_buttons.startswith("ARMED_DRAG") or state_for_buttons.startswith("RECORDING_DRAG"))):
                set_logger_state("FINISHED")
                st.rerun()
                
        st.markdown("<br>", unsafe_allow_html=True)
        
        # HANDLING RUN
        st.markdown("#### 🏎️ Handling Run Recording")
        st.caption("Optimiert für Kurvenfahrten (G-Force, Sektoren). Trigger: Sobald das Auto fährt.")
        h_col1, h_col2 = st.columns(2)
        with h_col1:
            if st.button("🏎️ HANDLING START", width='stretch', type="primary", disabled=state_for_buttons.startswith("ARMED") or state_for_buttons.startswith("RECORDING")):
                set_logger_state("ARMED_HANDLING")
                st.rerun()
        with h_col2:
            if st.button("⏹️ HANDLING STOPP", width='stretch', disabled=not (state_for_buttons.startswith("ARMED_HANDLING") or state_for_buttons.startswith("RECORDING_HANDLING"))):
                set_logger_state("FINISHED")
                st.rerun()
                
        st.markdown("---")
        if state_for_buttons != "IDLE" and state_for_buttons != "FINISHED":
            if st.button("🔴 Not-Stopp / System Reset", width='stretch', type='secondary'):
                set_logger_state("FINISHED")
                st.rerun()
                
    ampel_placeholder = col_light.empty()
    
    @st.fragment(run_every=0.5)
    def render_auto_updating_status():
        state = get_logger_state()
        
        with status_placeholder.container():
            st.markdown("### 📡 SYSTEM STATUS")
            if state == "IDLE":
                st.error("🔴 STANDBY / KEINE AUFZEICHNUNG")
            elif state.startswith("ARMED"):
                st.warning(f"🟡 BEREIT ({state}) - WARTET AUF TRIGGER (Gaspedal / Bewegung)...")
            elif state.startswith("RECORDING"):
                st.success(f"🟢 AUFZEICHNUNG LÄUFT! ({state}) - DATEN WERDEN GESAMMELT...")
            elif state == "FINISHED":
                st.info("✅ RUN ERFOLGREICH GESPEICHERT! (Datenbank aktualisiert)")

        with ampel_placeholder.container():
            st.subheader("🚥 Status Ampel")
            color_red = "#ff3333" if state == "IDLE" or state == "FINISHED" else "#330000"
            color_yellow = "#ffcc00" if state.startswith("ARMED") else "#333300"
            color_green = "#33cc33" if state.startswith("RECORDING") else "#003300"
            
            ampel_html = f"""
            <div style='background-color: #222; border-radius: 20px; padding: 20px; width: 120px; margin: 0 auto; display: flex; flex-direction: column; align-items: center; gap: 15px; border: 3px solid #444; box-shadow: 0px 0px 15px rgba(0,0,0,0.5);'>
                <div style='width: 60px; height: 60px; border-radius: 50%; background-color: {color_red}; box-shadow: 0 0 {"20px" if color_red=="#ff3333" else "0px"} {color_red};'></div>
                <div style='width: 60px; height: 60px; border-radius: 50%; background-color: {color_yellow}; box-shadow: 0 0 {"20px" if color_yellow=="#ffcc00" else "0px"} {color_yellow};'></div>
                <div style='width: 60px; height: 60px; border-radius: 50%; background-color: {color_green}; box-shadow: 0 0 {"20px" if color_green=="#33cc33" else "0px"} {color_green};'></div>
            </div>
            """
            st.markdown(ampel_html, unsafe_allow_html=True)
            
    render_auto_updating_status()        
    with col_monitor:
        st.subheader("⏱️ Echtzeit-Monitor")
        
        # Integriertes Live-Gauge
        import sys
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.append(current_dir)
            
        try:
            from pyRfactor2SharedMemory.sharedMemoryAPI import SimInfoAPI
            info = SimInfoAPI()
            live_active = info.isRF2running() and info.isSharedMemoryAvailable()
        except:
            live_active = False
            
        if not live_active:
            st.info("Keine aktive Verbindung zu LMU / rFactor 2 Shareld Memory gefunden.")
            # Dummy Gauge
            fig_gauge = go.Figure(go.Indicator(
                mode="gauge+number", value=0, title={'text': "Speed (km/h)"},
                gauge={'axis': {'range': [None, 350]}, 'bar': {'color': "#ff0055"}}
            ))
            fig_gauge.update_layout(height=250, margin=dict(l=20, r=20, t=30, b=20), template="plotly_dark")
            st.plotly_chart(fig_gauge, width='stretch')
        else:
            speed_placeholder = st.empty()
            st.caption("Livedaten-Stream (Aktualisiert automatisch alle 0.5s wenn aktiv)")
            
            # Um das UI-Blockieren zu verhindern, initialisieren wir ein Gauge, und machen ein Optionales Auto Update
            update_live = st.toggle("Live Telemetrie Update aktivieren", value=state_for_buttons.startswith("ARMED") or state_for_buttons.startswith("RECORDING"))
            
            if update_live:
                @st.fragment(run_every=0.5)
                def render_live_gauge():
                    try:
                        if info.isRF2running() and info.isOnTrack():
                            telemetry = info.playersVehicleTelemetry()
                            speed_kmh = abs(telemetry.mLocalVel.z) * 3.6
                            rpm = telemetry.mEngineRPM
                        else:
                            speed_kmh = 0
                            rpm = 0
                            
                        fig_gauge = go.Figure(go.Indicator(
                            mode="gauge+number",
                            value=speed_kmh,
                            title={'text': f"Speed<br><span style='font-size:0.8em;color:gray'>RPM: {rpm:.0f}</span>"},
                            gauge={
                                'axis': {'range': [None, 350]},
                                'bar': {'color': "#00ff88"},
                                'steps': [{'range': [0, 100], 'color': '#222'}, {'range': [100, 200], 'color': '#333'}],
                                'threshold': {'line': {'color': "red", 'width': 4}, 'thickness': 0.75, 'value': speed_kmh}
                            }
                        ))
                        fig_gauge.update_layout(height=280, margin=dict(l=20, r=20, t=10, b=10), template="plotly_dark")
                        speed_placeholder.plotly_chart(fig_gauge, width='stretch')
                    except Exception as e:
                        st.warning("Echtzeit-Verbindungsfehler.")
                
                render_live_gauge()
            else:
                fig_gauge = go.Figure(go.Indicator(
                    mode="gauge+number", value=0, title={'text': "Speed (km/h)"},
                    gauge={'axis': {'range': [None, 350]}, 'bar': {'color': "#555"}}
                ))
                fig_gauge.update_layout(height=280, margin=dict(l=20, r=20, t=10, b=10), template="plotly_dark")
                speed_placeholder.plotly_chart(fig_gauge, width='stretch')

with tab1:
    st.header("Shift Point Optimizer")
    st.markdown("Berechne den perfekten Schaltpunkt pro Gang basierend auf den Engine-Torque-Kurven.")
    
    drag_runs = runs_df[runs_df['run_type'] == 'DRAG']
    
    if drag_runs.empty:
        st.warning("Keine Drag-Telemetrie-Daten gefunden. Bitte zuerst mit `data_logger.py` aufzeichnen!")
    else:
        run_options = get_run_options(drag_runs)
        selected_run_str = st.selectbox("Wähle einen Telemetrie-Run aus", run_options)
        
        selected_run_id = int(selected_run_str.split(" - ")[0])
        
        st.subheader("Getriebe-Daten & Übersetzung")
        detection_mode = st.radio("Gear Ratio Detection Mode", ["Auto-Detect aus Telemetrie (Empfohlen)", "Manuelle Eingabe"])
        
        gear_ratios = []
        final_drive_input = 1.0
        opt = ShiftOptimizer(DB_PATH)
        
        if detection_mode == "Auto-Detect aus Telemetrie (Empfohlen)":
            st.info("Das Tool berechnet das Verhältnis von Speed zu RPM basierend auf den Log-Daten des Autos automatisch und normiert die Kurven.")
            detected_r_values = opt.get_auto_gear_ratios(selected_run_id)
            if not detected_r_values:
                st.warning("Noch nicht genügend Telemetrie vorhanden (oder keine Volllast-Sektionen > 0.9 Throttle), um die Gänge automatisch zu erkennen. Bitte wechsle zur manuellen Eingabe.")
            else:
                formatted_rs = ", ".join([f"G{i+1}: {r:.4f}" for i, r in enumerate(detected_r_values)])
                st.success(f"Erkannte Speed/RPM Ratio pro Gang: {formatted_rs}")
                # Mathematische Normierung: Ratio = 0.12 / R-Wert (entspricht dem alten Proxy in der Visualisierung)
                gear_ratios = [0.12 / r for r in detected_r_values]
                final_drive_input = 1.0 # Base factor
                
        else:
            col1, col2 = st.columns(2)
            ratios_input = col1.text_input("Gear Ratios (kommagetrennt, z.B. 2.50, 1.90, 1.45, 1.20, 1.0, 0.85)", "2.50, 1.90, 1.45, 1.20, 1.0, 0.85")
            final_drive_input = col2.number_input("Final Drive Ratio", value=3.40, step=0.1)
            try:
                gear_ratios = [float(r.strip()) for r in ratios_input.split(',')]
            except:
                st.error("Bitte überprüfe das Format der Gear Ratios.")
        
        st.subheader("Physikalische Fahrzeug-Parameter")
        st.info("Du musst diese Werte nicht exakt wissen. Wähle einfach die ungefähre Fahrzeugklasse aus dem Dropdown, um realistische Standardwerte für Masse und Aerodynamik zu laden. Dies reicht für hochpräzise Schaltpunkte völlig aus!")
        
        presets = {
            "GTE / LM GTE": {"mass": 1245.0, "radius": 0.35, "cwa": 1.60},
            "Hypercar (LMH / LMDh)": {"mass": 1050.0, "radius": 0.35, "cwa": 1.35},
            "LMP2": {"mass": 930.0, "radius": 0.33, "cwa": 1.25},
            "GT3": {"mass": 1300.0, "radius": 0.34, "cwa": 1.55},
            "Manuelle Eingabe": {"mass": 1200.0, "radius": 0.33, "cwa": 1.50}
        }
        
        # Versuche eine smarte Vorauswahl basierend auf Fahrzeugnamen, falls möglich:
        default_idx = 0 # GTE
        try:
            v_name_lower = runs_df[runs_df['id'] == selected_run_id]['vehicle_name'].values[0].lower()
            if "hypercar" in v_name_lower or "lmdh" in v_name_lower or "lmh" in v_name_lower or "toyota" in v_name_lower or "ferrari_499" in v_name_lower or "porsche_963" in v_name_lower:
                default_idx = 1
            elif "lmp2" in v_name_lower or "oreca" in v_name_lower:
                default_idx = 2
            elif "gt3" in v_name_lower:
                default_idx = 3
        except:
            pass
            
        preset_choice = st.selectbox("Fahrzeugklasse (Preset)", list(presets.keys()), index=default_idx)
        def_vals = presets[preset_choice]

        col_p1, col_p2, col_p3, col_p4 = st.columns(4)
        c_mass = col_p1.number_input("Fahrzeugmasse (kg)", value=def_vals["mass"], step=10.0, help="Masse inkl. Fahrer und Kraftstoff")
        c_radius = col_p2.number_input("Radradius (m)", value=def_vals["radius"], step=0.01, help="Statischer Radius der Reifen. GTE/LMH: ca. 0.35m")
        c_cwa = col_p3.number_input("Luftwiderstand $C_w \\cdot A$", value=def_vals["cwa"], step=0.1, help="Widerstandsbeiwert × Stirnfläche")
        c_rho = col_p4.number_input("Luftdichte (kg/m³)", value=1.23, step=0.01)

        if st.button("Schaltpunkte berechnen"):
            if not gear_ratios:
                st.stop()
                
            token_curve = opt.get_torque_curve_from_run(
                selected_run_id, gear_ratios, final_drive_input,
                mass_kg=c_mass, wheel_radius_m=c_radius, c_w_a=c_cwa, rho=c_rho
            )
            
            if token_curve is None or len(token_curve) < 5:
                st.error("Nicht genug valide Daten im ausgewählten Run oder die Telemetrie ist verschlüsselt (Torque=0).")
            else:
                st.subheader("Berechnetes physikalisches Motor-Drehmoment")
                fig_engine = go.Figure()
                fig_engine.add_trace(go.Scatter(x=token_curve['rpm_rounded'], y=token_curve['torque_smoothed'], mode='lines', name='Torque Curve', line=dict(color='#ffaa00', width=3)))
                fig_engine.update_layout(xaxis_title="RPM", yaxis_title="Motor Drehmoment (Nm)", template="plotly_dark")
                st.plotly_chart(fig_engine, width='stretch')
                
                # Berechne Schaltpunkte
                shift_points, rpms, wheel_torques = opt.calculate_ideal_shift_points(token_curve, gear_ratios, final_drive_input, wheel_radius_m=c_radius)
                
                # Speichere die Schaltpunkte ab für das Overlay
                import json
                try:
                    conn_sp = sqlite3.connect(DB_PATH)
                    cursor_sp = conn_sp.cursor()
                    cursor_sp.execute('CREATE TABLE IF NOT EXISTS saved_profiles (run_id INTEGER PRIMARY KEY, vehicle_name TEXT, shift_points_json TEXT)')
                    
                    # Hole Fahrzeugnamen
                    v_name = runs_df[runs_df['id'] == selected_run_id]['vehicle_name'].values[0]
                    
                    cursor_sp.execute('''
                        INSERT OR REPLACE INTO saved_profiles (run_id, vehicle_name, shift_points_json) 
                        VALUES (?, ?, ?)
                    ''', (selected_run_id, v_name, json.dumps(shift_points)))
                    conn_sp.commit()
                    conn_sp.close()
                except Exception as e:
                    st.warning(f"Konnte Profile für Overlay nicht speichern: {e}")
                
                st.subheader("Brutto Radzugkraft vs Speed (Sägezahn-Schnittpunkte)")
                st.markdown("Hier siehst du die erzeugte Kraft am Rad in Newton. Der Schnittpunkt (Kraftverlust) erzwingt mathematisch den optimalen Schaltpunkt.")
                fig_wheel = go.Figure()
                
                for i, wt in enumerate(wheel_torques):
                    # Berechne Proxy Geschwindgkeit, damit die Kurven sich auf der X-Achse überschneiden 
                    # v_m/s = (RPM * 2 * pi / 60) * (wheel_radius) / (gear_ratio * final_drive)
                    # v_km/h = v_m/s * 3.6
                    try:
                        ratio = gear_ratios[i]
                    except:
                        ratio = gear_ratios[-1]
                        
                    # Physisch korrekte Geschwindigkeit:
                    v_mps = (rpms * 2 * np.pi / 60) * c_radius / (ratio * final_drive_input)
                    speed_proxy = v_mps * 3.6
                    
                    fig_wheel.add_trace(go.Scatter(x=speed_proxy, y=wt, mode='lines', name=f'Gang {i+1}'))
                
                fig_wheel.update_layout(xaxis_title="Geschwindigkeit (km/h)", yaxis_title="Radzugkraft (F_wheel) [N]", template="plotly_dark")
                st.plotly_chart(fig_wheel, width='stretch')
                
                st.subheader("✅ Empfohlene Schaltpunkte")
                for sp in shift_points:
                    st.success(f"Schalte **Gang {sp['from_gear']} ➡️ {sp['to_gear']}** bei **{sp['shift_rpm']:.0f} RPM** (RPM fällt auf ca. {sp['rpm_drop_to']:.0f})")

with tab2:
    st.header("Vehicle Benchmarker")
    st.markdown("Vergleiche Beschleunigungszeiten und Vmax zwischen Fahrzeugen/Setups.")
    
    drag_runs = runs_df[runs_df['run_type'] == 'DRAG']
    
    if drag_runs.empty:
        st.warning("Keine Drag-Daten zum Vergleichen vorhanden.")
    else:
        run_options = get_run_options(drag_runs)
        
        col1, col2 = st.columns(2)
        car_a_str = col1.selectbox("Fahrzeug/Setup A", run_options, key="car_a_bench")
        car_b_str = col2.selectbox("Fahrzeug/Setup B", run_options, key="car_b_bench")
        
        run_a_id = int(car_a_str.split(" - ")[0])
        run_b_id = int(car_b_str.split(" - ")[0])
        
        mode = st.radio("Analyse-Modus:", ["Original-Telemetrie (Rohdaten)", "Virtual Best-Run (Mathematisch korrigiert)"], 
                        help="Virtual Best-Run berechnet die Zeiten iterativ anhand der maximalen Beschleunigungskraft pro km/h ('Envelope'). Verlorene Zeit im Begrenzer ('Treppchen') wird mathematisch gelöscht und Schaltvorgänge auf 0.08s standardisiert. Dies ist der Goldstandard für reine Performance-Vergleiche!")
        use_virtual_run = "Virtual Best-Run" in mode
        
        st.number_input("Speed-Trigger für Synchronisation (km/h)", min_value=1, max_value=200, value=50, step=5, key="sync_speed_bench", help="Die Läufe werden exakt an dem Punkt ausgerichtet (Zeit=0), an dem sie diese Geschwindigkeit überschreiten. Ein Wert > 50 km/h eliminiert Fehler durch Schlupf oder unterschiedliche Reaktionszeiten am Start.")
        
        if st.button("Vergleich Starten"):
            tele_a = load_telemetry(run_a_id)
            tele_b = load_telemetry(run_b_id)
            sync_speed_bench_val = st.session_state.sync_speed_bench
            
            if use_virtual_run:
                def generate_virtual_run(df):
                    df_valid = df[df['torque'] > 0].copy()
                    if df_valid.empty: return df
                    
                    df_valid['speed_bin'] = df_valid['speed_kmh'].round()
                    envelope = df_valid.groupby('speed_bin')['torque'].quantile(0.95).reset_index()
                    if len(envelope) < 5: return df
                    
                    min_bin = int(envelope['speed_bin'].min())
                    max_bin = int(envelope['speed_bin'].max())
                    envelope = envelope.set_index('speed_bin').reindex(range(min_bin, max_bin + 1)).interpolate(method='linear').reset_index()
                    
                    start_speed = max(0.0, df['speed_kmh'].iloc[0])
                    max_speed = df['speed_kmh'].max()
                    
                    accels_m_s2 = np.maximum(envelope['torque'].values / 1000.0, 0.05)
                    accel_interp = interp1d(envelope['speed_bin'].values, accels_m_s2, kind='linear', fill_value="extrapolate")
                    
                    sim_speeds = np.arange(start_speed, max_speed, 0.1)
                    sim_times = []
                    current_time = df['time_elapsed'].iloc[0]
                    
                    shift_speeds = []
                    last_gear = df_valid['gear'].iloc[0]
                    for i in range(1, len(df_valid)):
                        if df_valid['gear'].iloc[i] > last_gear:
                            shift_speeds.append(df_valid['speed_kmh'].iloc[i])
                            last_gear = df_valid['gear'].iloc[i]
                            
                    shift_delay = 0.08
                    shifts_applied = sum(1 for s in shift_speeds if s < start_speed)
                    
                    for v in sim_speeds:
                        a = accel_interp(v)
                        if a < 0.05: a = 0.05
                        dt = (0.1 / 3.6) / a
                        
                        while shifts_applied < len(shift_speeds) and v >= shift_speeds[shifts_applied]:
                            current_time += shift_delay
                            shifts_applied += 1
                            
                        current_time += dt
                        sim_times.append(current_time)
                        
                    return pd.DataFrame({
                        'time_elapsed': sim_times,
                        'speed_kmh': sim_speeds
                    })
                
                tele_a = generate_virtual_run(tele_a)
                tele_b = generate_virtual_run(tele_b)
            
            def normalize_bench_run(df, target_sync_speed):
                df_start = df[df['speed_kmh'] >= target_sync_speed]
                if not df_start.empty:
                    t0 = df_start.iloc[0]['time_elapsed']
                    df_norm = df[df['time_elapsed'] >= t0].copy()
                    df_norm['time_elapsed'] = df_norm['time_elapsed'] - t0
                    return df_norm
                return df

            tele_a = normalize_bench_run(tele_a, sync_speed_bench_val)
            tele_b = normalize_bench_run(tele_b, sync_speed_bench_val)

            def calculate_0_to_x(df, target_speed):
                filtered = df[df['speed_kmh'] >= target_speed]
                if filtered.empty:
                    return None
                return filtered.iloc[0]['time_elapsed'] - df.iloc[0]['time_elapsed']

            metrics_a = {
                f"{sync_speed_bench_val}-100 km/h": calculate_0_to_x(tele_a, 100),
                f"{sync_speed_bench_val}-200 km/h": calculate_0_to_x(tele_a, 200),
                f"{sync_speed_bench_val}-300 km/h": calculate_0_to_x(tele_a, 300),
                "Vmax": tele_a['speed_kmh'].max()
            }
            
            metrics_b = {
                f"{sync_speed_bench_val}-100 km/h": calculate_0_to_x(tele_b, 100),
                f"{sync_speed_bench_val}-200 km/h": calculate_0_to_x(tele_b, 200),
                f"{sync_speed_bench_val}-300 km/h": calculate_0_to_x(tele_b, 300),
                "Vmax": tele_b['speed_kmh'].max()
            }
            
            st.subheader("📊 Performance KPIs")
            
            m_col1, m_col2, m_col3, m_col4 = st.columns(4)
            
            def render_metric(col, label, key):
                val_a = metrics_a[key]
                val_b = metrics_b[key]
                
                if val_a is None: val_a_str = "N/A"
                elif key == "Vmax": val_a_str = f"{val_a:.1f} km/h"
                else: val_a_str = f"{val_a:.2f} s"
                
                if val_a is not None and val_b is not None:
                    delta = val_a - val_b
                    # Für Vmax ist positiv besser, für Beschleunigung ist negativ besser
                    if key == "Vmax":
                        delta_str = f"{delta:+.1f} km/h (A vs B)"
                        color = "normal" if delta == 0 else "inverse"  # invert colors natively in streamlit is tricky, let's just show string
                    else:
                        delta_str = f"{delta:+.2f} s (A vs B)"
                else:
                    delta_str = "N/A"
                    
                col.metric(label=f"A: {label}", value=val_a_str, delta=delta_str, delta_color="inverse" if key != "Vmax" else "normal")
            
            render_metric(m_col1, f"{sync_speed_bench_val}-100 km/h", f"{sync_speed_bench_val}-100 km/h")
            render_metric(m_col2, f"{sync_speed_bench_val}-200 km/h", f"{sync_speed_bench_val}-200 km/h")
            render_metric(m_col3, f"{sync_speed_bench_val}-300 km/h", f"{sync_speed_bench_val}-300 km/h")
            render_metric(m_col4, "Vmax", "Vmax")
            
            st.subheader("Geschwindigkeit über Zeit / Speed-Curve Overlay")
            
            # Beschneide die längere Linie auf die Zeit der kürzeren Linie für einen optisch fairen Vergleich
            max_plot_time = min(tele_a['time_elapsed'].max(), tele_b['time_elapsed'].max())
            tele_a_plot = tele_a[tele_a['time_elapsed'] <= max_plot_time]
            tele_b_plot = tele_b[tele_b['time_elapsed'] <= max_plot_time]
            
            fig_speed = go.Figure()
            fig_speed.add_trace(go.Scatter(x=tele_a_plot['time_elapsed'], y=tele_a_plot['speed_kmh'], name=f"A: {car_a_str.split(' - ')[1]}", mode='lines', line=dict(color='#00ff88')))
            fig_speed.add_trace(go.Scatter(x=tele_b_plot['time_elapsed'], y=tele_b_plot['speed_kmh'], name=f"B: {car_b_str.split(' - ')[1]}", mode='lines', line=dict(color='#ff0055')))
            
            fig_speed.update_layout(xaxis_title="Time (s)", yaxis_title="Speed (km/h)", template="plotly_dark")
            st.plotly_chart(fig_speed, width='stretch')

with tab_shift:
    st.header("Drag & Shift Performance Analyzer")
    st.markdown("Vergleiche zwei Beschleunigungsfahrten hochpräzise. Analysiere Schaltlatenzen, RPM Tipps und finde heraus, welches Auto oder Setup auf der Geraden dominiert.")
    
    # Quick Record UI for Shift Analyzer
    scol1, scol2 = st.columns([2, 1])
    
    with scol1:
        st.subheader("⏱️ Live Quick-Record")
        st.caption("Nimm Drag-Läufe separat von der Haupt-DB direkt hier auf ('Quick_Shift').")
        q_col1, q_col2, q_col3 = st.columns(3)
        shift_state = get_logger_state()
        
        with q_col1:
            if st.button("🚀 Quick Record Auto A", type="primary" if not shift_state.startswith("ARMED") else "secondary", width='stretch', disabled=shift_state.startswith("ARMED") or shift_state.startswith("RECORDING")):
                set_logger_state("ARMED_QUICK_SHIFT_A")
                st.rerun()
        with q_col2:
            if st.button("🚀 Quick Record Auto B", type="primary" if not shift_state.startswith("ARMED") else "secondary", width='stretch', disabled=shift_state.startswith("ARMED") or shift_state.startswith("RECORDING")):
                set_logger_state("ARMED_QUICK_SHIFT_B")
                st.rerun()
        with q_col3:
            if st.button("⏹️ Stopp / Abbrechen", width='stretch', disabled=shift_state == "IDLE" or shift_state == "FINISHED"):
                set_logger_state("FINISHED")
                st.rerun()
                
    with scol2:
        st.info(f"**Status:** {shift_state}")
        # Kleines Speedometer (wird nur aktualisiert wenn wir auf der Shift page sind und manuell togglen)
        s_update_live = st.toggle("Live Telemetrie Update", value=shift_state.startswith("ARMED") or shift_state.startswith("RECORDING"), key="shift_live_tog")
        if s_update_live:
            @st.fragment(run_every=0.5)
            def render_quick_measure():
                try:
                    import sys
                    current_dir = os.path.dirname(os.path.abspath(__file__))
                    if current_dir not in sys.path:
                        sys.path.append(current_dir)
                    from pyRfactor2SharedMemory.sharedMemoryAPI import SimInfoAPI
                    info = SimInfoAPI()
                    if info.isRF2running() and info.isOnTrack():
                        t = info.playersVehicleTelemetry()
                        st.metric("Live Speed", f"{abs(t.mLocalVel.z) * 3.6:.1f} km/h", f"RPM: {t.mEngineRPM:.0f}")
                    else:
                        st.metric("Live Speed", "0.0 km/h", "Waiting for track...")
                except:
                    pass
            render_quick_measure()
        else:
            st.metric("Live Speed", "--- km/h", "Offline")

    st.markdown("---")
    
    # Load Runs for Analysis (Include both Drag and Temporary Quick_Shifts)
    drag_runs = runs_df[(runs_df['run_type'] == 'DRAG') | (runs_df['run_type'].str.startswith('QUICK_SHIFT'))]
    
    if drag_runs.empty:
        st.warning("Keine Drag-Daten zum Vergleichen vorhanden.")
    else:
        run_options = get_run_options(drag_runs)
        
        col1, col2 = st.columns(2)
        car_a_str = col1.selectbox("Fahrzeug/Setup A (Referenz)", run_options, key="car_a")
        car_b_str = col2.selectbox("Fahrzeug/Setup B (Vergleich)", run_options, key="car_b")
        
        run_a_id = int(car_a_str.split(" - ")[0])
        run_b_id = int(car_b_str.split(" - ")[0])
        
        st.number_input("Speed-Trigger für Synchronisation (km/h)", min_value=1, max_value=200, value=50, step=5, key="sync_speed", help="Die Läufe werden exakt an dem Punkt ausgerichtet (Zeit=0), an dem sie diese Geschwindigkeit überschreiten. Ein Wert > 50 km/h eliminiert Fehler durch Schlupf oder unterschiedliche Reaktionszeiten am Start.")
        
        if st.button("🏁 Analyse Starten", type="primary", width='stretch'):
            tele_a_raw = load_telemetry(run_a_id)
            tele_b_raw = load_telemetry(run_b_id)
            
            sync_speed = st.session_state.sync_speed
            
            def normalize_run(df, target_sync_speed):
                df_start = df[df['speed_kmh'] >= target_sync_speed]
                if not df_start.empty:
                    t0 = df_start.iloc[0]['time_elapsed']
                    df_norm = df[df['time_elapsed'] >= t0].copy()
                    df_norm['time_elapsed'] = df_norm['time_elapsed'] - t0
                else:
                    df_norm = df.copy()
                    
                # Berechne zurückgelegte Strecke (ds = v * dt)
                df_norm['distance_m'] = (df_norm['speed_kmh'] / 3.6) * df_norm['time_elapsed'].diff().fillna(0)
                df_norm['distance_cum'] = df_norm['distance_m'].cumsum()
                
                return df_norm
                
            tele_a = normalize_run(tele_a_raw, sync_speed)
            tele_b = normalize_run(tele_b_raw, sync_speed)
            
            def extract_shift_metrics(df):
                metrics = []
                # Shift detection: changing gear
                df = df.reset_index(drop=True)
                gear_changes = df[df['gear'].diff() > 0]
                
                for idx, row in gear_changes.iterrows():
                    if idx == 0: continue
                    gear_from = df.loc[idx-1, 'gear']
                    gear_to = row['gear']
                    shift_time = row['time_elapsed']
                    
                    # Window +/- 0.4s
                    window = df[(df['time_elapsed'] >= shift_time - 0.4) & (df['time_elapsed'] <= shift_time + 0.4)]
                    if window.empty: continue
                    
                    # Berechne avg torque im gelernten Gang (zur Referenz für Dips)
                    avg_torque = 2000
                    target_gear_data = df[(df['gear'] == gear_to) & (df['throttle'] > 0.9)]
                    if not target_gear_data.empty:
                        avg_t = target_gear_data['torque'].quantile(0.8)
                        if avg_t > 0: avg_torque = avg_t
                        
                    drop_threshold = avg_torque * 0.4 # Einbruch unter 40% der Zugkraft = Shift Phase
                    
                    dip_window = window[window['torque'] < drop_threshold]
                    
                    if not dip_window.empty:
                        latency = dip_window['time_elapsed'].max() - dip_window['time_elapsed'].min()
                        latency_ms = latency * 1000
                        # RPM landepunkt = first point after latency where torque >= threshold
                        recover_df = window[(window['time_elapsed'] > dip_window['time_elapsed'].max()) & (window['torque'] >= drop_threshold)]
                        rpm_land = recover_df.iloc[0]['rpm'] if not recover_df.empty else row['rpm']
                    else:
                        latency_ms = 0.0
                        rpm_land = row['rpm']
                        
                    metrics.append({
                        'gear_from': int(gear_from),
                        'gear_to': int(gear_to),
                        'time': shift_time,
                        'latency_ms': latency_ms,
                        'rpm_land': rpm_land
                    })
                return metrics
                
            shifts_a = extract_shift_metrics(tele_a)
            shifts_b = extract_shift_metrics(tele_b)
            
            def calculate_0_to_x(df, target_speed):
                filtered = df[df['speed_kmh'] >= target_speed]
                if filtered.empty: return None
                return filtered.iloc[0]['time_elapsed'] - df.iloc[0]['time_elapsed']

            # --- Layout: KPIs ---
            st.subheader("📊 Beschleunigungs-Intervalle & Vmax")
            c1, c2, c3, c4 = st.columns(4)
            
            def render_accel_kpi(col, label, speed_start, speed_target):
                df_start_a = tele_a[tele_a['speed_kmh'] >= speed_start]
                df_start_b = tele_b[tele_b['speed_kmh'] >= speed_start]
                
                df_end_a = tele_a[tele_a['speed_kmh'] >= speed_target]
                df_end_b = tele_b[tele_b['speed_kmh'] >= speed_target]
                
                val_a = (df_end_a.iloc[0]['time_elapsed'] - df_start_a.iloc[0]['time_elapsed']) if not df_end_a.empty and not df_start_a.empty else None
                val_b = (df_end_b.iloc[0]['time_elapsed'] - df_start_b.iloc[0]['time_elapsed']) if not df_end_b.empty and not df_start_b.empty else None
                
                val_a_str = f"{val_a:.2f} s" if val_a else "N/A"
                if val_a and val_b:
                    delta = val_a - val_b
                    col.metric(label, val_a_str, delta=f"{delta:+.2f} s", delta_color="inverse")
                else:
                    col.metric(label, val_a_str)
                    
            render_accel_kpi(c1, "0-100 km/h", sync_speed, 100) # Normed to sync_speed because of t=0 shift
            render_accel_kpi(c2, "0-200 km/h", sync_speed, 200)
            render_accel_kpi(c3, "100-250 km/h", 100, 250)
            
            vmax_a = tele_a['speed_kmh'].max()
            vmax_b = tele_b['speed_kmh'].max()
            c4.metric("Vmax", f"{vmax_a:.1f} km/h", delta=f"{vmax_a - vmax_b:+.1f} km/h", delta_color="normal")
            
            st.markdown("---")
            
            st.subheader("⚙️ Shift-Performance Tabelle")
            col_t1, col_t2 = st.columns(2)
            
            with col_t1:
                st.markdown("**Auto A: Schaltanalyse**")
                if shifts_a:
                    df_sa = pd.DataFrame(shifts_a)
                    df_sa['Gangwechsel'] = df_sa['gear_from'].astype(str) + " ➡️ " + df_sa['gear_to'].astype(str)
                    df_sa['Latenz (Zugkraftunterbrechung)'] = df_sa['latency_ms'].map("{:.0f} ms".format)
                    df_sa['RPM Landepunkt'] = df_sa['rpm_land'].map("{:.0f} RPM".format)
                    st.dataframe(df_sa[['Gangwechsel', 'Latenz (Zugkraftunterbrechung)', 'RPM Landepunkt']], hide_index=True, width='stretch')
                else:
                    st.info("Keine Schaltvorgänge gefunden.")
                    
            with col_t2:
                st.markdown("**Auto B: Schaltanalyse**")
                if shifts_b:
                    df_sb = pd.DataFrame(shifts_b)
                    df_sb['Gangwechsel'] = df_sb['gear_from'].astype(str) + " ➡️ " + df_sb['gear_to'].astype(str)
                    df_sb['Latenz (Zugkraftunterbrechung)'] = df_sb['latency_ms'].map("{:.0f} ms".format)
                    df_sb['RPM Landepunkt'] = df_sb['rpm_land'].map("{:.0f} RPM".format)
                    st.dataframe(df_sb[['Gangwechsel', 'Latenz (Zugkraftunterbrechung)', 'RPM Landepunkt']], hide_index=True, width='stretch')
                else:
                    st.info("Keine Schaltvorgänge gefunden.")
                    
            # Gear by gear accel
            def gear_accel(df):
                res = []
                for g in sorted(df['gear'].unique()):
                    if g < 1: continue
                    d = df[(df['gear'] == g) & (df['throttle'] > 0.9) & (df['torque'] > 0)]
                    if not d.empty:
                        res.append({'Gang': int(g), 'Accel': d['torque'].mean() / 1000.0})
                return pd.DataFrame(res)
                
            ga_a = gear_accel(tele_a)
            ga_b = gear_accel(tele_b)
            if not ga_a.empty and not ga_b.empty:
                ga_merged = pd.merge(ga_a, ga_b, on='Gang', how='outer', suffixes=(' Auto A', ' Auto B')).fillna(0)
                fig_bar = go.Figure(data=[
                    go.Bar(name='Auto A', x=ga_merged['Gang'], y=ga_merged['Accel Auto A'], marker_color='#00ff88'),
                    go.Bar(name='Auto B', x=ga_merged['Gang'], y=ga_merged['Accel Auto B'], marker_color='#ff0055')
                ])
                fig_bar.update_layout(title="Durchschnittliche Beschleunigung pro Gang (Volllast) [m/s²]", barmode='group', template='plotly_dark')
                st.plotly_chart(fig_bar, width='stretch')
                
            st.markdown("---")
            
            st.subheader("📈 The Shift Gap - Speed Delta Overlay")
            st.markdown("Zoome in die Kurve rein, um den Geschwindigkeits-Dip bei jedem Gangwechsel ('Time Lost due to Over-Revving' / Latenz) genau zu sehen.")
            
            max_t = min(tele_a['time_elapsed'].max(), tele_b['time_elapsed'].max())
            ta_plot = tele_a[tele_a['time_elapsed'] <= max_t]
            tb_plot = tele_b[tele_b['time_elapsed'] <= max_t]
            
            from plotly.subplots import make_subplots
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.08)
            
            fig.add_trace(go.Scatter(x=ta_plot['time_elapsed'], y=ta_plot['speed_kmh'], name="Speed Auto A", mode='lines', line=dict(color='#00ff88')), row=1, col=1)
            fig.add_trace(go.Scatter(x=tb_plot['time_elapsed'], y=tb_plot['speed_kmh'], name="Speed Auto B", mode='lines', line=dict(color='#ff0055')), row=1, col=1)
            
            # Delta
            if len(tb_plot) > 5 and len(ta_plot) > 5:
                interp_b = interp1d(tb_plot['time_elapsed'], tb_plot['speed_kmh'], kind='linear', fill_value="extrapolate")
                delta_speed = ta_plot['speed_kmh'] - interp_b(ta_plot['time_elapsed'])
                fig.add_trace(go.Scatter(x=ta_plot['time_elapsed'], y=delta_speed, name='Delta (A - B) [km/h]', mode='lines', line=dict(color='#00bfff', width=1), fill='tozeroy'), row=2, col=1)
            
            # Shift Markers
            for s in shifts_a:
                fig.add_vline(x=s['time'], line_dash="dash", line_color="rgba(0, 255, 136, 0.8)", row=1, col=1)
            for s in shifts_b:
                fig.add_vline(x=s['time'], line_dash="dash", line_color="rgba(255, 0, 85, 0.8)", row=1, col=1)
                
            fig.update_layout(height=600, template="plotly_dark", hovermode="x unified")
            fig.update_xaxes(title_text="Zeit (s)", row=2, col=1)
            fig.update_yaxes(title_text="Geschw. (km/h)", row=1, col=1)
            fig.update_yaxes(title_text="Delta km/h", row=2, col=1)
            st.plotly_chart(fig, width='stretch')
            
            st.markdown("---")
            st.subheader("🏎️ Virtual Drag Race (Distanz-Delta)")
            st.markdown("Das Distanz-Delta zeigt an, um wie viele Meter ein Auto voraus ist. Ein positiver Wert bedeutet, Auto A führt.")
            
            if len(tb_plot) > 5 and len(ta_plot) > 5:
                interp_dist_b = interp1d(tb_plot['time_elapsed'], tb_plot['distance_cum'], kind='linear', fill_value="extrapolate")
                dist_delta = ta_plot['distance_cum'] - interp_dist_b(ta_plot['time_elapsed'])
                
                fig_dist = go.Figure()
                fig_dist.add_trace(go.Scatter(x=ta_plot['time_elapsed'], y=dist_delta, name='Vorsprung Auto A (Meter)', mode='lines', line=dict(color='#e0e0e0', width=3), fill='tozeroy'))
                fig_dist.update_layout(height=350, template="plotly_dark", xaxis_title="Zeit (s)", yaxis_title="Vorsprung Auto A [Meter]")
                
                # Highlight Shifts im Distanzgraphen um zu sehen, wie sich der Abstand beim Schalten aufbaut
                for s in shifts_a:
                    fig_dist.add_vline(x=s['time'], line_dash="dash", line_color="rgba(0, 255, 136, 0.5)")
                    
                st.plotly_chart(fig_dist, width='stretch')
                
                final_gap = dist_delta.iloc[-1]
                distance_driven = ta_plot['distance_cum'].iloc[-1]
                if final_gap > 0:
                    st.success(f"🏁 **Zielkreuzung (nach {distance_driven:.0f} Metern):** Auto A gewinnt mit **{final_gap:.2f} Metern** Vorsprung!")
                elif final_gap < 0:
                    st.error(f"🏁 **Zielkreuzung (nach {distance_driven:.0f} Metern):** Auto B gewinnt mit **{abs(final_gap):.2f} Metern** Vorsprung!")
                else:
                    st.info(f"🏁 **Zielkreuzung (nach {distance_driven:.0f} Metern):** Unentschieden!")

with tab3:
    st.header("Datenbank / Logs verwalten")
    st.markdown("Hier kannst du alle gespeicherten Telemetrie-Aufzeichnungen sehen und endgültig aus der Datenbank löschen.")
    
    if runs_df.empty:
        st.info("Die Datenbank ist derzeit leer.")
    else:
        st.dataframe(
            runs_df[['id', 'run_type', 'vehicle_name', 'vehicle_class', 'track_name', 'timestamp', 'notes']], 
            width='stretch',
            hide_index=True
        )
        
        run_options_edit = get_run_options(runs_df)
        
        st.markdown("---")
        st.subheader("💾 Backup (Export / Import)")
        st.markdown("Hier kannst du die komplette Datenbank herunterladen oder ein existierendes Backup wiederherstellen.")
        
        col_down, col_up = st.columns(2)
        
        with col_down:
            try:
                with open(DB_PATH, "rb") as f:
                    db_bytes = f.read()
                    st.download_button(
                        label="📥 Gesamte Datenbank (.db) herunterladen",
                        data=db_bytes,
                        file_name=f"lmu_telemetry_backup_{time.strftime('%Y%m%d_%H%M%S')}.db",
                        mime="application/octet-stream",
                        width='stretch'
                    )
            except Exception as e:
                 st.error("Fehler beim Erstellen des Backups.")
                
        with col_up:
            uploaded_file = st.file_uploader("📤 Datenbank (.db) hochladen", type=["db"])
            if uploaded_file is not None:
                if st.button("⚠️ Backup einspielen (Überschreibt alle Daten!)", type="primary", width='stretch'):
                    try:
                        with open(DB_PATH, "wb") as f:
                            f.write(uploaded_file.getbuffer())
                        st.success("Backup erfolgreich eingespielt! Seite lädt neu...")
                        time.sleep(1.5)
                        st.rerun()
                    except Exception as e:
                        st.error("Fehler beim Einspielen des Backups.")

        st.markdown("---")

        st.subheader("📊 Einzelnen Run exportieren")
        selected_export_str = st.selectbox("Wähle einen Run für den CSV-Export", run_options_edit, key="export_selectbox")
        
        if selected_export_str:
            export_id = int(selected_export_str.split(" - ")[0])
            export_df = load_telemetry(export_id)
            if not export_df.empty:
                csv = export_df.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label=f"📥 Run {export_id} als CSV herunterladen",
                    data=csv,
                    file_name=f"lmu_run_{export_id}.csv",
                    mime="text/csv",
                )

        st.markdown("---")
        
        st.subheader("📝 Notiz bearbeiten / hinzufügen")
        selected_note_str = st.selectbox("Wähle einen Run, um eine Notiz zu bearbeiten", run_options_edit, key="note_selectbox")
        
        if selected_note_str:
            note_id = int(selected_note_str.split(" - ")[0])
            current_note = runs_df[runs_df['id'] == note_id].iloc[0].get('notes', '')
            if pd.isna(current_note):
                current_note = ""
                
            new_note = st.text_area("Notiz (z.B. Setup-Vorgaben, Besonderheiten beim Launch, etc.):", value=current_note)
            
            if st.button("💾 Notiz speichern"):
                try:
                    conn_note = sqlite3.connect(DB_PATH)
                    c_note = conn_note.cursor()
                    c_note.execute("UPDATE runs SET notes = ? WHERE id = ?", (new_note, note_id))
                    conn_note.commit()
                    conn_note.close()
                    st.success("Notiz erfolgreich gespeichert!")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Fehler: {e}")

        st.markdown("---")
        
        st.subheader("🗑️ Eintrag löschen")
        st.warning("⚠️ Beim Löschen werden auch tausende Telemetrie-Datenpunkte aus der Datenbank entfernt. Dies kann nicht rückgängig gemacht werden.")
        selected_del_str = st.selectbox("Wähle einen Run zum Löschen aus", run_options_edit, key="delete_selectbox")
        
        if selected_del_str:
            del_id = int(selected_del_str.split(" - ")[0])
            del_info = runs_df[runs_df['id'] == del_id].iloc[0]
            st.error(f"⚠️ **Folgender Eintrag wird gelöscht:**\n\n**ID:** `{del_id}` | **Typ:** `{del_info['run_type']}` | **Fahrzeug:** `{del_info['vehicle_name']}` | **Strecke:** `{del_info['track_name']}` | **Zeit:** `{del_info['timestamp']}`")
        
        if st.button("🗑️ Run permanent löschen", type="primary"):
            del_id = int(selected_del_str.split(" - ")[0])
            try:
                conn_del = sqlite3.connect(DB_PATH)
                c = conn_del.cursor()
                # Lösche raw telemetry
                c.execute(f"DELETE FROM telemetry_data WHERE run_id = {del_id}")
                # Lösche run Metadaten
                c.execute(f"DELETE FROM runs WHERE id = {del_id}")
                # Lösche evtl. generierte Overlay-Profile
                try:
                     c.execute(f"DELETE FROM saved_profiles WHERE run_id = {del_id}")
                except:
                     pass
                conn_del.commit()
                conn_del.close()
                
                st.success(f"Run {del_id} und alle dazugehörigen Telemetriedaten wurden gelöscht!")
                time.sleep(1) # kurzes Delay für die Success-Nachricht
                st.rerun()
            except Exception as e:
                st.error(f"Fehler beim Löschen: {e}")

with tab_handling:
    st.header("Handling & Grip Analyzer")
    st.markdown("Vergleiche das Fahrwerks- und Aerodynamik-Potenzial (Traktionskreis, Kurvenspeed, G-Kräfte) zwischen zwei Autos oder Setups.")
    
    handling_runs = runs_df[runs_df['run_type'] == 'HANDLING']
    
    if handling_runs.empty:
        st.warning("Keine Handling-Daten zum Vergleichen vorhanden.")
    else:
        run_options = get_run_options(handling_runs)
        
        col1, col2 = st.columns(2)
        car_a_str_h = col1.selectbox("Auto A (Referenz)", run_options, key="handling_a")
        car_b_str_h = col2.selectbox("Auto B (Vergleich)", run_options, key="handling_b")
        
        c1, c2 = st.columns(2)
        fuel_a = c1.number_input("Fuel Load Auto A (Liters)", value=50, step=1, key="fuel_a")
        fuel_b = c2.number_input("Fuel Load Auto B (Liters)", value=50, step=1, key="fuel_b")
        
        if st.button("🔧 Handling-Daten Analysieren", type="primary", width='stretch'):
            run_a_id = int(car_a_str_h.split(" - ")[0])
            run_b_id = int(car_b_str_h.split(" - ")[0])
            
            tele_a = load_telemetry(run_a_id)
            tele_b = load_telemetry(run_b_id)
            
            if 'lat_g' not in tele_a.columns or tele_a['lat_g'].sum() == 0:
                st.error("Achtung: Diesem Run fehlen die G-Force-Daten! Bitte stelle sicher, dass du mit dem aktuellsten Data-Logger neue Runden aufzeichnest.")
            else:
                col_graph1, col_graph2 = st.columns(2)
                
                with col_graph1:
                    st.subheader("G-G Diagramm (Traction Circle)")
                    st.markdown("Zeigt das absolute Limit der Reifen. Ein größerer Kreis bedeutet mehr mechanischen und aerodynamischen Grip.")
                    
                    fig_gg = go.Figure()
                    
                    def add_traction_circle(fig, df, name, color, color_fill):
                        # Filtere sinnlose Steh-Punkte raus
                        df_filt = df[df['speed_kmh'] > 20].copy()
                        if df_filt.empty: return
                        
                        # Entferne krasse Outliers (z.B. Curbs/Unfälle) für ein sauberes Polygon
                        q_lat_high = df_filt['lat_g'].quantile(0.999)
                        q_lat_low = df_filt['lat_g'].quantile(0.001)
                        q_lon_high = df_filt['lon_g'].quantile(0.999)
                        q_lon_low = df_filt['lon_g'].quantile(0.001)
                        
                        df_filt = df_filt[
                            (df_filt['lat_g'] >= q_lat_low) & (df_filt['lat_g'] <= q_lat_high) &
                            (df_filt['lon_g'] >= q_lon_low) & (df_filt['lon_g'] <= q_lon_high)
                        ]
                        
                        lat = df_filt['lat_g'].values
                        lon = df_filt['lon_g'].values
                        
                        points = np.column_stack((lat, lon))

                        # Erzeuge ein "runderes" Limit: Punkte in Winkel-Segmente gruppieren
                        # und pro Segment das 95. Perzentil des Radius verwenden.
                        try:
                            # Polar-Koordinaten (theta vom x-axis, also lat)
                            thetas = np.arctan2(lon, lat)
                            radii = np.sqrt(lat**2 + lon**2)

                            bin_deg = 5
                            bins = int(360 / bin_deg)
                            edges = np.linspace(-np.pi, np.pi, bins + 1)
                            seg_points = []
                            for i in range(len(edges) - 1):
                                start, end = edges[i], edges[i+1]
                                # Maske für Winkel in diesem Segment
                                if start < end:
                                    mask = (thetas >= start) & (thetas < end)
                                else:
                                    mask = (thetas >= start) | (thetas < end)

                                if not np.any(mask):
                                    continue

                                r95 = np.nanpercentile(radii[mask], 95)
                                if np.isnan(r95) or r95 <= 0:
                                    continue

                                angle_center = (start + end) / 2.0
                                x = r95 * np.cos(angle_center)
                                y = r95 * np.sin(angle_center)
                                seg_points.append((angle_center, x, y))

                            if len(seg_points) >= 3:
                                # Sortiere nach Winkel und schließe das Polygon
                                seg_points.sort(key=lambda t: t[0])
                                hull_points = np.array([[p[1], p[2]] for p in seg_points])
                                hull_points = np.vstack((hull_points, hull_points[0]))

                                fig.add_trace(go.Scatter(
                                    x=hull_points[:, 0], y=hull_points[:, 1],
                                    mode='lines', fill='toself', name=f"Limit {name}",
                                    line=dict(color=color, width=2),
                                    fillcolor=color_fill,
                                    opacity=0.8
                                ))
                            else:
                                # Fallback auf ConvexHull falls zu wenige Segmente
                                hull = ConvexHull(points)
                                hull_points = points[hull.vertices]
                                hull_points = np.vstack((hull_points, hull_points[0]))
                                fig.add_trace(go.Scatter(
                                    x=hull_points[:, 0], y=hull_points[:, 1],
                                    mode='lines', fill='toself', name=f"Limit {name}",
                                    line=dict(color=color, width=2),
                                    fillcolor=color_fill,
                                    opacity=0.8
                                ))
                        except Exception:
                            # Falls etwas schief geht, einfach die Rohpunkte anzeigen
                            try:
                                hull = ConvexHull(points)
                                hull_points = points[hull.vertices]
                                hull_points = np.vstack((hull_points, hull_points[0]))
                                fig.add_trace(go.Scatter(
                                    x=hull_points[:, 0], y=hull_points[:, 1],
                                    mode='lines', fill='toself', name=f"Limit {name}",
                                    line=dict(color=color, width=2),
                                    fillcolor=color_fill,
                                    opacity=0.8
                                ))
                            except Exception:
                                pass
                            
                        # Rohdaten-Punkte schwach im Hintergrund
                        fig.add_trace(go.Scatter(
                            x=lat, y=lon,
                            mode='markers', name=f"Data {name}",
                            marker=dict(size=2, color=color, opacity=0.1),
                            showlegend=False
                        ))
                    
                    # Rot für A und Cyan für B, semi-transparent
                    add_traction_circle(fig_gg, tele_a, "A", "#00ff88", "rgba(0, 255, 136, 0.2)")
                    add_traction_circle(fig_gg, tele_b, "B", "#ff0055", "rgba(255, 0, 85, 0.2)")
                    
                    fig_gg.update_layout(
                        xaxis_title="Lateral G (Kurve)", 
                        yaxis_title="Longitudinal G (Bremsen/Gas)",
                        yaxis=dict(range=[-3.0, 1.5], scaleanchor="x", scaleratio=1),
                        xaxis=dict(range=[-2.5, 2.5]),
                        width=500, height=500,
                        template="plotly_dark",
                        showlegend=True,
                        legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01)
                    )
                    # Gitter kreuz in die mitte
                    fig_gg.add_vline(x=0, line_width=1, line_color="gray", opacity=0.5)
                    fig_gg.add_hline(y=0, line_width=1, line_color="gray", opacity=0.5)
                    
                    st.plotly_chart(fig_gg, width='stretch')
                
                with col_graph2:
                    st.subheader("🏁 Speed Heatmap über Track Distance")
                    st.markdown("Direkter Vergleich der Kurvengeschwindigkeiten. Wer bremst später? Wer beschleunigt früher?")
                    
                    fig_track = go.Figure()
                    
                    # Sort by lap distance
                    tele_a_sorted = tele_a[tele_a['lap_distance'] > 0].sort_values('lap_distance')
                    tele_b_sorted = tele_b[tele_b['lap_distance'] > 0].sort_values('lap_distance')
                    
                    if not tele_a_sorted.empty and not tele_b_sorted.empty:
                        fig_track.add_trace(go.Scatter(
                            x=tele_a_sorted['lap_distance'], y=tele_a_sorted['speed_kmh'], 
                            name="Auto A", mode='lines', line=dict(color='#00ff88', width=2)
                        ))
                        fig_track.add_trace(go.Scatter(
                            x=tele_b_sorted['lap_distance'], y=tele_b_sorted['speed_kmh'], 
                            name="Auto B", mode='lines', line=dict(color='#ff0055', width=2)
                        ))
                        
                        fig_track.update_layout(
                            xaxis_title="Streckenposition (Lap Distance) [m]", 
                            yaxis_title="Speed [km/h]",
                            template="plotly_dark",
                            height=500,
                            legend=dict(yanchor="top", y=0.99, xanchor="right", x=0.99)
                        )
                        st.plotly_chart(fig_track, width='stretch')
                    else:
                        st.info("Keine Track-Distance Daten vorhanden. Bist du schon eine fliegende Runde gefahren?")
                        
                st.markdown("---")
                st.subheader("📐 Sektor-Performance & Cornering KPIs")
                
                # Sektoren extrahieren wenn vorhanden
                sector_data = []
                sectors_a = tele_a[tele_a['lap_distance'] > 0]['sector'].unique() if 'sector' in tele_a else []
                
                def get_sector_kpi(df, s_id):
                    s_df = df[df['sector'] == s_id].copy()
                    if s_df.empty: return None, None, None, None
                    
                    # Finde Kurven in diesem Sektor (lat_g > 0.5)
                    s_df['is_corner'] = s_df['lat_g'].abs() > 0.5
                    
                    v_min_avg = s_df[s_df['is_corner']]['speed_kmh'].mean()
                    max_lat_g = s_df['lat_g'].abs().max()
                    max_brake_g = s_df['lon_g'].min() # negative G for braking
                    duration = s_df['time_elapsed'].max() - s_df['time_elapsed'].min()
                    return duration, v_min_avg, max_lat_g, max_brake_g
                
                for s in sorted(sectors_a):
                    if s < 0: continue # ignore pit/invalid
                    
                    dur_a, v_a, lat_a, brk_a = get_sector_kpi(tele_a, s)
                    dur_b, v_b, lat_b, brk_b = get_sector_kpi(tele_b, s)
                    
                    if dur_a and dur_b:
                        sector_data.append({
                            "Sektor": f"Sektor {int(s)+1}",
                            "Auto A Zeit": f"{dur_a:.2f}s",
                            "Auto B Zeit": f"{dur_b:.2f}s",
                            "Delta (A-B)": f"{dur_a-dur_b:+.2f}s",
                            "V-Min (Avg) A": f"{v_a:.1f} km/h" if pd.notna(v_a) else "-",
                            "V-Min (Avg) B": f"{v_b:.1f} km/h" if pd.notna(v_b) else "-",
                            "Max Quer-G A": f"{lat_a:.2f}G" if pd.notna(lat_a) else "-",
                            "Max Quer-G B": f"{lat_b:.2f}G" if pd.notna(lat_b) else "-",
                            "Max Brems-G A": f"{abs(brk_a):.2f}G" if pd.notna(brk_a) else "-",
                            "Max Brems-G B": f"{abs(brk_b):.2f}G" if pd.notna(brk_b) else "-"
                        })
                
                if sector_data:
                    st.dataframe(pd.DataFrame(sector_data), width='stretch')
                else:
                    st.info("Keine vollständigen Sektor-Zeiten gefunden. Bitte fahre ganze Runden für die Sektor-Analyse.")

with tab_scoring:
    st.header("Overall Performance Scoring")
    st.markdown("Vergleiche zwei Fahrzeuge head-to-head und generiere einen Track-spezifischen Overall Performance Index (OPI).")
    
    drag_runs = runs_df[runs_df['run_type'] == 'DRAG']
    handling_runs = runs_df[runs_df['run_type'] == 'HANDLING']
    
    if drag_runs.empty or handling_runs.empty:
        st.warning("Du brauchst sowohl Drag- als auch Handling-Daten in der Datenbank für einen kompletten Scoring-Vergleich.")
    else:
        drag_options = drag_runs['id'].astype(str) + " - [" + drag_runs['run_type'] + "] " + drag_runs['vehicle_name'] + " (" + drag_runs['timestamp'] + ")"
        handling_options = handling_runs['id'].astype(str) + " - [" + handling_runs['run_type'] + "] " + handling_runs['vehicle_name'] + " (" + handling_runs['timestamp'] + ")"
        
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Fahrzeug A (Referenz)")
            a_drag_str = st.selectbox("Wähle Drag-Daten (Power & Vmax)", drag_options, key="opi_a_drag")
            a_handling_str = st.selectbox("Wähle Handling-Daten (Grip & Brake)", handling_options, key="opi_a_handling")
            
        with col2:
            st.subheader("Fahrzeug B (Vergleich)")
            b_drag_str = st.selectbox("Wähle Drag-Daten (Power & Vmax)", drag_options, key="opi_b_drag")
            b_handling_str = st.selectbox("Wähle Handling-Daten (Grip & Brake)", handling_options, key="opi_b_handling")
        
        track_type = st.radio("Streckencharakteristik (Gewichtung):", 
                              ["High Speed (z.B. Le Mans - Power & Aero)", 
                               "Technical (z.B. Imola - Grip & Accel)", 
                               "Endurance / Race Pace (Fokus auf Konsistenz)",
                               "Balanced (Standard)"], horizontal=True)
                               
        if st.button("🏆 Performance Score berechnen", type="primary", width='stretch'):
            
            run_a_drag_id = int(a_drag_str.split(" - ")[0])
            run_a_hand_id = int(a_handling_str.split(" - ")[0])
            run_b_drag_id = int(b_drag_str.split(" - ")[0])
            run_b_hand_id = int(b_handling_str.split(" - ")[0])
            
            # Display names for the charts
            car_a_name_drag = drag_runs[drag_runs['id'] == run_a_drag_id]['vehicle_name'].iloc[0]
            car_a_name_hand = handling_runs[handling_runs['id'] == run_a_hand_id]['vehicle_name'].iloc[0]
            car_a_name = f"{car_a_name_drag} / {car_a_name_hand}" if car_a_name_drag != car_a_name_hand else car_a_name_drag
            
            car_b_name_drag = drag_runs[drag_runs['id'] == run_b_drag_id]['vehicle_name'].iloc[0]
            car_b_name_hand = handling_runs[handling_runs['id'] == run_b_hand_id]['vehicle_name'].iloc[0]
            car_b_name = f"{car_b_name_drag} / {car_b_name_hand}" if car_b_name_drag != car_b_name_hand else car_b_name_drag
            
            # Helper funcs
            def get_drag_metrics_for_run(rid):
                t = load_telemetry(rid)
                t, _, _ = analyze_run_quality(t)
                if t.empty: return None, None, None
                t_100 = t[t['speed_kmh'] >= 100]
                t_200 = t[t['speed_kmh'] >= 200]
                best_100 = t_100.iloc[0]['time_elapsed'] - t.iloc[0]['time_elapsed'] if not t_100.empty else None
                best_200 = t_200.iloc[0]['time_elapsed'] - t.iloc[0]['time_elapsed'] if not t_200.empty else None
                return best_100, best_200, t['speed_kmh'].max() if not t.empty else None
                
            def get_handling_metrics_for_run(rid):
                t = load_telemetry(rid)
                t, csi, crash = analyze_run_quality(t)
                if t.empty or 'lat_g' not in t.columns: return None, None, 50.0, False
                lat_col = 'lat_g_smooth' if 'lat_g_smooth' in t.columns else 'lat_g'
                lon_col = 'lon_g_smooth' if 'lon_g_smooth' in t.columns else 'lon_g'
                lat = t[lat_col].abs().max()
                brake = t[lon_col].min()
                return lat if lat > 0 else None, abs(brake) if brake < 0 else None, csi, crash
                
            with st.spinner("Scanne Datenbank nach globalen Bestwerten (für das 100er Score-Rating)..."):
                a_100, a_200, a_vmax = get_drag_metrics_for_run(run_a_drag_id)
                b_100, b_200, b_vmax = get_drag_metrics_for_run(run_b_drag_id)
                
                a_lat, a_brk, a_csi, a_crash = get_handling_metrics_for_run(run_a_hand_id)
                b_lat, b_brk, b_csi, b_crash = get_handling_metrics_for_run(run_b_hand_id)
                
                # Show Crash Warnings
                if a_crash:
                    st.error(f"⚠️ **Crash/Impact detected** im Handling-Run von Fahrzeug A ({car_a_name})! (Extreme G-Kräfte > 4.0G). Peaks wurden gecleant, aber die Daten könnten verfälscht sein.")
                if b_crash:
                    st.error(f"⚠️ **Crash/Impact detected** im Handling-Run von Fahrzeug B ({car_b_name})! (Extreme G-Kräfte > 4.0G). Peaks wurden gecleant, aber die Daten könnten verfälscht sein.")
                
                # Globale Bestwerte (alle Autos in der Datenbank)
                global_best_100, global_best_200 = 999.0, 999.0
                global_best_vmax, global_best_lat, global_best_brk = 0.0, 0.0, 0.0
                
                for rid in drag_runs['id'].values:
                    c_100, c_200, c_vmax = get_drag_metrics_for_run(rid)
                    if c_100 and c_100 < global_best_100: global_best_100 = c_100
                    if c_200 and c_200 < global_best_200: global_best_200 = c_200
                    if c_vmax and c_vmax > global_best_vmax: global_best_vmax = c_vmax
                    
                for rid in handling_runs['id'].values:
                    c_lat, c_brk, _, _ = get_handling_metrics_for_run(rid)
                    if c_lat and c_lat > global_best_lat: global_best_lat = c_lat
                    if c_brk and c_brk > global_best_brk: global_best_brk = c_brk
                    
            missing_data = []
            if not a_100 or not b_100: missing_data.append("Drag / Beschleunigung")
            if not a_lat or not b_lat: missing_data.append("Handling / Grip")
            
            if missing_data:
                st.warning(f"Achtung: Unvollständiger Vergleich! Es fehlen Logs vom Typ: {', '.join(missing_data)} für eines der Autos. Diese Kategorien werden im Score mit 0 gewertet.")
            if True:
                # Calculate Base 100 Scores Global (Safe against None values)
                def calc_score_lower_better(val, comp): 
                    if val is None or comp is None or val <= 0: return 0
                    return (comp / val) * 100
                    
                def calc_score_higher_better(val, comp): 
                    if val is None or comp is None or comp <= 0: return 0
                    return (val / comp) * 100
                
                scores_a = {
                    "Accel Low (0-100)": calc_score_lower_better(a_100, global_best_100),
                    "Accel High (0-200)": calc_score_lower_better(a_200, global_best_200),
                    "Top Speed": calc_score_higher_better(a_vmax, global_best_vmax),
                    "Kurvengrip (Lat G)": calc_score_higher_better(a_lat, global_best_lat),
                    "Bremskraft": calc_score_higher_better(a_brk, global_best_brk),
                    "Konsistenz / Stabilität": a_csi
                }
                
                scores_b = {
                    "Accel Low (0-100)": calc_score_lower_better(b_100, global_best_100),
                    "Accel High (0-200)": calc_score_lower_better(b_200, global_best_200),
                    "Top Speed": calc_score_higher_better(b_vmax, global_best_vmax),
                    "Kurvengrip (Lat G)": calc_score_higher_better(b_lat, global_best_lat),
                    "Bremskraft": calc_score_higher_better(b_brk, global_best_brk),
                    "Konsistenz / Stabilität": b_csi
                }
                
                # Weightings
                if "High Speed" in track_type:
                    w = [0.10, 0.20, 0.40, 0.10, 0.10, 0.10]
                elif "Technical" in track_type:
                    w = [0.20, 0.15, 0.10, 0.30, 0.15, 0.10]
                elif "Endurance" in track_type:
                    w = [0.05, 0.10, 0.15, 0.10, 0.10, 0.50]
                else:
                    w = [0.15, 0.15, 0.15, 0.20, 0.15, 0.20]
                    
                keys = list(scores_a.keys())
                
                opi_a = sum(scores_a[k] * w[i] for i, k in enumerate(keys))
                opi_b = sum(scores_b[k] * w[i] for i, k in enumerate(keys))
                
                # Plot
                c1, c2, c3 = st.columns([1, 2, 1])
                c1.metric(f"Overall Score: {car_a_name}", f"{opi_a:.1f} / 100")
                c3.metric(f"Overall Score: {car_b_name}", f"{opi_b:.1f} / 100", delta=f"{opi_b - opi_a:+.1f} vs A", delta_color="normal" if opi_b > opi_a else "inverse")
                
                with c2:
                    fig_radar = go.Figure()
                    
                    fig_radar.add_trace(go.Scatterpolar(
                        r=[scores_a[k] for k in keys] + [scores_a[keys[0]]],
                        theta=keys + [keys[0]],
                        fill='toself',
                        name=car_a_name,
                        line_color='#00ff88',
                        fillcolor='rgba(0, 255, 136, 0.4)',
                        opacity=0.8
                    ))
                    
                    fig_radar.add_trace(go.Scatterpolar(
                        r=[scores_b[k] for k in keys] + [scores_b[keys[0]]],
                        theta=keys + [keys[0]],
                        fill='toself',
                        name=car_b_name,
                        line_color='#ff0055',
                        fillcolor='rgba(255, 0, 85, 0.4)',
                        opacity=0.8
                    ))
                    
                    fig_radar.update_layout(
                        polar=dict(radialaxis=dict(visible=True, range=[min(min(scores_a.values()), min(scores_b.values())) - 2, 100])),
                        showlegend=True,
                        template="plotly_dark",
                        height=500
                    )
                    
                    st.plotly_chart(fig_radar, width='stretch')
                    
                st.markdown("---")
                st.subheader("Raw Head-to-Head Data")
                
                def render_row(label, val_a, val_b, format_str, lower_is_better):
                    safe_a = val_a if val_a is not None else (999.0 if lower_is_better else 0.0)
                    safe_b = val_b if val_b is not None else (999.0 if lower_is_better else 0.0)
                    
                    best = min(safe_a, safe_b) if lower_is_better else max(safe_a, safe_b)
                    
                    str_a = format_str.format(val_a) if val_a is not None else "N/A"
                    str_b = format_str.format(val_b) if val_b is not None else "N/A"
                    
                    color_a = "color: #00ff88; font-weight: bold;" if safe_a == best and val_a is not None else "color: #ffffff;"
                    color_b = "color: #00ff88; font-weight: bold;" if safe_b == best and val_b is not None else "color: #ffffff;"
                    
                    return f'<tr style="border-bottom: 1px solid #333; background-color: rgba(255,255,255,0.02);"><td style="padding: 12px 16px;">{label}</td><td style="padding: 12px 16px; {color_a}">{str_a}</td><td style="padding: 12px 16px; {color_b}">{str_b}</td></tr>'
                
                html = (
                    f'<table style="width: 100%; text-align: left; border-collapse: collapse; margin-top: 10px; font-family: sans-serif;">'
                    f'<thead><tr style="border-bottom: 2px solid #555; background-color: rgba(255,255,255,0.05);"><th style="padding: 12px 16px; color: #aaa;">Metrik</th>'
                    f'<th style="padding: 12px 16px; color: #00ff88;">{car_a_name}</th><th style="padding: 12px 16px; color: #ff0055;">{car_b_name}</th></tr></thead><tbody>'
                    f'{render_row("0-100 km/h", a_100, b_100, "{:.2f} s", True)}'
                    f'{render_row("0-200 km/h", a_200, b_200, "{:.2f} s", True)}'
                    f'{render_row("Top Speed", a_vmax, b_vmax, "{:.1f} km/h", False)}'
                    f'{render_row("Max Lateral G (Clean)", a_lat, b_lat, "{:.2f} G", False)}'
                    f'{render_row("Max Brake G (Clean)", a_brk, b_brk, "{:.2f} G", False)}'
                    f'{render_row("Stability & Confidence (CSI)", a_csi, b_csi, "{:.1f} Pkt", False)}'
                    f'</tbody></table>'
                )
                st.markdown(html, unsafe_allow_html=True)
