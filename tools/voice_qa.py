"""Bounded live voice/prompt checks. Tools never perform user actions here."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(os.environ.get("ATHENA_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / "src"))
from athena.config import load_local_environment
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.prompts import packaged_prompt_path
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry
from athena.tts.edge import EdgeSynthesizer


async def synthesize(text, legacy=False):
    import athena.tts.edge as module
    from unittest.mock import patch
    original = module.decoder_command
    def command(rate=None):
        args = original(rate)
        if legacy:
            args[args.index("-f"):args.index("-i")] = []
        return args
    synth = EdgeSynthesizer()
    synth._loop = asyncio.get_running_loop()
    turn = uuid4()
    start = time.perf_counter()
    pcm, first = bytearray(), None
    async def read():
        nonlocal first
        async for chunk in synth.audio(turn):
            if first is None:
                first = time.perf_counter() - start
            pcm.extend(chunk.pcm)
    with patch.object(module, "decoder_command", command):
        reader = asyncio.create_task(read())
        try:
            await synth.send_text(turn, text)
            await synth.flush(turn)
            await asyncio.wait_for(reader, 30)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await synth.close()
    if not pcm:
        raise RuntimeError("Edge returned no decoded audio")
    return {"first_pcm_s": round(first, 3), "audio_seconds": round(len(pcm)/48000, 3)}, bytes(pcm)


async def run(args):
    load_local_environment()
    report = {"tts": [], "wake": [], "prompts": [], "tools": [], "stt": []}
    samples = {}
    for legacy in (True, False):
        for sentence in ("Athena, what time is it?", "The weather is sunny today."):
            try:
                result, pcm = await synthesize(sentence, legacy)
                report["tts"].append({"legacy": legacy, "text": sentence, **result})
                if not legacy:
                    converter = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", "24000", "-ac", "1",
                        "-i", "pipe:0", "-f", "s16le", "-ar", "16000", "pipe:1",
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE)
                    converted, _ = await converter.communicate(pcm)
                    if converter.returncode:
                        raise RuntimeError("Audio sample conversion failed")
                    samples[sentence] = converted
            except Exception as error:
                report["tts"].append({"legacy": legacy, "error": str(error)})

    from athena.stt.keyword import KeywordGate, keyword_available
    if keyword_available():
        gate = KeywordGate()
        await gate.connect()
        for text, pcm in {**samples, "silence": bytes(64000)}.items():
            gate.reset()
            start, detected = time.perf_counter(), None
            for offset in range(0, len(pcm), 2560):
                if await gate.process(pcm[offset:offset+2560]):
                    detected = offset / 32000
                    break
            if detected is None and await gate.process(b"", final=True):
                detected = len(pcm) / 32000
            report["wake"].append({"text": text, "detected": detected is not None,
                                   "detected_audio_s": detected,
                                   "compute_s": round(time.perf_counter()-start, 3)})
        await gate.close()

    # One short, real Qwen utterance. Silence/background tests above are local.
    if samples and os.environ.get("DASHSCOPE_API_KEY"):
        from athena.stt.fun_asr import FunAsrRecognizer
        pcm = samples.get("Athena, what time is it?")
        if pcm:
            stt = FunAsrRecognizer(os.environ["DASHSCOPE_API_KEY"],
                os.environ.get("ATHENA_STT_MODEL", "qwen-audio-3.0-asr-flash-streaming"), 16000,
                prewarm=True)
            turn = uuid4()
            try:
                await stt.connect()
                await stt.start_turn(turn)
                final = asyncio.get_running_loop().create_future()
                async def collect():
                    async for result in stt.results():
                        if result.turn_id == turn and result.is_final:
                            if not final.done(): final.set_result(result.text)
                            return
                reader = asyncio.create_task(collect())
                start = time.perf_counter()
                for offset in range(0, len(pcm), 3200):
                    await stt.send_audio(pcm[offset:offset+3200])
                    await asyncio.sleep(0.1)
                end = time.perf_counter()
                await stt.finish_turn()
                text = await asyncio.wait_for(final, 12)
                report["stt"].append({"text": text, "audio_seconds": len(pcm)/32000,
                    "final_after_audio_s": round(time.perf_counter()-end, 3),
                    "warm_sessions_reused": stt.reused_sessions})
            except Exception as error:
                report["stt"].append({"error": str(error)})
            finally:
                if 'reader' in locals():
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                await stt.close()

    if args.prompts:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                             base_url="https://api.deepseek.com", timeout=15, max_retries=0)
        registry = ToolRegistry.discover()
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            router = DeepSeekLanguageModel("test", "test", registry,
                        RuntimeSettingsStore(Path(directory)/"settings.json"))
            prompts = {"previous": packaged_prompt_path("system").read_text(),
                       "compact": packaged_prompt_path("voice").read_text()}
            cases = [("Hello Athena", None), ("Explain why the sky is blue in two sentences.", None),
                     ("Check the weather in Shenzhen now.", "get_weather"),
                     ("Show my Teams assignments due this week.", "teams_assignments")]
            try:
                for name, prompt in prompts.items():
                    for text, expected in cases:
                        started, first = time.perf_counter(), None
                        tools = registry.definitions(router._tool_names_for(text))
                        request = {"model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                            "messages": [{"role":"system","content":prompt}, {"role":"user","content":text}],
                            "stream": True, "stream_options": {"include_usage": True},
                            "max_tokens": 120, "temperature":0,
                            "extra_body":{"thinking":{"type":"disabled"}}}
                        if tools: request.update(tools=tools, tool_choice="auto")
                        answer, calls, usage = [], [], {}
                        try:
                            stream = await client.chat.completions.create(**request)
                            async with stream:
                                async for chunk in stream:
                                    if chunk.usage: usage = chunk.usage.model_dump()
                                    if not chunk.choices: continue
                                    delta = chunk.choices[0].delta
                                    if delta.content or delta.tool_calls:
                                        if first is None: first=time.perf_counter()-started
                                    if delta.content: answer.append(delta.content)
                                    for call in delta.tool_calls or []:
                                        if call.function and call.function.name: calls.append(call.function.name)
                            report["prompts"].append({"prompt":name,"input":text,"reply":"".join(answer),
                                "calls":calls,"pass":expected in calls if expected else bool(answer) and not calls,
                                "first_text_s":round(first,3) if first is not None else None,
                                "total_s":round(time.perf_counter()-started,3),"usage":usage})
                        except Exception as error:
                            report["prompts"].append({"prompt":name,"input":text,"error":str(error)})
            finally:
                await router.close()
                await client.close()

    from athena.tools.web import ReadWebpageTool, SearchWebTool
    from athena.tools.weather import WeatherTool
    for name, tool, arguments in [
        ("reader", ReadWebpageTool(), {"url":"https://example.com/"}),
        ("search", SearchWebTool(), {"query":"深圳 今日 新闻", "limit":3}),
        ("weather", WeatherTool(), {"location":"22.54,114.06", "days":1}),
    ]:
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(tool.execute(arguments), 40)
            report["tools"].append({"tool":name,"success":result.success,
                "message":result.spoken_text,"elapsed_s":round(time.perf_counter()-started,3)})
        except Exception as error:
            report["tools"].append({"tool":name,"error":str(error)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompts", action="store_true", help="Eight bounded paid model requests")
    parser.add_argument("--output", type=Path, default=Path("outputs/voice-qa.json"))
    asyncio.run(run(parser.parse_args()))
