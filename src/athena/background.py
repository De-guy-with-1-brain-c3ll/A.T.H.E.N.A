"""Bounded independent model/tool jobs; only the voice hub presents consent."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4

@dataclass
class Job:
    id: UUID
    text: str
    model: object | None
    created_at: float = field(default_factory=time.time)
    forked: bool = False
    task: asyncio.Task | None = None
    reply: str | None = None
    # The voice coordinator used to receive only `reply`, after the entire model
    # stream had been joined.  That made a streaming model and streaming TTS wait
    # for the whole answer anyway.  Keep the fragments as they arrive so playback
    # can start with the first complete speakable clause.
    reply_fragments: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    reply_started: bool = False
    delivery_started: bool = False
    acknowledged: bool = False
    acknowledgement_due: bool = False
    speech_eos_at: float | None = None
    first_text_at: float | None = None


class BackgroundAgents:
    APPROVAL_LIFETIME_SECONDS = 120

    def __init__(self, model, limit=3):
        self.model = model
        self.limit = limit
        self.jobs: dict[UUID, Job] = {}
        self.approval_model = None
        self._approval_expiry_task = None
        self.changed = asyncio.Event()

    def is_confirmation_reply(self, text):
        return self.approval_model is not None and self.approval_model.is_confirmation_reply(text)

    def download_status(self):
        candidates = [job.model._tools for job in self.jobs.values()]
        if self.approval_model is not None:
            candidates.append(self.approval_model._tools)
        for registry in reversed(candidates):
            if registry.download_context()["last_download"]:
                return registry.download_status()
        return self.model._tools.download_status()

    def command_status(self):
        candidates = [job.model._tools for job in self.jobs.values() if job.model is not None]
        if self.approval_model is not None:
            candidates.append(self.approval_model._tools)
        for registry in reversed(candidates):
            if registry.command_context()["last_command"]:
                return registry.command_status()
        return self.model._tools.command_status()

    def contextual_status(self, text, context=None):
        from athena.tools.status import is_status_question, topic_tool
        from athena.tools.models import ToolResult
        if not is_status_question(text):
            return None
        wanted = topic_tool(text)
        if not wanted:
            for message in reversed(context or []):
                if message.get('role') == 'user' and not is_status_question(message.get('content', '')):
                    wanted = topic_tool(message.get('content', ''))
                    if wanted:
                        break
        active = [job for job in self.jobs.values() if job.task is not None and not job.task.done()]
        if not wanted and len(active) > 1:
            return ToolResult(False, 'Which task do you mean? ' + '; '.join(job.text[:80] for job in active))
        for job in reversed(list(self.jobs.values())):
            if not wanted or topic_tool(job.text) == wanted:
                if job.task is not None and not job.task.done():
                    registry = getattr(job.model, '_tools', self.model._tools)
                    actual = registry.status_store.rows(wanted or topic_tool(job.text))
                    if actual and actual[0]['updated'] >= getattr(job, 'created_at', float('inf')):
                        return registry.contextual_status(text, context)
                    return ToolResult(True, 'I am still processing your request: ' + job.text[:160] +
                                      '. I have no confirmed completion yet.', {'status': 'processing'})
        registry = self.approval_model._tools if self.approval_model else self.model._tools
        return registry.contextual_status(text, context)

    def submit(self, text, context):
        if len(self.jobs) >= self.limit:
            return None
        # The only agent allowed to consume a yes is the one whose request was
        # actually spoken. Other agents cannot see or overwrite its grant.
        approval_worker = self.approval_model
        if approval_worker is not None and self._approval_expiry_task is not None:
            self._approval_expiry_task.cancel()
            self._approval_expiry_task = None
        # The normal one-at-a-time conversation reuses ATHENA's main model.
        # A lightweight isolated fork is needed only for real concurrency or
        # to finish the exact approval flow already shown to the user.
        if approval_worker is not None:
            worker, forked = approval_worker, approval_worker is not self.model
        elif not self.jobs:
            worker, forked = self.model, False
        else:
            worker, forked = self.model.fork(), True
        self.approval_model = None
        pending_context = []
        for existing in self.jobs.values():
            pending_context.extend([
                {"role": "user", "content": existing.text},
                {"role": "assistant", "content": (
                    "This conversation request is still being processed; this is NOT proof that any tool action has started."
                    if existing.task is not None and not existing.task.done() else
                    "This conversation reply is awaiting delivery; check_tool_status is required to verify any action.")},
            ])
        job = Job(uuid4(), text, worker, forked=forked)
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._work(job, [*context, *pending_context]))
        return job

    async def _work(self, job, context):
        def acknowledge():
            if not job.acknowledged:
                job.acknowledgement_due = True
                self.changed.set()

        # Also acknowledge a received request if opening the HTTP stream itself
        # stalls. This says "On it", not "connected" or "completed".
        # Web searches and downloads benefit from an immediate audible receipt;
        # ordinary requests get one only after a genuinely long connection.
        web_heavy = re.search(r"\b(?:web|website|browse|browser|search|download|internet)\b", job.text.casefold())
        timer = asyncio.get_running_loop().call_later(0.15 if web_heavy else 1.25, acknowledge)
        try:
            parts: list[str] = []
            async for part in job.model.stream_reply(
                    job.id, job.text, context,
                    on_connected=acknowledge if web_heavy else lambda: None):
                parts.append(part)
                await job.reply_fragments.put(part)
                if not job.reply_started:
                    job.first_text_at = time.time()
                    job.reply_started = True
                    self.changed.set()
            job.reply = "".join(parts)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Provider failures already contain deliberately safe, actionable text.
            job.reply = (str(error) if error.__class__.__name__ == "DeepSeekUnavailable"
                         else "That request failed. Please try again.")
        finally:
            timer.cancel()
            # The consumer must be ended even when a provider fails or is
            # cancelled; otherwise it would wait forever after speaking the
            # first few fragments.
            await job.reply_fragments.put(None)
            self.changed.set()

    def next_output(self):
        if self.approval_model is not None and not self.approval_model._tools.has_pending_approval:
            self.approval_model._tools.clear_approval()
            self.approval_model = None
        # Prefer a real answer over filler. Never replace an unanswered approval
        # with a second approval that could make a subsequent yes ambiguous.
        for job in self.jobs.values():
            if (job.reply_started or job.reply is not None) and not job.delivery_started:
                # A prepared prompt may wait behind another approval. Its timer
                # starts only when the prompt is actually delivered.
                pending = job.model._tools._pending is not None
                if pending and self.approval_model is not None:
                    continue
                return job, False
        for job in self.jobs.values():
            if job.reply is None and job.acknowledgement_due and not job.acknowledged:
                return job, True
        return None

    async def reply_stream(self, job: Job):
        """Yield one job's model output as it becomes available."""
        while True:
            fragment = await job.reply_fragments.get()
            if fragment is None:
                return
            yield fragment

    def delivered(self, job):
        if job.model._tools._pending is not None:
            job.model._tools.present_approval()
            self.approval_model = job.model
            self._approval_expiry_task = asyncio.create_task(
                self._expire_approval(job.model))
        self.jobs.pop(job.id, None)
        if self.approval_model is not job.model:
            # Drop the completed coroutine and temporary model immediately.
            # Forks share the root HTTP pool, so there is no separate client to close.
            job.task = None
            job.model = None

    async def _expire_approval(self, model):
        try:
            await asyncio.sleep(self.APPROVAL_LIFETIME_SECONDS)
            if self.approval_model is model:
                model._tools.clear_approval()
                self.approval_model = None
                self.changed.set()
        except asyncio.CancelledError:
            pass
        finally:
            if self._approval_expiry_task is asyncio.current_task():
                self._approval_expiry_task = None

    async def cancel_all(self):
        jobs = list(self.jobs.values())
        self.jobs.clear()
        if self.approval_model is not None:
            self.approval_model._tools.clear_approval()
            self.approval_model = None
        if self._approval_expiry_task is not None:
            self._approval_expiry_task.cancel()
            await asyncio.gather(self._approval_expiry_task, return_exceptions=True)
            self._approval_expiry_task = None
        for job in jobs:
            job.task.cancel()
        await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)
        for job in jobs:
            job.task = None
            job.model = None
        self.changed.clear()

    def task_rows(self):
        return [{"id": str(job.id)[:8], "text": job.text,
                 "status": "finished" if job.reply is not None else "running"}
                for job in self.jobs.values()]

    async def cancel(self, prefix):
        matches = [job for job in self.jobs.values() if str(job.id).startswith(prefix)]
        if len(matches) != 1:
            return False
        job = matches[0]
        self.jobs.pop(job.id, None)
        task = job.task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        job.task = None
        job.model = None
        return True
