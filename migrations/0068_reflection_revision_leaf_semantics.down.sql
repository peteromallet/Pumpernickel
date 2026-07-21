-- Reverse 0068 by restoring the exact 0067 view object.

BEGIN;

DROP VIEW mediator.v_searchable_content;

ALTER VIEW mediator.v_searchable_content_pre_0068
    RENAME TO v_searchable_content;

COMMENT ON VIEW mediator.v_searchable_content IS
    'Unified retrieval read surface for messages, memories, observations, distillations, conversation notes, themes, artifacts, and reflections. Excludes deleted/suppressed messages, inactive durable rows, deleted artifacts, empty conversation notes, non-processed reflection sessions, superseded reflection entries, and dyad_shareable non-message content.';

COMMIT;
