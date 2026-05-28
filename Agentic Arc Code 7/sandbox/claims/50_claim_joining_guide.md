# Claim Joining Guide

How the uploaded datasets relate to each other.

- **claims to lines**: Join on claim_id to link claim header data to line details.
- **claims to events**: Join on claim_id to link claim header data to event timeline.
- **flat file**: Already combines line data with claim header data in one table.
- **Primary key at claim level**: claim_id
- **Primary key at line level**: line_id, with claim_id and line_num as context.
- **Primary key at event level**: event_id, with claim_id and event_seq as context.
