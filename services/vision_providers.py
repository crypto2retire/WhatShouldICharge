"""
Vision provider abstraction for WSIC estimation pipeline.
Supports Gemini (primary), Claude (fallback), and OpenRouter (last resort).
"""

import base64
import json
import os
import re
import logging
import time
from abc import ABC, abstractmethod

logger = logging.getLogger("wsic.vision")


class VisionResult:
    __slots__ = ("data", "provider_name", "model_used", "input_tokens", "output_tokens", "cost_cents", "raw_text", "latency_ms")

    def __init__(self, data: dict, provider_name: str, model_used: str,
                 input_tokens: int = 0, output_tokens: int = 0, cost_cents: int = 0, raw_text: str = ""):
        self.data = data
        self.provider_name = provider_name
        self.model_used = model_used
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_cents = cost_cents
        self.raw_text = raw_text
        self.latency_ms = 0


class VisionProviderError(Exception):
    def __init__(self, provider_name: str, message: str):
        self.provider_name = provider_name
        self.message = message
        self.model_name = ""
        self.latency_ms = 0
        super().__init__(f"{provider_name}: {message}")


class VisionProvider(ABC):
    @abstractmethod
    async def estimate(self, images: list, prompt: str) -> VisionResult:
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...


def parse_ai_json(raw_text: str) -> dict:
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    raw_text = raw_text.strip()
    raw_text = raw_text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass
    json_match = re.search(r'\{[\s\S]*\}', raw_text)
    candidate = json_match.group(0) if json_match else (raw_text if raw_text.startswith("{") else "")
    if not candidate:
        raise ValueError(f"Could not parse AI JSON response: {raw_text[:500]}")
    candidate = candidate.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    candidate = re.sub(r'(\d)"([A-Za-z(])', r'\1in\2', candidate)
    candidate = re.sub(r'(\d)"(\s)', r'\1in\2', candidate)
    candidate = re.sub(r'(\d)"$', r'\1in', candidate, flags=re.MULTILINE)
    candidate = re.sub(r',\s*([}\]])', r'\1', candidate)
    candidate = re.sub(r'[\x00-\x1f]', ' ', candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    open_brackets = candidate.count("[") - candidate.count("]")
    open_braces = candidate.count("{") - candidate.count("}")
    if open_brackets > 0 or open_braces > 0:
        patched = candidate + "]" * open_brackets + "}" * open_braces
        try:
            return json.loads(patched)
        except json.JSONDecodeError:
            pass
    last_complete_item = candidate.rfind("},")
    if last_complete_item > 0:
        truncated = candidate[:last_complete_item + 1]
        t_brackets = truncated.count("[") - truncated.count("]")
        t_braces = truncated.count("{") - truncated.count("}")
        for _ in range(t_brackets):
            truncated += "]"
        truncated += "}" * t_braces
        try:
            return json.loads(truncated)
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse AI JSON response: {raw_text[:500]}")


class GeminiProvider(VisionProvider):
    def __init__(self, model: str = "gemini-2.5-flash"):
        self._model = os.environ.get("GEMINI_MODEL", model)
        self._client = None

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return self._model

    def _get_client(self):
        if self._client is None:
            from google import genai
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise ValueError("GEMINI_API_KEY not configured")
            self._client = genai.Client(api_key=api_key)
        return self._client

    async def estimate(self, images: list, prompt: str) -> VisionResult:
        import asyncio
        try:
            started = time.perf_counter()
            client = self._get_client()
            contents = self._build_contents(images, prompt)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._sync_call, client, contents)
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result
        except Exception as e:
            err = VisionProviderError(self.name, str(e))
            err.model_name = self._model
            raise err from e

    def _sync_call(self, client, contents):
        from google.genai import types
        config = types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=8192,
            response_mime_type="application/json",
            system_instruction="You are an expert junk removal estimator. Return only valid JSON.",
        )
        response = client.models.generate_content(
            model=self._model,
            contents=contents,
            config=config,
        )
        raw_text = response.text or ""
        parsed = parse_ai_json(raw_text)
        usage = response.usage_metadata
        input_tokens = int(usage.prompt_token_count or 0) if usage else 0
        output_tokens = int(usage.candidates_token_count or 0) if usage else 0
        if "pro" in self._model.lower():
            cost_cents = int((input_tokens * 1.25 + output_tokens * 10.0) / 10_000)
        else:
            cost_cents = int((input_tokens * 0.15 + output_tokens * 0.60) / 10_000)
        return VisionResult(
            data=parsed, provider_name="gemini", model_used=self._model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_cents=cost_cents, raw_text=raw_text,
        )

    def _build_contents(self, images: list, prompt: str) -> list:
        from google.genai import types

        parts = []
        for block in images:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "").strip().lower()
            if btype == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    parts.append(types.Part.from_text(text=text))
            elif btype == "image":
                source = block.get("source") or {}
                data = str(source.get("data") or "").strip()
                media_type = str(source.get("media_type") or "image/jpeg")
                if data:
                    mime = media_type.split(";")[0].strip() or "image/jpeg"
                    image_bytes = base64.b64decode(data)
                    parts.append(types.Part.from_bytes(data=image_bytes, mime_type=mime))
        parts.append(types.Part.from_text(text=prompt))
        parts.append(types.Part.from_text(text="Analyze these photos and provide your estimate as JSON."))
        return [types.UserContent(parts=parts)]


