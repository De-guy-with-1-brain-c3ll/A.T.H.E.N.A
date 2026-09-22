"""Reading pictures, because some homework is only ever posted as a photo.

A Communication Journal post is often a photograph of the day's notes: the message
body is a bare ``<attachment>`` tag and the content is a PNG in the team's
SharePoint library. The conversation model has no vision, so the image has to be
turned into text before it can be reasoned about. Qwen-VL does that on the same
DashScope account the speech services already use.
"""
from __future__ import annotations

import base64
from urllib.parse import unquote, urlsplit

from openai import AsyncOpenAI

from athena.paths import data_directory


DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# The flash tier is plenty for reading handwriting and printed notes, and it is
# an order of magnitude cheaper than the plus tier.
DEFAULT_VISION_MODEL = "qwen3-vl-flash"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# Asking for the content rather than a description: the point is to make the
# post readable, not to admire the picture.
NOTES_PROMPT = (
    "This is a page from a student's school Communication Journal. Transcribe it "
    "faithfully and completely as plain text, keeping any headings, dates, "
    "homework items and deadlines. Do not summarise, do not add commentary, and "
    "do not describe the image. If part is illegible, write [illegible]."
)

SUFFIX_BY_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "heic": "image/heic",
}
IMAGE_SUFFIXES = frozenset(SUFFIX_BY_MIME)


def is_image_name(name: str) -> bool:
    return name.rsplit(".", 1)[-1].casefold() in IMAGE_SUFFIXES if "." in name else False


def mime_for(name: str) -> str:
    suffix = name.rsplit(".", 1)[-1].casefold() if "." in name else ""
    return SUFFIX_BY_MIME.get(suffix, "image/png")


def sharepoint_target(url: str) -> tuple[str, str, str] | None:
    """Split a SharePoint file URL into (host, site path, drive path).

    A reference attachment points at a URL that needs its own token. The same
    file is reachable through Graph, but only if the site and the path are pulled
    apart first.
    """
    parsed = urlsplit(url)
    if not parsed.hostname:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    lowered = [segment.casefold() for segment in segments]
    try:
        index = lowered.index("sites")
    except ValueError:
        return None
    if len(segments) < index + 3:
        return None
    site_path = "/" + "/".join(segments[index:index + 2])
    rest = segments[index + 2:]
    # "Shared Documents" is the default library, which Graph exposes as the root.
    if rest and rest[0].casefold() in {"shared documents", "documents"}:
        rest = rest[1:]
    if not rest:
        return None
    return parsed.hostname, site_path, "/" + "/".join(unquote(part) for part in rest)


class ImageReader:
    """Turn an image into text with Qwen-VL."""

    def __init__(self, api_key: str, model: str = DEFAULT_VISION_MODEL) -> None:
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=DASHSCOPE_BASE_URL)
        self.last_error: str | None = None

    async def read(self, data: bytes, mime: str, prompt: str = NOTES_PROMPT) -> str:
        if not data:
            raise ValueError("there were no image bytes to read")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"the image is {len(data) // 1024} KB, larger than the limit")
        encoded = base64.b64encode(data).decode("ascii")
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
            max_tokens=1500,
        )
        return (response.choices[0].message.content or "").strip()

    async def close(self) -> None:
        await self._client.close()


def vision_model() -> str:
    import os
    return os.environ.get("ATHENA_VISION_MODEL", "").strip() or DEFAULT_VISION_MODEL


def save_image(name: str, data: bytes) -> str:
    """Keep a copy so the same photo is only read once."""
    folder = data_directory() / "images"
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(character for character in name if character.isalnum() or character in "._-")
    path = folder / (safe or "image.png")
    path.write_bytes(data)
    return str(path)
