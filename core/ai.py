import base64
import json
import io
import mimetypes
import re
from datetime import date as dt_date
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

PASSPORT_FIELD_ALIASES = {
    "surname": ["Surname", "Family Name", "Last Name"],
    "given_names": ["Given Name(s)", "Given Names", "Given Name", "First Name(s)", "First Names"],
    "passport_no": ["Passport No.", "Passport No", "Passport Number", "Document No.", "Document Number"],
    "nationality": ["Nationality"],
}

FORM_FIELD_ALIASES = {
    "member_no": ["Member No", "Member Number", "Member ID", "Membership No", "Card No"],
    "employee_no": ["Employee No", "Employee Number", "Employee ID", "Emp No", "Emp ID", "Staff No", "Staff ID"],
    "national_id": ["Civil ID", "Civil Number", "National ID", "Resident ID", "ID No", "Identification No"],
    "full_name": ["Member Name", "Insured Name", "Employee Name", "Full Name", "Name"],
    "relationship": ["Relationship", "Relation", "Dependent Type"],
    "date_of_birth": ["Date of Birth", "DOB", "Birth Date"],
    "gender": ["Gender", "Sex"],
    "plan_code": ["Plan Code", "Benefit Plan", "Medical Class", "Benefit Class", "Category", "Class", "Plan"],
    "sum_assured": ["Sum Assured", "Sum Insured", "Coverage Amount"],
    "effective_date": ["Effective Date", "Addition Date", "Deletion Date", "Endorsement Date"],
}



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
    def _select_vision_provider(exclude_pk=None, exclude_glm_ocr=False):
        qs = AIProviderConfig.objects.filter(is_active=True, supports_vision=True)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        if exclude_glm_ocr:
            qs = qs.exclude(model_name__icontains="glm-ocr")
        return qs.order_by("-updated_at", "-pk").first()

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

    def _form_aliases(self):
        aliases = {key: list(values) for key, values in FORM_FIELD_ALIASES.items()}
        profile = self._profile(AIExtractionProfile.Task.DOCUMENT_EXTRACTION)
        if profile and isinstance(profile.field_aliases, dict):
            for key, values in profile.field_aliases.items():
                if key not in aliases:
                    continue
                if isinstance(values, str):
                    values = [values]
                if isinstance(values, list):
                    configured = [str(value).strip() for value in values if str(value).strip()]
                    aliases[key] = configured + [value for value in aliases[key] if value not in configured]
        return aliases

    @staticmethod
    def _clean_ocr_value(value):
        value = str(value or "").strip().strip("|").strip()
        value = re.sub(r"^[\-–—:;=]+\s*", "", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip(" \t|;,")

    def _find_labeled_value(self, text, aliases, all_aliases):
        lines = [line.strip() for line in str(text or "").replace("\r", "\n").split("\n")]
        lines = [line for line in lines if line]
        next_label_pattern = "|".join(
            re.escape(alias)
            for alias in sorted(all_aliases, key=len, reverse=True)
            if alias
        )

        for alias in aliases:
            escaped = re.escape(alias)
            inline = re.compile(
                rf"(?i)(?:^|[|;])\s*{escaped}\s*(?::|=|\-|–|—|\|)\s*(.+)$"
            )
            loose = re.compile(rf"(?i)^\s*{escaped}\s{{2,}}(.+)$")
            label_only = re.compile(rf"(?i)^\s*{escaped}\s*[:=\-–—|]?\s*$")

            for index, line in enumerate(lines):
                match = inline.search(line) or loose.search(line)
                if match:
                    value = match.group(1)
                    if next_label_pattern:
                        value = re.split(
                            rf"(?i)\s+(?={next_label_pattern}\s*(?::|=|\-|–|—|\|))",
                            value,
                            maxsplit=1,
                        )[0]
                    value = self._clean_ocr_value(value)
                    if value:
                        return value

                if label_only.match(line) and index + 1 < len(lines):
                    value = self._clean_ocr_value(lines[index + 1])
                    if value and not any(
                        re.match(rf"(?i)^\s*{re.escape(other)}\s*[:=\-–—|]?", value)
                        for other in all_aliases
                    ):
                        return value

            match = re.search(
                rf"(?i)\b{escaped}\b\s*(?::|=|\-|–|—)\s*([^\n|;]+)",
                text,
            )
            if match:
                value = self._clean_ocr_value(match.group(1))
                if value:
                    return value
        return None

    def _resolve_form_plan(self, value, text):
        valid_plans = self.context.get("valid_plans") if isinstance(self.context, dict) else []
        if not isinstance(valid_plans, list) or not valid_plans:
            return value

        candidate = self._clean_ocr_value(value).lower() if value else ""
        for plan in valid_plans:
            code = str(plan.get("code") or "").strip()
            name = str(plan.get("name") or "").strip()
            values = [part for part in (code, name) if part]
            for part in values:
                normalized = part.lower()
                if candidate and (
                    candidate == normalized
                    or normalized in candidate
                    or candidate in normalized
                ):
                    return code or name

        source = str(text or "").lower()
        candidates = []
        for plan in valid_plans:
            code = str(plan.get("code") or "").strip()
            name = str(plan.get("name") or "").strip()
            if code:
                candidates.append((len(code), code, code))
            if name:
                candidates.append((len(name), name, code or name))
        for _, token, resolved in sorted(candidates, reverse=True):
            if re.search(rf"(?<![\w]){re.escape(token.lower())}(?![\w])", source):
                return resolved
        return value

    @staticmethod
    def _mrz_date(value):
        value = re.sub(r"[^0-9]", "", str(value or ""))[:6]
        if len(value) != 6:
            return None
        try:
            yy, mm, dd = int(value[:2]), int(value[2:4]), int(value[4:6])
            current_yy = dt_date.today().year % 100
            year = 2000 + yy if yy <= current_yy else 1900 + yy
            parsed = dt_date(year, mm, dd)
            return parsed.isoformat()
        except ValueError:
            return None

    @staticmethod
    def _clean_person_name(value):
        value = str(value or "").replace("<", " ")
        value = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ'\- ]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value.title() if value else None

    def _passport_fields_from_mrz(self, text):
        lines = []
        for raw in str(text or "").splitlines():
            raw = re.sub(r"(?i)^\s*MRZ\s*[12]?\s*[:=\-]?\s*", "", raw)
            compact = re.sub(r"\s+", "", raw.upper())
            if len(compact) >= 30 and "<" in compact:
                lines.append(compact)

        mrz1 = next((line for line in lines if line.startswith("P<") and len(line) >= 30), None)
        mrz2 = None
        if mrz1:
            idx = lines.index(mrz1)
            if idx + 1 < len(lines):
                mrz2 = lines[idx + 1]

        if not mrz1:
            match = re.search(r"(P<[A-Z0-9<]{28,})", str(text or "").upper().replace(" ", ""))
            mrz1 = match.group(1) if match else None
        if mrz1 and not mrz2:
            source = str(text or "").upper()
            first_pos = source.find("P<")
            tail = source[first_pos:].splitlines() if first_pos >= 0 else []
            compact_tail = [re.sub(r"\s+", "", line) for line in tail if line.strip()]
            if len(compact_tail) >= 2:
                mrz2 = compact_tail[1]

        result = {}
        if mrz1:
            name_part = mrz1[5:] if len(mrz1) > 5 else ""
            if "<<" in name_part:
                surname_raw, given_raw = name_part.split("<<", 1)
                surname = self._clean_person_name(surname_raw)
                given = self._clean_person_name(given_raw)
                if surname:
                    result["surname"] = surname
                if given:
                    result["given_names"] = given
                if surname or given:
                    result["full_name"] = " ".join(part for part in (given, surname) if part).strip()

        if mrz2:
            mrz2 = re.sub(r"\s+", "", mrz2)
            if len(mrz2) >= 28:
                passport_no = mrz2[:9].replace("<", "").strip()
                nationality = mrz2[10:13].replace("<", "").strip()
                dob = self._mrz_date(mrz2[13:19])
                sex = mrz2[20:21].replace("<", "").strip()
                if passport_no:
                    result["passport_no"] = passport_no
                if nationality:
                    result["nationality"] = nationality
                if dob:
                    result["date_of_birth"] = dob
                if sex in {"M", "F"}:
                    result["gender"] = "Male" if sex == "M" else "Female"

        return result

    def _passport_fields_from_labels(self, text):
        all_aliases = [
            alias
            for aliases in [*PASSPORT_FIELD_ALIASES.values(), *self._form_aliases().values()]
            for alias in aliases
        ]
        result = {}
        for field, aliases in PASSPORT_FIELD_ALIASES.items():
            value = self._find_labeled_value(text, aliases, all_aliases)
            if not value:
                for alias in aliases:
                    match = re.search(
                        rf"(?im)^\s*{re.escape(alias)}\s+([^\n|]{{2,80}})$",
                        str(text or ""),
                    )
                    if match:
                        value = self._clean_ocr_value(match.group(1))
                        break
            if value:
                result[field] = value

        surname = self._clean_person_name(result.get("surname"))
        given_names = self._clean_person_name(result.get("given_names"))
        if surname or given_names:
            result["full_name"] = " ".join(
                part for part in (given_names, surname) if part
            ).strip()
        return result

    @staticmethod
    def _rotate_image_bytes(content, degrees):
        try:
            from PIL import Image
            image = Image.open(io.BytesIO(content))
            rotated = image.rotate(degrees, expand=True)
            output = io.BytesIO()
            fmt = "PNG" if (image.format or "").upper() not in {"JPEG", "JPG", "WEBP"} else image.format
            rotated.save(output, format=fmt or "PNG")
            return output.getvalue(), ("image/png" if (fmt or "PNG").upper() == "PNG" else f"image/{str(fmt).lower()}")
        except Exception:
            return None, None

    def extract_form_fields_from_text(self, text):
        text = str(text or "").strip()
        if not text:
            raise ValueError("OCR returned no readable text.")

        aliases = self._form_aliases()
        all_aliases = [alias for values in aliases.values() for alias in values]
        row = {key: None for key in CANONICAL_FIELDS}

        for field, field_aliases in aliases.items():
            row[field] = self._find_labeled_value(text, field_aliases, all_aliases)

        passport = self._passport_fields_from_labels(text)
        mrz = self._passport_fields_from_mrz(text)
        passport = {**mrz, **{key: value for key, value in passport.items() if value not in (None, "")}}

        if not row.get("full_name") and passport.get("full_name"):
            row["full_name"] = passport["full_name"]
        if not row.get("date_of_birth") and passport.get("date_of_birth"):
            row["date_of_birth"] = passport["date_of_birth"]
        if not row.get("gender") and passport.get("gender"):
            row["gender"] = passport["gender"]

        row["plan_code"] = self._resolve_form_plan(row.get("plan_code"), text)
        row["_source_raw"] = {
            "ocr_text": text[:12000],
            "extraction_mode": "direct_form_fill",
            "document_type": "passport" if passport else "member_document",
            "passport": passport,
        }

        meaningful = {
            key: value
            for key, value in row.items()
            if key != "_source_raw" and value not in (None, "")
        }
        if not meaningful:
            raise ValueError(
                "OCR completed, but SmartEndorse could not find recognizable member/passport fields. "
                "The scan may be rotated or the OCR output may not contain field labels."
            )
        return row

    def extract_form_fields_from_image_bytes(self, content, mime="image/png"):
        self._require_provider(vision=True)

        def run_ocr(image_bytes, image_mime):
            encoded = base64.b64encode(image_bytes).decode("ascii")
            if (
                self.config.provider == AIProviderConfig.Provider.OLLAMA
                and "glm-ocr" in (self.config.model_name or "").lower()
            ):
                ocr_text = self._vision_form_text(encoded, image_mime)
                return self.extract_form_fields_from_text(ocr_text)
            rows = self.extract_image_bytes(image_bytes, image_mime)
            if not rows:
                raise ValueError("Vision model returned no member data.")
            return rows[0]

        try:
            try:
                return run_ocr(content, mime)
            except ValueError as first_error:
                rotated, rotated_mime = self._rotate_image_bytes(content, -90)
                if rotated:
                    try:
                        result = run_ocr(rotated, rotated_mime or mime)
                        result.setdefault("_source_raw", {})
                        if isinstance(result["_source_raw"], dict):
                            result["_source_raw"]["rotation_retry"] = "90_clockwise"
                        return result
                    except ValueError:
                        pass
                raise first_error
        except RuntimeError as exc:
            if self._is_glm_ollama_repeat_error(exc):
                fallback_config = self._select_vision_provider(
                    exclude_pk=self.config.pk,
                    exclude_glm_ocr=True,
                )
                if fallback_config:
                    fallback = AIService(
                        config=fallback_config,
                        product=self.product,
                        context=self.context,
                    )
                    rows = fallback.extract_image_bytes(content, mime)
                    if rows:
                        return rows[0]
            raise

    def extract_image_rows(self, path):
        path = Path(path)
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        return self.extract_image_bytes(path.read_bytes(), mime)

    def extract_image_bytes(self, content, mime="image/png"):
        self._require_provider(vision=True)
        encoded = base64.b64encode(content).decode("ascii")

        if self._uses_ocr_text_pipeline():
            try:
                ocr_text = self._vision_ocr_text(encoded, mime)
            except RuntimeError as exc:
                if self._is_glm_ollama_repeat_error(exc):
                    fallback_config = self._select_vision_provider(
                        exclude_pk=self.config.pk,
                        exclude_glm_ocr=True,
                    )
                    if fallback_config:
                        fallback = AIService(
                            config=fallback_config,
                            product=self.product,
                            context=self.context,
                        )
                        rows = fallback.extract_image_bytes(content, mime)
                        self.last_profile = fallback.last_profile
                        return rows
                    raise RuntimeError(
                        "GLM-OCR hit the known Ollama token-repeat regression. "
                        "Ollama 0.34.1+ can return HTTP 500 'prediction aborted, token repeat limit reached' "
                        "for GLM-OCR even when the document was read correctly. "
                        "Use Ollama 0.34.0 for GLM-OCR or configure another active non-GLM vision provider "
                        "as an OCR fallback."
                    ) from exc
                raise
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

    def _is_glm_ollama_repeat_error(self, exc):
        return (
            self.config.provider == AIProviderConfig.Provider.OLLAMA
            and "glm-ocr" in (self.config.model_name or "").lower()
            and "token repeat limit reached" in str(exc).lower()
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
        configured.pop("vision_pipeline", None)
        defaults = {
            "temperature": float(self.config.temperature),
        }
        if vision:
            is_glm_ocr = "glm-ocr" in (self.config.model_name or "").lower()
            defaults.update({
                "num_ctx": 8192 if is_glm_ocr else 4096,
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

    def _vision_form_text(self, encoded, mime):
        """
        Fast OCR specifically for the member correction form.

        GLM-OCR is asked for short labelled text only. We intentionally avoid
        JSON/schema generation here because the form parser consumes the labels
        directly.
        """
        cfg = self.config
        if cfg.provider != AIProviderConfig.Provider.OLLAMA:
            raise RuntimeError("Direct OCR form filling currently requires an Ollama vision provider.")

        base = self._base_url()
        url = base + "/api/chat"
        options = self._ollama_runtime_options(vision=True)
        options["num_ctx"] = min(int(options.get("num_ctx") or 4096), 4096)
        options["num_predict"] = min(int(options.get("num_predict") or 256), 256)
        options["temperature"] = 0.0

        prompt = (
            "Text Recognition: Read the visible identity/member information in this document. "
            "The document may be an insurance member form, ID card, or passport and may be rotated. "
            "Return short plain-text labelled lines only for fields that are visible. "
            "For insurance/member documents use: Member No, Employee No, Civil ID, Full Name, "
            "Relationship, Date of Birth, Gender, Plan, Effective Date. "
            "For passports also include: Surname, Given Names, Passport No, Nationality, Date of Birth, Sex. "
            "If an MRZ is visible, include exactly two lines labelled MRZ1 and MRZ2. "
            "Do not explain, do not summarize, do not repeat text, and do not return JSON."
        )
        payload = {
            "model": cfg.model_name,
            "stream": False,
            "messages": [{"role": "user", "content": prompt, "images": [encoded]}],
            "options": options,
            "keep_alive": self._ollama_keep_alive(),
        }

        with httpx.Client(timeout=cfg.timeout_seconds) as client:
            response = client.post(url, headers=self._headers(), json=payload)
            data = self._ollama_response_json(response, client, base, "/api/chat")

        return (data.get("message") or {}).get("content", "")

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
