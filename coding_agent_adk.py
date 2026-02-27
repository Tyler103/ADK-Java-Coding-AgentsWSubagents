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

PROJECT_DIR = "adk_coding_agent_workspace"

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
- Prefer:
    1) cd {PROJECT_DIR} && ./gradlew test
- If ./gradlew isn't executable, run:
    chmod +x {PROJECT_DIR}/gradlew
    then run tests again.
- Store results in session state (tool does this automatically).
Return a short summary of pass/fail.
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

def build_root_agent() -> SequentialAgent:
    # LLM agent #1: write the first version of code
    first_version_agent = LlmAgent(
        name="first_version_llm",
        instruction=FIRST_VERSION_INSTRUCTIONS,
        tools=[write_text_file, read_text_file],  # allow writing files
    )

    # Loop sub-agents
    junit_agent = LlmAgent(
        name="junit5_setup_and_tests_llm",
        instruction=JUNIT_SETUP_AND_TESTS_INSTRUCTIONS,
        tools=[write_text_file, read_text_file],
    )

    run_tests_agent = LlmAgent(
        name="run_tests_llm",
        instruction=RUN_TESTS_INSTRUCTIONS,
        tools=[run_shell_command],
    )

    improve_agent = LlmAgent(
        name="improve_code_llm",
        instruction=IMPROVE_CODE_INSTRUCTIONS,
        tools=[write_text_file, read_text_file],
    )

    checker_agent = CheckResultAndEscalate(name="check_and_escalate")

    loop_agent = LoopAgent(
        name="coding_loop",
        max_iterations=20,
        sub_agents=[
            junit_agent,
            run_tests_agent,
            improve_agent,
            checker_agent,
        ],
    )

    # Root sequential agent: first draft -> loop
    root = SequentialAgent(
        name="root_sequential",
        sub_agents=[
            first_version_agent,
            loop_agent,
        ],
    )

    return root


# ----------------------------
# Bootstrap: seed minimal files
# ----------------------------

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
    root_agent = build_root_agent()

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
