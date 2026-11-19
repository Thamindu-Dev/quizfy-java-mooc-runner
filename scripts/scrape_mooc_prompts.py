"""
Automated MOOC.fi Exercise Prompt Scraper for Quizfy.
Fetches all exercise descriptions, paragraphs, code snippets, and sample outputs
directly from the official University of Helsinki Java Programming MOOC course pages
(https://java-programming.mooc.fi).

Outputs:
1. exercises/prompts.json (full dictionary of prompts keyed by exercise ID)
2. Updates individual exercises/<id>.json files with a "prompt" property
3. Generates/updates src/data/java-prompts.ts for the Quizfy frontend
"""

import urllib.request
import json
import re
import os
import html
from pathlib import Path
from typing import Dict, Any, List

BASE_DIR = Path(__file__).resolve().parent.parent
EXERCISES_DIR = BASE_DIR / "exercises"

MOOC_BASE = "https://java-programming.mooc.fi"
PART_1_PAGES = [
    "/part-1/1-starting-programming",
    "/part-1/2-printing",
    "/part-1/3-reading",
    "/part-1/4-variables",
    "/part-1/5-calculating",
    "/part-1/6-conditional-statements",
    "/part-1/7-programming-in-our-society",
]

def clean_html_text(text: str) -> str:
    # Unescape HTML entities
    text = html.unescape(text)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Normalize whitespace while preserving linebreaks where appropriate
    return text.strip()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_tmcname_to_id(tmcname: str) -> str:
    # e.g. "part01-Part01_14.BooleanInput" -> "part01-14-booleaninput"
    match = re.match(r"(?:part\d+-)?Part(\d+)_(\d+)\.(.+)", tmcname, re.IGNORECASE)
    if match:
        part = int(match.group(1))
        order = int(match.group(2))
        raw_name = match.group(3)
        return f"part{part:02d}-{order:02d}-{slugify(raw_name)}"
    return slugify(tmcname)


def parse_sample_output(so_html: str) -> Dict[str, Any]:
    # Lines inside <sample-output> can have <strong>input</strong>
    # Remove outer tags
    inner = re.sub(r"^<sample-output[^>]*>|</sample-output>$", "", so_html.strip(), flags=re.IGNORECASE)
    inner = re.sub(r"^<p[^>]*>|</p>$", "", inner.strip(), flags=re.IGNORECASE)

    raw_lines = inner.split("\n")
    parsed_lines = []

    for line in raw_lines:
        line_clean = line.strip()
        if not line_clean:
            continue

        # Check if line contains <strong>...</strong> or <b>...</b>
        strong_match = re.search(r"<(?:strong|b)>([^<]+)</(?:strong|b)>", line_clean)
        if strong_match and line_clean.strip() == strong_match.group(0):
            # Entire line is user input
            parsed_lines.append({"text": clean_html_text(strong_match.group(1)), "isInput": True})
        elif strong_match:
            # Inline strong, e.g. "Prefix: <strong>input</strong>"
            before = line_clean[:strong_match.start()].strip()
            after = line_clean[strong_match.end():].strip()
            if before:
                parsed_lines.append(clean_html_text(before))
            parsed_lines.append({"text": clean_html_text(strong_match.group(1)), "isInput": True})
            if after:
                parsed_lines.append(clean_html_text(after))
        else:
            parsed_lines.append(clean_html_text(line_clean))

    return {
        "caption": "Sample output",
        "lines": parsed_lines
    }


