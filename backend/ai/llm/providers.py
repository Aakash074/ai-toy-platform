"""LLM adapters.

- openai_compat : any OpenAI-compatible /chat/completions endpoint. Covers Ollama (local, free),
                  LM Studio, Groq, OpenRouter, OpenAI, Together, vLLM... via LLM_BASE_URL + LLM_MODEL.
- anthropic     : Claude via the official Anthropic SDK (LLM_API_KEY, or ANTHROPIC_API_KEY).
- mock          : canned, personality-aware replies. Offline, for tests and wiring checks only.
"""
from __future__ import annotations

import logging
import random
import re

import httpx

from backend.ai.base import ChatMessage, LLMProvider, LLMResult, ProviderError

log = logging.getLogger("ai.llm")


class OpenAICompatLLM(LLMProvider):
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float):
        self.base_url, self.api_key, self.model, self.timeout_s = base_url.rstrip("/"), api_key, model, timeout_s

    async def chat(self, system: str, messages: list[ChatMessage], max_tokens: int) -> LLMResult:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
        except httpx.TimeoutException:
            raise ProviderError("llm", f"timed out after {self.timeout_s}s", retryable=True)
        except httpx.ConnectError:
            raise ProviderError("llm", f"cannot connect to {self.base_url} (is the LLM server running?)", retryable=True)
        except httpx.HTTPError as e:
            raise ProviderError("llm", f"network error: {e}", retryable=True)
        if r.status_code >= 400:
            raise ProviderError("llm", f"HTTP {r.status_code}: {r.text[:300]}",
                                retryable=r.status_code >= 500 or r.status_code == 429)
        try:
            data = r.json()
            choice = data["choices"][0]
            usage = data.get("usage") or {}
            return LLMResult(
                text=(choice["message"].get("content") or "").strip(),
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                model=data.get("model", self.model),
                finish_reason=choice.get("finish_reason") or "",
            )
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError("llm", f"unexpected response shape: {e}")


