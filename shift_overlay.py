import sys
import os
import json
import time
import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pyRfactor2SharedMemory'))
from sharedMemoryAPI import SimInfoAPI

DB_PATH = "lmu_telemetry.db"
CONFIG_PATH = "overlay_settings.json"

class ShiftOverlay(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LMU Shift Overlay Setup")
        self.geometry("500x350")
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
        self.overlay_window = None

        self.is_locked = tk.BooleanVar(value=False)
        self.overlay_x = 100
        self.overlay_y = 100
        self._load_config()

        self._setup_ui()
        self._load_profiles()

    def _load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    config = json.load(f)
                    self.overlay_x = config.get("x", 100)
                    self.overlay_y = config.get("y", 100)
                    self.is_locked.set(config.get("locked", False))
            except:
                pass

    def save_config(self, *args):
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump({
                    "x": self.overlay_x,
                    "y": self.overlay_y,
                    "locked": self.is_locked.get()
                }, f)
        except:
            pass

    def _setup_ui(self):
        self.main_frame = tk.Frame(self, bg="#2E2E2E")
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        tk.Label(self.main_frame, text="LMU Shift Overlay", font=("Segoe UI", 16, "bold"), bg="#2E2E2E", fg="white").pack(pady=(0, 20))

        tk.Label(self.main_frame, text="Fahrzeug / Benchmark:", bg="#2E2E2E", fg="white").pack(anchor=tk.W)
        
        profile_frame = tk.Frame(self.main_frame, bg="#2E2E2E")
        profile_frame.pack(fill=tk.X, pady=5)
        
        self.cb_profiles = ttk.Combobox(profile_frame, state="readonly", width=45)
        self.cb_profiles.pack(side=tk.LEFT, padx=(0, 10))
        self.cb_profiles.bind("<<ComboboxSelected>>", self._on_profile_selected)
        
        ttk.Button(profile_frame, text="🔄 Refresh", command=self._load_profiles, width=10).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(profile_frame, text="❌ Löschen", command=self.delete_profile, width=10).pack(side=tk.LEFT)
        
        tk.Label(self.main_frame, text="Schalt-Vorwarnzeit / Delay (ms):", bg="#2E2E2E", fg="white").pack(anchor=tk.W, pady=(10, 0))
        tk.Label(self.main_frame, text="(Reaktionszeit-Ausgleich, positiver Wert = früher schalten)", bg="#2E2E2E", fg="gray", font=("Segoe UI", 8)).pack(anchor=tk.W)
        delay_entry = ttk.Entry(self.main_frame, textvariable=self.delay_ms, width=10)
        delay_entry.pack(anchor=tk.W, pady=5)

        lock_cb = tk.Checkbutton(self.main_frame, text="Position sperren (gegen versehentliches Verschieben)", 
                       variable=self.is_locked, command=self.save_config, 
                       bg="#2E2E2E", fg="white", selectcolor="#444", activebackground="#2E2E2E", activeforeground="white")
        lock_cb.pack(anchor=tk.W, pady=10)

        self.btn_toggle = ttk.Button(self.main_frame, text="Overlay Starten", command=self.toggle_overlay)
        self.btn_toggle.pack(pady=10)

    def _start_move(self, event):
        if self.is_locked.get():
            return
        self._x = event.x
        self._y = event.y

    def _do_move(self, event):
        if self.is_locked.get():
            return
        if not self.overlay_window:
            return
        x = self.overlay_window.winfo_x() + event.x - self._x
        y = self.overlay_window.winfo_y() + event.y - self._y
        self.overlay_window.geometry(f"+{x}+{y}")
        self.overlay_x = x
        self.overlay_y = y

    def _on_profile_selected(self, event=None):
        sel = self.cb_profiles.get()
        if sel in self.profiles:
            sp_json = self.profiles[sel]
            try:
                sp_data = json.loads(sp_json)
                self.shift_data = {item['from_gear']: item['shift_rpm'] for item in sp_data}
            except Exception as e:
                print(f"Error parsing profile data: {e}")

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
            self._on_profile_selected()
        except Exception as e:
            self.cb_profiles["values"] = [f"Fehler: {e}"]

    def delete_profile(self):
        sel = self.cb_profiles.get()
        if not sel or sel not in self.profiles:
            messagebox.showinfo("Löschen", "Bitte wähle zuerst ein gültiges Profil aus.")
            return
            
        try:
            run_id = int(sel.split(":")[0].replace("Run ", ""))
            
            if messagebox.askyesno("Profil löschen", f"Möchtest du das Profil\n\n'{sel}'\n\nwirklich aus dem Overlay entfernen?"):
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM saved_profiles WHERE run_id = ?", (run_id,))
                conn.commit()
                conn.close()
                
                self._load_profiles()
                messagebox.showinfo("Erfolg", "Profil wurde erfolgreich aus dem Overlay entfernt.")
        except Exception as e:
            messagebox.showerror("Fehler", f"Fehler beim Löschen: {e}")

    def toggle_overlay(self):
        if self.is_overlay_active:
            self.stop_overlay()
        else:
            self.start_overlay()

    def start_overlay(self):
        sel = self.cb_profiles.get()
        if sel not in self.profiles:
            return
            
        self._on_profile_selected()

        try:
            if not hasattr(self, 'info'):
                self.info = SimInfoAPI()
        except:
            tk.messagebox.showerror("Fehler", "Konnte LMU Shared Memory nicht laden.")
            return

        if self.overlay_window is not None and self.overlay_window.winfo_exists():
            return

        self.overlay_window = tk.Toplevel(self)
        self.overlay_window.geometry(f"150x150+{self.overlay_x}+{self.overlay_y}")
        self.overlay_window.overrideredirect(True)
        self.overlay_window.attributes("-topmost", True)
        self.overlay_window.wm_attributes("-transparentcolor", "black")

        self.overlay_frame = tk.Frame(self.overlay_window, bg="black")
        self.overlay_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(self.overlay_frame, width=150, height=150, bg="black", highlightthickness=0)
        self.canvas.pack()
        self.lamp = self.canvas.create_oval(10, 10, 140, 140, fill="#333333", outline="gray", width=2)
        
        self.info_text = self.canvas.create_text(75, 75, text="Wait...", fill="white", font=("Segoe UI", 12, "bold"))
        
        self.overlay_frame.bind("<Button-1>", self._start_move)
        self.overlay_frame.bind("<B1-Motion>", self._do_move)
        self.overlay_frame.bind("<ButtonRelease-1>", self.save_config)
        self.overlay_frame.bind("<Double-Button-1>", self.stop_overlay)
        
        self.canvas.bind("<Button-1>", self._start_move)
        self.canvas.bind("<B1-Motion>", self._do_move)
        self.canvas.bind("<ButtonRelease-1>", self.save_config)
        self.canvas.bind("<Double-Button-1>", self.stop_overlay)

        self.is_overlay_active = True
        self.btn_toggle.config(text="Overlay Stoppen")
        
        self.prev_rpm = 0
        self.prev_time = time.time()

        self._update_loop()

    def stop_overlay(self, event=None):
        self.is_overlay_active = False
        if getattr(self, "overlay_window", None) and self.overlay_window.winfo_exists():
            self.overlay_window.destroy()
            self.overlay_window = None
        self.btn_toggle.config(text="Overlay Starten")

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

                if dt > 0.01:
                    rpm_vel = (current_rpm - self.prev_rpm) / dt
                else:
                    rpm_vel = 0
                
                self.prev_time = curr_time
                self.prev_rpm = current_rpm
                
                target_rpm = self.shift_data.get(str(gear)) or self.shift_data.get(gear)
                
                if target_rpm:
                    delay_sec = self.delay_ms.get() / 1000.0
                    projected_rpm = current_rpm + (rpm_vel * delay_sec)
                    
                    if current_rpm < 1000:
                         self.canvas.itemconfig(self.lamp, fill="#333333")
                         self.canvas.itemconfig(self.info_text, text=f"Gear: {gear}")
                    elif current_rpm >= target_rpm or projected_rpm >= target_rpm:
                        self.canvas.itemconfig(self.lamp, fill="#00FF00")
                        self.canvas.itemconfig(self.info_text, text="SHIFT!")
                    elif projected_rpm >= target_rpm - 300: 
                        self.canvas.itemconfig(self.lamp, fill="#FFDD00")
                        self.canvas.itemconfig(self.info_text, text=f"{current_rpm:.0f}")
                    else:
                        self.canvas.itemconfig(self.lamp, fill="#333333")
                        self.canvas.itemconfig(self.info_text, text=f"{current_rpm:.0f}")
                else:
                    self.canvas.itemconfig(self.lamp, fill="#333333")
                    self.canvas.itemconfig(self.info_text, text=f"Gear: {gear}")

            else:
                self.canvas.itemconfig(self.lamp, fill="#333333")
                self.canvas.itemconfig(self.info_text, text="Waiting LMU")
                
        except Exception as e:
            self.canvas.itemconfig(self.info_text, text="Error")

        self.after(33, self._update_loop)

if __name__ == "__main__":
    app = ShiftOverlay()
    app.mainloop()
