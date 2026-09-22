import sqlite3
import json

conn = sqlite3.connect(r'C:\Users\admin\.n8n\database.sqlite')
cursor = conn.cursor()
cursor.execute("SELECT nodes FROM workflow_entity WHERE id = 'bAc4fWUGaGUpoOcK'")
nodes = json.loads(cursor.fetchone()[0])
for n in nodes:
    jsCode = n.get('parameters', {}).get('jsCode', '')
    if '$env' in jsCode:
        print(f"Node '{n.get('name')}' uses $env in jsCode:")
        for line in jsCode.split('\n'):
            if '$env' in line:
                print('   ', line)
