import os
import sys

print(f"Python version: {sys.version}")
print(f"PYTHONPATH: {os.environ.get('PYTHONPATH', 'Not set')}")

try:
    import numpy

    print(f"Numpy version: {numpy.__version__}")
except Exception as e:
    print(f"Numpy import failed: {e}")

try:
    import torch

    print(f"Torch version: {torch.__version__}")
except Exception as e:
    print(f"Torch import failed: {e}")

try:
    from transformers.utils.import_utils import is_torch_available

    print(f"Transformers is_torch_available(): {is_torch_available()}")
except Exception as e:
    print(f"Transformers import failed: {e}")
