import sqlite3
import json

def unflatten(parsed):
    if not isinstance(parsed, list):
        return parsed
    keys = parsed[0]
    def resolve(idx):
        if not isinstance(idx, str) or not idx.isdigit():
            return idx
        val = parsed[int(idx)]
        if isinstance(val, dict):
            return {k: resolve(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [resolve(v) for v in val]
        return val
    return resolve("0")

conn = sqlite3.connect(r'C:\Users\admin\.n8n\database.sqlite')
cursor = conn.cursor()
cursor.execute("SELECT d.data FROM execution_data d WHERE d.executionId = (SELECT MAX(id) FROM execution_entity)")
raw = cursor.fetchone()[0]
d = json.loads(raw)

decoded = unflatten(d)
runData = decoded.get('resultData', {}).get('runData', {})
print("=== Executed Nodes in Order ===")
for node_name, runs in runData.items():
    print(f"\n--- Node: {node_name} (runs: {len(runs)}) ---")
    for r in runs:
        execTime = r.get('executionTime', 0)
        status = r.get('executionStatus', 'ok')
        print(f"  Status: {status}, Time: {execTime}ms")
        if 'error' in r:
            print("  Error:", r['error'])
        if 'data' in r and 'main' in r['data']:
            main_data = r['data']['main']
            for out_idx, out_items in enumerate(main_data):
                if out_items:
                    print(f"  Output pin {out_idx} count: {len(out_items)}")
                    print("  Sample item 0:", json.dumps(out_items[0].get('json', {}), indent=2)[:400])
