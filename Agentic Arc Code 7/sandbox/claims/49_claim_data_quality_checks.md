# Claim Data Quality Checks

Useful checks when validating synthetic or real claim data for AI use.

- **Identifier completeness**: Confirm claim_id, member_id, and provider_id are present.
- **Date order**: Service dates should not be after received date in most normal scenarios.
- **Amount balancing**: Sum of line charges should align with claim total charge when expected.
- **Status consistency**: Closed claims should usually have final status and dates populated.
- **Denial consistency**: Denied lines should normally have a denial code and explanation.
- **Event ordering**: event_seq should match the lifecycle order and event timestamps.
- **Code validity**: Procedure, diagnosis, POS, and revenue codes should be in approved lookup sets.
- **Duplicate detection**: Check for repeated claims or lines with same member, service date, and code.
