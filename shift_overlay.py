import sys
import os
import json
import time
import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox

sys.path.append(os.path.join(os.path.dirname(__file__), 'pyRfactor2SharedMemory'))
from sharedMemoryAPI import SimInfoAPI

DB_PATH = "lmu_telemetry.db"

class ShiftOverlay(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LMU Shift Overlay Setup")
        self.geometry("500x300")
        self.configure(bg="#2E2E2E")
        self.attributes("-topmost", True)

        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        self.profiles = {}
        self.selected_profile_id = tk.IntVar()
        self.shift_data = {}  # Format: {gear_num: target_rpm}
        
        self.delay_ms = tk.IntVar(value=150)
        self.is_overlay_active = False

        self._setup_ui()
        self._load_profiles()

    def _setup_ui(self):
        self.main_frame = tk.Frame(self, bg="#2E2E2E")
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        tk.Label(self.main_frame, text="LMU Shift Overlay", font=("Segoe UI", 16, "bold"), bg="#2E2E2E", fg="white").pack(pady=(0, 20))

        tk.Label(self.main_frame, text="Fahrzeug / Benchmark:", bg="#2E2E2E", fg="white").pack(anchor=tk.W)
        
        profile_frame = tk.Frame(self.main_frame, bg="#2E2E2E")
        profile_frame.pack(fill=tk.X, pady=5)
        
        self.cb_profiles = ttk.Combobox(profile_frame, state="readonly", width=45)
        self.cb_profiles.pack(side=tk.LEFT, padx=(0, 10))
        
        ttk.Button(profile_frame, text="🔄 Refresh", command=self._load_profiles, width=10).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(profile_frame, text="❌ Löschen", command=self.delete_profile, width=10).pack(side=tk.LEFT)
        
        tk.Label(self.main_frame, text="Schalt-Vorwarnzeit / Delay (ms):", bg="#2E2E2E", fg="white").pack(anchor=tk.W, pady=(10, 0))
        tk.Label(self.main_frame, text="(Reaktionszeit-Ausgleich, positiver Wert = früher schalten)", bg="#2E2E2E", fg="gray", font=("Segoe UI", 8)).pack(anchor=tk.W)
        delay_entry = ttk.Entry(self.main_frame, textvariable=self.delay_ms, width=10)
        delay_entry.pack(anchor=tk.W, pady=5)

        ttk.Button(self.main_frame, text="Overlay Starten", command=self.start_overlay).pack(pady=20)

        # Overlay Mode UI (Hidden initially)
        self.overlay_frame = tk.Frame(self, bg="black")
        self.canvas = tk.Canvas(self.overlay_frame, width=150, height=150, bg="black", highlightthickness=0)
        self.canvas.pack()
        self.lamp = self.canvas.create_oval(10, 10, 140, 140, fill="#333333", outline="gray", width=2)
        
        self.info_text = self.canvas.create_text(75, 75, text="Wait...", fill="white", font=("Segoe UI", 12, "bold"))
        
        self.overlay_frame.bind("<Button-1>", self._start_move)
        self.overlay_frame.bind("<B1-Motion>", self._do_move)
        self.overlay_frame.bind("<Double-Button-1>", self.stop_overlay)
        self.canvas.bind("<Button-1>", self._start_move)
        self.canvas.bind("<B1-Motion>", self._do_move)
        self.canvas.bind("<Double-Button-1>", self.stop_overlay)

    def _start_move(self, event):
        self._x = event.x
        self._y = event.y

    def _do_move(self, event):
        x = self.winfo_x() + event.x - self._x
        y = self.winfo_y() + event.y - self._y
        self.geometry(f"+{x}+{y}")

    def _load_profiles(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='saved_profiles'")
            if not cursor.fetchone():
                self.cb_profiles["values"] = ["Keine Profile gefunden. Zuerst via Streamlit berechnen!"]
                return
                
            cursor.execute("SELECT run_id, vehicle_name, shift_points_json FROM saved_profiles")
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                self.cb_profiles["values"] = ["Keine Profile in der Datenbank!"]
                return

            vals = []
            for r in rows:
                name = f"Run {r[0]}: {r[1]}"
                vals.append(name)
                self.profiles[name] = r[2] # json data
            
            self.cb_profiles["values"] = vals
            self.cb_profiles.current(0)
        except Exception as e:
            self.cb_profiles["values"] = [f"Fehler: {e}"]

    def delete_profile(self):
        sel = self.cb_profiles.get()
        if not sel or sel not in self.profiles:
            messagebox.showinfo("Löschen", "Bitte wähle zuerst ein gültiges Profil aus.")
            return
            
        try:
            # Beispiel sel: "Run 12: LMU Porsche 963" -> splitten nach Run ID
            run_id = int(sel.split(":")[0].replace("Run ", ""))
            
            if messagebox.askyesno("Profil löschen", f"Möchtest du das Profil\n\n'{sel}'\n\nwirklich aus dem Overlay entfernen?"):
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                # Löscht nur das Profil, die rohen Telemetriedaten in anderen Tabellen bleiben erhalten
                cursor.execute("DELETE FROM saved_profiles WHERE run_id = ?", (run_id,))
                conn.commit()
                conn.close()
                
                self._load_profiles()
                messagebox.showinfo("Erfolg", "Profil wurde erfolgreich aus dem Overlay entfernt.")
        except Exception as e:
            messagebox.showerror("Fehler", f"Fehler beim Löschen: {e}")

    def start_overlay(self):
        sel = self.cb_profiles.get()
        if sel not in self.profiles:
            return
            
        sp_json = self.profiles[sel]
        try:
            sp_data = json.loads(sp_json)
            # dictionary von gang -> ziel RPM wandeln
            self.shift_data = {item['from_gear']: item['shift_rpm'] for item in sp_data}
        except:
            tk.messagebox.showerror("Fehler", "Konnte Profil-Daten nicht parsen.")
            return

        # Try init SimInfoAPI
        try:
            self.info = SimInfoAPI()
        except:
            tk.messagebox.showerror("Fehler", "Konnte LMU Shared Memory nicht laden.")
            return

        self.main_frame.pack_forget()
        self.geometry("150x150")
        
        # Transparent machen, falls von OS unterstützt
        self.wm_attributes("-transparentcolor", "black")
        self.overrideredirect(True) # Keine Fenster-Rahmen
        
        self.overlay_frame.pack(fill=tk.BOTH, expand=True)
        self.is_overlay_active = True
        
        self.prev_rpm = 0
        self.prev_time = time.time()

        self._update_loop()

    def stop_overlay(self, event=None):
        self.is_overlay_active = False
        self.overrideredirect(False)
        self.geometry("500x300")
        self.wm_attributes("-transparentcolor", "") # disabled
        self.overlay_frame.pack_forget()
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

    def _update_loop(self):
        if not self.is_overlay_active:
            return

        try:
            if self.info.isRF2running() and self.info.isSharedMemoryAvailable() and self.info.isOnTrack():
                telemetry = self.info.playersVehicleTelemetry()
                gear = telemetry.mGear
                current_rpm = telemetry.mEngineRPM
                
                curr_time = time.time()
                dt = curr_time - self.prev_time

                # RPM Velocity calc
                if dt > 0.01:
                    rpm_vel = (current_rpm - self.prev_rpm) / dt
                else:
                    rpm_vel = 0
                
                self.prev_time = curr_time
                self.prev_rpm = current_rpm
                
                target_rpm = self.shift_data.get(gear)
                
                if target_rpm:
                    # Delay vorwarnung (ms -> sekunden)
                    delay_sec = self.delay_ms.get() / 1000.0
                    projected_rpm = current_rpm + (rpm_vel * delay_sec)
                    
                    if current_rpm < 1000:
                         # Stand / Auto aus
                         self.canvas.itemconfig(self.lamp, fill="#333333")
                         self.canvas.itemconfig(self.info_text, text=f"Gear: {gear}")
                    elif current_rpm >= target_rpm or projected_rpm >= target_rpm:
                        # Schalten!
                        self.canvas.itemconfig(self.lamp, fill="#00FF00")
                        self.canvas.itemconfig(self.info_text, text="SHIFT!")
                    elif projected_rpm >= target_rpm - 300: # Nahe dran (Gelb)
                        self.canvas.itemconfig(self.lamp, fill="#FFDD00")
                        self.canvas.itemconfig(self.info_text, text=f"{current_rpm:.0f}")
                    else:
                        self.canvas.itemconfig(self.lamp, fill="#333333")
                        self.canvas.itemconfig(self.info_text, text=f"{current_rpm:.0f}")
                else:
                    # Im höchsten Gang (keine shift logic mehr) oder Rückwärtsgang
                    self.canvas.itemconfig(self.lamp, fill="#333333")
                    self.canvas.itemconfig(self.info_text, text=f"Gear: {gear}")

            else:
                self.canvas.itemconfig(self.lamp, fill="#333333")
                self.canvas.itemconfig(self.info_text, text="Waiting LMU")
                
        except Exception as e:
            self.canvas.itemconfig(self.info_text, text="Error")

        # loop mit ~30fps 
        self.after(33, self._update_loop)

if __name__ == "__main__":
    app = ShiftOverlay()
    app.mainloop()
