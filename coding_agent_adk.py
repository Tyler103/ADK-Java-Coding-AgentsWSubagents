"""
coding_agent_adk.py

A minimal "coding agent" team using Google ADK that:
  1) Generates a first version of a Java program (LlmAgent)
  2) Loops up to 20 times:
      a) Creates/updates a JUnit5 test harness + build files
      b) Runs unit tests (via a tool that runs shell commands)
      c) Improves Java code based on test failures
      d) Checks result and escalates to stop the loop when tests pass

Notes:
- This example uses Gradle (JUnit Jupiter) to avoid manually downloading jars.
- The "MCP tools" requirement is represented here by a tool boundary that
  runs compilation/tests (you can later swap it with your actual MCP tool).
"""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import asyncio
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional

from google.genai import types

from google.adk.agents import LlmAgent, SequentialAgent, LoopAgent, BaseAgent
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_context import ToolContext

import asyncio
from google.adk.agents import BaseAgent

PROJECT_DIR = "adk_coding_agent_workspace"

print("API KEY LOADED:", bool(os.getenv("GOOGLE_API_KEY")))

# ----------------------------
# "MCP-like" tools (local)
# ----------------------------

def write_text_file(path: str, content: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Writes content to a file, creating parent directories if needed.
    Stores last_written_path in session state for visibility across agents.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    tool_context.state["last_written_path"] = path
    return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}


def read_text_file(path: str, tool_context: ToolContext) -> Dict[str, Any]:
    """Reads a file (utf-8) and returns its content."""
    with open(path, "r", encoding="utf-8") as f:
        data = f.read()
    return {"ok": True, "path": path, "content": data}


def run_shell_command(
    command: str,
    cwd: str = ".",
    timeout_seconds: int = 120,
    tool_context: ToolContext = None,  # ADK injects ToolContext when called as a tool
) -> Dict[str, Any]:
    """
    Runs a shell command and returns stdout/stderr/exit code.
    This is the "compilation/run tool" boundary you can later replace with a real MCP tool.
    """
    proc = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    result = {
        "ok": proc.returncode == 0,
        "command": command,
        "cwd": cwd,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-4000:],  # keep last chunk
        "stderr": proc.stderr[-4000:],
    }

    # Persist for the next agents in the loop
    if tool_context is not None:
        tool_context.state["last_cmd"] = command
        tool_context.state["last_exit_code"] = proc.returncode
        tool_context.state["last_stdout"] = result["stdout"]
        tool_context.state["last_stderr"] = result["stderr"]
        tool_context.state["tests_passed"] = (proc.returncode == 0)

    return result

# Runs gradle tests locally before calling any llm

def local_gradle_test_check() -> bool:
    """
    Runs Gradle tests locally before invoking any LLM.
    Returns True if tests pass, False otherwise.
    """
    print("\n🔎 Running local pre-check: gradle clean test --no-daemon\n")

    env = os.environ.copy()

    # made sure that we are using correct jdk version(17)
    env["JAVA_HOME"] = "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"
    env["PATH"] = env["JAVA_HOME"] + "/bin:" + env.get("PATH", "")

    # run command to clean gradle enviroment
    # ensures a clean build
    cmd = "gradle clean test --no-daemon"
    proc = subprocess.run(
        cmd,
        cwd=PROJECT_DIR,
        shell=True,
        capture_output=True,
        text=True,
        env=env,
    )

    print("---- stdout (tail) ----")
    print(proc.stdout[-2000:])
    print("---- stderr (tail) ----")
    print(proc.stderr[-2000:])
    print("exit code:", proc.returncode)

    return proc.returncode == 0

# spaces out LLM calls
class SleepAgent(BaseAgent):
    async def _run_async_impl(self, ctx):
        await asyncio.sleep(20)
        return
    

# ----------------------------
# Custom checker + escalate
# ----------------------------