class AnthropicLLM(LLMProvider):
    """Claude through the official SDK. Uses low effort (short, fast, conversational replies)
    and server-side refusal fallbacks."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout_s: float):
        import anthropic

        self._anthropic = anthropic
        # api_key=None lets the SDK resolve ANTHROPIC_API_KEY / `ant auth login` profiles itself.
        self.client = anthropic.AsyncAnthropic(api_key=api_key or None, timeout=timeout_s, max_retries=1)
        self.model = model

    async def chat(self, system: str, messages: list[ChatMessage], max_tokens: int) -> LLMResult:
        a = self._anthropic
        # Server-side refusal fallbacks exist for Opus/Sonnet/Fable; Haiku rejects the field (400).
        extra = {} if "haiku" in self.model else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            resp = await self.client.beta.messages.create(
                model=self.model,
                # Thinking runs adaptively; reserve headroom on top of the spoken-reply budget.
                max_tokens=max(2048, max_tokens * 4),
                system=system,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                output_config={"effort": "low"},
                **extra,
            )
        except a.APITimeoutError:
            raise ProviderError("llm", "Claude request timed out", retryable=True)
        except a.RateLimitError:
            raise ProviderError("llm", "Claude rate limit hit", retryable=True)
        except a.AuthenticationError:
            raise ProviderError("llm", "Claude authentication failed (check LLM_API_KEY)")
        except a.APIStatusError as e:
            raise ProviderError("llm", f"Claude HTTP {e.status_code}: {e.message}", retryable=e.status_code >= 500)
        except a.APIConnectionError as e:
            raise ProviderError("llm", f"cannot reach Claude API: {e}", retryable=True)

        if resp.stop_reason == "refusal":
            return LLMResult(text="", model=resp.model, finish_reason="refusal")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return LLMResult(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            model=resp.model,
            finish_reason=resp.stop_reason or "",
        )


    async def stream_chat(self, system: str, messages: list[ChatMessage], max_tokens: int):
        """Yields text deltas as Claude writes them, then one final LLMResult (usage, stop reason).
        Streaming lets the toy start speaking the first sentence before the reply is finished.
        (No server-side fallbacks here: the stream must stay one model; our output guard still applies.)"""
        a = self._anthropic
        try:
            async with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=max(2048, max_tokens * 4),
                system=system,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                output_config={"effort": "low"},
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                final = await stream.get_final_message()
        except a.APITimeoutError:
            raise ProviderError("llm", "Claude request timed out", retryable=True)
        except a.RateLimitError:
            raise ProviderError("llm", "Claude rate limit hit", retryable=True)
        except a.AuthenticationError:
            raise ProviderError("llm", "Claude authentication failed (check LLM_API_KEY)")
        except a.APIStatusError as e:
            raise ProviderError("llm", f"Claude HTTP {e.status_code}: {e.message}", retryable=e.status_code >= 500)
        except a.APIConnectionError as e:
            raise ProviderError("llm", f"cannot reach Claude API: {e}", retryable=True)
        yield LLMResult(text="", input_tokens=final.usage.input_tokens, output_tokens=final.usage.output_tokens,
                        model=final.model, finish_reason=final.stop_reason or "")


class MockLLM(LLMProvider):
    """Deterministic-ish offline replies so the full pipeline can be exercised with no AI service."""

    name = "mock"
    REPLIES = {
        "teddy": ["Hi friend! I'm so happy to talk with you. What's your favourite animal?",
                  "Ooh, that sounds fun! Do you want to hear a little story about a brave bunny?"],
        "teacher": ["Great job! Let's count together: one, two, three. What number comes next?",
                    "Let's learn a letter! A is for apple. Can you think of another word that starts with A?"],
        "storyteller": ["Once upon a time, a tiny dragon found a glowing egg. Should the dragon open it, or hide it?",
                        "The dragon tiptoed into the forest. Should it follow the river, or climb the hill?"],
        "gappu_hi": ["पोम-पोम-पम! गप्पू आ गया! तुम्हारा पसंदीदा रंग कौन सा है?",
                     "वाह, कितना मज़ेदार! चलो, साथ में गिनती करें: एक, दो, तीन। आगे क्या आता है?"],
        "gappu_te": ["పోం-పోం-పం! గప్పు వచ్చేశాడు! నీకు ఇష్టమైన రంగు ఏది?",
                     "వావ్, భలే సరదాగా ఉంది! కలిసి లెక్కపెడదాం: ఒకటి, రెండు, మూడు. తర్వాత ఏది?"],
        "bheem_hi": ["नमस्ते दोस्त! मैं छोटा भीम हूँ। आज हम मिलकर कौन सा अच्छा काम करें?",
                     "वाह दोस्त! चलो लड्डू गिनते हैं: एक, दो, तीन। आगे कौन सा नंबर आता है?"],
        "bheem_te": ["నమస్తే మిత్రమా! నేను ఛోటా భీమ్. ఈరోజు మనం కలిసి ఏ మంచి పని చేద్దాం?",
                     "భలే మిత్రమా! లడ్డూలు లెక్కపెడదాం: ఒకటి, రెండు, మూడు. తర్వాత ఏది?"],
    }
    # Used when an unknown personality asks for a non-English language.
    LANG_DEFAULTS = {"hi": "gappu_hi", "te": "gappu_te"}

    async def chat(self, system: str, messages: list[ChatMessage], max_tokens: int) -> LLMResult:
        key = next((k for k in self.REPLIES if f"(id: {k})" in system), None)
        if key is None:
            m = re.search(r"language code '([A-Za-z]{2})", system)
            key = self.LANG_DEFAULTS.get(m.group(1).lower() if m else "", "teddy")
        text = "(mock) " + random.choice(self.REPLIES[key])
        return LLMResult(text=text, input_tokens=len(system) // 4, output_tokens=len(text) // 4, model="mock")


def build_llm(s) -> LLMProvider:
    p = s.llm_provider
    if p == "openai_compat":
        return OpenAICompatLLM(s.llm_base_url, s.llm_api_key, s.llm_model, s.llm_timeout_s)
    if p == "anthropic":
        model = s.llm_model if s.llm_model.startswith("claude") else "claude-opus-5-5"
        return AnthropicLLM(s.llm_api_key, model, s.llm_timeout_s)
    if p == "mock":
        return MockLLM()
    raise ValueError(f"Unknown LLM_PROVIDER '{p}' (use openai_compat | anthropic | mock)")
