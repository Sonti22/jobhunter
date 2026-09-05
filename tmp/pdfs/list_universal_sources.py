import json
from pathlib import Path
p=Path(__file__).parent
for prefix,name in [('B','backend'),('A','ai'),('R','ru')]:
    for i,r in enumerate(json.loads((p/f'universal_sources_{name}.json').read_text(encoding='utf-8'))):
        print(f'{prefix}{i+1:02d} | {r["kind"]} | {r["title"]} | {r["url"]}')
