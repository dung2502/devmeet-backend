import sqlite3
import json

conn = sqlite3.connect(r'C:\Users\admin\.n8n\database.sqlite')
cursor = conn.cursor()
cursor.execute("SELECT id, status, finished, startedAt, stoppedAt FROM execution_entity ORDER BY id DESC LIMIT 2")
rows = cursor.fetchall()
for r in rows:
    print(f"Execution {r[0]}: status={r[1]}, finished={r[2]}, startedAt={r[3]}, stoppedAt={r[4]}")

cursor.execute("SELECT d.data FROM execution_data d WHERE d.executionId = (SELECT MAX(id) FROM execution_entity)")
raw = cursor.fetchone()[0]
d = json.loads(raw)

print("Total elements in execution data:", len(d))
for idx, item in enumerate(d):
    if isinstance(item, str) and any(k in item for k in ['summary', 'decisions', 'action_items', 'error', 'Google', 'OpenAI']):
        print(f"  [{idx}] {item[:150]}")
