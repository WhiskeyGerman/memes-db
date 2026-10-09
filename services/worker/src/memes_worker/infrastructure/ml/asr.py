"""Изолированный faster-whisper с локальными фиксированными весами."""

import json
import sys
from pathlib import Path

from faster_whisper import WhisperModel

from memes_worker.application.dto.runtime import JSONValue
from memes_worker.infrastructure.ml.diagnostics import DiagnosticMessage, PluginInputError
from memes_worker.infrastructure.ml.plugin_runtime import entrypoint
from memes_worker.infrastructure.wire import object_payload, value_integer, value_string


def main() -> None:
    """Обработать ограниченные фрагменты и объединить текст перекрытий."""
    request = object_payload(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    model = WhisperModel(
        value_string(request.get("asr_model")),
        device=value_string(request.get("device")),
        local_files_only=True,
    )
    inputs = request.get("inputs")
    if not isinstance(inputs, list):
        raise PluginInputError(DiagnosticMessage.INVALID_MODEL_INPUT)
    fragments: list[JSONValue] = []
    prior_end = 0
    for raw in inputs:
        item = object_payload(raw)
        span = object_payload(item.get("span"))
        offset = value_integer(span.get("start_ms"))
        segments, info = model.transcribe(
            value_string(item.get("path")),
            vad_filter=True,
            word_timestamps=True,
            condition_on_previous_text=False,
        )
        if info.language not in {"ru", "en"}:
            continue
        for segment in segments:
            words = segment.words or []
            selected = [word for word in words if round(word.end * 1000) + offset > prior_end]
            text = (
                "".join(word.word for word in selected).strip() if words else segment.text.strip()
            )
            start = (
                round(selected[0].start * 1000) + offset
                if selected
                else round(segment.start * 1000) + offset
            )
            end = (
                round(selected[-1].end * 1000) + offset
                if selected
                else round(segment.end * 1000) + offset
            )
            if not text or end <= prior_end or end <= start:
                continue
            fragments.append(
                {
                    "text": text,
                    "language": info.language,
                    "span": {"start_ms": max(start, prior_end), "end_ms": end},
                }
            )
            prior_end = end
    Path(sys.argv[2]).write_text(
        json.dumps({"fragments": fragments}, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    entrypoint(main)
