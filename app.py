try:
    import spaces
except ImportError:
    class _MockSpaces:
        def GPU(self, fn=None, *args, **kwargs):
            if fn is not None:
                return fn
            def decorator(f):
                return f
            return decorator
    spaces = _MockSpaces()

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import gradio as gr
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

BASE_DIR = Path(__file__).resolve().parent
TESTS_DIR = Path(os.environ.get("TESTS_DIR", str(BASE_DIR / "tests"))).resolve()
LIBS_DIR = Path(os.environ.get("LIBS_DIR", str(BASE_DIR / "libs"))).resolve()
RUNNER_JAVA = Path(os.environ.get("RUNNER_JAVA", str(BASE_DIR / "runner" / "JsonTestRunner.java"))).resolve()
POLICY_PATH = Path(os.environ.get("POLICY_PATH", str(BASE_DIR / "sandbox.policy"))).resolve()
TMP_ROOT = Path(os.environ.get("TMP_ROOT", tempfile.gettempdir())).resolve()

PORT = int(os.environ.get("PORT", os.environ.get("GRADIO_SERVER_PORT", "7860")))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "20"))

COMPILE_TIMEOUT_SEC = 10.0
RUN_TIMEOUT_SEC = 10.0
MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB cap
MAX_FILES = 20
MAX_FILE_BYTES = 50 * 1024    # 50 KB per file
MAX_BODY_BYTES = 256 * 1024   # 256 KB request payload limit

EXERCISE_ID_RE = re.compile(r"^[a-z0-9-]+$")
STUDENT_FILE_RE = re.compile(r"^src/main/java/([a-zA-Z0-9_]+/)*[a-zA-Z0-9_]+\.java$")

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "ALLOWED_ORIGINS", "https://quizfy.zynthlab.com,http://localhost:3000"
    ).split(",")
    if origin.strip()
]

RUNTIME_POLICY_PATH = TMP_ROOT / "quizfy-runtime-sandbox.policy"


# --- Required JAR Dependencies ---
REQUIRED_JARS = {
    "junit-4.12.jar": "https://repo1.maven.org/maven2/junit/junit/4.12/junit-4.12.jar",
    "hamcrest-core-1.3.jar": "https://repo1.maven.org/maven2/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar",
    "edu-test-utils-0.4.2.jar": "https://raw.githubusercontent.com/Thamindu-Dev/quizfy-java-mooc-runner/main/libs/edu-test-utils-0.4.2.jar",
}


def ensure_jars_exist() -> None:
    LIBS_DIR.mkdir(parents=True, exist_ok=True)
    import urllib.request
    for jar_name, jar_url in REQUIRED_JARS.items():
        jar_path = LIBS_DIR / jar_name
        if not jar_path.exists() or jar_path.stat().st_size == 0:
            print(f"[STARTUP] Downloading missing {jar_name} from {jar_url}...")
            try:
                urllib.request.urlretrieve(jar_url, str(jar_path))
                print(f"[STARTUP] Downloaded {jar_name} ({jar_path.stat().st_size} bytes)")
            except Exception as e:
                print(f"[STARTUP WARNING] Could not download {jar_name}: {e}")


