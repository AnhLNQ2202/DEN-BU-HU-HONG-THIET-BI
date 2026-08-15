# Compensation preview API

`POST /api/compensation/preview` applies the approved TranNNB compensation rules in
memory. It does not update cases, write workbooks, draft email, or send email.

## Request

Dates use `YYYY-MM-DD`; money is whole VND. `lost_date` defaults to the server's current
date when omitted. A request can contain 1 to 100 assets.

```json
{
  "assets": [
    {
      "tag_number": "LAP10001",
      "asset_name": "Synthetic laptop",
      "domain": "demo.user",
      "lost_date": "2026-08-14",
      "cost": 12000000,
      "start_date": "2025-02-14",
      "physical": true,
      "lookup_status": "MATCHED"
    }
  ]
}
```

`physical` and `lookup_status` are intentionally not inferred. Omitting either returns a
`NEEDS_REVIEW` result. A zero cost also returns `NEEDS_REVIEW`; it is not treated as zero
compensation.

The optional lookup fields are `asset_number`, `book`, `entity`, `cost_center`,
`product_code`, and `location`. Optional `group` accepts `FOUR_YEAR` or `SIX_YEAR`;
optional `fee_rate` accepts `0.05` or `0.30`. If either override conflicts with the
approved barcode rule, the item is returned as `NEEDS_REVIEW` rather than calculated.

For a barcode absent from the approved mapping, a reviewed operator may provide both `group`
and `fee_rate` with `classification_confirmed: true`. Without all three values, the unknown
barcode remains `NEEDS_REVIEW`.

Set `lookup_status` to `AMBIGUOUS` or `NOT_FOUND` when upstream reference matching did
not produce one verified row. Those items also require review.

## Response

Each result contains the normalized `input`, `status`, `review_required`, `reasons`,
`usage_months`, `depreciation_group`, residual rate/value, fee rate/value,
`total_amount`, `exempt`, and a human-readable `formula_explanation`.

Possible statuses are:

- `CALCULATED`: a deterministic preview is available.
- `EXEMPT`: cost is below 500,000 VND and use is at least 12 months.
- `NOT_APPLICABLE`: the item is not a physical IT asset.
- `NEEDS_REVIEW`: reference data, barcode classification, or an override is unresolved.

Unknown barcodes and ambiguous matches are never guessed. A preview is not an approval
to overwrite an original workbook or send an email.
