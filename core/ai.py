import base64
import json
import mimetypes
from pathlib import Path

import httpx

from .models import AIExtractionProfile, AIProviderConfig


CANONICAL_FIELDS = [
    "member_no", "employee_no", "national_id", "full_name", "relationship",
    "date_of_birth", "gender", "plan_code", "annual_salary", "sum_assured",
    "effective_date",
]

NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_NUMBER = {"type": ["number", "string", "null"]}
MEMBER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "member_no": NULLABLE_STRING,
                    "employee_no": NULLABLE_STRING,
                    "national_id": NULLABLE_STRING,
                    "full_name": NULLABLE_STRING,
                    "relationship": {
                        "type": ["string", "null"],
                        "enum": ["Employee", "Spouse", "Child", None],
                    },
                    "date_of_birth": NULLABLE_STRING,
                    "gender": {
                        "type": ["string", "null"],
                        "enum": ["Male", "Female", None],
                    },
                    "plan_code": NULLABLE_STRING,
                    "annual_salary": NULLABLE_NUMBER,
                    "sum_assured": NULLABLE_NUMBER,
                    "effective_date": NULLABLE_STRING,
                    "_source_raw": {"type": ["object", "null"]},
                },
                "required": CANONICAL_FIELDS,
                "additionalProperties": True,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

DEFAULT_SYSTEM_PROMPT = (
    "You are a precise insurance data extraction engine. "
    "Use only information supported by the supplied source. Never invent missing values. "
    "Return strict JSON only."
)

DEFAULT_DOCUMENT_INSTRUCTIONS = (
    "Extract group insurance endorsement member data from the supplied document or image. "
    "Return one object per distinct member/dependent. Keep identifiers exactly as printed except surrounding whitespace. "
    "Normalize dates to YYYY-MM-DD only when unambiguous. Normalize gender to Male or Female when clearly stated. "
    "Normalize relationship to Employee, Spouse or Child only when supported by the source. "
    "For plan_code, copy the visible plan/category/class code or name and never substitute the member name. "
    "Unknown or unreadable values must be null."
)

DEFAULT_STRUCTURED_INSTRUCTIONS = (
    "Normalize spreadsheet or CSV source rows into the canonical member fields. "
    "Map synonymous headers without inventing values. Preserve the original source object in _source_raw."
)