def prepare_runtime_environment() -> Tuple[bool, str]:
    """
    Ensures test libraries exist, compiles runner/JsonTestRunner.java into libs/runner.jar on startup,
    and prepares a runtime security policy file that trusts both /app/libs/- and the actual LIBS_DIR.
    """
    LIBS_DIR.mkdir(parents=True, exist_ok=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)

    ensure_jars_exist()

    # Prepare runtime security policy so sandbox.policy works in any directory (/home/user/app, /app, etc.)
    policy_text = POLICY_PATH.read_text(encoding="utf-8")
    libs_posix = LIBS_DIR.as_posix()
    if not libs_posix.startswith("/"):
        libs_posix = "/" + libs_posix
    custom_grant = (
        f'\ngrant codeBase "file:{libs_posix}/-" {{\n'
        f"    permission java.security.AllPermission;\n"
        f"}};\n"
    )
    if f'file:{libs_posix}/-' not in policy_text:
        policy_text = policy_text + "\n" + custom_grant
    RUNTIME_POLICY_PATH.write_text(policy_text, encoding="utf-8")

    # Compile JsonTestRunner.java -> libs/runner.jar
    build_dir = TMP_ROOT / "runner-build"
    shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    try:
        libs_cp = str(LIBS_DIR / "*")
        proc = subprocess.run(
            ["javac", "-encoding", "UTF-8", "-cp", libs_cp, "-d", str(build_dir), str(RUNNER_JAVA)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "Unknown javac error").strip()
            print(f"[STARTUP ERROR] Failed to compile JsonTestRunner.java:\n{err}")
            return False, err

        runner_jar_path = LIBS_DIR / "runner.jar"
        with zipfile.ZipFile(runner_jar_path, "w", zipfile.ZIP_DEFLATED) as jar_zf:
            for class_file in build_dir.rglob("*.class"):
                arcname = class_file.relative_to(build_dir).as_posix()
                jar_zf.write(class_file, arcname)

        print(f"[STARTUP] Compiled JsonTestRunner.java -> {runner_jar_path}")
        return True, f"Compiled {runner_jar_path.name} successfully"
    except Exception as exc:
        print(f"[STARTUP ERROR] Exception compiling JsonTestRunner.java: {exc}")
        return False, str(exc)
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


STARTUP_OK, STARTUP_MSG = prepare_runtime_environment()


def run_command(cmd: List[str], cwd: Path, timeout_sec: float) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as exc:
        return {"code": -1, "stdout": "", "stderr": str(exc), "timedOut": False}

    timed_out = False
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=2.0)
        except Exception:
            stdout_bytes, stderr_bytes = b"", b""

    stdout_str = (stdout_bytes or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr_str = (stderr_bytes or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return {
        "code": proc.returncode if proc.returncode is not None else -1,
        "stdout": stdout_str,
        "stderr": stderr_str,
        "timedOut": timed_out,
    }


def list_java_files(directory: Path) -> List[str]:
    if not directory.exists():
        return []
    return [str(p) for p in sorted(directory.rglob("*.java")) if p.is_file()]


def sanitize_output(text: str, work_dir: Path) -> str:
    wd = str(work_dir)
    wd_posix = work_dir.as_posix()
    cleaned = text.replace(wd + os.sep, "").replace(wd_posix + "/", "")
    cleaned = cleaned.replace(wd, "").replace(wd_posix, "")
    return cleaned.strip()


def grade_submission(exercise_id: Any, files: Any) -> Dict[str, Any]:
    start_time = time.time()

    if not isinstance(exercise_id, str) or not EXERCISE_ID_RE.match(exercise_id):
        return {"status": "error", "error": "Invalid exerciseId format."}

    if not isinstance(files, dict):
        return {"status": "error", "error": "Payload must include a 'files' object."}

    file_entries = list(files.items())
    if len(file_entries) == 0 or len(file_entries) > MAX_FILES:
        return {"status": "error", "error": f"Provide between 1 and {MAX_FILES} Java files."}

    for rel_path, content in file_entries:
        if not isinstance(rel_path, str) or not STUDENT_FILE_RE.match(rel_path):
            return {"status": "error", "error": f"Invalid file path: {rel_path}"}
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return {"status": "error", "error": f"File too large or invalid: {rel_path}"}

    # Resolve test directory:
    # Primary: tests/<courseSubdir>/part-{N}/<id> (e.g. tests/java-programming-i/part-1/part01-01-sandbox)
    # Fallback: tests/part-{N}/<id> or flat tests/<id>
    part_match = re.match(r"^part(\d+)-", exercise_id)
    test_dir = None
    if part_match:
        part_num = int(part_match.group(1))
        course_subdir = "java-programming-i" if part_num <= 7 else "java-programming-ii"
        candidate = TESTS_DIR / course_subdir / f"part-{part_num}" / exercise_id
        if (candidate / "meta.json").exists():
            test_dir = candidate
        else:
            cand2 = TESTS_DIR / f"part-{part_num}" / exercise_id
            if (cand2 / "meta.json").exists():
                test_dir = cand2

    if test_dir is None:
        flat_dir = TESTS_DIR / exercise_id
        if (flat_dir / "meta.json").exists():
            test_dir = flat_dir
        else:
            return {"status": "error", "error": f"Exercise '{exercise_id}' not found."}

    meta_path = test_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "error", "error": f"Exercise '{exercise_id}' not found."}

    run_id = str(uuid.uuid4())
    work_dir = TMP_ROOT / f"quizfy-run-{run_id}"
    main_src_dir = work_dir / "src" / "main" / "java"
    test_src_dir = work_dir / "src" / "test" / "java"
    main_out_dir = work_dir / "out-main"
    test_out_dir = work_dir / "out-test"
    result_json_path = work_dir / "result.json"

    try:
        main_src_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        test_src_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        main_out_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        test_out_dir.mkdir(parents=True, mode=0o700, exist_ok=True)

        for rel_path, content in file_entries:
            dest = work_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        src_test_java = test_dir / "src" / "test" / "java"
        if src_test_java.exists():
            shutil.copytree(src_test_java, test_src_dir, dirs_exist_ok=True)

        resources_dir = test_dir / "resources"
        if resources_dir.exists():
            shutil.copytree(resources_dir, work_dir, dirs_exist_ok=True)

        # 1. Compile student code
        student_java_files = list_java_files(main_src_dir)
        compile_student = run_command(
            ["javac", "-J-Xmx256m", "-encoding", "UTF-8", "-d", str(main_out_dir), *student_java_files],
            work_dir,
            COMPILE_TIMEOUT_SEC,
        )

        if compile_student["timedOut"]:
            return {"status": "timeout", "error": "Compilation timed out."}

        if compile_student["code"] != 0:
            return {
                "status": "compile_error",
                "compileError": sanitize_output(
                    compile_student["stderr"] or compile_student["stdout"], work_dir
                ),
                "durationMs": int((time.time() - start_time) * 1000),
            }

        # 2. Compile MOOC JUnit tests against student classes
        test_java_files = list_java_files(test_src_dir)
        cp_sep = ";" if os.name == "nt" else ":"
        libs_classpath = str(LIBS_DIR / "*")
        compile_tests = run_command(
            [
                "javac",
                "-J-Xmx256m",
                "-encoding",
                "UTF-8",
                "-cp",
                f"{main_out_dir}{cp_sep}{libs_classpath}",
                "-d",
                str(test_out_dir),
                *test_java_files,
            ],
            work_dir,
            COMPILE_TIMEOUT_SEC,
        )

        if compile_tests["timedOut"]:
            return {"status": "timeout", "error": "Compilation timed out."}

        if compile_tests["code"] != 0:
            return {
                "status": "compile_error",
                "compileError": (
                    "Your code compiled, but is missing a class, constructor, or method signature expected by the tests:\n\n"
                    + sanitize_output(compile_tests["stderr"] or compile_tests["stdout"], work_dir)
                ),
                "durationMs": int((time.time() - start_time) * 1000),
            }

        # 3. Execute JUnit via JsonTestRunner under JDK 11 SecurityManager
        policy_file = RUNTIME_POLICY_PATH if RUNTIME_POLICY_PATH.exists() else POLICY_PATH
        run_result = run_command(
            [
                "java",
                "-Xmx128m",
                "-Xms32m",
                "-XX:ActiveProcessorCount=1",
                "-XX:+ExitOnOutOfMemoryError",
                "-Dfile.encoding=UTF-8",
                "-Djava.security.manager",
                f"-Djava.security.policy=={policy_file}",
                f"-Dsandbox.dir={work_dir}",
                f"-Dmain.classes.dir={main_out_dir}",
                f"-Dtest.classes.dir={test_out_dir}",
                "-cp",
                f"{test_out_dir}{cp_sep}{main_out_dir}{cp_sep}{libs_classpath}",
                "JsonTestRunner",
                str(result_json_path),
                *meta.get("testClasses", []),
            ],
            work_dir,
            RUN_TIMEOUT_SEC,
        )

        if run_result["timedOut"]:
            return {
                "status": "timeout",
                "error": "Execution timed out after 10 seconds. Check your code for infinite loops or missing input termination.",
                "durationMs": int((time.time() - start_time) * 1000),
            }

        try:
            raw_json = result_json_path.read_text(encoding="utf-8")
            parsed = json.loads(raw_json)
            return {
                "status": "completed",
                **parsed,
                "durationMs": int((time.time() - start_time) * 1000),
            }
        except Exception:
            err_msg = sanitize_output(run_result["stderr"] or run_result["stdout"], work_dir)
            return {
                "status": "runtime_error",
                "error": err_msg
                or "Execution terminated unexpectedly (possible out-of-memory or security violation).",
                "durationMs": int((time.time() - start_time) * 1000),
            }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# --- Concurrency Queue & Per-IP Rate Limiter ---
ip_hits: Dict[str, List[float]] = {}
active_runs = 0
queued_runs = 0
semaphore = asyncio.Semaphore(MAX_CONCURRENT)


def check_rate_limit(ip: str) -> bool:
    now = time.time()
    window_sec = 60.0
    recent = [t for t in ip_hits.get(ip, []) if now - t < window_sec]
    if len(recent) >= 15:
        ip_hits[ip] = recent
        return False
    recent.append(now)
    ip_hits[ip] = recent
    return True


async def enqueue_grade(exercise_id: Any, files: Any) -> Dict[str, Any]:
    global active_runs, queued_runs
    if queued_runs >= MAX_QUEUE:
        raise RuntimeError("BUSY")

    queued_runs += 1
    try:
        await semaphore.acquire()
    finally:
        queued_runs -= 1

    active_runs += 1
    try:
        return await asyncio.to_thread(grade_submission, exercise_id, files)
    finally:
        active_runs -= 1
        semaphore.release()


# --- REST API Handlers ---
async def health_endpoint():
    return {
        "ok": True,
        "activeRuns": active_runs,
        "queuedRuns": queued_runs,
    }


async def grade_endpoint(request: Request):
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else "unknown")
    )

    if not check_rate_limit(client_ip):
        return JSONResponse(
            status_code=429,
            content={"status": "error", "error": "Too many submissions. Please wait a minute."},
        )

    try:
        raw_body = await request.body()
        if len(raw_body) > MAX_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"status": "error", "error": "Payload too large."},
            )
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        body = {}

    if not isinstance(body, dict):
        body = {}

    exercise_id = body.get("exerciseId")
    files = body.get("files")

    try:
        result = await enqueue_grade(exercise_id, files)
        return JSONResponse(status_code=200, content=result)
    except RuntimeError as exc:
        if str(exc) == "BUSY":
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "error": "Runner is busy. Please retry in a few seconds.",
                },
            )
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "Internal grading error."},
        )
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "Internal grading error."},
        )


