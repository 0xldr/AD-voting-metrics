# Delegate Tracking

This repository contains a Python script to track the states of votes cast in polls and spells at SKY.

## Important Note

- The script is not compatible with Python 3.12 due to the deprecation of certain datetime functions used in the code.

## Current Functionality

- Collects information of delegates from vote.sky.money and Dune.
- Retrieves information of polls corresponding to the entered dates from vote.sky.money.
- Retrieves information of spells corresponding to the entered dates from vote.sky.money.
- Exports a CSV file with the SKY holdings of each delegate per date.
- Exports a CSV file with the total SKY ranking of each delegate per date.
- Exports two CSV files (one of them transposed for usability) with the status of the votes corresponding to each poll and spell.

## Requirements

- Python 3.11 or later. Dependencies are declared in `pyproject.toml` and installed automatically via `pip install -e .`.

## Installation

Follow these steps to set up the project:

1. Clone the repository:

   ```bash
   git clone 
   ```

2. Navigate to the cloned directory:

   ```bash
   cd AD-voting-metrics
   ```

3. Install the required dependencies and the package itself:

   ```bash
   pip install -e .
   ```

## Configuration

The script needs a Dune Analytics API key to fetch delegated amounts.

1. Get an API key from [Dune](https://dune.com/settings/api).
2. Copy `.env.example` to `.env`:

   ```bash
   cp .env.example .env
   ```

3. Edit `.env` and set `DUNE_API_KEY` to your key.

## Maintaining the delegate roster

The list of Aligned Delegates lives in `delegates.yaml` at the repo root. This is the source of truth for the script which reads it and verifies against the vote.sky.money API.

Each entry has:

- `name`: display name of the delegate
- `voteDelegateAddress`: the on-chain voteDelegate contract (lowercase 0x...)
- `startDate`: the date that the delegate was recognized as aligned. This is not necessarily the same date the contract was deployed.
- `endDate`: optional. The inclusive last day that they were an AD. `null` for currently active delegates.

When a new delegate is recognized, add an entry with their `startDate` and `endDate: null`. When a delegate exits, set their `endDate` to the inclusive last day they were active. The script will warn if the YAML drifts from the API state (e.g., a delegate marked active in YAML who no longer appears in the API as currently-aligned).

## Usage

Run the script for a specific month:

```bash
+python -m ad_voting_metrics --month "April 2026"
```

Output CSVs are written to `output_data/`.

## To Dos

- [ ] General code clean up.
- [ ] Add more information about the polls and spells to the CSV file.
