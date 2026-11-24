"""GitHub -> Telegram notifier. Polls GitHub for new events on your repos
and sends a Telegram message for each one."""
import html
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()
GH_TOKEN = os.environ["GITHUB_TOKEN"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "80"))

GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def send_telegram(text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if not r.ok:
        print("Telegram error:", r.text)


def get_repos() -> list[str]:
    """All repos you own (public + private)."""
    r = requests.get(
        "https://api.github.com/user/repos",
        headers=GH_HEADERS,
        params={"affiliation": "owner", "per_page": 100},
        timeout=15,
    )
    r.raise_for_status()
    return [repo["full_name"] for repo in r.json()]


def get_events(repo: str) -> list[dict]:
    r = requests.get(
        f"https://api.github.com/repos/{repo}/events",
        headers=GH_HEADERS,
        params={"per_page": 30},
        timeout=15,
    )
    if not r.ok:
        print(f"GitHub error for {repo}: {r.status_code}")
        return []
    return r.json()


def describe(event: dict) -> str | None:
    """Turn a GitHub event into a message. Return None to ignore it."""
    actor = html.escape(event["actor"]["login"])
    repo = html.escape(event["repo"]["name"])
    url = f"https://github.com/{event['repo']['name']}"
    p = event["payload"]
    kind = event["type"]

    if kind == "PushEvent":
        branch = html.escape(p.get("ref", "").replace("refs/heads/", ""))
        commits = p.get("commits", [])
        lines = [f"🚀 <b>{actor}</b> pushed to <b>{repo}</b> ({branch})"]
        for c in commits[:5]:
            msg = html.escape(c["message"].split("\n")[0])
            lines.append(f"• {msg}")
        if len(commits) > 5:
            lines.append(f"…and {len(commits) - 5} more")
        lines.append(url)
        return "\n".join(lines)

    if kind == "PullRequestEvent":
        pr = p["pull_request"]
        title = html.escape(pr["title"])
        return f"🔀 <b>{actor}</b> {p['action']} PR in <b>{repo}</b>: {title}\n{pr['html_url']}"

    if kind == "IssuesEvent":
        issue = p["issue"]
        title = html.escape(issue["title"])
        return f"🐛 <b>{actor}</b> {p['action']} issue in <b>{repo}</b>: {title}\n{issue['html_url']}"

    if kind == "IssueCommentEvent":
        issue = p["issue"]
        title = html.escape(issue["title"])
        return f"💬 <b>{actor}</b> commented in <b>{repo}</b> on: {title}\n{p['comment']['html_url']}"

    if kind == "CreateEvent":
        return f"✨ <b>{actor}</b> created {p['ref_type']} {html.escape(p.get('ref') or '')} in <b>{repo}</b>\n{url}"

    if kind == "DeleteEvent":
        return f"🗑 <b>{actor}</b> deleted {p['ref_type']} {html.escape(p.get('ref') or '')} in <b>{repo}</b>"

    if kind == "ForkEvent":
        return f"🍴 <b>{actor}</b> forked <b>{repo}</b>"

    if kind == "ReleaseEvent":
        return f"📦 <b>{actor}</b> {p['action']} a release in <b>{repo}</b>\n{p['release']['html_url']}"

    return None  # ignore stars, etc.


def main() -> None:
    last_seen: dict[str, int] = {}  # repo -> newest event id already handled
    send_telegram("✅ GitHub notifier started.")
    print("Bot running. Press Ctrl+C to stop.")

    while True:
        try:
            repos = get_repos()
            print("Watching:", repos)
            for repo in repos:
                events = get_events(repo)
                if not events:
                    continue

                if repo not in last_seen:
                    # First time we see this repo: remember position, don't spam history
                    last_seen[repo] = max(int(e["id"]) for e in events)
                    continue

                new = [e for e in events if int(e["id"]) > last_seen[repo]]
                for event in reversed(new):  # oldest first
                    msg = describe(event)
                    if msg:
                        send_telegram(msg)
                if new:
                    last_seen[repo] = max(int(e["id"]) for e in new)
        except Exception as exc:  # keep the bot alive on network hiccups
            print("Error:", exc)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()