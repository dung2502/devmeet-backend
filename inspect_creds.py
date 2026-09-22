import sqlite3
import json

conn = sqlite3.connect(r'C:\Users\admin\.n8n\database.sqlite')
cursor = conn.cursor()
cursor.execute("SELECT nodes FROM workflow_entity WHERE id = 'bAc4fWUGaGUpoOcK'")
nodes = json.loads(cursor.fetchone()[0])
for n in nodes:
    if 'credentials' in n:
        print(f"Node '{n.get('name')}' uses credentials:", n['credentials'])
