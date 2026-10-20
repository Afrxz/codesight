"""Evaluation & observability harness (TASKPLAN-2, Layer A).

Runs the *existing* decision loop against a fixed suite of frozen cases,
traces every step, grades the results, flags failure modes and reports
metrics — then diffs runs against a baseline to catch regressions.
"""
