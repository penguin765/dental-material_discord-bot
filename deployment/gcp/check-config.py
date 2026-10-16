"""Offline configuration check; never print credential contents."""
import json
import os
from pathlib import Path


def main():
    for name in ('DISCORD_BOT_TOKEN', 'SPREADSHEET_ID'):
        if not os.environ.get(name, '').strip():
            raise SystemExit(f'Missing configuration: {name}')
    try:
        credentials = json.loads(Path('creds.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise SystemExit('Cannot read valid service-account JSON: creds.json') from None
    if credentials.get('type') != 'service_account' or not all(
        credentials.get(name) for name in ('client_email', 'private_key', 'token_uri')
    ):
        raise SystemExit('Incomplete service-account JSON')
    print('Configuration checked locally; Discord connectivity is not yet verified.')


if __name__ == '__main__':
    main()
