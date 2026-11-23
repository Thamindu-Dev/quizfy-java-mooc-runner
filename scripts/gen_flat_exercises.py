import json, os, glob, re

by_course_root = 'exercises/by-course'
flat_root = 'exercises'
created = 0
skipped = 0

def derive_class_name(ex_id):
    name_part = '-'.join(ex_id.split('-')[2:])
    return name_part.replace('-', ' ').title().replace(' ', '')

def derive_part_order(ex_id):
    m = re.match(r'part(\d+)-(\d+)-', ex_id)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0

def make_starter_scanner(class_name):
    lines = [
        '',
        'import java.util.Scanner;',
        '',
        'public class ' + class_name + ' {',
        '',
        '    public static void main(String[] args) {',
        '        Scanner scanner = new Scanner(System.in);',
        '',
        '    }',
        '}',
        ''
    ]
    return '\n'.join(lines)

def make_starter_simple(class_name):
    lines = [
        '',
        'public class ' + class_name + ' {',
        '',
        '    public static void main(String[] args) {',
        '        // Write your program here',
        '    }',
        '}',
        ''
    ]
    return '\n'.join(lines)

for path in sorted(glob.glob(by_course_root + '/**/*.json', recursive=True)):
    fname = os.path.basename(path)
    if fname == 'questions.json':
        continue

    with open(path, encoding='utf-8') as f:
        bc = json.load(f)

    ex_id = bc.get('id') or fname.replace('.json', '')
    flat_path = os.path.join(flat_root, ex_id + '.json')

    if os.path.exists(flat_path):
        skipped += 1
        continue

    part, order = derive_part_order(ex_id)
    class_name = derive_class_name(ex_id)
    prompt = bc.get('prompt', {})
    paragraphs = prompt.get('paragraphs', [])
    desc = paragraphs[0] if paragraphs else ('Complete the Java programming exercise: ' + bc.get('title', class_name) + '.')

    # Parts 1-2 mostly use Scanner; parts 3+ are mixed but default to Scanner
    starter = make_starter_scanner(class_name)

    flat = {
        'id': ex_id,
        'part': part,
        'order': order,
        'title': bc.get('title', class_name),
        'description': desc,
        'dataFiles': [],
        'starterFiles': {
            'src/main/java/' + class_name + '.java': starter
        },
        'prompt': prompt
    }

    with open(flat_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(flat, f, indent=2, ensure_ascii=False)
        f.write('\n')
    created += 1
    print('Created: ' + flat_path)

print('Total created: ' + str(created) + ', already existed (skipped): ' + str(skipped))
