#!/usr/bin/env python3

import os
import re
import logging
import magic
from flask import Flask, render_template, url_for, send_from_directory, request, make_response
from bs4 import BeautifulSoup

app = Flask(__name__)

log = logging.getLogger('werkzeug')
log.disabled = True
app.logger.disabled = True

@app.route("/favicon.ico")
def favicon():
    return url_for("static", filename="data:,")


@app.route("/")
def home():
    ctx = app.config["CONTACTS"]
    messages = app.config["MESSAGES"]
    owner = app.config["OWNER"]
    printing = app.config["PRINTING"]
    device = app.config["DEVICE"]
    return render_template(
        "index.html",
        contacts=ctx,
        messages=messages,
        owner=owner,
        printing=printing,
        device=device,
        title="ICQ",
        description="ICQ Contacts",
    )


@app.route("/<contact>")
def conversation(contact):
    messages = app.config["MESSAGES"]
    ctx = app.config["CONTACTS"]
    owner = app.config["OWNER"]
    links = app.config["LINKS"]
    printing = app.config["PRINTING"]
    device=app.config["DEVICE"]
    contact_name = ctx[contact]
    avatar = get_avatar(contact_name)
    if contact in messages:
        messages = messages[contact]
        try:
            sorted_items = sorted(
                messages.items(), key=lambda item: item[1]["MESSAGE"]["TIME_RAW"]
            )
        except KeyError:
            sorted_items = sorted(
                messages.items(), key=lambda item: item[1]["MESSAGE"]["MESSAGE_ID"]
            )
        messages = dict(sorted_items)
    else:
        messages = None
    if device == "desktop":
        html = "desktop-conversations.html"
    else:
        html = "conversations.html"
    return render_template(
        html,
        contact_name=contact_name,
        contact=contact,
        contacts=ctx,
        messages=messages,
        owner=owner,
        avatar=avatar,
        links=links,
        printing=printing,
        device=device,
        title="ICQ",
        description="ICQ Conversations",
    )


@app.route("/contact/<contact>")
def contacts(contact):
    ctx = app.config["CONTACTS"]
    owner = app.config["OWNER"]
    contact_name = ctx[contact]
    links = app.config["LINKS"]
    printing = app.config["PRINTING"]
    device = app.config["DEVICE"]
    avatar = get_avatar(contact_name)
    return render_template(
        "contact.html",
        contact_name=contact_name,
        contact=contact,
        contacts=ctx,
        owner=owner,
        avatar=avatar,
        links=links,
        printing=printing,
        device=device,
        title=f"ICQ Contact - {contact}",
        description="ICQ Contacts",
    )


@app.route("/attachments.html")
def attachments():
    files = app.config["FILES"]
    printing = app.config["PRINTING"]
    links = app.config["LINKS"]
    device = app.config["DEVICE"]
    return render_template(
        "attachments.html",
        files=files,
        printing=printing,
        links=links,
        device=device,
        title="ICQ File Attachments",
        description="ICQ File Attachments",
    )


@app.route("/files/<path:file>")
def serve(file):
    dirname = os.path.dirname(os.path.abspath(file))
    filename = os.path.basename(os.path.abspath(file))
    mime = magic.Magic(mime=True)
    mime_type = mime.from_file(os.path.join(dirname, filename))
    response = make_response(send_from_directory(dirname, filename, as_attachment=False, mimetype=mime_type))
    response.headers['Content-Disposition'] = 'inline'
    return response


def get_avatar(contact):
    avatar_keys = ["AvatarPreview", "AvatarOriginal", "AvatarOther"]
    for key in avatar_keys:
        if key in contact and contact[key] is not None:
            return contact[key]
    return None


def clean_text(text: str) -> str:
    return " ".join(text.split())


def build_search_index(icqapp):
    index = []

    with icqapp.test_client() as client:
        url = "/"
        resp = client.get(f"{url}")
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.data, "html.parser")
            text = clean_text(soup.get_text(separator=" ", strip=True))
            index.append({"url": f"{url}", "content": text})
        for contact_id, _ in icqapp.config.get("CONTACTS", {}).items():
            url = f"/{contact_id}"
            resp = client.get(url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.data, "html.parser")
                text = clean_text(soup.get_text(" ", strip=True))
                index.append({"url": url, "content": text})
            url = f"/contact/{contact_id}"
            resp = client.get(url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.data, "html.parser")
                text = clean_text(soup.get_text(" ", strip=True))
                index.append(
                    {"url": url, "content": text}
                )
        resp = client.get("/attachments.html")
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.data, "html.parser")
            text = clean_text(soup.get_text(" ", strip=True))
            index.append({"url": "/attachments.html", "content": text})
        for contact_id, msgs in icqapp.config.get("MESSAGES", {}).items():
            for _, msg_obj in msgs.items():
                body = ""
                msg = msg_obj.get("MESSAGE", {})
                for key in ("TEXT", "BODY", "CONTENT", "MESSAGE"):
                    if isinstance(msg.get(key), str):
                        body = msg.get(key)
                        break
                if not body:
                    for v in msg_obj.values():
                        if isinstance(v, str) and len(v) < 2000:
                            body = v
                            break
                if body:
                    body = clean_text(body)
                    index.append({"url": f"/{contact_id}", "content": body})
        files = icqapp.config.get("FILES", {})
        files_texts = []
        if isinstance(files, dict):
            for _, fobj in files.items():
                if isinstance(fobj, dict):
                    for val in fobj.values():
                        if isinstance(val, str):
                            files_texts.append(val)
                elif isinstance(fobj, str):
                    files_texts.append(fobj)
        elif isinstance(files, list):
            for fobj in files:
                if isinstance(fobj, str):
                    files_texts.append(fobj)
        if files_texts:
            index.append(
                {
                    "url": "/attachments.html",
                    "content": clean_text(" ".join(files_texts)),
                }
            )
    icqapp.config["SEARCH_INDEX"] = index
    return index


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    results = {}
    if not query:
        return render_template("results.html", query=query, results=[])

    for entry in app.config.get("SEARCH_INDEX", []):
        content = entry["content"]
        matches = [
            (m.start(), m.end())
            for m in re.finditer(re.escape(query), content, re.IGNORECASE)
        ]
        snippets = []
        for start_pos, end_pos in matches:
            start = max(start_pos - 30, 0)
            end = min(end_pos + 30, len(content))
            snippet = content[start:end]
            snippet = (
                snippet[: start_pos - start]
                + "<mark>"
                + content[start_pos:end_pos]
                + "</mark>"
                + snippet[end_pos - start :]
            )
            snippets.append(snippet)
        if snippets:
            url = entry["url"]
            if url in results:
                for snippet in snippets:
                    results[url]["snippets"].append(snippet)
            else:
                results[url] = {}
                results[url]["url"] = url
                results[url]["snippets"] = snippets
    return render_template("results.html", query=query, results=results)
