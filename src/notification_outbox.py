"""Durable invoice deliveries. All methods run inside the application's Sheets lock.

No automatic replay of an ambiguous message POST. An interrupted 'sending'
record becomes 'uncertain' on the next drain and needs administrator review.
"""
import json

import gspread

TITLE = 'Notifications_Outbox'
HEADERS = ['delivery_id', 'user_id', 'embed_json', 'status', 'detail']


def sheet(doc):
    try:
        return doc.worksheet(TITLE)
    except gspread.WorksheetNotFound:
        result = doc.add_worksheet(title=TITLE, rows='10000', cols='5')
        result.update(values=[HEADERS], range_name='A1', value_input_option='RAW')
        return result


def read(doc):
    result = sheet(doc)
    rows = result.get_all_values()
    if not rows or rows[0] != HEADERS:
        raise RuntimeError('Outbox headers do not match; refusing to overwrite')
    return result, [row + [''] * (len(HEADERS) - len(row)) for row in rows[1:]]


def save(result, rows):
    result.update(values=[HEADERS] + rows, range_name='A1', value_input_option='RAW')


def enqueue(doc, settlement, messages):
    result, rows = read(doc)
    existing = {row[0] for row in rows}
    for user_id, embed in messages:
        key = settlement + ':' + str(user_id)
        if key not in existing:
            rows.append([key, str(user_id), json.dumps(embed.to_dict(), ensure_ascii=False),
                         'pending', ''])
            existing.add(key)
    save(result, rows)
    # Read-back verification must succeed before source orders are cleared.
    _, verified = read(doc)
    if verified != rows:
        raise RuntimeError('Outbox verification failed; source orders retained')


def pending(doc):
    result, rows = read(doc)
    changed = False
    for row in rows:
        if row[3] == 'sending':
            row[3:5] = ['uncertain', 'interrupted; check Discord before any resend']
            changed = True
    if changed:
        save(result, rows)
    return [dict(zip(HEADERS, row)) for row in rows if row[3] == 'pending']


def mark(doc, key, status, detail=''):
    result, rows = read(doc)
    for row in rows:
        if row[0] == key:
            row[3:5] = [status, detail]
            save(result, rows)
            return
    raise RuntimeError('Outbox delivery record missing')
