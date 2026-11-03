"""Setup configuration for agent-runtime-secure package."""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

# The SQL guard's parser and engine. Pinned to the SAME versions as
# requirements-api.txt (tests/unit/test_dependency_pins.py checks they agree).
# Unpinned, `pip install -e ".[dev]"` gave the guard no sqlglot at all, and a
# hand-installed newer sqlglot (30.29.0) makes phi_guard fail closed on every
# FHIR query, including plain GROUP BY counts (found 2026-11-03).
GUARD_PINS = ["sqlglot==30.11.0", "duckdb==1.5.4"]

setup(
    name="agent-runtime-secure",
    version="0.1.0",
    author="Agent Runtime Security Team",
    description="Capability-based security runtime for AI agents",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/example/agent-runtime-secure",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Security",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.8",
    install_requires=requirements + GUARD_PINS,
    extras_require={
        "dev": GUARD_PINS + [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "pytest-mock>=3.10.0",
            "black>=23.0.0",
            "flake8>=6.0.0",
            "mypy>=1.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "agent-runtime=src.runtime.agent_runtime:main",
            "agent-loop=src.runtime.agent_loop:main",
        ],
    },
)