class CheckResultAndEscalate(BaseAgent):
    """
    Stops the LoopAgent early when tests pass by returning an Event with escalate=True.
    In ADK, LoopAgent terminates if a sub-agent returns an Event with escalate=True. :contentReference[oaicite:1]{index=1}
    """

    async def _run_async_impl(self, ctx):
        tests_passed = bool(ctx.session.state.get("tests_passed", False))
        exit_code = ctx.session.state.get("last_exit_code", None)

        if tests_passed:
            msg = f"✅ Tests passed (exit_code={exit_code}). Escalating to stop loop."
            yield Event(
                author=self.name,
                content=types.Content(role=self.name, parts=[types.Part(text=msg)]),
                actions=EventActions(escalate=True),
            )
            return

        msg = f"❌ Tests not passing yet (exit_code={exit_code}). Continue looping."
        yield Event(
            author=self.name,
            content=types.Content(role=self.name, parts=[types.Part(text=msg)]),
        )


# ----------------------------
# LLM agent prompts
# ----------------------------



FIRST_VERSION_INSTRUCTIONS = f"""
You are a coding agent.

Goal: Create a small Java project that can be tested with JUnit5.
Write initial version of a simple library class and a main entrypoint.

Requirements:
- Use Gradle (Groovy build.gradle) with JUnit Jupiter (JUnit5).
- Put production code in: {PROJECT_DIR}/src/main/java/example/
- Put tests in: {PROJECT_DIR}/src/test/java/example/
- Start with a simple class: Calculator with methods:
    - int add(int a, int b)
    - int sub(int a, int b)
    - int mul(int a, int b)
    - int div(int a, int b) throws IllegalArgumentException on divide-by-zero
- Also include a Main class that demonstrates a couple operations.

You MUST create files by calling the tool write_text_file(path, content).
Do not print the full files in chat; write them to disk via the tool.
"""


JUNIT_SETUP_AND_TESTS_INSTRUCTIONS = f"""
You are the "JUnit5 setup + unit-tests development" agent.

Task:
- Ensure the project has Gradle wrapper + build files and JUnit5 configured.
- Create/refresh the unit tests for example.Calculator with:
    - add/sub/mul/div happy paths
    - div by zero must throw IllegalArgumentException
- If build files already exist, update them only as needed.

Files to ensure exist:
- {PROJECT_DIR}/settings.gradle
- {PROJECT_DIR}/build.gradle
- {PROJECT_DIR}/gradlew and {PROJECT_DIR}/gradlew.bat (you can create minimal placeholders if needed)
- {PROJECT_DIR}/gradle/wrapper/gradle-wrapper.properties (minimal placeholder ok for milestone)
- {PROJECT_DIR}/src/test/java/example/CalculatorTest.java

Use the tool write_text_file(...) to create/update files.
"""

RUN_TESTS_INSTRUCTIONS = f"""
You are the "Run unit tests with MCP tools for compilation and run" agent.

Task:
- Run unit tests using the tool run_shell_command.
- Use system Gradle (NOT ./gradlew), because this milestone uses a placeholder wrapper.
- Run:
    1) cd {PROJECT_DIR} && gradle clean test --no-daemon
- If it fails, rerun with:
    2) cd {PROJECT_DIR} && gradle clean test --no-daemon --stacktrace
- Store results in session state (tool does this automatically).
Return a short summary of pass/fail and the key error lines.
"""

IMPROVE_CODE_INSTRUCTIONS = f"""
You are the "Improve code using failures" agent.

Task:
- Read the last test output from session state:
    last_stdout, last_stderr, last_exit_code
- If tests failed, edit production code in:
    {PROJECT_DIR}/src/main/java/example/Calculator.java
  to fix the issues.
- Keep changes minimal and targeted to the failure.
- Use write_text_file(...) to apply changes.

If tests already passed, do nothing.
"""


# ----------------------------
# Helper: minimal Gradle files
# ----------------------------

# For a milestone, you can use simple Gradle build files.
# If you want a truly runnable wrapper, you'd generate wrapper scripts properly,
# but the loop agent can still iterate on build config as needed.

MIN_SETTINGS_GRADLE = """rootProject.name = 'adk-coding-agent'
"""

MIN_BUILD_GRADLE = """plugins {
    id 'java'
}

group = 'example'
version = '1.0.0'

repositories {
    mavenCentral()
}

dependencies {
    testImplementation 'org.junit.jupiter:junit-jupiter:5.10.2'
}

test {
    useJUnitPlatform()
}
"""

