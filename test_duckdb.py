import duckdb

filepath = r"G:\SteamLibrary\steamapps\common\Le Mans Ultimate\UserData\Telemetry\Circuit de la Sarthe_P_2026-02-23T07_53_30Z.duckdb"

try:
    conn = duckdb.connect(filepath, read_only=True)
    channels = conn.execute("SELECT * FROM channelsList").fetchall()
    print("Kanäle:")
    for c in channels:
        print(c)
        
except Exception as e:
    print(f"Fehler: {e}")
