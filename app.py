import os
import json
import base64
from email.message import EmailMessage

from flask import Flask, render_template, request, session, redirect, url_for
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # OK for localhost

# Configuration
CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.settings.basic"]
REDIRECT_URI = "http://localhost:5000/callback"

anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


def generate_email_draft(recipient, purpose, key_points, tone):
    """Use Claude to generate a polished email draft."""
    tone_instruction = tone

    prompt = f"""Write a professional email with the following details:

Recipient: {recipient}
Purpose: {purpose}
Key Points to include:
{key_points}

Tone instruction: {tone_instruction}

Write a complete, polished email draft with an appropriate subject line, greeting, body, and closing. Sign off with just "Best," or "Thanks," followed by a blank line — do not use "[Your name]" or any placeholder for the signature."""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text


def get_gmail_service(token=None):
    """Get Gmail API service with credentials."""
    if token:
        creds = Credentials.from_authorized_user_info(token, SCOPES)
    else:
        flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES, redirect_uri=REDIRECT_URI)
        flow.fetch_token(code=session.get("auth_code"))
        creds = flow.credentials
        session["token"] = creds.to_json()

    return build("gmail", "v1", credentials=creds)


def create_draft_message(subject, body, to):
    """Create a draft email message."""
    message = EmailMessage()
    message.set_content(body)
    message["To"] = to
    message["From"] = "me"
    message["Subject"] = subject

    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return {"raw": encoded_message}


def send_message(service, user_id, message):
    """Send an email."""
    return service.users().messages().send(userId=user_id, body=message).execute()


def create_draft(service, user_id, message):
    """Create a draft in Gmail."""
    return service.users().drafts().create(userId=user_id, body={"message": message}).execute()


@app.route("/")
def index():
    """Show the email form."""
    if "token" in session:
        return render_template("index.html", logged_in=True)
    return render_template("index.html", logged_in=False)


@app.route("/login")
def login():
    """Start OAuth flow."""
    flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES, redirect_uri=REDIRECT_URI)
    authorization_url, state = flow.authorization_url(prompt="consent", access_type="offline")
    session["state"] = state
    return redirect(authorization_url)


@app.route("/callback")
def callback():
    """OAuth callback - store credentials."""
    flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES, redirect_uri=REDIRECT_URI, state=session.get("state"))
    flow.fetch_token(code=request.args.get("code"))
    session["token"] = flow.credentials.to_json()
    return redirect("/")


@app.route("/generate", methods=["POST"])
def generate():
    """Generate email draft using Claude."""
    if "token" not in session:
        return redirect("/login")

    recipient = request.form.get("recipient", "")
    purpose = request.form.get("purpose", "")
    key_points = request.form.get("key_points", "")
    tone = request.form.get("tone", "formal")

    draft_content = generate_email_draft(recipient, purpose, key_points, tone)

    # Parse subject and body from Claude's response
    lines = draft_content.split("\n")
    subject = "No Subject"
    body_start = 0

    for i, line in enumerate(lines):
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            body_start = i + 1
            break

    while body_start < len(lines) and lines[body_start].strip() == "":
        body_start += 1

    body = "\n".join(lines[body_start:]).strip()

    session["last_email"] = {
        "recipient": recipient,
        "subject": subject,
        "body": body
    }

    return render_template("result.html", subject=subject, body=body, recipient=recipient)


@app.route("/send", methods=["POST"])
def send():
    """Send the generated email."""
    if "token" not in session:
        return redirect("/login")

    email_data = session.get("last_email", {})
    if not email_data:
        return redirect("/")

    if request.form.get("edited_subject"):
        email_data["subject"] = request.form.get("edited_subject")
    if request.form.get("edited_body"):
        email_data["body"] = request.form.get("edited_body")

    try:
        creds = Credentials.from_authorized_user_info(json.loads(session["token"]), SCOPES)
        service = build("gmail", "v1", credentials=creds)

        profile = service.users().getProfile(userId="me").execute()
        sender_email = profile.get("emailAddress", "")
        settings = service.users().settings().sendAs().list(userId="me").execute()
        sender_name = "there"
        for alias in settings.get("sendAs", []):
            if alias.get("isPrimary"):
                sender_name = alias.get("displayName", "there")
                break

        message_body = email_data["body"].replace("[Your name]", sender_name).replace("[Your Name]", sender_name)
        message = create_draft_message(email_data["subject"], message_body, email_data["recipient"])
        sent_message = send_message(service, "me", message)

        return render_template("success.html", action="sent", message_id=sent_message.get("id", ""))

    except HttpError as error:
        return render_template("error.html", error=str(error))


@app.route("/save-draft", methods=["POST"])
def save_draft():
    """Save as Gmail draft."""
    if "token" not in session:
        return redirect("/login")

    email_data = session.get("last_email", {})
    if not email_data:
        return redirect("/")

    if request.form.get("edited_subject"):
        email_data["subject"] = request.form.get("edited_subject")
    if request.form.get("edited_body"):
        email_data["body"] = request.form.get("edited_body")

    try:
        creds = Credentials.from_authorized_user_info(json.loads(session["token"]), SCOPES)
        service = build("gmail", "v1", credentials=creds)

        profile = service.users().getProfile(userId="me").execute()
        sender_email = profile.get("emailAddress", "")
        
        settings = service.users().settings().sendAs().list(userId="me").execute()
        sender_name = "there"
        for alias in settings.get("sendAs", []):
            if alias.get("isPrimary"):
                sender_name = alias.get("displayName", "there")
                break

        message_body = email_data["body"].replace("[Your name]", sender_name).replace("[Your Name]", sender_name)
        message = create_draft_message(email_data["subject"], message_body, email_data["recipient"])
        draft = create_draft(service, "me", message)

        return render_template("success.html", action="saved as draft", draft_id=draft.get("id", ""))

    except HttpError as error:
        return render_template("error.html", error=str(error))


@app.route("/logout")
def logout():
    """Clear session."""
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
