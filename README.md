# AD Voting Metrics

Python pipeline for recording the monthly on-chain voting participation of SKY DAO Aligned Delegates.

## How It Works

One run covers one calendar month:

1. Load the delegate roster from `delegates.yaml` and check it against the live vote.sky.money aligned-delegate list, warning on drift.
2. Replay on-chain Lock/Free events to get each delegate's SKY delegation on every day of the month, and rank delegates per day.
3. Fetch the month's polls and executive spells from vote.sky.money and assign each (delegate, poll) pair a participation status.
4. Verify pending executive votes against chief `Vote` events on-chain to settle whether each vote landed inside the deadline.
5. Write two CSVs to `output_data/<YYYY-MM>/` and a reconciliation log entry.

Everything downstream — 6-month track records, Level 3 slot eligibility, the metrics modifier, and compensation amounts — is handled outside this repo from these CSVs.

### Participation statuses

Each (delegate, poll or spell) cell holds one of:

- `Yes` — voted, and for spells, inside the deadline.
- `No` — had SKY delegated across the voting window and did not vote.
- `Late` — spells only: voted after the deadline. Counts as non-participation but is labelled distinctly.
- `Not Started` — the delegate's alignment began after the poll closed.
- `Voting Open` — the poll had not closed when the data was fetched; re-run after it closes.
- `No Delegated SKY` — zero SKY delegated on the relevant days, so non-participation is not held against them.
- `Pending verification` — a spell cell the on-chain check could not settle.

A poll counts against a delegate only if they held SKY on the close day and on at least one earlier day of the window.

### Executive spell voting deadline

A delegate has **3 business days (UTC)** from the day a spell goes live to vote for it. Weekends are skipped and no holiday calendar is applied, so a spell going live Monday must be voted on by Thursday, and one going live Friday by the following Wednesday. A vote landing anywhere within the deadline day counts.

Timing can only be established on-chain, from chief `Vote` events. The public supporters endpoint reports who currently supports a spell but carries no timestamp, so it is not used. Any spell cell the on-chain check cannot settle — including every cell when `SKY_RPC_URL` is unset — stays `Pending verification` rather than being credited unverified.

## Requirements

- Python 3.14 or later, managed with [uv](https://docs.astral.sh/uv/).
- A mainnet JSON-RPC endpoint (Alchemy, Infura, or any public RPC).

## Installation

```bash
git clone <repo-url>
cd <repo-dir>
uv sync
```

Run the tool from the repo root so the default `delegates.yaml` and `output_data/` paths resolve, or point `--roster` and `--output-dir` elsewhere.

## Configuration

Copy `.env.example` to `.env` and set `SKY_RPC_URL` to a mainnet JSON-RPC endpoint. It is used to fetch on-chain delegation events and to establish when each executive vote was cast.

### Delegate Roster

`delegates.yaml` at the repo root is the source of truth for who is an Aligned Delegate. Each entry has:

- `name`: display name
- `vote_delegate_address`: on-chain voteDelegate contract, lowercase `0x...`
- `start_date`: date the delegate was recognized as aligned
- `end_date`: inclusive last day they were active, or `null` if currently active

When a delegate is recognized, add an entry with `end_date: null`. When they exit, set `end_date`. The script warns if the YAML drifts from the live vote.sky.money state.

## Usage

```bash
uv run ad-voting-metrics --month "April 2026"

# equivalently, without relying on the installed console script:
uv run python -m ad_voting_metrics --month "April 2026"
```

The month argument accepts either natural form (`"April 2026"`) or ISO (`"2026-04"`). The month must have ended — the script refuses an in-progress period, since poll close-day rules can't be applied to polls still in their voting window.

Runs sync only new blocks since the last one by default, reusing cached on-chain events in the output directory. Pass `--rebuild` to discard the cache and resync the full delegation history from the V3 factory block.

`--roster FILE` and `--output-dir DIR` override the default `delegates.yaml` and `output_data` locations, which are resolved relative to the working directory.

## Outputs

All outputs for a month land in `<output-dir>/<YYYY-MM>/`, so `output_data/2026-04/` by default. Re-running the same month overwrites them; other months are untouched.

- `sky.csv` — one row per delegate per day, sorted by date then rank. Columns: `contract`, `name`, `date`, `sky` (SKY delegated), `rank` (1 = most SKY that day).
- `vote_participation.csv` — one row per poll or spell, sorted by start date. Columns: `Poll Id` (poll id or spell address), `Start Date`, `End Date` (blank for spells), `Title`, then one column per delegate holding the participation status.

Titles come from external APIs, so cells that begin with a formula character are prefixed with an apostrophe to stop spreadsheet applications executing them.

## Reconciliation log

Every run also writes a JSON file to `<output-dir>/reconciliation/`, named `<YYYY-MM>_<UTC-timestamp>.json`, with the period, timestamp, roster and API delegate counts, drift warnings, on-chain sync state, and the CSV paths produced. Useful for answering "what happened on this run" without re-running.
