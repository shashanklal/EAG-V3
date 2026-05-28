# Claim Event - Core Event Fields

Descriptions of the event-level fields that explain claim lifecycle activity.

## event_id
Unique identifier for a claim event.

## event_seq
Sequence number showing event order within the claim lifecycle.

## event_type
Type of event, such as Received, Eligibility+Edits, Pended, Info Received, Routed, Auto Adjudicated, Manual Adjudicated, Payment Issued, or Denial Notified.

## event_ts
Date and time when the event happened.

## event_actor
Who performed the event, such as System, Provider, Examiner, RulesEngine, or Finance.

## status_after
Claim status immediately after the event.

## note
Free-text note that adds context for the event.
