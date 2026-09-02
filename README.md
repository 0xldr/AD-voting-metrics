# AD Voting Metrics

Python pipeline for computing monthly compensation for SKY DAO Aligned Delegates from their on-chain voting participation and off-chain communication record.

## How It Works

The pipeline is two-step, with operator review in between:

1. **`fetch`** pulls SKY delegations from on-chain Lock/Free events and poll/spell vote data from vote.sky.money for a given month. It writes per-delegate ranks, participation statuses, and a starting communication record to the configured Google Sheets workbook.
2. An operator reviews the **Communication Master** tab in the workbook, marking each (delegate, poll) cell as `Yes`, `No`, `Did not vote`, or `Pending verification` based on whether the delegate communicated their vote rationale in time.
3. **`finalize`** reads back the reviewed workbook tabs, evaluates each delegate's 6-month participation and communication track record, runs Level 3 slot eligibility, applies the metrics modifier (linear ramp 0→1 between 75% and 95% on each dimension, multiplied), and writes a **Compensation** tab for the month.

Compensation is pro-rata by days held at each level (L1/L2/L3) and scaled by the modifier. Rates come from a workbook-wide **Config** tab.

### Executive spell voting deadline

A delegate has **3 business days (UTC)** from the day a spell goes live to vote for it. Weekends are skipped and no holiday calendar is applied, so a spell going live Monday must be voted on by Thursday, and one going live Friday by the following Wednesday. A vote landing anywhere within the deadline day counts.

A vote cast after the deadline is marked **`Late`** and counts as non-participation — the same weight as never voting — but is labelled distinctly so the operator can tell the two apart. In Communication Master a `Late` cell cross-references to `Did not vote`, which is discounted, so a late vote is not penalised twice.

Timing can only be established on-chain, from chief `Vote` events. The public supporters endpoint reports who currently supports a spell but carries no timestamp, so it is not used. Any spell cell the on-chain check cannot settle — including every cell when `SKY_RPC_URL` is unset — stays `Pending verification` for operator adjudication rather than being credited unverified.

## Requirements

- Python 3.14 or later.
- A mainnet JSON-RPC endpoint (Alchemy, Infura, or any public RPC)
- A Google Cloud service account with access to a Google Sheets workbook

## Installation

Follow these steps to set up the project:

```bash
git clone <repo-url>
cd AD-voting-metrics
pip install -e .
```

## Configuration

### Environment Variables

The script needs a mainnet JSON-RPC endpoint to fetch on-chain delegation events and to establish when each executive vote was cast. Without it no spell vote can be credited — every spell cell stays "Pending verification".

Copy `.env.example` to `.env` and fill in:

- `SKY_RPC_URL` - a mainnet JSON-RPC endpoint (Alchemy, Infura, or any public RPC)
- `GOOGLE_SERVICE_ACCOUNT_FILE` - path to a service account JSON key file (create in Google Cloud Console, store outside the repo)
- `SHEETS_WORKBOOK_ID` — the long ID in the workbook URL between `/d/` and `/edit`

Share the workbook with the service account email as Editor.

### Workbook Setup

The workbook needs a **Config** tab before `finalize` will run. Create a tab named exactly `Config` with two columns and these rows:

| Key          | Value |
|--------------|-------|
| L1_USDS      | 33333 |
| L2_USDS      | 14583 |
| L3_USDS      | 4000  |
| TOTAL_SLOTS  | 6     |

Adjust the USDS values to match the current DAO compensation rates. Values are monthly amounts in USDS.

### Delegate Roster

`delegates.yaml` at the repo root is the source of truth for who is an Aligned Delegate. Each entry has:

- `name`: display name
- `vote_delegate_address`: on-chain voteDelegate contract, lowercase `0x...`
- `start_date`: date the delegate was recognized as aligned
- `end_date`: inclusive last day they were active, or `null` if currently active

When a delegate is recognized, add an entry with `end_date: null`. When they exit, set `end_date`. `fetch` warns if the YAML drifts from the live vote.sky.money state.

## Usage

```bash
# Step 1: pull data for the month
python -m ad_voting_metrics fetch --month "April 2026"

# Step 2: review the Communication Master tab in the workbook, then:
python -m ad_voting_metrics finalize --month "April 2026"
```

Output CSVs are written to `output_data/`.
`fetch` syncs only new blocks since the last run by default, reusing cached on-chain events. Pass `--rebuild` to discard the cache and resync the full delegation history from the V3 factory block.

The month argument accepts either natural form (`"April 2026"`) or ISO (`"2026-04"`).

## Outputs

**`fetch`** writes:

- Workbook tabs: `Daily Data` (workbook-wide), `Communication Master` (workbook-wide), `Participation Raw Data <Month Year>` (per-period)
- CSVs in `output_data/`: `sky.csv` (per-delegate daily SKY balances), `vote_participation.csv` (per-delegate poll matrix)
- Pre-clear backups in `output_data/backups/`: before a workbook-wide tab (`Daily Data`, `Communication Master`) is cleared and rewritten, its current contents are saved to a timestamped CSV so an interrupted write can be recovered
- A reconciliation log entry in `output_data/reconciliation/`

**`finalize`** writes:

- Workbook tab: `<Month Year> Compensation` (per-period)
- A reconciliation log entry in `output_data/reconciliation/`

Re-runs of either command overwrite the relevant per-period tabs.

## Reconciliation log

Every run writes a JSON file to `output_data/reconciliation/` with the period, timestamp, delegate counts, and metadata about the run. Useful for answering "what happened on this run" without re-running. `fetch` entries carry CSV paths in `output_files`; `finalize` entries carry the compensation tab name prefixed with `workbook:`.
