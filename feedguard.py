"""
FeedGuard - protects a Google Shopping feed from silent truncation.
Runs inside GitHub Actions. No server, no cloud account, no card.

Modes:
  python feedguard.py check   - scheduled check
  python feedguard.py trust   - operator: the drop was intentional
  python feedguard.py keep    - operator: something is broken, hold the old feed
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from lxml import etree

GNS = "http://base.google.com/ns/1.0"
ID_TAG = f"{{{GNS}}}id"

FEED_URL = os.environ.get("FEED_URL", "")
MAX_REMOVED = int(os.environ.get("MAX_REMOVED", "100"))

STATE_FILE = Path("state.json")
OUTPUT_DIR = Path("public")
OUTPUT_FILE = OUTPUT_DIR / "feed.xml"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_state():
    if not STATE_FILE.exists():
        return {"baseline": None, "status": "ok", "history": []}
    try:
        return json.loads(STATE_FILE.read_text())
    except (ValueError, OSError):
        return {"baseline": None, "status": "ok", "history": []}


def write_state(state):
    state["history"] = state.get("history", [])[-50:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


def publish(xml_bytes):
    OUTPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_FILE.write_bytes(xml_bytes)


def fetch_feed(url, attempts=3):
    last = None
    for _ in range(attempts):
        try:
            r = requests.get(url, timeout=300,
                             headers={"User-Agent": "FeedGuard/1.0"})
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            last = exc
    raise RuntimeError(f"could not download the feed after {attempts} tries: {last}")


def count_items(xml_bytes):
    """Strict parse. A truncated file must fail loudly, never half-parse."""
    if not xml_bytes:
        raise ValueError("the feed was empty")
    root = etree.fromstring(xml_bytes, parser=etree.XMLParser(huge_tree=True))
    channel = root.find("channel")
    if channel is None:
        raise ValueError("no <channel> element found - this is not a valid feed")
    ids = {
        item.findtext(ID_TAG).strip()
        for item in channel.findall("item")
        if item.findtext(ID_TAG) and item.findtext(ID_TAG).strip()
    }
    if not ids:
        raise ValueError("the feed contained no products")
    return len(ids)


def emit(**outputs):
    """Hand values back to the GitHub Actions workflow."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as fh:
        for key, value in outputs.items():
            fh.write(f"{key}={value}\n")


# --------------------------------------------------------------------------

def check():
    state = read_state()

    try:
        raw = fetch_feed(FEED_URL)
        count = count_items(raw)
    except Exception as exc:
        state["last_run"] = now()
        state["last_error"] = str(exc)
        state.setdefault("history", []).append(
            {"at": now(), "count": None, "result": "download_failed"})
        write_state(state)
        print(f"::error::Could not read the feed: {exc}")
        print("The last good feed is still being served to Google.")
        emit(result="download_failed", publish="false", detail=str(exc))
        return 1

    state["last_error"] = None
    state["last_count"] = count
    state["last_run"] = now()
    baseline = state.get("baseline")

    if baseline is None:
        publish(raw)
        state.update(baseline=count, status="ok")
        state.setdefault("history", []).append(
            {"at": now(), "count": count, "result": "first_publish"})
        write_state(state)
        print(f"Baseline set: {count} products. Feed published.")
        emit(result="first_publish", publish="true", count=count)
        return 0

    removed = baseline - count

    if state.get("status") == "pending":
        state["pending_count"] = count
        state.setdefault("history", []).append(
            {"at": now(), "count": count, "result": "still_waiting"})
        write_state(state)
        print(f"Still waiting for your decision. Feed currently has {count} "
              f"products, baseline is {baseline}. Nothing published.")
        emit(result="still_waiting", publish="false", count=count)
        return 0

    if state.get("status") == "held":
        if removed <= MAX_REMOVED:
            publish(raw)
            state.update(baseline=count, status="ok")
            state.setdefault("history", []).append(
                {"at": now(), "count": count, "result": "recovered"})
            write_state(state)
            print(f"Your feed recovered ({count} products). Publishing resumed.")
            emit(result="recovered", publish="true", count=count)
            return 0
        state.setdefault("history", []).append(
            {"at": now(), "count": count, "result": "still_held"})
        write_state(state)
        print(f"Still holding. Your feed has {count} products, expected "
              f"around {baseline}.")
        emit(result="still_held", publish="false", count=count)
        return 0

    if removed <= MAX_REMOVED:
        publish(raw)
        state.update(baseline=count, status="ok")
        state.setdefault("history", []).append(
            {"at": now(), "count": count, "result": "published"})
        write_state(state)
        print(f"Published {count} products (change: {-removed:+d}).")
        emit(result="published", publish="true", count=count)
        return 0

    # Too many gone. Stop and ask.
    state.update(status="pending", pending_count=count, pending_at=now())
    state.setdefault("history", []).append(
        {"at": now(), "count": count, "result": "asked"})
    write_state(state)
    print(f"::warning::{removed} products disappeared from your feed "
          f"({baseline} -> {count}). Nothing published.")
    emit(result="asked", publish="false", count=count,
         baseline=baseline, removed=removed)
    return 0


def trust():
    state = read_state()
    if state.get("status") != "pending":
        print("Nothing is waiting for a decision.")
        emit(result="nothing_pending", publish="false")
        return 0

    raw = fetch_feed(FEED_URL)
    count = count_items(raw)
    publish(raw)
    state.update(baseline=count, status="ok")
    state.setdefault("history", []).append(
        {"at": now(), "count": count, "result": "you_approved"})
    write_state(state)
    print(f"Published {count} products. That is your new normal.")
    emit(result="you_approved", publish="true", count=count)
    return 0


def keep():
    state = read_state()
    if state.get("status") != "pending":
        print("Nothing is waiting for a decision.")
        emit(result="nothing_pending", publish="false")
        return 0

    state["status"] = "held"
    state.setdefault("history", []).append(
        {"at": now(), "count": state.get("pending_count"), "result": "you_held"})
    write_state(state)
    print(f"Keeping the last good feed ({state.get('baseline')} products). "
          f"Publishing resumes automatically once your feed recovers.")
    emit(result="you_held", publish="false")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if not FEED_URL:
        print("::error::FEED_URL is not set. Add it in repository settings "
              "under Secrets and variables > Actions > Variables.")
        sys.exit(1)
    sys.exit({"check": check, "trust": trust, "keep": keep}[mode]())
