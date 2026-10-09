"""Минимальная CLI-граница дочернего процесса без доступа к секретам API."""

import json
import sys
from datetime import timedelta
from enum import IntEnum
from pathlib import Path
from typing import cast
from uuid import UUID

from memes_catalog.domain.media import MediaPolicy
from memes_catalog.infrastructure.media.decoder import MediaDecoder
from memes_kernel.values import FileRef, Sha256
from memes_platform.application.dto.json import JsonObject


class Argument(IntEnum):
    """Позиции фиксированного argv без shell."""

    PATH = 1
    FILE_ID = 2
    SHA256 = 3
    SIZE = 4
    FFPROBE = 5
    POLICY = 6


def main() -> None:
    """Проверить файл и выдать только ограниченную техническую проекцию."""
    policy = cast(JsonObject, json.loads(sys.argv[Argument.POLICY]))
    decoder = MediaDecoder(
        MediaPolicy(
            maximum_bytes=int(str(policy["maximum_bytes"])),
            maximum_duration=timedelta(seconds=int(str(policy["maximum_seconds"]))),
        ),
        sys.argv[Argument.FFPROBE],
    )
    result = decoder._inspect(
        Path(sys.argv[Argument.PATH]),
        FileRef(
            id=UUID(sys.argv[Argument.FILE_ID]),
            sha256=Sha256(value=sys.argv[Argument.SHA256]),
            size_bytes=int(sys.argv[Argument.SIZE]),
        ),
        "",
    )
    decoder.policy.validate(result.original)
    sys.stdout.write(
        json.dumps(
            {
                "format": result.original.format.value,
                "kind": result.original.kind.value,
                "metadata": result.metadata,
            }
        )
    )


if __name__ == "__main__":
    main()
