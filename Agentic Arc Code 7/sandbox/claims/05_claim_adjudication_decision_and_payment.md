# Claim Adjudication - Decision and Payment Fields

Fields commonly produced during claim adjudication and payment calculation.

## claim_status
Final or current status of the claim, such as Paid, Denied, Pended, Adjusted, or PartiallyDenied.

## line_status
Status of each claim line after adjudication.

## auto_adjudicated_ind
Flag showing whether the claim was processed automatically by rules.

## allowed_amt
Amount allowed by plan rules for a line.

## total_allowed_amt
Total allowed amount for the claim.

## paid_amt
Amount actually paid for a line.

## total_paid_amt
Total amount paid for the full claim.

## denial_code
Standard or internal denial/reduction code assigned during adjudication.

## denial_desc
Simple reason text for the denial or payment reduction.

## bill_code
Synthetic billing classification code used by adjudication rules.

## pricing_method
Synthetic pricing method such as fee schedule, DRG, APC, or percent of charge.