MIN_GRADLEW = """#!/usr/bin/env bash
# Minimal placeholder gradlew for milestone.
# Replace with a real Gradle wrapper for full fidelity.
echo "This is a placeholder gradlew. Install Gradle and run: gradle test"
exit 2
"""

MIN_GRADLEW_BAT = r"""@echo off
REM Minimal placeholder gradlew.bat for milestone.
echo This is a placeholder gradlew.bat. Install Gradle and run: gradle test
exit /b 2
"""

MIN_WRAPPER_PROPS = """distributionUrl=https\\://services.gradle.org/distributions/gradle-8.7-bin.zip
"""


# ----------------------------
# Build the agent tree
# ----------------------------

def build_root_agent(include_first_version: bool) -> SequentialAgent:
    
    # -----------------------------
    # 1) First-pass generation agent
    # -----------------------------
    # This agent is responsible for creating the initial Java project skeleton:
    # - src/main/java/example/Calculator.java
    # - src/main/java/example/Main.java
    # - baseline Gradle setup, etc.
    # It writes files using the write_text_file tool instead of printing code to chat.

    first_version_agent = LlmAgent(
        name="first_version_llm",
        instruction=FIRST_VERSION_INSTRUCTIONS,
        tools=[write_text_file, read_text_file],  # allow writing files
    )

    # --------------------------------
    # 2) Loop sub-agents (repair cycle)
    # --------------------------------
    # This agent ensures the JUnit 5 test harness and Gradle config exist and are correct.
    # It can update build.gradle/settings.gradle and create CalculatorTest.java.
    junit_agent = LlmAgent(
        name="junit5_setup_and_tests_llm",
        instruction=JUNIT_SETUP_AND_TESTS_INSTRUCTIONS,
        tools=[write_text_file, read_text_file],
    )

    # This agent runs the unit tests through a tool boundary (run_shell_command).
    # “MCP tools for compilation and run”.
    run_tests_agent = LlmAgent(
        name="run_tests_llm",
        instruction=RUN_TESTS_INSTRUCTIONS,
        tools=[run_shell_command],
    )

    # This agent reads the last test output (stdout/stderr/exit code in session state)
    # and makes targeted edits to the Java production code to fix failing tests.
    improve_agent = LlmAgent(
        name="improve_code_llm",
        instruction=IMPROVE_CODE_INSTRUCTIONS,
        tools=[write_text_file, read_text_file],
    )



    # This agent checks session.state["tests_passed"] and escalates (stops the loop)
    # when tests succeed.
    checker_agent = CheckResultAndEscalate(name="check_and_escalate")


    # add sleeper agents to pause execution
    # only got 5 llm calls per minute 
    # had to save resources
    sleep_1 = SleepAgent(name="sleep_1")
    sleep_2 = SleepAgent(name="sleep_2")
    sleep_3 = SleepAgent(name="sleep_3")

    # -----------------------------
    # LoopAgent: iterative repair
    # -----------------------------
    # Runs up to max_iterations times, executing the same sub-agent sequence each cycle:
    #   junit setup -> sleep -> run tests -> sleep -> improve code -> sleep -> check+stop?
    loop_agent = LoopAgent(
        name="coding_loop",
        max_iterations=20,
        sub_agents=[
            junit_agent,
            sleep_1,
            run_tests_agent,
            sleep_2,
            improve_agent,
            sleep_3,
            checker_agent,
        ],
    )
    
    # ✅ Conditionally include first_version_agent
    agents = []
    if include_first_version:
        agents.append(first_version_agent)
    agents.append(loop_agent)

    # Return the root SequentialAgent as the top-level agent to pass into Runner(...)
    return SequentialAgent(
        name="root_sequential",
        sub_agents=agents,
    )

    """
    Creates a minimal Java + Gradle project structure BEFORE the LLM runs.

    Why this exists:
    ----------------
    Instead of letting the LLM guess folder structure from scratch,
    we create a stable workspace baseline.

    This reduces:
    - hallucinated paths
    - missing directories
    - broken Gradle wrapper issues
    - unnecessary LLM repair cycles
    - API cost

    The JUnit/setup agent can later modify or replace these files
    if needed.
    """

