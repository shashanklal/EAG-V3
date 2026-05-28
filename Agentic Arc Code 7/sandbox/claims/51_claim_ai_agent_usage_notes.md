# AI Agent Usage Notes

Instructions-oriented notes for an AI agent using this knowledge pack.

- **Prefer plain English**: Answer business users with short, simple explanations first.
- **Use claim level first**: Start with claim header status, dates, and totals before drilling into lines.
- **Use line detail for denials**: If a claim is partially denied, inspect line_status and denial_code.
- **Use events for chronology**: Explain what happened using event_seq and event_ts.
- **Use rework guidance for gaps**: When data is missing, explain what likely needs to be corrected or sent.
- **Be explicit on synthetic content**: State clearly when a lookup or advisory value is synthetic.
- **Do not overstate certainty**: If the sample lacks a field, say it is inferred or synthetic.
- **Preserve identifiers exactly**: Return claim IDs, line IDs, and codes exactly as stored.