class AIService:
    def __init__(self, config=None, product="", context=None):
        self.config = config or self._select_text_provider()
        self.product = product or ""
        self.context = context or {}
        self.last_profile = None

    @staticmethod
    def _select_text_provider(exclude_pk=None):
        qs = AIProviderConfig.objects.filter(is_active=True)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        return (
            qs.filter(supports_vision=False).order_by("-updated_at", "-pk").first()
            or qs.order_by("-updated_at", "-pk").first()
        )

    @staticmethod
    def _select_vision_provider():
        return AIProviderConfig.objects.filter(is_active=True, supports_vision=True).order_by("-updated_at", "-pk").first()

    @property
    def available(self):
        return bool(self.config)

    @property
    def last_profile_name(self):
        return self.last_profile.name if self.last_profile else "Built-in default"

    def _profile(self, task):
        qs = AIExtractionProfile.objects.filter(task=task, is_active=True).prefetch_related("examples")
        if self.product:
            profile = qs.filter(product=self.product).order_by("-priority", "name").first()
            if profile:
                return profile
        return qs.filter(product="").order_by("-priority", "name").first()

    def _prompt_bundle(self, task):
        profile = self._profile(task)
        self.last_profile = profile

        if task == AIExtractionProfile.Task.STRUCTURED_MAPPING:
            default_instructions = DEFAULT_STRUCTURED_INSTRUCTIONS
        else:
            default_instructions = DEFAULT_DOCUMENT_INSTRUCTIONS

        system_prompt = (profile.system_prompt.strip() if profile and profile.system_prompt.strip() else DEFAULT_SYSTEM_PROMPT)
        instructions = (profile.instructions.strip() if profile and profile.instructions.strip() else default_instructions)
        aliases = profile.field_aliases if profile and isinstance(profile.field_aliases, dict) else {}

        sections = [
            instructions,
            "Canonical output keys: " + ", ".join(CANONICAL_FIELDS) + ".",
            (
                "Output requirements: return a JSON array only; every row must contain the canonical keys; "
                "unknown values must be null; preserve source evidence in _source_raw; never calculate premium."
            ),
        ]

        if self.context:
            sections.append("Policy/workflow context:\n" + json.dumps(self.context, ensure_ascii=False, default=str))
        if aliases:
            sections.append("Configured field aliases/guidance:\n" + json.dumps(aliases, ensure_ascii=False, default=str))

        examples = []
        if profile:
            for example in profile.examples.filter(is_active=True).order_by("sort_order", "id")[:8]:
                examples.append({
                    "name": example.name,
                    "input": example.input_text,
                    "expected_output": example.expected_output,
                })
        if examples:
            sections.append(
                "Few-shot training examples. Follow their mapping style but never copy values into a different document:\n"
                + json.dumps(examples, ensure_ascii=False, default=str)
            )

        return system_prompt, "\n\n".join(sections)

    def extract_text_rows(self, text):
        self._require_provider()
        system, prompt = self._prompt_bundle(AIExtractionProfile.Task.DOCUMENT_EXTRACTION)
        prompt += "\n\nDOCUMENT TEXT:\n" + text[:60000]
        return self._parse_json_array(self._chat(prompt, system, response_schema=MEMBER_OUTPUT_SCHEMA))

    def normalize_structured_rows(self, rows):
        self._require_provider()
        system, prompt = self._prompt_bundle(AIExtractionProfile.Task.STRUCTURED_MAPPING)
        serializable = json.dumps(rows[:500], default=str, ensure_ascii=False)
        prompt += "\n\nSOURCE ROWS:\n" + serializable
        return self._parse_json_array(self._chat(prompt, system, response_schema=MEMBER_OUTPUT_SCHEMA))

    def extract_image_rows(self, path):
        path = Path(path)
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        return self.extract_image_bytes(path.read_bytes(), mime)

    def extract_image_bytes(self, content, mime="image/png"):
        self._require_provider(vision=True)
        encoded = base64.b64encode(content).decode("ascii")

        if self._uses_ocr_text_pipeline():
            ocr_text = self._vision_ocr_text(encoded, mime)
            if not (ocr_text or "").strip():
                raise ValueError("OCR model returned no readable text.")

            mapper_config = self._select_text_provider(exclude_pk=self.config.pk)
            if not mapper_config or mapper_config.supports_vision:
                raise RuntimeError(
                    "GLM-OCR read the document, but no separate active text AI provider is configured "
                    "to convert OCR text into strict JSON. Keep your normal Ollama text model active "
                    "(for example qwen2.5:7b) with Supports vision disabled."
                )

            mapper = AIService(config=mapper_config, product=self.product, context=self.context)
            rows = mapper.extract_text_rows(ocr_text)
            self.last_profile = mapper.last_profile
            return rows

        system, prompt = self._prompt_bundle(AIExtractionProfile.Task.DOCUMENT_EXTRACTION)
        prompt += (
            "\n\nRead the attached image carefully and extract the supported member data."
            "\nReturn exactly one valid JSON object with a top-level key \"items\"."
            "\nThe value of \"items\" must be an array of member objects using the canonical fields."
            "\nDo not add markdown, commentary, explanations, code fences, or text outside the JSON object."
        )
        return self._parse_json_array(
            self._vision(prompt, encoded, mime, system, response_schema=MEMBER_OUTPUT_SCHEMA)
        )

    def _uses_ocr_text_pipeline(self):
        options = self.config.options or {}
        mode = str(options.get("vision_pipeline") or "").strip().lower()
        if mode in {"direct", "direct_json"}:
            return False
        if mode in {"ocr_then_json", "ocr_text_then_json"}:
            return True
        return (
            self.config.provider == AIProviderConfig.Provider.OLLAMA
            and "glm-ocr" in (self.config.model_name or "").lower()
        )

    def _require_provider(self, vision=False):
        if vision:
            if not self.config or not self.config.supports_vision:
                vision_provider = self._select_vision_provider()
                if vision_provider:
                    self.config = vision_provider
            if not self.config:
                raise RuntimeError("No active AI provider is configured.")
            if not self.config.supports_vision:
                raise RuntimeError(
                    "No active vision-capable AI provider is configured. "
                    "Add or enable a vision model in Django Admin > AI provider configs and mark Supports vision."
                )
            return
        if not self.config:
            self.config = self._select_text_provider()
        if not self.config:
            raise RuntimeError("No active AI provider is configured.")

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            if self.config.provider == AIProviderConfig.Provider.ANTHROPIC:
                headers.update({"x-api-key": self.config.api_key, "anthropic-version": "2023-06-01"})
            else:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update((self.config.options or {}).get("headers", {}))
        return headers

    def _ollama_runtime_options(self, *, vision=False):
        configured = dict(self.config.options or {})
        configured.pop("headers", None)
        configured.pop("keep_alive", None)
        defaults = {
            "temperature": float(self.config.temperature),
        }
        if vision:
            defaults.update({
                "num_ctx": 4096,
                "num_gpu": -1,
                "num_predict": 1024,
            })
        defaults.update(configured)
        return defaults

    def _ollama_keep_alive(self):
        return (self.config.options or {}).get("keep_alive", "15m")

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

    def _chat(self, prompt, system_prompt="", response_schema=None):
        cfg = self.config
        base = self._base_url()
        if cfg.provider == AIProviderConfig.Provider.OLLAMA:
            url = base + "/api/chat"
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            payload = {
                "model": cfg.model_name,
                "stream": False,
                "messages": messages,
                "options": self._ollama_runtime_options(vision=False),
                "keep_alive": self._ollama_keep_alive(),
            }
            if response_schema:
                payload["format"] = response_schema
        elif cfg.provider == AIProviderConfig.Provider.ANTHROPIC:
            url = base + "/v1/messages"
            payload = {
                "model": cfg.model_name,
                "max_tokens": 8192,
                "temperature": float(cfg.temperature),
                "messages": [{"role": "user", "content": prompt}],
            }
            if system_prompt:
                payload["system"] = system_prompt
        else:
            url = base + "/v1/chat/completions"
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            payload = {
                "model": cfg.model_name,
                "temperature": float(cfg.temperature),
                "messages": messages,
            }

        with httpx.Client(timeout=cfg.timeout_seconds) as client:
            data = client.post(url, headers=self._headers(), json=payload).raise_for_status().json()
        if cfg.provider == AIProviderConfig.Provider.OLLAMA:
            return data["message"]["content"]
        if cfg.provider == AIProviderConfig.Provider.ANTHROPIC:
            return "".join(block.get("text", "") for block in data.get("content", []))
        return data["choices"][0]["message"]["content"]

    def _ollama_installed_models(self, client, base):
        try:
            response = client.get(base + "/api/tags", headers=self._headers())
            if response.is_success:
                return [
                    model.get("name") or model.get("model")
                    for model in response.json().get("models", [])
                    if model.get("name") or model.get("model")
                ]
        except Exception:
            pass
        return []

    def _ollama_response_json(self, response, client, base, endpoint):
        if not response.is_success:
            detail = response.text.strip()
            try:
                detail = response.json().get("error") or detail
            except Exception:
                pass
            installed = self._ollama_installed_models(client, base) if response.status_code == 404 else []
            model_note = f" Installed models: {', '.join(installed)}." if installed else ""
            raise RuntimeError(
                f"Ollama {endpoint} failed with HTTP {response.status_code} for model "
                f"'{self.config.model_name}'. {detail or response.reason_phrase}.{model_note}"
            )
        try:
            return response.json()
        except ValueError as exc:
            preview = (response.text or "")[:500].strip()
            raise RuntimeError(
                f"Ollama {endpoint} returned a non-JSON HTTP response for model "
                f"'{self.config.model_name}': {preview or '<empty response>'}"
            ) from exc

    def _vision_ocr_text(self, encoded, mime):
        cfg = self.config
        if cfg.provider != AIProviderConfig.Provider.OLLAMA:
            raise RuntimeError("The OCR-text pipeline currently requires an Ollama vision provider.")

        base = self._base_url()
        url = base + "/api/generate"
        payload = {
            "model": cfg.model_name,
            "stream": False,
            "prompt": (
                "Text Recognition: Transcribe all visible text from this document faithfully. "
                "Preserve labels, values, line breaks, and table row order. "
                "Do not summarize, infer, normalize, or return JSON. Output OCR text only."
            ),
            "images": [encoded],
            "options": self._ollama_runtime_options(vision=True),
            "keep_alive": self._ollama_keep_alive(),
        }
        with httpx.Client(timeout=cfg.timeout_seconds) as client:
            response = client.post(url, headers=self._headers(), json=payload)
            data = self._ollama_response_json(response, client, base, "/api/generate")
        return data.get("response", "")

    def _vision(self, prompt, encoded, mime, system_prompt="", response_schema=None):
        cfg = self.config
        base = self._base_url()
        if cfg.provider == AIProviderConfig.Provider.OLLAMA:
            # /api/generate is the most compatible multimodal endpoint for older
            # Ollama vision models such as BakLLaVA/LLaVA.
            url = base + "/api/generate"
            payload = {
                "model": cfg.model_name,
                "stream": False,
                "prompt": prompt,
                "images": [encoded],
                "format": response_schema or "json",
                "options": self._ollama_runtime_options(vision=True),
                "keep_alive": self._ollama_keep_alive(),
            }
            if system_prompt:
                payload["system"] = system_prompt

            with httpx.Client(timeout=cfg.timeout_seconds) as client:
                response = client.post(url, headers=self._headers(), json=payload)
                # Very old Ollama builds/models may reject JSON mode. Retry once
                # without format; the parser below still extracts embedded JSON.
                if response.status_code == 400 and "format" in (response.text or "").lower():
                    payload.pop("format", None)
                    response = client.post(url, headers=self._headers(), json=payload)
                data = self._ollama_response_json(response, client, base, "/api/generate")
            return data.get("response", "")

        if cfg.provider == AIProviderConfig.Provider.ANTHROPIC:
            url = base + "/v1/messages"
            payload = {
                "model": cfg.model_name,
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": encoded}},
                    {"type": "text", "text": prompt},
                ]}],
            }
            if system_prompt:
                payload["system"] = system_prompt
        else:
            url = base + "/v1/chat/completions"
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ]})
            payload = {
                "model": cfg.model_name,
                "temperature": float(cfg.temperature),
                "messages": messages,
            }

        with httpx.Client(timeout=cfg.timeout_seconds) as client:
            response = client.post(url, headers=self._headers(), json=payload)
            response.raise_for_status()
            data = response.json()
        if cfg.provider == AIProviderConfig.Provider.ANTHROPIC:
            return "".join(block.get("text", "") for block in data.get("content", []))
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _rows_from_json_value(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "members", "rows", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
            if any(key in data for key in CANONICAL_FIELDS):
                return [data]
        return None

    @staticmethod
    def _parse_json_array(content):
        content = (content or "").strip()
        if not content:
            raise ValueError("AI response was empty.")

        fence = "```"
        if content.startswith(fence + "json"):
            content = content[len(fence + "json"):].strip()
        elif content.startswith(fence):
            content = content[len(fence):].strip()
        if content.endswith(fence):
            content = content[:-len(fence)].strip()

        try:
            data = json.loads(content)
            rows = AIService._rows_from_json_value(data)
            if rows is not None:
                return rows
        except json.JSONDecodeError:
            pass

        # Recover the first complete JSON array/object from conversational text
        # or concatenated model output such as: {...}{...}.
        decoder = json.JSONDecoder()
        for index, char in enumerate(content):
            if char not in "[{":
                continue
            try:
                data, _ = decoder.raw_decode(content[index:])
            except json.JSONDecodeError:
                continue
            rows = AIService._rows_from_json_value(data)
            if rows is not None:
                return rows

        raise ValueError(
            "AI response did not contain a valid SmartEndorse JSON object/array. "
            "The OCR text may have been read successfully, but JSON mapping failed."
        )
