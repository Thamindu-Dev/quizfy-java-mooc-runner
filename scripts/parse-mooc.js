/**
 * Read-only parser for local NetBeans/TMC MOOC.fi Java exercises.
 * NEVER modifies or deletes anything in the source directory.
 *
 * Usage:
 *   node scripts/parse-mooc.js --source "C:\Users\User\Documents\NetBeansProjects"
 */
import fs from "node:fs/promises";
import path from "node:path";

const args = process.argv.slice(2);
const srcFlagIdx = args.indexOf("--source");
if (srcFlagIdx === -1 || !args[srcFlagIdx + 1]) {
  console.error('Usage: node scripts/parse-mooc.js --source "<path-to-NetBeansProjects>"');
  process.exit(1);
}

const SOURCE_ROOT = path.resolve(args[srcFlagIdx + 1]);
const OUT_EXERCISES = path.resolve("exercises");
const OUT_TESTS = path.resolve("tests");

const FOLDER_RE = /^(?:part\d+-)?Part(\d+)_(\d+)\.(.+)$/i;
const FIXTURE_EXTS = new Set([".txt", ".csv", ".dat", ".tsv"]);

function humanizeTitle(raw) {
  return raw
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim();
}

function slugify(str) {
  return str
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

async function listFilesRecursive(dir, baseDir = dir) {
  const out = [];
  let entries;
  try {
    entries = await fs.readdir(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const e of entries) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      out.push(...(await listFilesRecursive(full, baseDir)));
    } else {
      out.push({ full, rel: path.relative(baseDir, full).replace(/\\/g, "/") });
    }
  }
  return out;
}

function extractFqcn(javaSource, relPathFromJavaRoot) {
  const pkgMatch = javaSource.match(/^\s*package\s+([a-zA-Z0-9_.]+)\s*;/m);
  const className = path.basename(relPathFromJavaRoot, ".java");
  return pkgMatch ? `${pkgMatch[1]}.${className}` : className;
}

async function findExerciseFolders(rootDir) {
  const found = [];
  const entries = await fs.readdir(rootDir, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const full = path.join(rootDir, entry.name);
    if (FOLDER_RE.test(entry.name)) {
      found.push({ name: entry.name, full });
    } else {
      // Check one level deeper (e.g. NetBeansProjects/mooc-java-programming-i/part01-Part01_01.Sandbox)
      try {
        const subEntries = await fs.readdir(full, { withFileTypes: true });
        for (const sub of subEntries) {
          if (sub.isDirectory() && FOLDER_RE.test(sub.name)) {
            found.push({ name: sub.name, full: path.join(full, sub.name) });
          }
        }
      } catch {
        // Ignore unreadable dirs
      }
    }
  }
  return found;
}

async function main() {
  await fs.mkdir(OUT_EXERCISES, { recursive: true });
  await fs.mkdir(OUT_TESTS, { recursive: true });

  const exerciseFolders = await findExerciseFolders(SOURCE_ROOT);
  const index = [];
  let skippedJavaFx = 0;

  for (const folder of exerciseFolders) {
    const match = folder.name.match(FOLDER_RE);
    if (!match) continue;

    const part = Number(match[1]);
    const order = Number(match[2]);
    const rawName = match[3];
    const title = humanizeTitle(rawName);
    const id = `part${String(part).padStart(2, "0")}-${String(order).padStart(2, "0")}-${slugify(rawName)}`;

    const projectDir = folder.full;
    const pomPath = path.join(projectDir, "pom.xml");

    // Skip JavaFX GUI exercises (Parts 13-14)
    try {
      const pom = await fs.readFile(pomPath, "utf8");
      if (pom.includes("javafx")) {
        skippedJavaFx++;
        continue;
      }
    } catch {
      // No pom.xml
    }

    const mainJavaRoot = path.join(projectDir, "src", "main", "java");
    const testJavaRoot = path.join(projectDir, "src", "test", "java");

    const mainFilesList = (await listFilesRecursive(mainJavaRoot)).filter((f) => f.rel.endsWith(".java"));
    const testFilesList = (await listFilesRecursive(testJavaRoot)).filter((f) => f.rel.endsWith(".java"));

    if (mainFilesList.length === 0 || testFilesList.length === 0) continue;

    const starterFiles = {};
    for (const f of mainFilesList) {
      starterFiles[`src/main/java/${f.rel}`] = await fs.readFile(f.full, "utf8");
    }

    const testDestBase = path.join(OUT_TESTS, id);
    const testClasses = [];

    for (const f of testFilesList) {
      const code = await fs.readFile(f.full, "utf8");
      const dest = path.join(testDestBase, "src", "test", "java", f.rel);
      await fs.mkdir(path.dirname(dest), { recursive: true });
      await fs.writeFile(dest, code, "utf8");

      if (code.includes("@Test")) {
        testClasses.push(extractFqcn(code, f.rel));
      }
    }

    if (testClasses.length === 0) continue;

    const rootEntries = await fs.readdir(projectDir, { withFileTypes: true });
    const dataFiles = [];
    for (const re of rootEntries) {
      if (!re.isFile()) continue;
      const ext = path.extname(re.name).toLowerCase();
      if (!FIXTURE_EXTS.has(ext)) continue;

      const resDest = path.join(testDestBase, "resources", re.name);
      await fs.mkdir(path.dirname(resDest), { recursive: true });
      await fs.writeFile(resDest, await fs.readFile(path.join(projectDir, re.name)));
      dataFiles.push(re.name);
    }

    await fs.writeFile(
      path.join(testDestBase, "meta.json"),
      JSON.stringify({ id, part, order, title, testClasses, dataFiles }, null, 2),
      "utf8"
    );

    await fs.writeFile(
      path.join(OUT_EXERCISES, `${id}.json`),
      JSON.stringify(
        {
          id,
          part,
          order,
          title,
          description: `Complete the Java programming exercise: ${title}.`,
          dataFiles,
          starterFiles,
        },
        null,
        2
      ),
      "utf8"
    );

    index.push({
      id,
      part,
      order,
      title,
      fileCount: mainFilesList.length,
      hasDataFiles: dataFiles.length > 0,
    });
  }

  index.sort((a, b) => a.part - b.part || a.order - b.order);
  await fs.writeFile(path.join(OUT_EXERCISES, "index.json"), JSON.stringify(index, null, 2), "utf8");

  console.log(`Parsed ${index.length} exercises (skipped ${skippedJavaFx} JavaFX exercises).`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
