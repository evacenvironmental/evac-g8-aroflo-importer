
import base64
import io
import mimetypes
import os
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Dict, List, Optional, Tuple

import pdfplumber
from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


def env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"true", "1", "yes", "y"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def get_gmail_service():
    scopes = env(
        "GOOGLE_SCOPES",
        "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send",
    ).split()

    creds = Credentials(
        token=None,
        refresh_token=env("GOOGLE_REFRESH_TOKEN", required=True),
        token_uri=env("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
        client_id=env("GOOGLE_CLIENT_ID", required=True),
        client_secret=env("GOOGLE_CLIENT_SECRET", required=True),
        scopes=scopes,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_labels(service) -> Dict[str, str]:
    response = service.users().labels().list(userId="me").execute()
    return {label["name"]: label["id"] for label in response.get("labels", [])}


def ensure_label(service, name: str) -> str:
    labels = list_labels(service)
    if name in labels:
        return labels[name]

    created = service.users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    print(f"Created Gmail label: {name}")
    return created["id"]


def get_header(headers: List[Dict[str, str]], name: str) -> str:
    wanted = name.lower()
    for h in headers:
        if h.get("name", "").lower() == wanted:
            return h.get("value", "")
    return ""


def walk_parts(payload: Dict) -> List[Dict]:
    parts = []
    if payload:
        parts.append(payload)
    for part in payload.get("parts", []) or []:
        parts.extend(walk_parts(part))
    return parts


def decode_body_data(data: str) -> str:
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data.encode("utf-8"))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def clean_text(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()




def html_to_text(html: str) -> str:
    """Convert email HTML into readable plain text for AroFlo import body."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")

    # Remove hidden/noisy elements.
    for tag in soup(["style", "script", "head", "title", "meta"]):
        tag.decompose()

    # Add line breaks around common block tags before extracting text.
    for tag in soup.find_all(["p", "div", "br", "tr", "table", "li"]):
        tag.append("\n")

    text = soup.get_text("\n")
    return clean_text(text)




def looks_like_html(text: str) -> bool:
    return bool(re.search(r"<\s*(html|body|table|tbody|tr|td|p|span|div|br)\b", text or "", flags=re.IGNORECASE))


def should_ignore_attachment(filename: str, mime_type: str, size: int, headers: List[Dict[str, str]]) -> bool:
    ignore_inline = env_bool("IGNORE_INLINE_IMAGES", True)
    min_real_image_size_kb = env_int("MIN_REAL_IMAGE_SIZE_KB", 50)

    if not ignore_inline:
        return False

    name = (filename or "").lower()
    disposition = " ".join(
        h.get("value", "") for h in headers if h.get("name", "").lower() == "content-disposition"
    ).lower()

    if mime_type.startswith("image/"):
        if "inline" in disposition:
            return True
        if size and size < min_real_image_size_kb * 1024:
            return True
        if re.match(r"^(image|logo|signature|facebook|linkedin|instagram|twitter|x)[-_ ]?\d*\.(png|jpe?g|gif|webp)$", name):
            return True

    return False


def extract_email_content_and_attachments(service, message: Dict) -> Tuple[str, List[Dict]]:
    payload = message.get("payload", {})
    plain_chunks = []
    html_chunks = []
    attachments = []

    for part in walk_parts(payload):
        mime_type = part.get("mimeType", "")
        filename = part.get("filename", "")
        body = part.get("body", {}) or {}
        headers = part.get("headers", []) or []

        if mime_type == "text/plain" and body.get("data"):
            plain_chunks.append(decode_body_data(body.get("data", "")))

        elif mime_type == "text/html" and body.get("data"):
            html_chunks.append(html_to_text(decode_body_data(body.get("data", ""))))

        attachment_id = body.get("attachmentId")
        if filename and attachment_id:
            size = int(body.get("size", 0) or 0)
            if should_ignore_attachment(filename, mime_type, size, headers):
                print(f"Ignoring likely inline/signature attachment: {filename}")
                continue

            att = service.users().messages().attachments().get(
                userId="me",
                messageId=message["id"],
                id=attachment_id,
            ).execute()

            data = base64.urlsafe_b64decode(att["data"].encode("utf-8"))
            attachments.append({
                "filename": filename,
                "mime_type": mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
                "data": data,
                "size": len(data),
            })

    # Prefer the plain-text email body if Gmail provides one.
    # Some systems incorrectly put HTML inside text/plain, so detect and clean that too.
    plain_text = "\n\n".join(plain_chunks).strip()
    if looks_like_html(plain_text):
        plain_text = html_to_text(plain_text)

    html_text = "\n\n".join(html_chunks).strip()
    body_text = plain_text or html_text
    return clean_text(body_text), attachments


def pdf_text(data: bytes) -> str:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return clean_text("\n".join(page.extract_text() or "" for page in pdf.pages))
    except Exception as exc:
        print(f"PDF text extraction failed: {exc}")
        return ""


def first_match(text: str, patterns: List[str], default: str = "") -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if match:
            return clean_text(match.group(1))
    return default




def amount_after_label(text: str, labels: List[str]) -> str:
    """Find the first currency/number amount close after a label like Job Spend or Approved Purchase Limit."""
    if not text:
        return ""
    lower = text.lower()
    for label in labels:
        idx = lower.find(label.lower())
        if idx == -1:
            continue

        # Look shortly after the label. This handles formats such as:
        # Approved Purchase Limit ($) 500
        # Approved Purchase Limit ($): 500
        # Job Spend: 4,504.50
        chunk = text[idx + len(label): idx + len(label) + 250]
        match = re.search(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", chunk)
        if match:
            return match.group(1)
    return ""


def extract_work_order_fields(anchor_filename: str, anchor_text: str, email_body: str, subject: str) -> Dict[str, str]:
    combined = "\n".join([subject, email_body, anchor_filename, anchor_text])

    work_order_no = first_match(combined, [
        r"G8\s*Ed\s*Task\s*JN\s*([0-9]+)",
        r"Work\s*Order\s*No\s*[:#]?\s*([0-9]+)",
        r"CUST\s*ON\s*[:#]?\s*([0-9]+)",
        r"Work\s*Order\s*:\s*([0-9]+)",
    ], "UNKNOWN")

    po_number = first_match(combined, [
        r"PO\s*Number\s*[:#]?\s*([A-Z0-9\-]+)",
        r"\bPO\s+([A-Z0-9\-]+)",
        r"PO\s*([0-9]{5,})",
    ], "UNKNOWN")

    priority = first_match(combined, [
        r"Priority\s*[:#]?\s*([^\n\r]+)",
    ], "")

    job_spend = first_match(combined, [
        r"Job\s*Spend\s*[:#]?\s*\$?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        r"Approved\s*Purchase\s*Limit\s*\([^\)]*\)\s*[:#]?\s*\$?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        r"Approved\s*Purchase\s*Limit\s*[:#]?\s*\$?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
    ], "")

    if not job_spend:
        job_spend = amount_after_label(combined, ["Job Spend", "Approved Purchase Limit", "Approved Purchase Limit ($)"])

    site = first_match(anchor_text, [
        r"Site\s*:\s*([^\n\r]+)",
    ], "")

    location = first_match(anchor_text, [
        r"Location\s*:\s*([^\n\r]+(?:\n[^\n\r:]+)?)",
    ], "")

    service_category = first_match(combined, [
        r"Service\s*Category\s*[:#]?\s*([^\n\r]+)",
    ], "")

    description = first_match(combined, [
        r"Description\s*[:#]?\s*(.*?)(?:Approved Quote Number|Email body to accept|$)",
        r"Issue\s*[:#]?\s*([^\n\r]+)",
    ], "")

    approved_quote_number = first_match(combined, [
        r"Approved\s*Quote\s*Number.*?[:#]?\s*([A-Z0-9\-]+|False|True)",
    ], "False")
    if approved_quote_number.strip().lower() in {"upon", "please", "n/a", "na", "none"}:
        approved_quote_number = "False"

    return {
        "work_order_no": work_order_no,
        "po_number": po_number,
        "priority": priority,
        "job_spend": job_spend,
        "site": site,
        "location": location,
        "service_category": service_category,
        "description": description,
        "approved_quote_number": approved_quote_number,
    }


def find_anchor_pdf(attachments: List[Dict]) -> Tuple[Optional[Dict], str]:
    pdfs = [
        a for a in attachments
        if a["filename"].lower().endswith(".pdf") or a["mime_type"] == "application/pdf"
    ]

    # Best case: the PDF filename contains the G8 task pattern.
    for pdf in pdfs:
        if re.search(r"G8\s*Ed\s*Task\s*JN", pdf["filename"], flags=re.IGNORECASE):
            text = pdf_text(pdf["data"])
            return pdf, text

    # Next best case: the PDF text clearly looks like a G8 work order.
    for pdf in pdfs:
        text = pdf_text(pdf["data"])
        if "G8 Education Work Order" in text or re.search(r"Work\s*Order\s*No", text, flags=re.IGNORECASE):
            return pdf, text

    # Fallback: if there is any PDF at all, use the first PDF.
    # Some G8 PDFs are image/scanned or have filenames that differ from the expected pattern.
    if pdfs:
        print(f"No exact G8 anchor match. Falling back to first PDF: {pdfs[0]['filename']}")
        return pdfs[0], pdf_text(pdfs[0]["data"])

    return None, ""


def build_import_body(fields: Dict[str, str], original_subject: str, from_email: str, reply_to: str, email_body: str) -> str:
    timestamp = datetime.now(timezone.utc).isoformat()

    description = fields.get("description") or ""
    if fields.get("service_category") and fields["service_category"].lower() not in description.lower():
        description = f"Service Category: {fields['service_category']}\n{description}".strip()

    return f"""Import: {fields.get("work_order_no", "UNKNOWN")} PO {fields.get("po_number", "UNKNOWN")}

Imported as of {timestamp}

Subject: {original_subject}

CUST ON: {fields.get("work_order_no", "UNKNOWN")} PO {fields.get("po_number", "UNKNOWN")}
Priority: {fields.get("priority", "")}
Job Spend: {fields.get("job_spend", "")}
Reporting Email: {from_email}
Reply-To Email: {reply_to}

Description:
{description}

Approved Quote Number, if applicable: {fields.get("approved_quote_number", "False")}

Email body to accept work order:
{email_body}
""".strip()


def send_email(service, to_email: str, from_email: str, subject: str, body: str, attachments: List[Dict]):
    msg = EmailMessage()
    msg["To"] = to_email
    msg["From"] = from_email
    msg["Subject"] = subject
    msg.set_content(body)

    for att in attachments:
        maintype, subtype = (att["mime_type"].split("/", 1) + ["octet-stream"])[:2] if "/" in att["mime_type"] else ("application", "octet-stream")
        msg.add_attachment(att["data"], maintype=maintype, subtype=subtype, filename=att["filename"])

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def modify_labels(service, message_id: str, add: List[str], remove: List[str]):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": add, "removeLabelIds": remove},
    ).execute()


def process_message(service, message_id: str, label_ids: Dict[str, str], dry_run: bool):
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    existing_labels = set(message.get("labelIds", []))

    if existing_labels.intersection({
        label_ids["processed"],
        label_ids["review"],
        label_ids["failed"],
    }):
        print(f"Skipping already handled email: {message_id}")
        return

    headers = message.get("payload", {}).get("headers", [])
    original_subject = get_header(headers, "Subject")
    raw_from = get_header(headers, "From")
    raw_reply_to = get_header(headers, "Reply-To")
    from_email = parseaddr(raw_from)[1] or raw_from
    reply_to = parseaddr(raw_reply_to)[1] or ""

    print(f"Processing email: {original_subject}")
    print(f"From: {from_email}")

    email_body, attachments = extract_email_content_and_attachments(service, message)
    max_attachments = env_int("MAX_ATTACHMENTS_PER_EMAIL", 10)

    if len(attachments) > max_attachments:
        raise RuntimeError(f"Too many attachments: {len(attachments)} > {max_attachments}")

    if not attachments:
        raise RuntimeError("No usable attachments found")

    print(f"Usable attachments found ({len(attachments)}):")
    for att in attachments:
        print(f"  - {att['filename']} ({att['mime_type']}, {att['size']} bytes)")

    anchor_pdf, anchor_text = find_anchor_pdf(attachments)
    if not anchor_pdf:
        raise RuntimeError("No PDF attachment found to use as anchor")

    print(f"Anchor PDF found: {anchor_pdf['filename']}")

    fields = extract_work_order_fields(anchor_pdf["filename"], anchor_text, email_body, original_subject)

    reporting_source = env("REPORTING_EMAIL_SOURCE", "from").strip().lower()
    reporting_email = reply_to if reporting_source == "reply_to" and reply_to else from_email

    import_body = build_import_body(fields, original_subject, reporting_email, reply_to, email_body)
    import_subject = f"Import: {fields.get('work_order_no', 'UNKNOWN')} PO {fields.get('po_number', 'UNKNOWN')}"

    print("Extracted:")
    print(f"  Work Order: {fields.get('work_order_no')}")
    print(f"  PO: {fields.get('po_number')}")
    print(f"  Priority: {fields.get('priority')}")
    print(f"  Attachments: {[a['filename'] for a in attachments]}")

    if dry_run:
        print("DRY_RUN=true: not sending to AroFlo and not changing labels.")
        print("----- AROFLO BODY PREVIEW -----")
        print(import_body[:4000])
        print("----- END PREVIEW -----")
        return

    aroflo_import_email = env("AROFLO_IMPORT_EMAIL", required=True)
    send_from_email = env("SEND_FROM_EMAIL", required=True)

    sent = send_email(
        service=service,
        to_email=aroflo_import_email,
        from_email=send_from_email,
        subject=import_subject,
        body=import_body,
        attachments=attachments,
    )
    print(f"Sent AroFlo import email: {sent.get('id')}")

    modify_labels(service, message_id, add=[label_ids["processed"]], remove=[label_ids["input"]])
    print("Marked email as processed")


def main():
    dry_run = env_bool("DRY_RUN", True)
    max_emails = env_int("MAX_EMAILS_PER_RUN", 5)

    print("Starting EVAC G8 AroFlo importer")
    print(f"DRY_RUN={dry_run}")

    service = get_gmail_service()

    input_label_name = env("GMAIL_INPUT_LABEL", "Zap-G8workorders")
    processed_label_name = env("GMAIL_PROCESSED_LABEL", "Zap-G8workorders/Processed")
    review_label_name = env("GMAIL_REVIEW_LABEL", "Zap-G8workorders/Needs Review")
    failed_label_name = env("GMAIL_FAILED_LABEL", "Zap-G8workorders/Failed")

    label_ids = {
        "input": ensure_label(service, input_label_name),
        "processed": ensure_label(service, processed_label_name),
        "review": ensure_label(service, review_label_name),
        "failed": ensure_label(service, failed_label_name),
    }

    response = service.users().messages().list(
        userId="me",
        labelIds=[label_ids["input"]],
        maxResults=max_emails,
    ).execute()

    messages = response.get("messages", [])
    print(f"Found {len(messages)} email(s) under label {input_label_name}")

    for item in messages:
        message_id = item["id"]
        try:
            process_message(service, message_id, label_ids, dry_run)
        except Exception as exc:
            print(f"ERROR processing message {message_id}: {exc}")
            if not dry_run:
                try:
                    modify_labels(service, message_id, add=[label_ids["review"]], remove=[])
                except Exception as label_exc:
                    print(f"Could not apply review label: {label_exc}")


if __name__ == "__main__":
    try:
        main()
    except HttpError as exc:
        print(f"Google API error: {exc}")
        raise
