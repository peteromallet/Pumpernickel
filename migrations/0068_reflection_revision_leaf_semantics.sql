-- 0068_reflection_revision_leaf_semantics: make corrected reflection entries
-- current, searchable, and embeddable.
--
-- Reflection revisions are append-only: a newer row points at the prior row
-- through supersedes_entry_id.  The current revision is therefore the leaf
-- that no successor references.  Migration 0067 accidentally selected rows
-- whose own pointer was NULL, which selected the original revision instead.

BEGIN;

ALTER VIEW mediator.v_searchable_content
    RENAME TO v_searchable_content_pre_0068;

CREATE VIEW mediator.v_searchable_content AS
SELECT previous.*
FROM mediator.v_searchable_content_pre_0068 previous
WHERE previous.source_type <> 'reflection'

UNION ALL

SELECT
    'reflection'::text AS source_type,
    re.id AS source_id,
    NULL::uuid AS message_id,
    NULL::text AS direction,
    re.user_id AS sender_id,
    NULL::uuid AS recipient_id,
    re.user_id AS thread_owner_user_id,
    re.created_at AS sent_at,
    'routine'::text AS charge,
    NULL::timestamptz AS edited_at,
    NULL::jsonb AS edit_history,
    re.plaintext_searchable AS content,
    NULL::text AS media_type,
    jsonb_build_object(
        'session_id', re.session_id,
        'template_key', re.template_key,
        'temporal_scope', re.temporal_scope,
        'phase', re.phase,
        'revision_number', re.revision_number,
        'schema_version', re.schema_version,
        'supersedes_entry_id', re.supersedes_entry_id
    ) AS media_analysis,
    re.bot_id,
    re.topic_id,
    NULL::uuid AS dyad_id,
    NULL::text AS thread_owner_partner_share,
    re.plaintext_searchable AS canonical_text,
    to_tsvector('simple'::regconfig, COALESCE(re.plaintext_searchable, '')) AS search_tsv,
    re.created_at AS sort_at,
    re.topic_id AS primary_topic_id,
    CASE WHEN re.topic_id IS NULL THEN ARRAY[]::uuid[] ELSE ARRAY[re.topic_id] END AS topic_ids,
    re.created_at AS source_created_at,
    re.created_at AS source_updated_at
FROM mediator.reflection_entries re
JOIN mediator.reflection_sessions rs
  ON rs.id = re.session_id
 AND rs.status = 'processed'
WHERE NOT EXISTS (
        SELECT 1
        FROM mediator.reflection_entries successor
        WHERE successor.supersedes_entry_id = re.id
    )
  AND re.plaintext_searchable IS NOT NULL
  AND btrim(re.plaintext_searchable) <> '';

COMMENT ON VIEW mediator.v_searchable_content IS
    'Unified retrieval read surface. Reflection rows use append-only leaf semantics so corrected current revisions, rather than superseded originals, are searchable and embeddable.';

COMMENT ON VIEW mediator.v_searchable_content_pre_0068 IS
    'Rollback snapshot of the 0067 searchable-content view; reflection rows are replaced by mediator.v_searchable_content.';

COMMIT;
