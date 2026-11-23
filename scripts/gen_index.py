import json, os, glob, re

flat_root = 'exercises'
entries = []

for path in sorted(glob.glob(flat_root + '/part*.json')):
    fname = os.path.basename(path)
    if fname in ('index.json', 'prompts.json'):
        continue
    with open(path, encoding='utf-8') as f:
        ex = json.load(f)
    starter_count = len(ex.get('starterFiles', {}))
    data_count = len(ex.get('dataFiles', []))
    entries.append({
        'id': ex['id'],
        'part': ex['part'],
        'order': ex['order'],
        'title': ex['title'],
        'fileCount': max(starter_count, 1),
        'hasDataFiles': data_count > 0
    })

# sort by part then order
entries.sort(key=lambda e: (e['part'], e['order']))

with open(os.path.join(flat_root, 'index.json'), 'w', encoding='utf-8', newline='\n') as f:
    json.dump(entries, f, indent=2, ensure_ascii=False)
    f.write('\n')

print('index.json written with ' + str(len(entries)) + ' entries')
parts = {}
for e in entries:
    parts.setdefault(e['part'], 0)
    parts[e['part']] += 1
for p in sorted(parts):
    print('  Part ' + str(p) + ': ' + str(parts[p]) + ' exercises')
