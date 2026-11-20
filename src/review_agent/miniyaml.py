"""review.yaml을 읽기 위한 아주 작은 YAML 부분집합 파서.

PyYAML이 설치되어 있으면 config 모듈이 그것을 먼저 쓴다. 이 파서는 의존성 없이
설정 파일을 읽기 위한 대비책이며 다음만 지원한다.

- `key: value` 스칼라 (정수, 실수, true/false, null, 따옴표 문자열, 맨 문자열)
- 들여쓰기로 중첩된 맵
- `- item` 블록 리스트와 `[a, b]` 인라인 리스트
- `#` 주석, 빈 줄

앵커, 복수 문서, 블록 스칼라(`|`, `>`)는 지원하지 않는다.
"""

from __future__ import annotations

from typing import Any


class MiniYamlError(ValueError):
    pass


def _parse_scalar(token: str) -> Any:
    token = token.strip()
    if not token:
        return ""
    if token.startswith("#"):
        return ""
    if (token[0] == '"' and token[-1] == '"' and len(token) >= 2) or (
        token[0] == "'" and token[-1] == "'" and len(token) >= 2
    ):
        return token[1:-1]
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_inline(inner)]
    lowered = token.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~", "none"):
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _split_inline(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for char in text:
        if quote:
            buf.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            buf.append(char)
            continue
        if char == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    if buf:
        parts.append("".join(buf))
    return [p for p in (part.strip() for part in parts) if p]


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote: str | None = None
    for index, char in enumerate(line):
        if quote:
            out.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            break
        out.append(char)
    return "".join(out).rstrip()


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def loads(text: str) -> dict[str, Any]:
    """YAML 부분집합을 dict로 읽는다. 최상위는 반드시 맵이어야 한다."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        leading = raw[: len(raw) - len(raw.lstrip())]
        if "\t" in leading:
            raise MiniYamlError("들여쓰기에 탭을 쓸 수 없습니다")
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        lines.append((_indent_of(stripped), stripped.strip()))

    value, index = _parse_block(lines, 0, lines[0][0] if lines else 0)
    if index != len(lines):
        raise MiniYamlError(f"파싱하지 못한 줄이 남았습니다: {lines[index][1]!r}")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MiniYamlError("최상위는 맵이어야 합니다")
    return value


def _parse_block(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[Any, int]:
    if index >= len(lines):
        return None, index

    if lines[index][1].startswith("- "):
        items: list[Any] = []
        while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
            items.append(_parse_scalar(lines[index][1][2:]))
            index += 1
        return items, index

    if lines[index][1] == "-":
        raise MiniYamlError("빈 리스트 항목은 지원하지 않습니다")

    mapping: dict[str, Any] = {}
    while index < len(lines) and lines[index][0] == indent:
        line_indent, content = lines[index]
        if ":" not in content:
            raise MiniYamlError(f"'key: value' 형식이 아닙니다: {content!r}")
        key, _, rest = content.partition(":")
        key = key.strip().strip("\"'")
        rest = rest.strip()
        index += 1
        if rest:
            mapping[key] = _parse_scalar(rest)
            continue
        if index < len(lines) and lines[index][0] > line_indent:
            child, index = _parse_block(lines, index, lines[index][0])
            mapping[key] = child
        elif (
            index < len(lines)
            and lines[index][0] == line_indent
            and lines[index][1].startswith("- ")
        ):
            # 키와 같은 들여쓰기에 놓인 블록 리스트도 허용한다.
            child, index = _parse_block(lines, index, line_indent)
            mapping[key] = child
        else:
            mapping[key] = None
    return mapping, index
