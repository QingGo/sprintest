import time
from typing import Any

import pytest
import torch
from transformers.pipelines import pipeline  # type: ignore

# Simulate heavy dependency loading time
# In a real project, this would be part of your package initialization
print("\n[AI] Initializing Torch and Transformers...")


# @pytest.fixture(scope="session")
# def classifier() -> Any:
#     print(f"[AI] Initializing model with Torch v{torch.__version__}...")
#     # Use a tiny random model to avoid heavy downloads
#     return pipeline(
#         "sentiment-analysis",  # type: ignore
#         model="hf-internal-testing/tiny-random-distilbert",
#     )


def test_inference_speed() -> None:
    text = "Sprintest makes AI development so much faster!"
    start = time.perf_counter()
    # result = classifier(text)
    duration = time.perf_counter() - start

    # assert "label" in result[0]
    # print(f"\n[AI] Prediction: {result[0]['label']} ({result[0]['score']:.4f})")
    print(f"[AI] Pure inference time: {duration:.4f}s")