class ClaudeProvider(VisionProvider):
    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        self._model = model
        self._client = None

    @property
    def name(self) -> str:
        return "claude"

    @property
    def model_name(self) -> str:
        return self._model

    def _get_client(self):
        if self._client is None:
            import anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not configured")
            self._client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
        return self._client

    async def estimate(self, images: list, prompt: str) -> VisionResult:
        import asyncio
        try:
            started = time.perf_counter()
            client = self._get_client()
            content = self._build_content(images)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._sync_call, client, prompt, content)
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result
        except Exception as e:
            err = VisionProviderError(self.name, str(e))
            err.model_name = self._model
            raise err from e

    def _sync_call(self, client, prompt, content):
        response = client.messages.create(
            model=self._model, max_tokens=8192, temperature=0,
            system=prompt, messages=[{"role": "user", "content": content}],
        )
        raw_text = "".join(getattr(b, "text", "") for b in response.content)
        parsed = parse_ai_json(raw_text)
        input_tokens = int(response.usage.input_tokens) if response.usage else 0
        output_tokens = int(response.usage.output_tokens) if response.usage else 0
        if "haiku" in self._model.lower():
            cost_cents = int((input_tokens * 1.0 + output_tokens * 5.0) / 10_000)
        else:
            cost_cents = int((input_tokens * 3.0 + output_tokens * 15.0) / 10_000)
        return VisionResult(
            data=parsed, provider_name="claude", model_used=self._model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_cents=cost_cents, raw_text=raw_text,
        )

    def _build_content(self, images: list) -> list:
        content = []
        for block in images:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "").strip().lower()
            if btype == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    content.append({"type": "text", "text": text})
            elif btype == "image":
                source = block.get("source") or {}
                data = source.get("data", "")
                media_type = str(source.get("media_type") or "image/jpeg")
                if data:
                    content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type.split(";")[0].strip() or "image/jpeg",
                            "data": data,
                        },
                    })
        content.append({"type": "text", "text": "Analyze these photos and provide your estimate as JSON."})
        return content


class OpenRouterProvider(VisionProvider):
    def __init__(self, model: str = "qwen/qwen2.5-vl-72b-instruct"):
        self._model = model

    @property
    def name(self) -> str:
        return "openrouter"

    @property
    def model_name(self) -> str:
        return self._model

    async def estimate(self, images: list, prompt: str) -> VisionResult:
        import httpx
        try:
            started = time.perf_counter()
            api_key = os.environ.get("OPENROUTER_API_KEY")
            if not api_key:
                raise ValueError("OPENROUTER_API_KEY not configured")
            content_blocks = []
            for block in images:
                if not isinstance(block, dict):
                    continue
                btype = str(block.get("type") or "").strip().lower()
                if btype == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        content_blocks.append({"type": "text", "text": text})
                elif btype == "image":
                    source = block.get("source") or {}
                    data = str(source.get("data") or "").strip()
                    media_type = str(source.get("media_type") or "image/jpeg").strip() or "image/jpeg"
                    if data:
                        content_blocks.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}})
            content_blocks.append({"type": "text", "text": "Analyze these photos and provide your estimate as JSON."})
            payload = {
                "model": self._model, "temperature": 0, "max_tokens": 8192,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": content_blocks},
                ],
                "provider": {"sort": "throughput", "preferred_max_latency": {"p90": 60}},
            }
            headers = {
                "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                "HTTP-Referer": "https://whatshouldicharge.app", "X-Title": "WSIC Estimate",
            }
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
                if response.status_code >= 400:
                    raise RuntimeError(f"OpenRouter {response.status_code}: {response.text[:300]}")
                data = response.json()
            choice = (((data.get("choices") or [{}])[0]).get("message") or {})
            raw_text = choice.get("content") or ""
            parsed = parse_ai_json(raw_text)
            usage = data.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens", 0) or 0)
            output_tokens = int(usage.get("completion_tokens", 0) or 0)
            cost_cents = int((input_tokens * 0.08 + output_tokens * 0.08) / 1_000)
            result = VisionResult(
                data=parsed, provider_name="openrouter", model_used=str(data.get("model") or self._model),
                input_tokens=input_tokens, output_tokens=output_tokens,
                cost_cents=cost_cents, raw_text=raw_text,
            )
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result
        except Exception as e:
            err = VisionProviderError(self.name, str(e))
            err.model_name = self._model
            raise err from e


def get_provider(provider_name: str = "gemini") -> VisionProvider:
    if provider_name == "gemini":
        return GeminiProvider()
    elif provider_name == "claude":
        return ClaudeProvider()
    elif provider_name == "openrouter":
        return OpenRouterProvider()
    raise ValueError(f"Unknown provider: {provider_name}")
