"""Test tools directly or chat with the official DeepSeek API without audio."""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
from uuid import uuid4

from athena.config import load_local_environment
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry


async def run(args) -> int:
    load_local_environment()
    settings = RuntimeSettingsStore()
    registry = ToolRegistry.discover(services={"settings": settings})
    if args.action == "list":
        print(json.dumps(registry.definitions(), indent=2, ensure_ascii=False))
        return 0
    if args.action == "call":
        raw = Path(args.arguments_file).read_text(encoding="utf-8") if args.arguments_file else args.arguments
        result = await registry.execute(args.name, json.loads(raw))
        print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
        return 0 if result.success else 1
    from athena.llm.deepseek import DeepSeekLanguageModel
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise ValueError("Set DEEPSEEK_API_KEY in .env before starting chat.")
    llm = DeepSeekLanguageModel(key, os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"), registry, settings)
    history = []
    print("ATHENA tools chat. Type exit to stop. Coding is restricted to data/coding.")
    try:
        while not registry.shutdown_requested:
            text = await asyncio.to_thread(input, "You: ")
            if text.strip().lower() in {"exit", "quit"}:
                return 0
            if not text.strip():
                continue
            parts = []
            print("ATHENA: ", end="", flush=True)
            async for fragment in llm.stream_reply(uuid4(), text, history[-12:]):
                parts.append(fragment)
                print(fragment, end="", flush=True)
            print()
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": "".join(parts)}])
        return 0
    finally:
        await llm.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list")
    call = commands.add_parser("call")
    call.add_argument("name")
    call.add_argument("arguments", nargs="?", default="{}", help="JSON tool arguments")
    call.add_argument("--arguments-file", help="Read JSON from a file to avoid shell quoting")
    commands.add_parser("chat")
    try:
        return asyncio.run(run(parser.parse_args()))
    except (KeyboardInterrupt, EOFError):
        return 130
    except (ValueError, OSError) as error:
        print(f"ATHENA tools: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
