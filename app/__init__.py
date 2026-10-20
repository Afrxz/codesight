"""RAGG: a retrieval-augmented Q&A engine for codebases."""

import os

# Set before any library imports huggingface_hub: Windows can't make the cache's
# symlinks without admin rights (it falls back to copies, which is fine).
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
