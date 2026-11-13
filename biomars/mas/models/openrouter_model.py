"""
OpenRouter Model Adapter for InternAgent

Wraps OpenRouter via OpenAI-compatible AsyncOpenAI client using base_url.
"""

import logging
import os
import json
import re
from typing import Optional, List, Dict, Any

from openai import AsyncOpenAI
from .base_model import BaseModel, ModelResponseError

logger = logging.getLogger(__name__)

class OpenRouterModel(BaseModel):
    def __init__(self,
                 api_key: Optional[str] = None,
                 base_url: Optional[str] = None,
                 model_name: str = "openai/gpt-4o",
                 max_tokens: int = 2048,
                 temperature: float = 0.3,
                 timeout: int = 60,
                 reasoning_effort: Optional[str] = None):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.base_url = base_url or os.environ.get("OPENROUTER_BASE_URL")
        self.model_name = model_name or os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.reasoning_effort = (reasoning_effort if reasoning_effort is not None else
                                 os.environ.get("OPENROUTER_REASONING_EFFORT", "")).strip()
        self.exclude_reasoning = os.environ.get(
            "OPENROUTER_EXCLUDE_REASONING", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not self.api_key or not self.base_url:
            logger.warning("OpenRouter API key/base_url not provided.")
        try:
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
        except Exception as e:
            logger.error(f"Failed to init OpenRouter client: {e}")
            self.client = None

    def _request_options(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        options = dict(kwargs or {})
        extra_body = dict(options.pop("extra_body", {}) or {})
        reasoning = dict(extra_body.get("reasoning", {}) or {})
        if self.reasoning_effort:
            reasoning["effort"] = self.reasoning_effort
        if self.exclude_reasoning:
            reasoning["exclude"] = True
        if reasoning:
            extra_body["reasoning"] = reasoning
        if extra_body:
            options["extra_body"] = extra_body
        return options

    @staticmethod
    def _clean_response_text(content: Any) -> str:
        text = str(content or "").strip()
        if "</think>" in text:
            text = text.split("</think>", 1)[1].strip()
        return text

    async def generate(self,
                       prompt: str,
                       system_prompt: Optional[str] = None,
                       temperature: Optional[float] = None,
                       max_tokens: Optional[int] = None,
                       stop_sequences: Optional[List[str]] = None,
                       **kwargs) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = await self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
                stop=stop_sequences,
                **self._request_options(kwargs),
            )
            return self._clean_response_text(resp.choices[0].message.content)
        except Exception as e:
            logger.error(f"OpenRouter generate error: {e}")
            raise

    async def generate_json(self,
                            prompt: str,
                            schema: Dict[str, Any],
                            system_prompt: Optional[str] = None,
                            temperature: Optional[float] = None,
                            **kwargs) -> Dict[str, Any]:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        try:
            structured_output = kwargs.pop("structured_output", False)
            request_options = self._request_options(kwargs)
            if structured_output:
                request_options.setdefault("response_format", {"type": "json_schema", "json_schema": {
                    "name": schema.get("title", "response"), "strict": True, "schema": schema}})
                extra_body = request_options.setdefault("extra_body", {})
                extra_body["provider"] = dict(extra_body.get("provider", {}), require_parameters=True)
            else:
                request_options.setdefault("response_format", {"type": "json_object"})
            if (request_options.get("response_format", {}).get("type") == "json_object"
                    and not any("json" in message["content"].lower() for message in messages)):
                messages.insert(0, {"role": "system", "content":
                    "Return a JSON object matching this output schema:\n" +
                    json.dumps(schema, ensure_ascii=False)})
            resp = await self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=self.max_tokens,
                **request_options,
            )
            raw_content = (
                resp.choices[0].message.content
                if resp and resp.choices and resp.choices[0].message
                else ""
            )
            usage = getattr(resp, "usage", None)
            self.last_generation_metadata = {
                "response_model": getattr(resp, "model", None),
                "finish_reason": getattr(resp.choices[0], "finish_reason", None) if resp and resp.choices else None,
                "usage": usage.model_dump() if hasattr(usage, "model_dump") else None,
            }
            txt = self._clean_response_text(raw_content)
            txt = re.sub(r"^```(?:json)?\s*", "", txt, flags=re.IGNORECASE)
            txt = re.sub(r"\s*```$", "", txt)
            try:
                return json.loads(txt)
            except (ValueError, TypeError) as error:
                start = txt.find('{')
                end = txt.rfind('}')
                if start != -1 and end != -1 and end > start:
                    try:
                        return json.loads(txt[start:end+1])
                    except ValueError:
                        pass
                usage = getattr(resp, "usage", None)
                raise ModelResponseError(f"Model returned invalid JSON: {error}", txt,
                    {"finish_reason": getattr(resp.choices[0], "finish_reason", None),
                     "usage": usage.model_dump() if hasattr(usage, "model_dump") else None}) from error
        except ModelResponseError:
            raise
        except Exception as e:
            logger.warning(f"OpenRouter generate_json warning: {e}")
            # A transport/provider failure is not a model response. Preserve it
            # for the caller's request retry policy, never JSON-format repair.
            raise

    async def embed(self, text):
        if isinstance(text, str):
            return []
        return [[] for _ in (text or [])]

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'OpenRouterModel':
        return cls(
            api_key=config.get("api_key"),
            base_url=config.get("base_url"),
            model_name=config.get("model_name", os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o")),
            max_tokens=config.get("max_tokens", 2048),
            temperature=config.get("temperature", 0.3),
            timeout=config.get("timeout", 60),
            reasoning_effort=config.get("reasoning_effort")
        )
