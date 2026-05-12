"""Topic-filter helper for the artifact_topics join cutover (Sprint 3).

Single function ``join_artifact_topics`` that emits a JOIN fragment for
scoping artifact-table reads to a specific topic.  Every read of memories /
themes / observations / watch_items / distillations / out_of_bounds must go
through this join so topic-scoped queries leverage the partial index
``idx_artifact_topics_topic_artifact_active``.
"""

from __future__ import annotations

# Six canonical aliases → artifact_table values
_ALIAS_MAP: dict[str, str] = {
    "m": "memories",
    "t": "themes",
    "o": "observations",
    "w": "watch_items",
    "d": "distillations",
    "x": "out_of_bounds",
}


def join_artifact_topics(table_alias: str, topic_id_param: str) -> str:
    """Return a JOIN fragment that filters *table_alias* to *topic_id_param*.

    Parameters
    ----------
    table_alias:
        Single-character alias identifying the artifact table
        (``m``, ``t``, ``o``, ``w``, ``d``, ``x``).
    topic_id_param:
        The parameter placeholder that receives the topic UUID at
        execution time, e.g. ``"$3"``.  Callers using **append-last**
        binding must pass the *final* positional index here.

    Returns
    -------
    str
        SQL fragment: ``JOIN artifact_topics _at_<alias> ...``

    Raises
    ------
    TypeError
        If *topic_id_param* is ``None`` (must be an explicit bind like ``"$4"``).
    ValueError
        If *table_alias* is not one of the six recognised aliases.
    """
    if table_alias not in _ALIAS_MAP:
        raise ValueError(
            f"Unknown artifact-table alias {table_alias!r}; "
            f"expected one of {sorted(_ALIAS_MAP)}"
        )
    if topic_id_param is None:
        raise TypeError(
            "topic_id_param must be a string like '$4', not None. "
            "Pass the final positional bind index so the caller controls "
            "append-last ordering."
        )

    table = _ALIAS_MAP[table_alias]
    prefix = f"_at_{table_alias}"

    return (
        f"JOIN artifact_topics {prefix}"
        f" ON {prefix}.artifact_table = '{table}'"
        f" AND {prefix}.artifact_id = {table_alias}.id"
        f" AND {prefix}.topic_id = {topic_id_param}"
        f" AND {prefix}.status = 'active'"
    )