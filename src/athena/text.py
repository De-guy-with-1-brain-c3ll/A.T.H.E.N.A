"""Text-only ATHENA using the same model, tools, approvals, settings, and memory."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys
import threading
from uuid import uuid4

from athena.config import load_local_environment
from athena.background import BackgroundAgents
from athena.llm.deepseek import DeepSeekLanguageModel, DeepSeekUnavailable
from athena.memory.service import MemoryService
from athena.paths import database_path
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry


class TextSession:
    def __init__(self, model, memory, registry) -> None:
        self.model = model
        self.memory = memory
        self.registry = registry

    async def reply(self, text: str) -> str:
        turn_id = uuid4()
        parts: list[str] = []
        try:
            async for fragment in self.model.stream_reply(
                turn_id, text, self.memory.context_messages()
            ):
                parts.append(fragment)
                print(fragment, end="", flush=True)
        except DeepSeekUnavailable as error:
            print(str(error))
            return ""
        answer = "".join(parts).strip()
        print()
        if answer:
            await self.memory.remember_turn(turn_id, text, answer)
        return answer

    async def remember_job(self, job) -> None:
        answer = (job.reply or "").strip()
        if answer:
            await self.memory.remember_turn(job.id, job.text, answer)


async def console_input(prompt: str) -> str:
    """Read console input without making asyncio wait for a blocked worker at exit."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def read() -> None:
        try:
            outcome = (True, input(prompt))
        except BaseException as error:
            outcome = (False, error)

        def deliver() -> None:
            if future.done():
                return
            if outcome[0]:
                future.set_result(outcome[1])
            else:
                future.set_exception(outcome[1])

        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            pass

    threading.Thread(target=read, name="athena-console-input", daemon=True).start()
    return await future


def _build() -> tuple[TextSession, MemoryService]:
    load_local_environment()
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise ValueError("Add DEEPSEEK_API_KEY to the project .env file first.")
    settings = RuntimeSettingsStore()
    registry = ToolRegistry.discover(services={"settings": settings})
    model_name = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
    model = DeepSeekLanguageModel(key, model_name, registry, settings, interface="text")
    memory = MemoryService(
        database_path(),
        key,
        model_name,
        settings.get("memory_batch_delay_seconds"),
        settings,
    )
    return TextSession(model, memory, registry), memory


async def run(message: str | None = None) -> int:
    session, memory = _build()
    await memory.connect()
    try:
        if message:
            print("ATHENA: ", end="", flush=True)
            await session.reply(message)
            return 0
        print("ATHENA text chat v0.9 is ready. Slow work runs as background tasks. Type /help for commands.")
        background = BackgroundAgents(session.model, limit=5)
        input_task = asyncio.create_task(console_input("You: "))
        try:
            while not session.registry.shutdown_requested:
                changed_task = asyncio.create_task(background.changed.wait())
                done, _ = await asyncio.wait({input_task, changed_task},
                                             return_when=asyncio.FIRST_COMPLETED)
                if changed_task in done:
                    background.changed.clear()
                    while True:
                        item = background.next_output()
                        if item is None:
                            break
                        job, acknowledgement = item
                        short = str(job.id)[:8]
                        if acknowledgement:
                            job.acknowledged = True
                            print(f"\nATHENA [{short}]: Still working in the background.")
                        else:
                            print(f"\nATHENA [{short}]: {job.reply}")
                            await session.remember_job(job)
                            background.delivered(job)
                    if not input_task.done():
                        print("You: ", end="", flush=True)
                if input_task in done:
                    changed_task.cancel()
                    await asyncio.gather(changed_task, return_exceptions=True)
                    text = input_task.result().strip()
                    command = text.casefold()
                    if command in {"/exit", "/quit", "exit", "quit"}:
                        break
                    if command == "/help":
                        print("Commands: /tasks, /cancel ID, /usage, /exit. Every normal request runs independently, so web searches and other slow work never freeze chat.")
                    elif command == "/usage":
                        usage = session.model.usage_estimate
                        print(f"Session estimate: {usage['requests']} DeepSeek request(s), about "
                              f"{usage['estimated_input_tokens']} input and {usage['estimated_output_tokens']} output tokens. "
                              f"Memory summaries: {memory._concentration_calls}. The DeepSeek dashboard is authoritative.")
                    elif command == "/tasks":
                        rows = background.task_rows()
                        print("No model tasks are running." if not rows else "\n".join(
                            f"{row['id']}  {row['status']}: {row['text']}" for row in rows))
                        download = background.download_status()
                        command_status = background.command_status()
                        if download.data["status"] not in {"none", "complete"}:
                            print("Download: " + download.spoken_text)
                        if command_status.data["status"] not in {"none", "complete"}:
                            print("Command: " + command_status.spoken_text)
                    elif command.startswith("/cancel "):
                        cancelled = await background.cancel(command.split(maxsplit=1)[1])
                        print("Task cancelled." if cancelled else "Use one valid task ID from /tasks.")
                    elif text:
                        job = background.submit(text, memory.context_messages())
                        if job is None:
                            print("ATHENA: Five tasks are already running. Use /tasks or /cancel ID.")
                        else:
                            print(f"ATHENA [{str(job.id)[:8]}]: Task accepted; you can keep chatting.")
                    if session.registry.shutdown_requested:
                        break
                    input_task = asyncio.create_task(console_input("You: "))
                elif not changed_task.done():
                    changed_task.cancel()
                    await asyncio.gather(changed_task, return_exceptions=True)
        finally:
            input_task.cancel()
            await asyncio.gather(input_task, return_exceptions=True)
            await background.cancel_all()
        return 0
    finally:
        await asyncio.gather(session.model.close(), memory.close(), return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", nargs="*", help="Send one message and exit; omit for interactive chat")
    args = parser.parse_args()
    message = " ".join(args.message).strip() or None
    try:
        return asyncio.run(run(message))
    except (KeyboardInterrupt, EOFError):
        print("\nATHENA text chat stopped.")
        return 130
    except (ValueError, OSError) as error:
        print(f"ATHENA text chat failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
