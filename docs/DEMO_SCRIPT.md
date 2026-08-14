# Five-minute hackathon demo

## Story

Asset Compensation Hub turns an error-prone folder of emails and spreadsheets
into an auditable workflow. It detects damaged/lost asset cases, highlights
data risks, calculates lost-asset compensation under the TranNNB policy,
prevents duplicate accounting and exports a controlled ERP batch.

## Walkthrough

1. Start the app with demo mode enabled and open the dashboard.
2. Point out that the React interface deliberately preserves the first
   dashboard: VNG header, language switcher, task tabs, six KPI cards, filters
   and the compact case table.
3. Open **Task > TranNNB**, choose **Nạp dữ liệu demo**, then **Tính thử**.
   Show the usage months, depreciation group, remaining value, responsibility
   fee, total amount and plain-language formula. Explain that this call is a
   read-only preview: it does not change a workbook, case status or mailbox.
4. Change the lookup state to a missing/ambiguous option and calculate again.
   Show that the product returns `NEEDS_REVIEW` instead of inventing a match or
   amount.
5. Return to **Tổng quan**, open a case and show source details plus persistent
   status history. Explain that supplier/location conflicts remain explicit
   warnings until a reviewer resolves them.
6. Open **Task > NganTLT**, move an approved case to
   `READY_FOR_ACCOUNTING`, select eligible cases, create a batch and download
   the generated workbook.
   Show that it still uses the approved original sheet, 30-column layout and
   row formatting; an approved `.xlsm` template also keeps its VBA project.
7. Refresh the page: state and audit history persist in SQLite, while the same
   cases cannot be silently exported again.

## Closing line

“We did not automate a spreadsheet. We built a safe decision workflow around
the spreadsheet, with traceability from source evidence to ERP output.”
