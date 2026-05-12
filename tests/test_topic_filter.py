"""Unit tests for ``app.services.topic_filter``."""

from __future__ import annotations

import pytest

from app.services.topic_filter import join_artifact_topics


class TestJoinArtifactTopicsShape:
    """Confirm the emitted SQL fragment has the expected structure."""

    @pytest.mark.parametrize(
        "alias, expected_table",
        [
            ("m", "memories"),
            ("t", "themes"),
            ("o", "observations"),
            ("w", "watch_items"),
            ("d", "distillations"),
            ("x", "out_of_bounds"),
        ],
    )
    def test_fragment_contains_join_and_alias_prefix(
        self, alias: str, expected_table: str
    ) -> None:
        fragment = join_artifact_topics(alias, "$7")
        assert "JOIN artifact_topics" in fragment
        expected_alias = f"_at_{alias}"
        assert expected_alias in fragment
        assert f"artifact_table = '{expected_table}'" in fragment
        assert f"{expected_alias}.artifact_id = {alias}.id" in fragment

    def test_topic_id_bind_embedded(self) -> None:
        fragment = join_artifact_topics("t", "$4")
        assert "_at_t.topic_id = $4" in fragment

    def test_status_active_literal_present(self) -> None:
        fragment = join_artifact_topics("m", "$1")
        assert "_at_m.status = 'active'" in fragment


class TestJoinArtifactTopicsErrors:
    def test_raises_typeerror_when_topic_id_param_is_none(self) -> None:
        with pytest.raises(TypeError, match="topic_id_param"):
            join_artifact_topics("m", None)  # type: ignore[arg-type]

    def test_raises_valueerror_on_unknown_alias(self) -> None:
        with pytest.raises(ValueError, match="Unknown artifact-table alias"):
            join_artifact_topics("z", "$2")

    def test_raises_valueerror_on_empty_alias(self) -> None:
        with pytest.raises(ValueError, match="Unknown artifact-table alias"):
            join_artifact_topics("", "$2")

    def test_raises_valueerror_on_multi_char_alias(self) -> None:
        with pytest.raises(ValueError, match="Unknown artifact-table alias"):
            join_artifact_topics("mm", "$1")


class TestJoinArtifactTopicsEdgeCases:
    def test_single_digit_bind(self) -> None:
        fragment = join_artifact_topics("o", "$0")
        assert "_at_o.topic_id = $0" in fragment

    def test_large_bind_index(self) -> None:
        fragment = join_artifact_topics("d", "$99")
        assert "_at_d.topic_id = $99" in fragment

    def test_all_aliases_produce_unique_prefixes(self) -> None:
        """Each alias must produce a distinct _at_<alias> prefix."""
        prefixes = {
            alias: join_artifact_topics(alias, "$1")
            for alias in ("m", "t", "o", "w", "d", "x")
        }
        for alias, frag in prefixes.items():
            assert f"_at_{alias}" in frag
        # Ensure they are all different
        alias_names = {f"_at_{a}" for a in ("m", "t", "o", "w", "d", "x")}
        assert len(alias_names) == 6