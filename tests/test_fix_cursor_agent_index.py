import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fix_cursor_agent_index as recovery  # noqa: E402


def create_item_db(path: Path, rows: dict[str, object] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("create table ItemTable(key text primary key, value text)")
    for key, value in (rows or {}).items():
        conn.execute(
            "insert into ItemTable(key, value) values(?, ?)",
            (key, json.dumps(value, separators=(",", ":"))),
        )
    conn.commit()
    conn.close()


def read_json_item(path: Path, key: str) -> object:
    conn = sqlite3.connect(path)
    row = conn.execute("select value from ItemTable where key=?", (key,)).fetchone()
    conn.close()
    if row is None:
        raise AssertionError(f"Missing key: {key}")
    return json.loads(row[0])


class RecoveryScriptTests(unittest.TestCase):
    def test_project_slug_matches_cursor_project_folder_style(self) -> None:
        self.assertEqual(
            recovery.project_slug(Path("/Users/example/Desktop/sample_project")),
            "Users-example-Desktop-sample-project",
        )

    def test_cursor_running_detector_finds_cursor_app_processes(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="/Applications/Cursor.app/Contents/MacOS/Cursor\n",
        )
        with patch.object(recovery.subprocess, "run", return_value=completed):
            self.assertTrue(recovery.cursor_is_running())

    def test_apply_repairs_fake_cursor_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "sample_org" / "sample_project"
            workspace.mkdir(parents=True)
            cursor_home = root / ".cursor"
            app_support = root / "Application Support" / "Cursor"

            workspace_id = "workspace-current"
            old_workspace_id = "workspace-old"
            agent_project_id = "agent-project-current"
            old_project_id = "agent-project-old"
            stale_id = "11111111-1111-1111-1111-111111111111"
            current_id = "22222222-2222-2222-2222-222222222222"

            transcript_root = (
                cursor_home
                / "projects"
                / recovery.project_slug(workspace)
                / "agent-transcripts"
            )
            for composer_id in (stale_id, current_id):
                folder = transcript_root / composer_id
                folder.mkdir(parents=True)
                (folder / f"{composer_id}.jsonl").write_text(
                    '{"role":"user","message":{"content":[{"type":"text","text":"hi"}]}}\n',
                    encoding="utf-8",
                )

            workspace_storage = app_support / "User" / "workspaceStorage" / workspace_id
            workspace_storage.mkdir(parents=True)
            (workspace_storage / "workspace.json").write_text(
                json.dumps({"folder": workspace.resolve().as_uri()}),
                encoding="utf-8",
            )

            global_db = app_support / "User" / "globalStorage" / "state.vscdb"
            workspace_db = workspace_storage / "state.vscdb"

            old_identifier = {"id": old_workspace_id, "uri": {"fsPath": "/old"}}
            current_identifier = {
                "id": workspace_id,
                "uri": {
                    "$mid": 1,
                    "fsPath": str(workspace),
                    "external": workspace.as_uri(),
                    "path": str(workspace),
                    "scheme": "file",
                },
            }
            headers = {
                "allComposers": [
                    {
                        "type": "head",
                        "composerId": stale_id,
                        "name": "Stale chat",
                        "workspaceIdentifier": old_identifier,
                    },
                    {
                        "type": "head",
                        "composerId": current_id,
                        "name": "Current chat",
                        "workspaceIdentifier": current_identifier,
                    },
                ]
            }
            projects = [
                {
                    "id": agent_project_id,
                    "name": "Current project",
                    "workspace": {"id": workspace_id},
                    "isArchived": False,
                },
                {
                    "id": old_project_id,
                    "name": "Old project",
                    "workspace": {"id": old_workspace_id},
                    "isArchived": False,
                },
            ]
            create_item_db(
                global_db,
                {
                    "composer.composerHeaders": headers,
                    "glass.localAgentProjectMembership.v1": {
                        stale_id: old_project_id,
                        current_id: agent_project_id,
                    },
                    "glass.localAgentProjects.v1": projects,
                },
            )
            create_item_db(workspace_db)

            script = ROOT / "fix_cursor_agent_index.py"
            dry_run = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--workspace-path",
                    str(workspace),
                    "--cursor-home",
                    str(cursor_home),
                    "--app-support",
                    str(app_support),
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertIn(stale_id, dry_run.stdout)
            self.assertIn('"apply": false', dry_run.stdout)

            apply_run = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--workspace-path",
                    str(workspace),
                    "--cursor-home",
                    str(cursor_home),
                    "--app-support",
                    str(app_support),
                    "--apply",
                    "--allow-running",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertIn('"patched_headers": 1', apply_run.stdout)
            self.assertIn('"patched_memberships": 1', apply_run.stdout)

            repaired_headers = read_json_item(global_db, "composer.composerHeaders")
            by_id = {
                item["composerId"]: item
                for item in repaired_headers["allComposers"]
            }
            self.assertEqual(
                by_id[stale_id]["workspaceIdentifier"]["id"],
                workspace_id,
            )

            membership = read_json_item(global_db, "glass.localAgentProjectMembership.v1")
            self.assertEqual(membership[stale_id], agent_project_id)
            self.assertEqual(membership[current_id], agent_project_id)

            composer_data = read_json_item(workspace_db, "composer.composerData")
            self.assertEqual(
                composer_data["selectedComposerIds"],
                [stale_id, current_id],
            )

            self.assertTrue(
                list(global_db.parent.glob("state.vscdb.backup-before-agent-reindex-*"))
            )
            self.assertTrue(
                list(workspace_db.parent.glob("state.vscdb.backup-before-agent-reindex-*"))
            )


if __name__ == "__main__":
    unittest.main()
