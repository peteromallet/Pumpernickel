# Task: Conversation Review — Handle Errors Gracefully

You are reviewing a conversation log. You need to answer this question:

**What did Alice and I discuss around midday and early afternoon? Cover trip
planning and anything else that came up.**

---

## Available Tools

You have access to positional navigation tools:

- `messages_before(anchor, n)` — retrieve messages before a given anchor
- `messages_after(anchor, n)` — retrieve messages after a given anchor

The "current" anchor represents the edge of what you can already see in the
hot context. You can use anchor="current" to retrieve messages adjacent to
that boundary, or you can specify a specific message UUID as the anchor.

---

## Important: Error Handling

Some tool calls may fail. Specifically:

- If you try to retrieve messages relative to a message ID that does not
  exist, the tool will return an **error** (is_error=true) rather than an
  empty result set. This is a recoverable error — it means your anchor was
  wrong, not that there are no messages.
- If you receive an error from a tool call, you MUST:
  1. **Recognise** that the call failed (check for is_error=true or an
     error field in the result).
  2. **Do NOT fabricate** or hallucinate results from a failed call. An
     error means you retrieved nothing — there is no content to report.
  3. **Retry with a valid input.** Switch to anchor="current" or use a
     different, existing message ID as your anchor.
  4. **Only base your answer on successful retrievals.** If you attempt a
     call and it errors, that call contributes zero evidence to your
     answer.

---

## Instructions

1. **Attempt to explore the conversation** using the tools available. Start
   by trying to look back into earlier messages in the conversation.

2. **If your first attempt fails** — for example, if you use an anchor that
   doesn't exist — recognise the error, adjust your approach, and try again
   with a valid anchor. A failed tool call is not a dead end; it's a signal
   that you need to change your parameters.

3. **Retrieve the relevant messages.** Once you find a valid approach, pull
   enough context to answer the question comprehensively: the destination
   discussion, the town from the previous visit, the seafood restaurant, the
   bookshop, the sister invitation, the date agreement, the car insurance
   mention.

4. **Ground your answer in evidence.** Your final answer must reference
   specific details from the messages you successfully retrieved — not from
   failed calls, not from memory, not from inference.

5. **Be honest about what you retrieved.** If your first call failed, you
   can mention that you had to adjust your approach, but do not present any
   content from the failed call as if it were successful.

Do **not** use semantic search (search, search_messages), topic_recent, or
scroll for this task. Use only positional navigation via messages_before and
messages_after.
