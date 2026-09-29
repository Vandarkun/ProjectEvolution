"""ChangeDigestBuilder / Privacy Filtering 测试（phase-04 §8/§9）。"""

from __future__ import annotations

from project_evolution.evolution.change_digest import (
    ChangeDigestBuilder,
    privacy_exclusion,
)
from project_evolution.git.diff import FileChange, SnapshotIncrement

PRJ = "PRJ-test"


def inc(*changes: FileChange) -> SnapshotIncrement:
    return SnapshotIncrement(base_head="b" * 40, head="a" * 40, changes=list(changes))


def change(path: str, kind: str = "modified", **kw) -> FileChange:
    defaults = dict(
        path=path,
        change=kind,
        old_hash="0" * 64,
        new_hash="1" * 64,
        diff_text=(
            f"diff --git a/{path} b/{path}\n"
            "--- a/x\n+++ b/x\n@@ -1,2 +1,3 @@\n line1\n-old\n+new\n+added\n"
        ),
        added_lines=2,
        deleted_lines=1,
    )
    defaults.update(kw)
    return FileChange(**defaults)


# ------------------------------------------------------------------- privacy


def test_secret_files_never_in_digest() -> None:
    """phase-04 §9：.env / secret.key / credentials.json 不进入 digest。"""
    builder = ChangeDigestBuilder()
    digest = builder.build(
        inc(
            change("src/main.py"),
            change(".env"),
            change("config/secret.key"),
            change("credentials.json"),
            change("deploy/server.pem"),
        ),
        project_id=PRJ,
        task_id="TASK-1",
    )
    assert digest.changed_files == ["src/main.py"]
    assert all(
        path not in digest.changed_files
        for path in (".env", "config/secret.key", "credentials.json", "deploy/server.pem")
    )
    assert {item["path"] for item in digest.excluded} == {
        ".env",
        "config/secret.key",
        "credentials.json",
        "deploy/server.pem",
    }
    assert all(item["reason"] == "secret" for item in digest.excluded)
    # hunk 也不包含秘密文件
    assert all(h.path == "src/main.py" for h in digest.selected_hunks)


def test_generated_and_binary_filtered() -> None:
    builder = ChangeDigestBuilder()
    digest = builder.build(
        inc(
            change("bin/Debug/out.dll"),
            change("node_modules/pkg/index.js"),
            change("dist/bundle.js"),
            change("assets/logo.png", is_binary=True),
            change("src/app.py"),
        ),
        project_id=PRJ,
        task_id="TASK-1",
    )
    assert digest.changed_files == ["src/app.py"]
    reasons = {item["path"]: item["reason"] for item in digest.excluded}
    assert reasons["bin/Debug/out.dll"] == "generated"
    assert reasons["node_modules/pkg/index.js"] == "generated"
    assert reasons["dist/bundle.js"] == "generated"
    assert reasons["assets/logo.png"] == "binary"


def test_lock_filter_configurable() -> None:
    builder_with_locks = ChangeDigestBuilder(exclude_locks=False)
    digest = builder_with_locks.build(
        inc(change("poetry.lock")), project_id=PRJ, task_id="TASK-1"
    )
    assert digest.changed_files == ["poetry.lock"]

    builder_default = ChangeDigestBuilder()
    digest2 = builder_default.build(
        inc(change("poetry.lock")), project_id=PRJ, task_id="TASK-1"
    )
    assert digest2.changed_files == []
    assert digest2.excluded == [{"path": "poetry.lock", "reason": "lock"}]


def test_extra_patterns_from_config() -> None:
    builder = ChangeDigestBuilder(extra_exclude_patterns=("*.local", "private/*"))
    digest = builder.build(
        inc(change("settings.local"), change("private/notes.md"), change("ok.py")),
        project_id=PRJ,
        task_id="TASK-1",
    )
    assert digest.changed_files == ["ok.py"]
    assert all(item["reason"] == "secret" for item in digest.excluded)


