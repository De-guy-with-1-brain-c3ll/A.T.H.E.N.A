#!/usr/bin/env python3
"""Minerva setup — installs local STT/TTS dependencies."""
from setuptools import setup, find_packages

setup(
    name="minerva",
    version="0.1.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.11",
    install_requires=[
        "sherpa-onnx",
        "edge-tts",
    ],
    entry_points={
        "console_scripts": [
            "minerva=minerva.main:main",
        ],
    },
)
