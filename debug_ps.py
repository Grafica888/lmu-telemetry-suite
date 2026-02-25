import psutil
print("Folgende LMU/rfactor2 ähnliche Prozesse laufen:")
for p in psutil.process_iter(['pid', 'name']):
    if 'lemans' in p.info['name'].lower() or 'rfactor' in p.info['name'].lower() or 'lmu' in p.info['name'].lower():
        print(p.info)
