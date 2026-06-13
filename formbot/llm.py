import json
import logging
import re

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None

CANONICAL_KEYS = [
    "full_name",
    "first_name",
    "last_name",
    "email",
    "phone",
    "street_address",
    "city",
    "state_province",
    "postal_code",
    "country",
    "company",
    "job_title",
    "age",
    "gender",
    "dietary_restrictions",
    "t_shirt_size",
    "emergency_contact_name",
    "emergency_contact_phone",
    "date_of_birth",
    "website_url",
    "message",
    "other",
]

_CANONICAL_SET = set(CANONICAL_KEYS)
_HEURISTIC_PATTERNS = [
    (re.compile(r"\b(first|given)\s*name\b", re.I), "first_name"),
    (re.compile(r"\b(last|family|surname)\s*name\b|\bsurname\b", re.I), "last_name"),
    (re.compile(r"\b(full\s*)?name\b|\bfio\b|\bфио\b|\bаты[- ]?жөні\b", re.I), "full_name"),
    (re.compile(r"\be-?mail\b|\bemail\s*address\b|почт", re.I), "email"),
    (re.compile(r"\b(phone|mobile|tel|telephone|whatsapp)\b|телефон", re.I), "phone"),
    (re.compile(r"\b(age|your age)\b|возраст|жасыңыз|жасы\b", re.I), "age"),
    (re.compile(r"\b(gender|sex)\b|пол\b|жыныс", re.I), "gender"),
    (re.compile(r"\b(date\s*of\s*birth|birth\s*date|birthday|dob)\b|дата рождения|туған күн", re.I), "date_of_birth"),
    (re.compile(r"\b(company|organization|organisation|employer)\b|компан|ұйым", re.I), "company"),
    (re.compile(r"\b(job\s*title|position|role|occupation)\b|должност|лауазым", re.I), "job_title"),
    (re.compile(r"\b(country)\b|страна|еліңіз|мемлекет", re.I), "country"),
    (re.compile(r"\b(city|town)\b|город|қала", re.I), "city"),
    (re.compile(r"\b(zip|postal\s*code|postcode)\b|индекс", re.I), "postal_code"),
    (re.compile(r"\b(address|street)\b|адрес|мекен", re.I), "street_address"),
    (re.compile(r"\b(website|url|link)\b|сайт", re.I), "website_url"),
]

_MAP_SYSTEM = (
    "You map HTML form field labels to canonical profile keys. "
    f"Canonical keys: {', '.join(CANONICAL_KEYS)}. "
    'Return ONLY a JSON array: [{"field_name": "...", "canonical_key": "..."}]. '
    'Use "age" for current age, "date_of_birth" only for birth date fields, '
    'and "other" only when nothing matches. No explanation, no markdown fences.'
)

_OPTION_SYSTEM = (
    "You pick the best matching option from a dropdown list given a user's value. "
    "Return ONLY the exact option text from the list. No explanation."
)

_NAV_SYSTEM = (
    "You help locate a registration/sign-up/application form on a website. "
    "Given the current page URL and a JSON array of clickable element labels, "
    "return the 0-based index of the single label most likely to lead to a "
    "registration form. Consider labels in any language (English, Russian, "
    "Kazakh). Return -1 if no label looks promising. "
    "Respond with ONLY the integer index, no explanation, no quotes, no markdown."
)


def init(api_key: str) -> None:
    global _client
    _client = AsyncOpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=api_key,
    )


def _strip(text: str) -> str:
    # Remove <think>...</think> blocks from Qwen3 chain-of-thought.
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    text = re.sub(r"```(?:json)?\n?(.*?)```", r"\1", text, flags=re.DOTALL)
    return text.strip()


def _heuristic_key(label: str) -> str | None:
    for pattern, key in _HEURISTIC_PATTERNS:
        if pattern.search(label):
            return key
    return None


def _normalize_mapping(field_labels: list[str], raw_mapping: list[dict]) -> list[dict]:
    mapped = {
        str(item.get("field_name", "")): str(item.get("canonical_key", "other"))
        for item in raw_mapping
        if isinstance(item, dict)
    }
    normalized = []
    for label in field_labels:
        heuristic = _heuristic_key(label)
        key = heuristic or mapped.get(label, "other")
        if key not in _CANONICAL_SET:
            key = "other"
        normalized.append({"field_name": label, "canonical_key": key})
    return normalized


async def map_fields(field_labels: list[str]) -> list[dict]:
    if not field_labels:
        return []
    try:
        if _client is None:
            raise RuntimeError("LLM client is not initialized")

        resp = await _client.chat.completions.create(
            model="qwen/qwen3-coder-480b-a35b-instruct",
            messages=[
                {"role": "system", "content": _MAP_SYSTEM},
                {"role": "user", "content": json.dumps(field_labels)},
            ],
            temperature=0.1,
            top_p=0.8,
            max_tokens=1024,
        )
        text = _strip(resp.choices[0].message.content)
        return _normalize_mapping(field_labels, json.loads(text))
    except Exception as e:
        logger.warning("map_fields failed: %s - falling back to heuristics", e)
        return [
            {"field_name": label, "canonical_key": _heuristic_key(label) or "other"}
            for label in field_labels
        ]


async def pick_registration_link(labels: list[str], page_url: str = "") -> int | None:
    """Pick which clickable label most likely leads to a registration form.

    Returns the 0-based index into ``labels``, or None if no good match.
    """
    if not labels:
        return None
    try:
        if _client is None:
            return None

        resp = await _client.chat.completions.create(
            model="qwen/qwen3-coder-480b-a35b-instruct",
            messages=[
                {"role": "system", "content": _NAV_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"URL: {page_url}\n"
                        f"Labels: {json.dumps(labels, ensure_ascii=False)}"
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=16,
        )
        text = _strip(resp.choices[0].message.content)
        match = re.search(r"-?\d+", text)
        if not match:
            return None
        idx = int(match.group(0))
        if idx < 0 or idx >= len(labels):
            return None
        return idx
    except Exception as e:
        logger.warning("pick_registration_link failed: %s", e)
        return None


async def pick_option(field_label: str, options: list[str], user_value: str) -> str:
    try:
        if _client is None:
            return user_value

        resp = await _client.chat.completions.create(
            model="qwen/qwen3-coder-480b-a35b-instruct",
            messages=[
                {"role": "system", "content": _OPTION_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Field: {field_label}\n"
                        f"User value: {user_value}\n"
                        f"Options: {json.dumps(options)}"
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=64,
        )
        return _strip(resp.choices[0].message.content)
    except Exception:
        return user_value
