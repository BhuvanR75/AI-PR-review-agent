"""Operator scripts: schema migrations and one-off maintenance entry points.

A package (rather than loose modules) so `scripts.migrate` has exactly one module
identity, which keeps mypy and the CLI import consistent.
"""
