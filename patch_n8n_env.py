import sqlite3
import json

def update_js_code(code: str) -> str:
    # 1. Safe env secret check
    if "const expectedSecret = String($env.DEVMEET_WEBHOOK_SECRET" in code:
        code = code.replace(
            "const expectedSecret = String($env.DEVMEET_WEBHOOK_SECRET || '').trim();",
            """let expectedSecret = 'YOUR_SHARED_WEBHOOK_SECRET';
try {
  if (typeof $env !== 'undefined' && $env && $env.DEVMEET_WEBHOOK_SECRET) {
    expectedSecret = String($env.DEVMEET_WEBHOOK_SECRET).trim();
  }
} catch (e) {
  expectedSecret = 'YOUR_SHARED_WEBHOOK_SECRET';
}"""
        )
    # 2. Safe spreadsheetId check
    if "const spreadsheetId = $env.DEVMEET_SPREADSHEET_ID;" in code:
        code = code.replace(
            "const spreadsheetId = $env.DEVMEET_SPREADSHEET_ID;",
            """let spreadsheetId = '1M650KwhZoTSiesYkecuoTFF0aYc9OvUHRb2M8FPXvOA';
try {
  if (typeof $env !== 'undefined' && $env && $env.DEVMEET_SPREADSHEET_ID) {
    spreadsheetId = $env.DEVMEET_SPREADSHEET_ID;
  }
} catch (e) {
  spreadsheetId = '1M650KwhZoTSiesYkecuoTFF0aYc9OvUHRb2M8FPXvOA';
}"""
        )
    return code

# Update database.sqlite
conn = sqlite3.connect(r'C:\Users\admin\.n8n\database.sqlite')
cursor = conn.cursor()
cursor.execute("SELECT id, nodes FROM workflow_entity WHERE id = 'bAc4fWUGaGUpoOcK'")
wf_id, raw_nodes = cursor.fetchone()
nodes = json.loads(raw_nodes)

for n in nodes:
    if 'parameters' in n and 'jsCode' in n['parameters']:
        n['parameters']['jsCode'] = update_js_code(n['parameters']['jsCode'])

cursor.execute("UPDATE workflow_entity SET nodes = ? WHERE id = 'bAc4fWUGaGUpoOcK'", (json.dumps(nodes),))
conn.commit()
conn.close()
print("Updated n8n SQLite workflow bAc4fWUGaGUpoOcK successfully.")
