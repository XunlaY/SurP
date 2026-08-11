#!/usr/bin/env python3
"""
Gmail CLI — quick Gmail operations via the Google API.
Usage:
  python3 gmail-cli.py search QUERY [limit N]
  python3 gmail-cli.py inbox [limit N]
  python3 gmail-cli.py unread [limit N]
  python3 gmail-cli.py read MESSAGE_ID
  python3 gmail-cli.py send --to ADDRESS --subject SUBJ --body BODY
  python3 gmail-cli.py list-labels
  python3 gmail-cli.py list-labels-user
  python3 gmail-cli.py delete MESSAGE_ID
  python3 gmail-cli.py label MESSAGE_ID LABEL_NAME [--add/--remove]
  python3 gmail-cli.py threads [limit N]
"""

import argparse, json, sys, os
from datetime import datetime, timezone
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from email.mime.text import MIMEText

CREDENTIALS_PATH = os.path.join(
    os.path.expanduser("~"), ".gmail-mcp", "gmail-combined.json"
)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]


def get_service():
    creds = Credentials.from_authorized_user_file(CREDENTIALS_PATH, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            import google.auth.transport.requests
            req = google.auth.transport.requests.Request()
            creds.refresh(req)
        else:
            print("ERROR: Invalid or expired credentials. Re-authenticate with Gmail MCP auth.", file=sys.stderr)
            sys.exit(1)
    return build("gmail", "v1", credentials=creds)


def fmt_date(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def extract_body(message, parts=None, depth=0):
    if parts is None:
        parts = message.get("payload", {}).get("parts", [])
    if not parts and "body" in message.get("payload", {}):
        b = message["payload"]["body"]
        if b.get("data"):
            import base64
            s = b["data"].replace("-", "+").replace("_", "/")
            pad = 4 - len(s) % 4
            if pad != 4:
                s += "=" * pad
            try:
                return base64.b64decode(s).decode("utf-8", errors="replace")
            except Exception:
                return "(decode error)"
        return "(no body data)"
    for part in parts:
        mime = part.get("mimeType", "")
        ctype = part.get("mimeType", "")
        if ctype == "text/plain":
            if part.get("body", {}).get("data"):
                import base64
                s = part["body"]["data"].replace("-", "+").replace("_", "/")
                pad = 4 - len(s) % 4
                if pad != 4:
                    s += "=" * pad
                return base64.b64decode(s).decode("utf-8", errors="replace")
        elif mime in ("text/html", "multipart/alternative", "multipart/mixed"):
            sub = extract_body(message, part.get("parts", []), depth + 1)
            if sub:
                return sub
    return "(body could not be extracted)"


def print_message(msg, short=False):
    hdrs = msg.get("labelIds", [])
    snippet = msg.get("snippet", "").replace("\n", " ")
    headers = {}
    for h in msg.get("payload", {}).get("headers", []):
        headers[h["name"].lower()] = h["value"]
    print(f"  id:       {msg['id']}")
    print(f"  threadId: {msg.get('threadId', 'N/A')}")
    print(f"  from:     {headers.get('from', '???')}")
    print(f"  to:       {headers.get('to', '???')}")
    print(f"  subject:  {headers.get('subject', '(no subject)')}")
    print(f"  date:     {fmt_date(msg.get('internalDate'))}")
    print(f"  labels:   {', '.join(hdrs)}")
    print(f"  snippet:  {snippet[:120]}")
    if not short:
        body = extract_body(msg)
        print(f"\n  --- body (first 2000 chars) ---\n  {body[:2000]}")
    print()


# ── Commands ──────────────────────────────────────────────────────────

def cmd_search(args):
    service = get_service()
    q = args.query
    limit = args.limit or 10
    results = service.users().messages().list(
        userId="me", q=q, maxResults=limit
    ).execute()
    msgs = results.get("messages", [])
    if not msgs:
        print("(no results)")
        return
    print(f"Found {len(msgs)} results for '{q}':\n")
    for m in msgs:
        full = service.users().messages().get(
            userId="me", id=m["id"], format="full"
        ).execute()
        print_message(full)


def cmd_inbox(args):
    service = get_service()
    limit = args.limit or 20
    results = service.users().messages().list(
        userId="me", q="in:inbox", maxResults=limit
    ).execute()
    msgs = results.get("messages", [])
    if not msgs:
        print("(inbox is empty)")
        return
    print(f"Last {len(msgs)} inbox messages:\n")
    for m in msgs:
        full = service.users().messages().get(
            userId="me", id=m["id"], format="full"
        ).execute()
        print_message(full, short=True)


def cmd_unread(args):
    service = get_service()
    limit = args.limit or 10
    results = service.users().messages().list(
        userId="me", q="is:unread", maxResults=limit
    ).execute()
    msgs = results.get("messages", [])
    if not msgs:
        print("(no unread messages)")
        return
    print(f"Unread messages ({len(msgs)}):\n")
    for m in msgs:
        full = service.users().messages().get(
            userId="me", id=m["id"], format="full"
        ).execute()
        print_message(full, short=True)


def cmd_read(args):
    service = get_service()
    msg = service.users().messages().get(
        userId="me", id=args.message_id, format="full"
    ).execute()
    print_message(msg)


def cmd_send(args):
    service = get_service()
    msg = MIMEText(args.body or "")
    msg["To"] = args.to
    msg["From"] = "me"
    msg["Subject"] = args.subject or "(no subject)"
    raw = msg.as_string()
    encoded = raw.replace("+", "-").replace("_", "/").replace("=", "")
    body = {"raw": encoded}
    sent = service.users().messages().send(userId="me", body=body).execute()
    print(f"Sent!  id={sent['id']}")


def cmd_list_labels(args):
    service = get_service()
    resp = service.users().labels().list(userId="me").execute()
    sys_labels = [l for l in resp.get("labels", []) if l.get("type") == "system"]
    user_labels = [l for l in resp.get("labels", []) if l.get("type") != "system"]
    print("System labels:")
    for l in sys_labels:
        print(f"  {l['id']}: {l['name']}")
    print(f"\nUser labels ({len(user_labels)}):")
    for l in user_labels:
        print(f"  {l['id']}: {l['name']}")
    print(f"\nTotal: {len(sys_labels)} system, {len(user_labels)} user ({len(sys_labels) + len(user_labels)} combined)")


def cmd_list_labels_user(args):
    service = get_service()
    resp = service.users().labels().list(userId="me").execute()
    user_labels = [l for l in resp.get("labels", []) if l.get("type") != "system"]
    if not user_labels:
        print("(no user-defined labels)")
        return
    print(f"User labels ({len(user_labels)}):")
    for l in user_labels:
        print(f"  {l['id']}: {l['name']}")


def cmd_delete(args):
    service = get_service()
    service.users().messages().trash(userId="me", id=args.message_id).execute()
    print(f"Deleted (trashed) message {args.message_id}")


def cmd_label(args):
    service = get_service()
    label_name = args.label_name
    # Get or create label
    labels_resp = service.users().labels().list(userId="me").execute()
    existing = [l for l in labels_resp.get("labels", []) if l["name"] == label_name]
    if existing:
        label_id = existing[0]["id"]
    else:
        created = service.users().labels().create(
            userId="me",
            body={"name": label_name, "messageListVisibility": "show", "labelListVisibility": "labelShow"},
        ).execute()
        label_id = created["id"]
    print(f"Label '{label_name}' → {label_id}")

    # Apply to message
    msg = service.users().messages().get(userId="me", id=args.message_id).execute()
    current_labels = set(msg.get("labelIds", []))
    action = args.action or "add"
    if action == "add":
        current_labels.add(label_id)
    elif action == "remove":
        current_labels.discard(label_id)
    else:
        # toggle
        if label_id in current_labels:
            current_labels.discard(label_id)
            action = "removed"
        else:
            current_labels.add(label_id)
            action = "added"
    service.users().messages().modify(
        userId="me",
        id=args.message_id,
        body={"addLabelIds": [l for l in current_labels if l.startswith("Label_")],
               "removeLabelIds": []},
    )
    # Actually let's do it properly with add and remove separately
    to_add = [label_id] if action in ("add", "added") else []
    to_remove = [label_id] if action in ("remove", "removed") else []
    service.users().messages().modify(
        userId="me",
        id=args.message_id,
        body={"addLabelIds": to_add, "removeLabelIds": to_remove},
    )
    print(f"Label {action} on message {args.message_id}")


def cmd_threads(args):
    service = get_service()
    limit = args.limit or 10
    results = service.users().threads().list(userId="me", maxResults=limit).execute()
    threads = results.get("threads", [])
    if not threads:
        print("(no threads)")
        return
    print(f"Recent threads ({len(threads)}):\n")
    for t in threads:
        snippet = t.get("snippet", "").replace("\n", " ")
        headers = {}
        first_msg = t.get("messages", [{}])[0]
        for h in first_msg.get("payload", {}).get("headers", []):
            headers[h["name"].lower()] = h["value"]
        print(f"  id:        {t['id']}")
        print(f"  messages:  {len(t.get('messages', []))}")
        print(f"  from:      {headers.get('from', '???')}")
        print(f"  subject:   {headers.get('subject', '(no subject)')}")
        print(f"  snippet:   {snippet[:100]}")
        print()


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gmail CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Search emails")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search)

    # inbox
    p = sub.add_parser("inbox", help="Show inbox messages")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_inbox)

    # unread
    p = sub.add_parser("unread", help="Show unread messages")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_unread)

    # read
    p = sub.add_parser("read", help="Read a specific message")
    p.add_argument("message_id")
    p.set_defaults(func=cmd_read)

    # send
    p = sub.add_parser("send", help="Send an email")
    p.add_argument("--to", required=True)
    p.add_argument("--subject", default="(no subject)")
    p.add_argument("--body", required=True)
    p.set_defaults(func=cmd_send)

    # list-labels
    p = sub.add_parser("list-labels", help="List all labels")
    p.set_defaults(func=cmd_list_labels)
    p = sub.add_parser("list-labels-user", help="List user-defined labels only")
    p.set_defaults(func=cmd_list_labels_user)

    # delete
    p = sub.add_parser("delete", help="Delete (trash) a message")
    p.add_argument("message_id")
    p.set_defaults(func=cmd_delete)

    # label
    p = sub.add_parser("label", help="Add/remove a label on a message")
    p.add_argument("message_id")
    p.add_argument("label_name")
    p.add_argument("--add", dest="action", action="store_const", const="add", default="add")
    p.add_argument("--remove", dest="action", action="store_const", const="remove")
    p.set_defaults(func=cmd_label)

    # threads
    p = sub.add_parser("threads", help="List recent threads")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_threads)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()