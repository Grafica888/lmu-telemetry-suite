# 🏎️ Le Mans Ultimate (LMU) Telemetry & Performance Suite

Diese Suite bietet ein komplettes Toolkit zur Analyse und Optimierung deiner Performance in **Le Mans Ultimate** (sowie rFactor 2). Sie besteht aus drei Hauptkomponenten:
1. **Data Logger:** Zeichnet im Hintergrund live Telemetriedaten auf (RPM, Drehmoment, Gänge, Speed, etc.).
2. **Dashboard (Analyzer):** Ein interaktives Web-Dashboard (Streamlit) zur Berechnung der perfekten Schaltpunkte pro Gang und zum Vergleichen von Beschleunigungs-Benchmarks (0-100, 0-200, Vmax) zwischen verschiedenen Setup-Varianten.
3. **Shift Overlay (NEU!):** Ein transparentes Desktop-Overlay, das dir im Spiel via Live-Telemetrie einen visuellen "Grünen Punkt" 🟢 anzeigt, sobald es Zeit ist hochzuschalten. Mit Reaktionszeit-Ausgleich (Delay-Funktion)!

---

## 🛠️ Installation & Setup (WICHTIG!)

Damit dieses Tool funktioniert, benötigt es Zugriff auf den "Shared Memory" (Arbeitsspeicher-Schnittstelle) des Spiels.

### 1. Benötigte Software installieren
Dieses Tool ist in Python geschrieben. Du benötigst:
- **[Python 3.9 bis 3.12](https://www.python.org/downloads/)** (Bei der Installation UNBEDINGT den Haken bei *"Add Python to PATH"* setzen!)

### 2. Le Mans Ultimate Plugin installieren (rF2 Shared Memory)
Die Suite nutzt die bewährte rFactor 2 Shared Memory Struktur, da LMU auf derselben Engine basiert.

1. Lade dir das **rFactor 2 Shared Memory Plugin** herunter (eine `.dll` Datei). Das aktuellste Plugin findest du typischerweise hier:
   [rFactor 2 Shared Memory Map Plugin (Github)](https://github.com/TheIronWolfModding/rF2SharedMemoryMapPlugin/releases)
2. Kopiere die Datei `rFactor2SharedMemoryMapPlugin64.dll` aus dem Download.
3. Füge die `.dll`-Datei in folgendes Verzeichnis deines Spiels ein:
   `\SteamLibrary\steamapps\common\Le Mans Ultimate\Plugins\`
4. **WICHTIG:** Gehe in den Ordner `\SteamLibrary\steamapps\common\Le Mans Ultimate\UserData\player` und öffne die Datei `CustomPluginVariables.JSON` mit einem Texteditor.
5. Suche nach dem Eintrag für `rFactor2SharedMemoryMapPlugin64.dll` und setze den Wert `" Enabled"` auf `1` (falls er auf 0 steht oder der Eintrag fehlt, den folgenden Code in die Datei hinzufügen):
   ```json
   "rFactor2SharedMemoryMapPlugin64.dll":{
    " Enabled":1
   }
   ```

### 3. Starten der App (One-Click)
Nachdem Python und das Plugin installiert sind, musst du das Tool nicht umständlich über die Konsole starten!

👉 **Starte einfach per Doppelklick die Datei: `Start_LMU_Suite.bat`**

Das Script prüft beim ersten Start alle Paket-Abhängigkeiten (`pip install`) automatisch. Anschließend öffnen sich folgende Dinge:
- **Ein minimiertes Konsolenfenster:** Der Data-Logger, der auf Spiel-Events lauscht. Let it run!
- **Das Dashboard im Browser:** Hier kannst du die Telemetriedaten auswerten.
- **Ein Einstellungs-Fenster für das Overlay:** Hier kannst du das Live-Shift-Overlay für dein Spiel aktivieren und verwalten.

---

## 🚦 Nutzung des Shift Overlays (Reaktionszeit überbrücken)

Damit das Overlay weiß, wann du schalten musst, musst du zuerst die perfekten Schaltpunkte berechnen lassen:

1. **Aufzeichnung:** Fahre in LMU auf die Strecke. Mach eine Benchmark-Vollgas-Fahrt (z.B. aus dem Stand voll durchbeschleunigen bis Vmax). Das Tool zeichnet automatisch auf, sobald du Vollgas gibst und bricht erst ab, wenn du vom Gas gehst.
2. **Dashboard > Shift Optimizer:** Öffne das Dashboard.
3. **Schaltpunkte Berechnen:** Wähle den soeben gemachten Run aus dem Dropdown. Gib deine LMU-Autodaten ("Gear Ratios", "Final Drive") ein und klicke auf "Berechnen". Das Dashboard speichert das Profil "Run X" fürs Overlay.
4. **Overlay Setup GUI:** Öffne das `LMU Shift Overlay Setup`.
5. **Wähle das Profil:** Das Dropdown zeigt nun "Run X: [Fahrzeugname]". Wähle es aus. (Das Overlay aktualisiert die Schaltpunkte jetzt live, wenn du das Profil hier wechselst, ohne das Overlay schließen zu müssen!)
6. **Schalt-Vorwarnzeit / Delay:** Da der Mensch Reaktionszeit hat, kannst du einen Delay eingeben (z.B. `150` ms). Das Overlay gibt dir das "SHIFT!"-Signal entsprechend früher, damit du physisch genau am perfekten Punkt abschaltest.
7. **Sperren & Position:** Klicke auf **"Overlay Starten/Stoppen"**. Es erscheint ein rahmenloses Pop-up-Modul (Der eigentliche "Schalt-Kreis"), den du im Spielverlauf (bei "Randlos Fenster" in LMU) dorthin ziehen kannst, wo er dir am besten nützt.
   - **Position merken**: Das Tool merkt sich automatisch, wo du das Overlay auf dem Bildschirm abgelegt hast!
   - **Sperre**: Setze im Haupt-Fenster den Haken "Position sperren", damit du das Overlay im Rennbetrieb nie wieder versehentlich mit der Maus verschieben kannst!

---
*Happy Racing & Shifting!* 🏎️💨
