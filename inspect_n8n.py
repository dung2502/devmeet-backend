import sqlite3
import json

conn = sqlite3.connect(r'C:\Users\admin\.n8n\database.sqlite')
cursor = conn.cursor()
cursor.execute("SELECT nodes FROM workflow_entity WHERE id = 'bAc4fWUGaGUpoOcK'")
nodes = json.loads(cursor.fetchone()[0])
for n in nodes:
    if n.get('name') == 'Authenticate & Validate Input':
        print('=== Authenticate & Validate Input JS Code ===')
        print(n.get('parameters', {}).get('jsCode', ''))
