"""Одноразовая оценка качества на внешнем размеченном наборе."""

import argparse
import json
import sys
from pathlib import Path

from memes_worker.application.use_cases.evaluation import evaluate
from memes_worker.infrastructure.evaluation.json import JSONEvaluationDataset


def main() -> None:
    """Напечатать реальные метрики; без набора никакие показатели не заявляются."""
    parser = argparse.ArgumentParser(
        description="Evaluate measured pairs, rankings, OCR and ASR against independent labels."
    )
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--require-prd-size", action="store_true")
    arguments = parser.parse_args()
    try:
        result = evaluate(
            JSONEvaluationDataset(arguments.dataset),
            require_prd_size=arguments.require_prd_size,
        )
    except (OSError, ValueError) as error:
        sys.stderr.write(json.dumps({"evaluation_failed": str(error)}) + "\n")
        raise SystemExit(1) from error
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
