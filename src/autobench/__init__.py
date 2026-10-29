"""Autobench keeps registered benchmark evidence fresh using Device Scheduler."""

__version__ = "0.1.0"

from .api import init_project, register_benchmark, register_definition

__all__ = ["__version__", "init_project", "register_benchmark", "register_definition"]

