"""Reflection capture classification, period resolution, and session management.

M2 — Capture, Processing, and Derivation module.
Keeps classifier, period, and session-attachment logic isolated from the
broader persistence layer.  The session manager is pure business logic —
it does NOT access the database directly.
"""