def parse_exercise_block(block_html: str) -> Dict[str, Any]:
    tmc_m = re.search(r"tmcname=['\"]([^'\"]+)['\"]", block_html)
    name_m = re.search(r"name=['\"]([^'\"]+)['\"]", block_html)

    if not tmc_m:
        return None

    tmcname = tmc_m.group(1)
    title = name_m.group(1) if name_m else tmcname
    exercise_id = parse_tmcname_to_id(tmcname)

    # Remove all HTML comments (which often contain Finnish originals)
    clean_block = re.sub(r"<!--.*?-->", "", block_html, flags=re.DOTALL)

    # Extract sample outputs first
    sample_outputs = []
    so_matches = list(re.finditer(r"<sample-output[^>]*>(.*?)</sample-output>", clean_block, re.DOTALL))
    for so_m in so_matches:
        so = parse_sample_output(so_m.group(0))
        if so["lines"]:
            sample_outputs.append(so)

    # Extract code snippets (<pre><code>) if any
    code_snippets = []
    for c_m in re.finditer(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", clean_block, re.DOTALL):
        code_snippets.append(html.unescape(re.sub(r"<[^>]+>", "", c_m.group(1))).strip())

    # Remove <sample-output> and <pre> code blocks from text parsing
    text_content = re.sub(r"<sample-output[^>]*>.*?</sample-output>", "", clean_block, flags=re.DOTALL)
    text_content = re.sub(r"<pre[^>]*>.*?</pre>", "</p><p>", text_content, flags=re.DOTALL)

    # Extract paragraphs (<p>...</p>)
    paragraphs = []
    for p_m in re.finditer(r"<p[^>]*>(.*?)</p>", text_content, re.DOTALL):
        p_text = clean_html_text(p_m.group(1))
        if p_text and not p_text.lower().startswith("sample output"):
            paragraphs.append(p_text)

    prompt = {
        "points": 1,
        "paragraphs": paragraphs,
    }
    if code_snippets:
        prompt["codeSnippet"] = code_snippets[0]
    if sample_outputs:
        prompt["sampleOutputs"] = sample_outputs

    return {
        "id": exercise_id,
        "tmcname": tmcname,
        "title": title,
        "prompt": prompt
    }


def fetch_page_data(path: str) -> str:
    url = f"{MOOC_BASE}/page-data{path}/page-data.json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data["result"]["data"]["page"]["html"]


def scrape_all_part1() -> Dict[str, Any]:
    all_prompts = {}

    for page_path in PART_1_PAGES:
        print(f"[FETCH] Fetching {page_path}...")
        try:
            page_html = fetch_page_data(page_path)
        except Exception as e:
            print(f"[ERROR] Failed to fetch {page_path}: {e}")
            continue

        # Find all English <programming-exercise ...> blocks
        # English ones start with tmcname='part01-Part01_...'
        pattern = r"(<programming-exercise\s+[^>]*tmcname=['\"]part01-[^'\"]+['\"][^>]*>.*?</programming-exercise>)"
        exercises = re.findall(pattern, page_html, re.DOTALL)

        print(f"  -> Found {len(exercises)} English exercises")
        for ex_html in exercises:
            parsed = parse_exercise_block(ex_html)
            if parsed and parsed["id"]:
                all_prompts[parsed["id"]] = parsed
                print(f"     + {parsed['id']}: {parsed['title']} ({len(parsed['prompt']['paragraphs'])} paragraphs, {len(parsed['prompt'].get('sampleOutputs', []))} sample outputs)")

    return all_prompts


def main():
    print("=== Scraping MOOC.fi Part 1 Programming Exercises ===")
    prompts = scrape_all_part1()
    print(f"\nTotal scraped exercises: {len(prompts)}")

    # 1. Save prompts.json
    prompts_json_path = EXERCISES_DIR / "prompts.json"
    prompts_dict = {k: v["prompt"] for k, v in prompts.items()}
    with open(prompts_json_path, "w", encoding="utf-8") as f:
        json.dump(prompts_dict, f, indent=2, ensure_ascii=False)
    print(f"[SAVED] {prompts_json_path}")

    # 2. Update existing exercises/<id>.json files
    updated_count = 0
    for ex_id, data in prompts.items():
        ex_file = EXERCISES_DIR / f"{ex_id}.json"
        if ex_file.exists():
            try:
                with open(ex_file, "r", encoding="utf-8") as f:
                    curr = json.load(f)
                curr["prompt"] = data["prompt"]
                if data["prompt"]["paragraphs"]:
                    curr["description"] = data["prompt"]["paragraphs"][0]
                with open(ex_file, "w", encoding="utf-8") as f:
                    json.dump(curr, f, indent=2, ensure_ascii=False)
                updated_count += 1
            except Exception as e:
                print(f"[WARN] Failed to update {ex_file}: {e}")

    print(f"[UPDATED] {updated_count} individual exercise JSON files in {EXERCISES_DIR}")

    # 3. Generate local TypeScript export in Repo 2 (Quizfy frontend fetches exercises/<id>.json on demand)
    ts_code = [
        'import type { JavaExercisePrompt } from "@/lib/java-runner/types";',
        "",
        "/**",
        " * Automatically scraped Java Exercise Prompts from University of Helsinki MOOC.fi.",
        " * Generated by scripts/scrape_mooc_prompts.py",
        " */",
        "export const JAVA_EXERCISE_PROMPTS: Record<string, JavaExercisePrompt> = " + json.dumps(prompts_dict, indent=2, ensure_ascii=False) + ";\n",
    ]
    ts_content = "\n".join(ts_code)

    local_ts = EXERCISES_DIR / "java-prompts.ts"
    with open(local_ts, "w", encoding="utf-8") as f:
        f.write(ts_content)
    print(f"[SAVED LOCAL] Wrote {local_ts}")


if __name__ == "__main__":
    main()
