"""Event-driven durable invoice delivery, based on the 0919 settlement flow.

An interrupted/ambiguous send is never automatically replayed. Historical rows
remain append-only; status changes touch only one row and pending scans read
only the status column, not every historical invoice payload.
"""
import json
import gspread

TITLE = "Notifications_Outbox"
HEADERS = ["delivery_id", "user_id", "embed_json", "status", "detail"]


def sheet(doc, *, verify=True):
    result = getattr(doc, "_dental_outbox_worksheet", None)
    if result is None:
        try:
            result = doc.worksheet(TITLE)
        except gspread.WorksheetNotFound:
            result = doc.add_worksheet(title=TITLE, rows="1000", cols="5")
            result.update(values=[HEADERS], range_name="A1", value_input_option="RAW")
        doc._dental_outbox_worksheet = result  # Handle only, never invoice rows.
    if verify and result.get("A1:E1") != [HEADERS]:
        raise RuntimeError("Outbox headers do not match; refusing to overwrite")
    return result


def _index(result):
    return {row[0]: number for number, row in enumerate(result.get("A2:A"), start=2) if row and row[0]}


def enqueue(doc, settlement, messages):
    result = sheet(doc)
    existing = _index(result)
    expected = {}
    for user_id, embed in messages:
        key = settlement + ":" + str(user_id)
        expected[key] = [key, str(user_id), json.dumps(embed.to_dict(), ensure_ascii=False)]
    new_rows = [values + ["pending", ""] for key, values in expected.items() if key not in existing]
    if new_rows:
        result.append_rows(new_rows, value_input_option="RAW")
    # An ambiguous append is not retried here. The settlement checkpoint remains.
    # Recovery deduplicates IDs, and verifies payloads before clearing orders.
    existing = _index(result)
    keys = list(expected)
    if any(key not in existing for key in keys):
        raise RuntimeError("Outbox verification failed; source orders retained")
    ranges = [f"A{existing[key]}:C{existing[key]}" for key in keys]
    verified = result.batch_get(ranges) if ranges else []
    if len(verified) != len(keys) or any(rows != [expected[key]] for key, rows in zip(keys, verified)):
        raise RuntimeError("Outbox verification failed; source orders retained")


def pending(doc, limit=None):
    result = sheet(doc)
    numbers = []
    for number, status in enumerate(result.get("D2:D"), start=2):
        state = status[0] if status else ""
        if state == "sending":
            row = result.get(f"A{number}:E{number}")
            if row and row[0]:
                mark(doc, row[0][0], "uncertain", "interrupted; check Discord before any resend", row_number=number)
        elif state == "pending":
            numbers.append(number)
            if limit is not None and len(numbers) >= limit:
                break
    values = result.batch_get([f"A{number}:E{number}" for number in numbers]) if numbers else []
    if len(values) != len(numbers):
        raise RuntimeError("Pending delivery rows missing")
    records = []
    for number, rows in zip(numbers, values):
        if not rows or not rows[0]:
            raise RuntimeError("Pending delivery row missing")
        row = rows[0] + [""] * (len(HEADERS) - len(rows[0]))
        record = dict(zip(HEADERS, row))
        record["_row"] = number
        records.append(record)
    return records


def mark(doc, key, status, detail="", *, row_number=None):
    result = sheet(doc, verify=False)
    number = row_number if row_number is not None else _index(result).get(key)
    if number is None or result.get(f"A{number}:A{number}") != [[key]]:
        raise RuntimeError("Outbox row moved or missing; refusing to update another invoice")
    result.update(values=[[status, detail]], range_name=f"D{number}:E{number}", value_input_option="RAW")
