"""Entry point for `python -m ad_voting_metrics`.

The CLI logic lives in cli.py so it can be imported and tested without the side effects of `__main__` execution.
"""

from .cli import main

if __name__ == "__main__":
    main()
