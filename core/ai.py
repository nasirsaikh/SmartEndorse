import base64
import json
import mimetypes
from pathlib import Path

import httpx
from .models import AIProviderConfig


EXTRACTION_PROMPT = """You extract group insurance endorsement member data.
Return JSON only as an array of objects. Never calculate premium.
Use only these keys when present:
member_no, employee_no, national_id, full_name, relationship, date_of_birth,
gender, plan_code, annual_salary, sum_assured, effective_date.
Dates must be YYYY-MM-DD. Unknown values must be null. Do not invent data.
Input:
"""


class AIService:
    def __init__(self, config=None):
        self.config = config or AIProviderConfig.objects.filter(is_active=True).first()

    @property
    def available(self):
        return bool(self.config)

    def extract_text_rows(self, text):
        if not self.config:
            raise RuntimeError("No active AI provider is configured.")
        content = self._chat(EXTRACTION_PROMPT + text[:50000])
        return self._parse_json_array(content)

    def extract_image_rows(self, path):
        if not self.config:
            raise RuntimeError("No active AI provider is configured.")
        if not self.config.supports_vision:
            raise RuntimeError(f"Active AI provider {self.config.name} is not marked as vision-capable.")
        path = Path(path)
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        content = self._vision(EXTRACTION_PROMPT, encoded, mime)
        return self._parse_json_array(content)

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            if self.config.provider == AIProviderConfig.Provider.ANTHROPIC:
                headers.update({"x-api-key": self.config.api_key, "anthropic-version": "2023-06-01"})
            else:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.options.get("headers", {}))
        return headers

    def _base_url(self):
        if self.config.base_url:
            return self.config.base_url.rstrip("/")
        if self.config.provider == AIProviderConfig.Provider.OLLAMA:
            return "http://127.0.0.1:11434"
        if self.config.provider == AIProviderConfig.Provider.OPENAI:
            return "https://api.openai.com"
        if self.config.provider == AIProviderConfig.Provider.ANTHROPIC:
            return "https://api.anthropic.com"
        raise RuntimeError("base_url is required for OpenAI-compatible providers.")

    def _chat(self, prompt):
        cfg = self.config
        base = self._base_url()
        if cfg.provider == AIProviderConfig.Provider.OLLAMA:
            url = base + "/api/chat"
            payload = {"model": cfg.model_name, "stream": False, "messages": [{"role": "user", "content": prompt}], "options": {"temperature": float(cfg.temperature)}}
        elif cfg.provider == AIProviderConfig.Provider.ANTHROPIC:
            url = base + "/v1/messages"
            payload = {"model": cfg.model_name, "max_tokens": 4096, "temperature": float(cfg.temperature), "messages": [{"role": "user", "content": prompt}]}
        else:
            url = base + "/v1/chat/completions"
            payload = {"model": cfg.model_name, "temperature": float(cfg.temperature), "messages": [{"role": "system", "content": "Return strict JSON only."}, {"role": "user", "content": prompt}]}
        with httpx.Client(timeout=cfg.timeout_seconds) as client:
            data = client.post(url, headers=self._headers(), json=payload).raise_for_status().json()
        if cfg.provider == AIProviderConfig.Provider.OLLAMA:
            return data["message"]["content"]
        if cfg.provider == AIProviderConfig.Provider.ANTHROPIC:
            return "".join(block.get("text", "") for block in data.get("content", []))
        return data["choices"][0]["message"]["content"]

    def _vision(self, prompt, encoded, mime):
        cfg = self.config
        base = self._base_url()
        if cfg.provider == AIProviderConfig.Provider.OLLAMA:
            url = base + "/api/chat"
            payload = {"model": cfg.model_name, "stream": False, "messages": [{"role": "user", "content": prompt, "images": [encoded]}]}
        elif cfg.provider == AIProviderConfig.Provider.ANTHROPIC:
            url = base + "/v1/messages"
            payload = {
                "model": cfg.model_name,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": encoded}},
                    {"type": "text", "text": prompt},
                ]}],
            }
        else:
            url = base + "/v1/chat/completions"
            payload = {
                "model": cfg.model_name,
                "temperature": float(cfg.temperature),
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ]}],
            }
        with httpx.Client(timeout=cfg.timeout_seconds) as client:
            data = client.post(url, headers=self._headers(), json=payload).raise_for_status().json()
        if cfg.provider == AIProviderConfig.Provider.OLLAMA:
            return data["message"]["content"]
        if cfg.provider == AIProviderConfig.Provider.ANTHROPIC:
            return "".join(block.get("text", "") for block in data.get("content", []))
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_json_array(content):
        fence = chr(96) * 3
        content = content.strip()
        if content.startswith(fence + "json"):
            content = content[len(fence + "json"):].strip()
        elif content.startswith(fence):
            content = content[len(fence):].strip()
        if content.endswith(fence):
            content = content[:-len(fence)].strip()
        data = json.loads(content)
        if isinstance(data, dict):
            for key in ("items", "members", "rows", "data"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise ValueError("AI response is not a JSON array.")
        return data
