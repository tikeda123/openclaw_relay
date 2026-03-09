from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ResponseExtractorError(ValueError):
    """Raised when a gateway response cannot be converted into final text."""


@dataclass(frozen=True)
class ResponseExtractor:
    def extract_text(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            raise ResponseExtractorError("response payload must be an object")

        output = payload.get("output")
        if not isinstance(output, list):
            raise ResponseExtractorError("response payload is missing output[]")

        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") != "output_text":
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())

        if not texts:
            raise ResponseExtractorError("response payload does not contain output_text")
        return "\n".join(texts)
