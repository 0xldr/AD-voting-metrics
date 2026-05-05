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
- Python 3.x (versions prior to 3.12) and dependencies listed in `requirements.txt`.

## Installation
Follow these steps to set up the project:
1. Clone the repository:
   ```bash
   git clone 
   ```
1. Navigate to the cloned directory:
   ```bash
   cd AD-voting-metrics
   ```
1. Install the required dependencies and the package itself:
   ```bash
   pip install -r requirements.txt
   pip install -e .
   ```

## Configuration

The script needs a Dune Analytics API key to fetch delegated amounts. 

1. Get an API key from https://dune.com/settings/api.
2. Copy `.env.example` to `.env`:

```
cp .env.example .env
```

3. Edit `.env` and set `DUNE_API_KEY` to your key.
   
## Usage

Run the script for a specific month:

```bash
+python -m ad_voting_metrics --month "April 2026"
```

Output CSVs are written to `output_data/`.

## To Dos

- [ ] General code clean up.
- [ ] Add more information about the polls and spells to the CSV file.