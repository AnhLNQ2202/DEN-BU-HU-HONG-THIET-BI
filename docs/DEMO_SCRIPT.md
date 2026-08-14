# Five-minute hackathon demo

## Story

Asset Compensation Hub turns an error-prone folder of emails and spreadsheets
into an auditable workflow. It detects damaged/lost asset cases, highlights
data risks, prevents duplicate accounting and exports a controlled ERP batch.

## Walkthrough

1. Start the app with demo mode enabled and open the dashboard.
2. Point out the KPI cards: total cases, compensation value, ready cases and
   data-quality warnings.
3. Open the lost mouse case. Show residual value, responsibility fee, supplier
   resolution and source/audit information.
4. Open the issues panel. Explain that ambiguous supplier domains and location
   mismatches are explicit review warnings instead of coloured cells people can
   miss.
5. Move an approved case to `READY_FOR_ACCOUNTING`.
6. Select ready cases, create a batch and download the generated workbook.
   Show that it still uses the approved original sheet, 30-column layout and
   row formatting; an approved `.xlsm` template also keeps its VBA project.
7. Refresh the page: state and audit history persist in SQLite, while the same
   cases cannot be silently exported again.

## Closing line

“We did not automate a spreadsheet. We built a safe decision workflow around
the spreadsheet, with traceability from source evidence to ERP output.”