def test_pel_directory_defensively_excluded() -> None:
    """采集层本就不该给出 .project-evolution/；digest 层防御性再过滤。"""
    builder = ChangeDigestBuilder()
    digest = builder.build(
        inc(change(".project-evolution/events/raw.jsonl"), change("src/a.py")),
        project_id=PRJ,
        task_id="TASK-1",
    )
    assert digest.changed_files == ["src/a.py"]
    assert digest.excluded[0]["reason"] == "pel-internal"


# -------------------------------------------------------------------- budget


def test_size_budget_truncates() -> None:
    big = "x" * 500
    builder = ChangeDigestBuilder(max_total_diff_chars=300)
    digest = builder.build(
        inc(
            change("a.py", diff_text="@@ -1 +1 @@\n" + big),
            change("b.py", diff_text="@@ -1 +1 @@\n" + big),
        ),
        project_id=PRJ,
        task_id="TASK-1",
    )
    assert digest.truncated is True
    assert len(digest.selected_hunks) == 0
    # 文件清单保留（事实不丢，只有 hunk 被裁剪）
    assert digest.changed_files == ["a.py", "b.py"]


def test_file_budget_truncates() -> None:
    builder = ChangeDigestBuilder(max_files=2)
    digest = builder.build(
        inc(change("a.py"), change("b.py"), change("c.py")),
        project_id=PRJ,
        task_id="TASK-1",
    )
    assert digest.truncated is True
    assert len(digest.changed_files) == 2
    assert {item["reason"] for item in digest.excluded} == {"file-budget"}


# -------------------------------------------------------------------- fields


def test_digest_fields() -> None:
    builder = ChangeDigestBuilder()
    digest = builder.build(
        inc(
            change("src/service.py"),
            change("tests/test_service.py"),
            change("docs/readme.md", kind="added"),
            change("old/temp.cs", kind="deleted"),
            change(
                "src/moved.py",
                kind="renamed",
                old_path="src/old.py",
                new_path="src/moved.py",
                similarity=0.9,
            ),
        ),
        project_id=PRJ,
        task_id="TASK-1",
        checkpoint_id="CHK-9",
    )
    assert digest.changed_files == [
        "docs/readme.md",
        "old/temp.cs",
        "src/moved.py",
        "src/service.py",
        "tests/test_service.py",
    ]
    assert digest.added_files == ["docs/readme.md"]
    assert digest.deleted_files == ["old/temp.cs"]
    assert digest.renamed_files == [
        {"old_path": "src/old.py", "new_path": "src/moved.py", "similarity": 0.9}
    ]
    assert digest.tests == ["tests/test_service.py"]
    assert digest.diff_stat == "5 files changed, 10 insertions(+), 5 deletions(-)"
    assert "5 files changed" in digest.summary
    assert digest.scope_confidence == "normal"
    assert digest.truncated is False
    # hunks 按 @@ 切分
    assert all(h.header.startswith("@@") for h in digest.selected_hunks)


def test_empty_increment_digest() -> None:
    digest = ChangeDigestBuilder().build(
        SnapshotIncrement(), project_id=PRJ, task_id="TASK-1"
    )
    assert digest.changed_files == []
    assert digest.summary == "no changes"
    assert digest.diff_stat == "0 files changed, 0 insertions(+), 0 deletions(-)"
    assert digest.truncated is False


def test_privacy_exclusion_unit() -> None:
    assert privacy_exclusion("src/main.py") is None
    assert privacy_exclusion(".env") == "secret"
    assert privacy_exclusion("config/prod.env") == "secret"
    assert privacy_exclusion("keys/id_rsa") == "secret"
    assert privacy_exclusion("out/build/x.py") == "generated"
    assert privacy_exclusion("img.png", is_binary=True) == "binary"
    assert privacy_exclusion("Cargo.lock") == "lock"
    assert privacy_exclusion("Cargo.lock", exclude_locks=False) is None
    assert privacy_exclusion(".project-evolution/events/raw.jsonl") == "pel-internal"
