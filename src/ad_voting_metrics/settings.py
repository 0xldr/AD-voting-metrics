"""Environment-derived configuration.

Single definition of every env var the pipeline reads. Values load from the process environment (field names match
env var names case-insensitively). cli.main's load_dotenv() populates the environment from .env beforehand; this
model deliberately doesn't read .env itself, so tests control values purely through the environment.

All fields are optional at this layer: on-chain executive verification degrades gracefully without the RPC URL, and
the workbook writes are skipped when the Sheets credentials are absent. Each consumer enforces its own requirement
with an operator-friendly error message.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    """Env vars used by the pipeline; instantiate to snapshot the current environment."""

    model_config = SettingsConfigDict(extra="ignore")

    # Mainnet JSON-RPC endpoint for Lock/Free delegation events and
    # executive-vote verification.
    sky_rpc_url: str | None = None

    # Path to the Google Cloud service-account JSON key file.
    google_service_account_file: str | None = None

    # Workbook ID from the Google Sheets URL (between /d/ and /edit).
    sheets_workbook_id: str | None = None
