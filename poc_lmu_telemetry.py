import sys
import time
import os

# Fügen Sie das heruntergeladene Modul zum Pfad hinzu
sys.path.append(os.path.join(os.path.dirname(__file__), 'pyRfactor2SharedMemory'))

from sharedMemoryAPI import SimInfoAPI

def main():
    print("LMU / rFactor 2 Telemetry PoC")
    print("Suche nach LMU / rFactor 2 Prozess...")

    try:
        info = SimInfoAPI()
    except Exception as e:
        print(f"Fehler beim Initialisieren der Shared Memory API: {e}")
        return

    print("Verbinde mit Shared Memory...")

    try:
        while True:
            # Überprüfen, ob das Spiel läuft und Shared Memory verfügbar ist
            if info.isRF2running():
                if info.isSharedMemoryAvailable():
                    if info.isOnTrack():
                        telemetry = info.playersVehicleTelemetry()
                        
                        # Auslesen der relevanten Daten aus rF2VehicleTelemetry
                        rpm = telemetry.mEngineRPM
                        torque = telemetry.mEngineTorque
                        gear = telemetry.mGear
                        speed = telemetry.mLocalVel.z # Z-Achse ist meist die Längsgeschwindigkeit (in m/s)
                        speed_kmh = abs(speed) * 3.6

                        # Terminal-Ausgabe überschreiben für eine saubere "Live"-Ansicht
                        sys.stdout.write(
                            f"\r[Live] Gang: {gear} | RPM: {rpm:6.0f} | Torque: {torque:6.1f} Nm | Speed: {speed_kmh:5.1f} km/h "
                        )
                        sys.stdout.flush()
                    else:
                        sys.stdout.write("\rSpiel läuft, aber Spieler ist nicht auf der Strecke...       ")
                        sys.stdout.flush()
                else:
                    sys.stdout.write("\rShared Memory Plugin nicht gefunden oder nicht aktiv...       ")
                    sys.stdout.flush()
            else:
                sys.stdout.write("\rWarte auf den Start von LMU / rFactor 2...                    ")
                sys.stdout.flush()

            time.sleep(0.05) # 20 Hz Update-Rate reicht für die Konsolen-Ansicht

    except KeyboardInterrupt:
        print("\nBeendet durch Benutzer.")

if __name__ == "__main__":
    main()