async def seed_minimal_project_files():
    """
    Seed a minimal Gradle-ish skeleton so the LLM has a stable target.
    (The JUnit agent will improve/replace as needed.)
    """
    # Call tools directly (outside LLM) to create folders and baseline build files.
    os.makedirs(f"{PROJECT_DIR}/src/main/java/example", exist_ok=True)
    os.makedirs(f"{PROJECT_DIR}/src/test/java/example", exist_ok=True)
    os.makedirs(f"{PROJECT_DIR}/gradle/wrapper", exist_ok=True)

    # Write baseline build configuration
    with open(f"{PROJECT_DIR}/settings.gradle", "w", encoding="utf-8") as f:
        f.write(MIN_SETTINGS_GRADLE)
    with open(f"{PROJECT_DIR}/build.gradle", "w", encoding="utf-8") as f:
        f.write(MIN_BUILD_GRADLE)
    with open(f"{PROJECT_DIR}/gradlew", "w", encoding="utf-8") as f:
        f.write(MIN_GRADLEW)
    with open(f"{PROJECT_DIR}/gradlew.bat", "w", encoding="utf-8") as f:
        f.write(MIN_GRADLEW_BAT)
    with open(f"{PROJECT_DIR}/gradle/wrapper/gradle-wrapper.properties", "w", encoding="utf-8") as f:
        f.write(MIN_WRAPPER_PROPS)


async def main():
    DISABLE_LLM = False

    """ if DISABLE_LLM:
        print("🚫 LLM disabled. Fix local Gradle failure first.")
    return"""
    
    # 1️⃣ First: local check BEFORE creating ADK agents
    if os.path.exists(PROJECT_DIR):
        tests_pass = local_gradle_test_check()

        # test pass, no futher reasoning required
        if tests_pass:
            print("\n✅ Tests already pass locally.")
            print("🚫 Skipping ADK LLM execution to save API quota.")
            print("Workspace:", os.path.abspath(PROJECT_DIR))
            return
        else:
            print("\n❌ Tests failing. Running ADK agent to fix...")

    # 2️⃣ Only build agents if needed

    # checks: Does the Java workspace folder already exist?
    include_first = not os.path.exists(PROJECT_DIR)

    # use original version to save resources 
    root_agent = build_root_agent(include_first_version=include_first)

    # --- Session Management ---
    session_service = InMemorySessionService()
    APP_NAME = "adk_java_coding_agent"
    USER_ID = "user_1"
    SESSION_ID = "session_001"

    # Create the session once
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )

    # --- Runner ---
    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    await seed_minimal_project_files()

    user_msg = types.Content(
        role="user",
        parts=[types.Part(text="Build the Java Calculator project and make tests pass.")]
    )
    from google.adk.models.google_llm import _ResourceExhaustedError
    from google.genai.errors import ClientError

    try:
        async for event in runner.run_async(
            new_message=user_msg,
            user_id=USER_ID,
            session_id=SESSION_ID,
        ):
            author = getattr(event, "author", "unknown")
            text = ""
            try:
                if event.content and event.content.parts:
                    text = "".join([p.text or "" for p in event.content.parts])[:3000]
            except Exception:
                pass

            if text.strip():
                print(f"\n[{author}] {text}")
    
    except _ResourceExhaustedError as e:
        print("\n⚠️ Gemini quota/rate limit hit (429).")
        print("Tip: run again after the retryDelay shown in the error, or reduce LLM calls.")
        return
    except ClientError as e:
        print("\n⚠️ Gemini ClientError:", e)
        return
    # Fetch updated session state at end
    updated_session = await session_service.get_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )

    print("\n--- Final session state ---")
    print("tests_passed:", updated_session.state.get("tests_passed"))
    print("last_exit_code:", updated_session.state.get("last_exit_code"))
    print("workspace:", os.path.abspath(PROJECT_DIR))


if __name__ == "__main__":
    asyncio.run(main())
