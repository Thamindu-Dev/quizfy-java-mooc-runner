import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { spawn } from "node:child_process";

const TESTS_DIR = path.resolve(process.env.TESTS_DIR || "/app/tests");
const LIBS_DIR = path.resolve(process.env.LIBS_DIR || "/app/libs");
const POLICY_PATH = path.resolve(process.env.POLICY_PATH || "/app/sandbox.policy");
const TMP_ROOT = process.env.TMP_ROOT || "/tmp";

const COMPILE_TIMEOUT_MS = 10_000;
const RUN_TIMEOUT_MS = 10_000;
const MAX_OUTPUT_BYTES = 64 * 1024; // 64 KB cap
const MAX_FILES = 20;
const MAX_FILE_BYTES = 50 * 1024;   // 50 KB per file

const EXERCISE_ID_RE = /^[a-z0-9-]+$/;
const STUDENT_FILE_RE = /^src\/main\/java\/([a-zA-Z0-9_]+\/)*[a-zA-Z0-9_]+\.java$/;

function runCommand(cmd, args, cwd, timeoutMs) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, {
      cwd,
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
    });

    let stdout = "";
    let stderr = "";
    let timedOut = false;

    const appendCapped = (current, chunk) => {
      if (current.length >= MAX_OUTPUT_BYTES) return current;
      const remaining = MAX_OUTPUT_BYTES - current.length;
      return current + chunk.toString("utf8").slice(0, remaining);
    };

    child.stdout.on("data", (d) => { stdout = appendCapped(stdout, d); });
    child.stderr.on("data", (d) => { stderr = appendCapped(stderr, d); });

    const timer = setTimeout(() => {
      timedOut = true;
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {
        try { child.kill("SIGKILL"); } catch {}
      }
    }, timeoutMs);

    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? -1, stdout, stderr, timedOut });
    });

    child.on("error", (err) => {
      clearTimeout(timer);
      resolve({ code: -1, stdout, stderr: err.message, timedOut: false });
    });
  });
}

async function listJavaFiles(dir) {
  const results = [];
  async function walk(current) {
    let entries;
    try {
      entries = await fs.readdir(current, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) {
        await walk(full);
      } else if (entry.name.endsWith(".java")) {
        results.push(full);
      }
    }
  }
  await walk(dir);
  return results;
}

function sanitizeOutput(text, workDir) {
  return text.split(workDir + path.sep).join("").split(workDir).join("").trim();
}

