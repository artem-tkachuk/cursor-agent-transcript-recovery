#!/usr/bin/env python3
"""Repair Cursor Agent transcript indexing for a local workspace.

Run from an external terminal after quitting Cursor. The script updates Cursor's
local SQLite state so transcript JSONL files that already exist on disk are
associated with the active workspace and show up in the Agent picker again.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def default_app_support_dir(
    platform: str = sys.platform,
    environ: dict[str, str] | None = None,
) -> Path:
    if platform == "darwin":
        return Path.home() / "Library/Application Support/Cursor"

    if platform.startswith("linux"):
        env = os.environ if environ is None else environ
        config_home = env.get("XDG_CONFIG_HOME")
        if config_home:
            return Path(config_home).expanduser() / "Cursor"
        return Path.home() / ".config" / "Cursor"

    return Path.home() / ".config" / "Cursor"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recover Cursor Agent transcript index.")
    parser.add_argument("--workspace-path", default=".", help="Workspace folder. Defaults to cwd.")
    parser.add_argument("--cursor-home", default=str(Path.home() / ".cursor"))
    parser.add_argument(
        "--app-support",
        default=str(default_app_support_dir()),
        help="Cursor application support directory.",
    )
    parser.add_argument("--transcript-root", help="Override agent-transcripts path.")
    parser.add_argument("--workspace-id", help="Override Cursor workspaceStorage id.")
    parser.add_argument("--agent-project-id", help="Override local Agent project id.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run.")
    parser.add_argument(
        "--allow-running",
        action="store_true",
        help="Allow running while Cursor is open. Not recommended.",
    )
    return parser.parse_args()


def command_is_cursor(command: str) -> bool:
    if not command:
        return False

    if "/Applications/Cursor.app/" in command:
        return True
    if "Cursor Helper" in command and (
        "Application Support/Cursor" in command or ".config/Cursor" in command
    ):
        return True

    executable = command.split(maxsplit=1)[0]
    name = Path(executable).name.lower()
    return name in {"cursor", "cursor-url-handler"}


def cursor_is_running() -> bool:
    result = subprocess.run(
        ["ps", "ax", "-o", "command="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False

    for command in result.stdout.splitlines():
        if command_is_cursor(command):
            return True
    return False


def project_slug(path: Path) -> str:
    raw = "-".join(part for part in path.resolve().parts if part != path.anchor)
    return re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-")


def file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def backup_sqlite(path: Path, timestamp: str) -> Path:
    backup = path.with_name(f"{path.name}.backup-before-agent-reindex-{timestamp}")
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    target = sqlite3.connect(backup)
    with target:
        source.backup(target)
    source.close()
    target.close()
    return backup


def load_json(conn: sqlite3.Connection, key: str, default: Any) -> Any:
    row = conn.execute("select value from ItemTable where key=?", (key,)).fetchone()
    return default if row is None else json.loads(row[0])


def find_transcript_root(cursor_home: Path, workspace_path: Path) -> Path:
    root = cursor_home / "projects" / project_slug(workspace_path) / "agent-transcripts"
    if not root.exists():
        raise FileNotFoundError(f"Transcript root not found: {root}")
    return root


def find_workspace_id(app_support: Path, workspace_path: Path) -> str:
    workspace_uri = file_uri(workspace_path)
    storage_root = app_support / "User" / "workspaceStorage"
    for workspace_json in storage_root.glob("*/workspace.json"):
        try:
            data = json.loads(workspace_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("folder") == workspace_uri or data.get("workspace") == workspace_uri:
            return workspace_json.parent.name
    raise FileNotFoundError("Could not auto-detect workspace id. Pass --workspace-id.")


def find_agent_project_id(projects: list[dict[str, Any]], workspace_id: str) -> str | None:
    for project in projects:
        workspace = project.get("workspace") or {}
        if workspace.get("id") == workspace_id and not project.get("isArchived", False):
            return project.get("id")
    for project in projects:
        workspace = project.get("workspace") or {}
        if workspace.get("id") == workspace_id:
            return project.get("id")
    return None


def main() -> int:
    args = parse_args()
    workspace_path = Path(args.workspace_path).expanduser().resolve()
    cursor_home = Path(args.cursor_home).expanduser()
    app_support = Path(args.app_support).expanduser()

    if args.apply and cursor_is_running() and not args.allow_running:
        print(
            "Cursor is running. Quit Cursor completely, then rerun with --apply.",
            file=sys.stderr,
        )
        return 2

    transcript_root = (
        Path(args.transcript_root).expanduser()
        if args.transcript_root
        else find_transcript_root(cursor_home, workspace_path)
    )
    workspace_id = args.workspace_id or find_workspace_id(app_support, workspace_path)

    global_db = app_support / "User" / "globalStorage" / "state.vscdb"
    workspace_db = app_support / "User" / "workspaceStorage" / workspace_id / "state.vscdb"

    transcript_ids = sorted(
        path.name
        for path in transcript_root.iterdir()
        if path.is_dir() and (path / f"{path.name}.jsonl").exists()
    )
    if not transcript_ids:
        print(f"No parent transcript JSONLs found under {transcript_root}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(global_db)
    headers = load_json(conn, "composer.composerHeaders", {"allComposers": []})
    membership = load_json(conn, "glass.localAgentProjectMembership.v1", {})
    projects = load_json(conn, "glass.localAgentProjects.v1", [])

    by_id = {item.get("composerId"): item for item in headers.get("allComposers", [])}
    missing_headers = [cid for cid in transcript_ids if cid not in by_id]
    stale_headers = [
        cid
        for cid in transcript_ids
        if cid in by_id and (by_id[cid].get("workspaceIdentifier") or {}).get("id") != workspace_id
    ]

    agent_project_id = args.agent_project_id or find_agent_project_id(projects, workspace_id)
    stale_memberships: list[str] = []
    if agent_project_id:
        stale_memberships = [cid for cid in transcript_ids if membership.get(cid) != agent_project_id]

    report = {
        "workspace_path": str(workspace_path),
        "workspace_id": workspace_id,
        "transcript_root": str(transcript_root),
        "transcripts": len(transcript_ids),
        "missing_headers": missing_headers,
        "stale_headers": stale_headers,
        "agent_project_id": agent_project_id,
        "stale_memberships": stale_memberships,
        "apply": args.apply,
    }
    print(json.dumps(report, indent=2))

    if not args.apply:
        print("Dry run only. Quit Cursor and rerun with --apply to write changes.")
        conn.close()
        return 0

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backups = [backup_sqlite(global_db, timestamp), backup_sqlite(workspace_db, timestamp)]

    workspace_identifier = {
        "id": workspace_id,
        "uri": {
            "$mid": 1,
            "fsPath": str(workspace_path),
            "external": file_uri(workspace_path),
            "path": str(workspace_path),
            "scheme": "file",
        },
    }

    for composer_id in stale_headers:
        by_id[composer_id]["workspaceIdentifier"] = workspace_identifier
    conn.execute(
        "update ItemTable set value=? where key=?",
        (json.dumps(headers, separators=(",", ":")), "composer.composerHeaders"),
    )

    if agent_project_id:
        for composer_id in stale_memberships:
            membership[composer_id] = agent_project_id
        conn.execute(
            "update ItemTable set value=? where key=?",
            (
                json.dumps(membership, separators=(",", ":")),
                "glass.localAgentProjectMembership.v1",
            ),
        )

    recent_cache = {
        "agentId": transcript_ids[-1],
        "version": 3,
        "cachedAtMs": int(datetime.now().timestamp() * 1000),
    }
    conn.execute(
        "insert or replace into ItemTable(key,value) values(?, ?)",
        (
            "cursor/glass.startupDefaultStateRecentChatCache",
            json.dumps(recent_cache, separators=(",", ":")),
        ),
    )
    conn.commit()
    conn.close()

    workspace_conn = sqlite3.connect(workspace_db)
    composer_data = {
        "selectedComposerIds": transcript_ids,
        "lastFocusedComposerIds": list(reversed(transcript_ids)),
        "hasMigratedComposerData": True,
        "hasMigratedMultipleComposers": True,
    }
    workspace_conn.execute(
        "insert or replace into ItemTable(key,value) values(?, ?)",
        ("composer.composerData", json.dumps(composer_data, separators=(",", ":"))),
    )
    workspace_conn.commit()
    workspace_conn.close()

    print(
        json.dumps(
            {
                "patched_headers": len(stale_headers),
                "patched_memberships": len(stale_memberships),
                "backups": [str(path) for path in backups],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
