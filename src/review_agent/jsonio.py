"""LLM 출력에서 JSON을 꺼내는 관용적 파서.

CLI 백엔드는 로그나 서문을 섞어 내보낼 수 있으므로, 텍스트에서 균형이 맞는
JSON 배열/객체 후보를 찾아 파싱 가능한 마지막 후보를 쓴다.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


class JsonExtractionError(ValueError):
    pass


def _candidates(text: str, open_char: str, close_char: str) -> list[str]:
    """균형이 맞는 최상위 괄호 구간을 앞에서부터 모은다. 문자열 리터럴은 건너뛴다."""
    found: list[str] = []
    depth = 0
    start = -1
    in_string = False
    quote = ""
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue

        if char in ('"', "'"):
            in_string = True
            quote = char
            continue

        if char == open_char:
            if depth == 0:
                start = index
            depth += 1
        elif char == close_char and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                found.append(text[start : index + 1])
                start = -1

    return found


def _try_load(chunk: str) -> Optional[Any]:
    try:
        return json.loads(chunk)
    except json.JSONDecodeError:
        return None


def _fenced_blocks(text: str) -> list[str]:
    return [match.group(1).strip() for match in _FENCE_RE.finditer(text)]


def extract_json_array(text: str) -> list[Any]:
    """텍스트에서 JSON 배열을 꺼낸다. 없으면 JsonExtractionError."""
    text = text or ""

    for block in reversed(_fenced_blocks(text)):
        value = _try_load(block)
        if isinstance(value, list):
            return value

    stripped = text.strip()
    value = _try_load(stripped)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        # {"findings": [...]} 처럼 감싸서 내보내는 경우를 받아 준다.
        for key in ("findings", "items", "results", "data"):
            inner = value.get(key)
            if isinstance(inner, list):
                return inner

    for chunk in reversed(_candidates(text, "[", "]")):
        value = _try_load(chunk)
        if isinstance(value, list):
            return value

    for chunk in reversed(_candidates(text, "{", "}")):
        value = _try_load(chunk)
        if isinstance(value, dict):
            for key in ("findings", "items", "results", "data"):
                inner = value.get(key)
                if isinstance(inner, list):
                    return inner

    raise JsonExtractionError("출력에서 JSON 배열을 찾지 못했습니다")


def extract_json_object(text: str) -> dict[str, Any]:
    """텍스트에서 JSON 객체를 꺼낸다. 없으면 JsonExtractionError."""
    text = text or ""

    for block in reversed(_fenced_blocks(text)):
        value = _try_load(block)
        if isinstance(value, dict):
            return value

    value = _try_load(text.strip())
    if isinstance(value, dict):
        return value

    for chunk in reversed(_candidates(text, "{", "}")):
        value = _try_load(chunk)
        if isinstance(value, dict):
            return value

    raise JsonExtractionError("출력에서 JSON 객체를 찾지 못했습니다")