export async function gradeSubmission(exerciseId, files) {
  const startTime = Date.now();

  if (typeof exerciseId !== "string" || !EXERCISE_ID_RE.test(exerciseId)) {
    return { status: "error", error: "Invalid exerciseId format." };
  }

  if (!files || typeof files !== "object" || Array.isArray(files)) {
    return { status: "error", error: "Payload must include a 'files' object." };
  }

  const fileEntries = Object.entries(files);
  if (fileEntries.length === 0 || fileEntries.length > MAX_FILES) {
    return { status: "error", error: `Provide between 1 and ${MAX_FILES} Java files.` };
  }

  for (const [relPath, content] of fileEntries) {
    if (!STUDENT_FILE_RE.test(relPath)) {
      return { status: "error", error: `Invalid file path: ${relPath}` };
    }
    if (typeof content !== "string" || Buffer.byteLength(content, "utf8") > MAX_FILE_BYTES) {
      return { status: "error", error: `File too large or invalid: ${relPath}` };
    }
  }

  // Resolve test directory:
  // Primary layout: tests/<courseSubdir>/part-{N}/<id> (e.g. tests/java-programming-i/part-1/part01-01-sandbox)
  // Fallbacks: tests/part-{N}/<id>, tests/<id>
  let testDir;
  let meta;
  const partMatch = exerciseId.match(/^part(\d+)-/);
  if (partMatch) {
    const partNum = parseInt(partMatch[1], 10);
    const courseSubdir = partNum <= 7 ? "java-programming-i" : "java-programming-ii";
    const partSubdir = `part-${partNum}`;
    testDir = path.join(TESTS_DIR, courseSubdir, partSubdir, exerciseId);
  }
  if (!testDir) {
    testDir = path.join(TESTS_DIR, exerciseId);
  }
  try {
    meta = JSON.parse(await fs.readFile(path.join(testDir, "meta.json"), "utf8"));
  } catch {
    // Fall back to tests/part-{N}/<id> or flat layout
    let resolved = null;
    if (partMatch) {
      const partNum = parseInt(partMatch[1], 10);
      const intermediateDir = path.join(TESTS_DIR, `part-${partNum}`, exerciseId);
      try {
        meta = JSON.parse(await fs.readFile(path.join(intermediateDir, "meta.json"), "utf8"));
        resolved = intermediateDir;
      } catch {}
    }
    if (!resolved) {
      const flatDir = path.join(TESTS_DIR, exerciseId);
      try {
        meta = JSON.parse(await fs.readFile(path.join(flatDir, "meta.json"), "utf8"));
        resolved = flatDir;
      } catch {
        return { status: "error", error: `Exercise '${exerciseId}' not found.` };
      }
    }
    testDir = resolved;
  }

  const runId = crypto.randomUUID();
  const workDir = path.join(TMP_ROOT, `quizfy-run-${runId}`);
  const mainSrcDir = path.join(workDir, "src", "main", "java");
  const testSrcDir = path.join(workDir, "src", "test", "java");
  const mainOutDir = path.join(workDir, "out-main");
  const testOutDir = path.join(workDir, "out-test");
  const resultJsonPath = path.join(workDir, "result.json");

  try {
    await fs.mkdir(mainSrcDir, { recursive: true, mode: 0o700 });
    await fs.mkdir(testSrcDir, { recursive: true, mode: 0o700 });
    await fs.mkdir(mainOutDir, { recursive: true, mode: 0o700 });
    await fs.mkdir(testOutDir, { recursive: true, mode: 0o700 });

    for (const [relPath, content] of fileEntries) {
      const dest = path.join(workDir, relPath);
      await fs.mkdir(path.dirname(dest), { recursive: true });
      await fs.writeFile(dest, content, "utf8");
    }

    await fs.cp(path.join(testDir, "src", "test", "java"), testSrcDir, { recursive: true });

    const resourcesDir = path.join(testDir, "resources");
    try {
      await fs.cp(resourcesDir, workDir, { recursive: true });
    } catch {
      // No resources directory for this exercise
    }

    // 1. Compile student code
    const studentJavaFiles = await listJavaFiles(mainSrcDir);
    const compileStudent = await runCommand(
      "javac",
      ["-J-Xmx256m", "-encoding", "UTF-8", "-d", mainOutDir, ...studentJavaFiles],
      workDir,
      COMPILE_TIMEOUT_MS
    );

    if (compileStudent.timedOut) {
      return { status: "timeout", error: "Compilation timed out." };
    }
    if (compileStudent.code !== 0) {
      return {
        status: "compile_error",
        compileError: sanitizeOutput(compileStudent.stderr || compileStudent.stdout, workDir),
        durationMs: Date.now() - startTime,
      };
    }

    // 2. Compile MOOC JUnit tests against student classes
    const testJavaFiles = await listJavaFiles(testSrcDir);
    const cpSep = process.platform === "win32" ? ";" : ":";
    const libsClasspath = path.join(LIBS_DIR, "*");
    const compileTests = await runCommand(
      "javac",
      [
        "-J-Xmx256m",
        "-encoding",
        "UTF-8",
        "-cp",
        `${mainOutDir}${cpSep}${libsClasspath}`,
        "-d",
        testOutDir,
        ...testJavaFiles,
      ],
      workDir,
      COMPILE_TIMEOUT_MS
    );

    if (compileTests.code !== 0) {
      return {
        status: "compile_error",
        compileError:
          "Your code compiled, but is missing a class, constructor, or method signature expected by the tests:\n\n" +
          sanitizeOutput(compileTests.stderr || compileTests.stdout, workDir),
        durationMs: Date.now() - startTime,
      };
    }

    // 3. Execute JUnit via JsonTestRunner under JDK 11 SecurityManager
    const runResult = await runCommand(
      "java",
      [
        "-Xmx128m",
        "-Xms32m",
        "-XX:ActiveProcessorCount=1",
        "-XX:+ExitOnOutOfMemoryError",
        "-Dfile.encoding=UTF-8",
        "-Djava.security.manager",
        `-Djava.security.policy==${POLICY_PATH}`,
        `-Dsandbox.dir=${workDir}`,
        `-Dmain.classes.dir=${mainOutDir}`,
        `-Dtest.classes.dir=${testOutDir}`,
        "-cp",
        `${testOutDir}${cpSep}${mainOutDir}${cpSep}${libsClasspath}`,
        "JsonTestRunner",
        resultJsonPath,
        ...meta.testClasses,
      ],
      workDir,
      RUN_TIMEOUT_MS
    );

    if (runResult.timedOut) {
      return {
        status: "timeout",
        error: "Execution timed out after 10 seconds. Check your code for infinite loops or missing input termination.",
        durationMs: Date.now() - startTime,
      };
    }

    try {
      const rawJson = await fs.readFile(resultJsonPath, "utf8");
      const parsed = JSON.parse(rawJson);
      return {
        status: "completed",
        ...parsed,
        durationMs: Date.now() - startTime,
      };
    } catch {
      return {
        status: "runtime_error",
        error:
          sanitizeOutput(runResult.stderr || runResult.stdout, workDir) ||
          "Execution terminated unexpectedly (possible out-of-memory or security violation).",
        durationMs: Date.now() - startTime,
      };
    }
  } finally {
    await fs.rm(workDir, { recursive: true, force: true }).catch(() => {});
  }
}