# --- Gradio Status & Interactive Test UI on / ---
def get_system_status() -> Dict[str, Any]:
    try:
        java_ver = subprocess.run(
            ["java", "-version"], capture_output=True, text=True, timeout=5
        )
        jdk_info = (java_ver.stderr or java_ver.stdout).splitlines()[0] if (java_ver.stderr or java_ver.stdout) else "Unknown"
    except Exception as exc:
        jdk_info = f"Unavailable ({exc})"

    # Count exercises across all course/part subdirectories
    exercise_count = len(list(TESTS_DIR.rglob("meta.json"))) if TESTS_DIR.exists() else 0
    return {
        "ok": True,
        "jdk": jdk_info,
        "runnerJarCompiled": (LIBS_DIR / "runner.jar").exists(),
        "startupMessage": STARTUP_MSG,
        "exercisesAvailable": exercise_count,
        "activeRuns": active_runs,
        "queuedRuns": queued_runs,
    }


@spaces.GPU
def run_gradio_test(exercise_id: str, file_path: str, java_code: str) -> Dict[str, Any]:
    return grade_submission(exercise_id.strip(), {file_path.strip(): java_code})


with gr.Blocks(title="Quizfy Java MOOC Runner") as demo:
    gr.Markdown(
        """
        # ☕ Quizfy Java MOOC Runner
        Open-source JUnit 4 grading runner and exercise bank for **Quizfy**, adapted from the [University of Helsinki Java Programming MOOC](https://java-programming.mooc.fi/).

        ### REST API Endpoints
        - **`GET /health`** — Container health & concurrency queue status
        - **`POST /grade`** — Compiles and runs MOOC JUnit tests under JDK 11 SecurityManager (`sandbox.policy`)
        """
    )

    with gr.Tab("Runner Smoke Test"):
        with gr.Row():
            ex_input = gr.Textbox(label="Exercise ID", value="part01-01-sandbox")
            path_input = gr.Textbox(label="File Path", value="src/main/java/Sandbox.java")
        code_input = gr.Code(
            label="Java Source Code",
            language="javascript",
            value='public class Sandbox {\n    public static void main(String[] args) {\n        System.out.println("Hello Quizfy!");\n    }\n}\n',
            lines=8,
        )
        grade_btn = gr.Button("Compile & Run JUnit Tests", variant="primary")
        grade_output = gr.JSON(label="Grading Output")
        grade_btn.click(
            fn=run_gradio_test,
            inputs=[ex_input, path_input, code_input],
            outputs=grade_output,
        )

    with gr.Tab("System Status"):
        status_btn = gr.Button("Refresh Status")
        status_output = gr.JSON(label="Health & Environment", value=get_system_status)
        status_btn.click(fn=get_system_status, inputs=[], outputs=status_output)


# Attach custom routes & CORS to Gradio's internal FastAPI app
def register_custom_routes(fastapi_app):
    if fastapi_app is None:
        return
    try:
        fastapi_app.add_middleware(
            CORSMiddleware,
            allow_origins=ALLOWED_ORIGINS,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )
    except Exception:
        pass
    fastapi_app.add_api_route("/health", health_endpoint, methods=["GET"])
    fastapi_app.add_api_route("/grade", grade_endpoint, methods=["POST"])


app = demo.app
register_custom_routes(app)


if __name__ == "__main__":
    import inspect

    launch_kwargs = {
        "server_name": "0.0.0.0",
        "server_port": PORT,
        "prevent_thread_lock": True,
    }
    if "ssr_mode" in inspect.signature(demo.launch).parameters:
        launch_kwargs["ssr_mode"] = False

    res = demo.queue().launch(**launch_kwargs)
    if isinstance(res, tuple) and len(res) >= 1:
        register_custom_routes(res[0])
    if hasattr(demo, "server") and hasattr(demo.server, "app"):
        register_custom_routes(demo.server.app)

    demo.block_thread()


