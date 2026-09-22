import sqlite3
import json

conn = sqlite3.connect(r'C:\Users\admin\.n8n\database.sqlite')
cursor = conn.cursor()
cursor.execute("SELECT d.data FROM execution_data d WHERE d.executionId = (SELECT MAX(id) FROM execution_entity)")
raw = cursor.fetchone()[0]
d = json.loads(raw)

print("=== Flattened elements in execution 184 ===")
for i, item in enumerate(d):
    if isinstance(item, dict):
        keys = list(item.keys())
        if 'summary' in keys or 'action_items' in keys or 'decisions' in keys or 'status' in keys:
            print(f"Dict at [{i}]:", item)
    elif isinstance(item, str) and len(item) > 20:
        if 'summary' in item.lower() or 'decision' in item.lower() or 'action' in item.lower() or 'devmeet' in item.lower():
            print(f"Str at [{i}]:", item[:300])
