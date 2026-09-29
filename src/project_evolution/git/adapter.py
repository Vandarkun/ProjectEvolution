"""GitAdapter：只读 Git Evidence Adapter（06-git-integration.md §6/§15）。

安全约定（全部硬性）：

- subprocess 参数数组，绝不拼接 shell 字符串；
- 不启 shell；
- 每条命令带 timeout；
- cwd 固定 repo root（--no-index diff 例外，见 docstring）；
- 子命令白名单，仅只读命令；add/commit/checkout/reset/clean/merge/tag 等
  一律 :class:`GitSafetyError`；
- ``.project-evolution/`` 从一切采集（status/diff/changed files）中永久排除，
  不可配置关闭（06 §8、04-event-ledger.md §9 自指风险）。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from project_evolution.errors import PelError

#: PEL 自身数据目录前缀：从一切 Evidence 采集中永久排除（06 §8，不可配置）
PEL_EXCLUDED_DIR = ".project-evolution"

#: 只读子命令白名单；未列出的子命令一律拒绝执行
_READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "rev-parse",
        "status",
        "diff",
        "show",
        "cat-file",
        "log",
        "ls-files",
        "symbolic-ref",
        "for-each-ref",
    }
)


class GitError(PelError):
    """Git 命令执行失败。"""


class GitSafetyError(GitError):
    """尝试执行白名单之外的（写）Git 命令。"""


def normalize_path(path: str) -> str:
    """统一为 posix 风格相对路径（git porcelain 输出本就是 / 分隔）。"""
    return path.replace("\\", "/")


def is_excluded_path(path: str) -> bool:
    """.project-evolution/ 自排除规则：硬编码，不可配置关闭。"""
    normalized = normalize_path(path)
    return normalized == PEL_EXCLUDED_DIR or normalized.startswith(PEL_EXCLUDED_DIR + "/")


@dataclass(frozen=True)
class StatusEntry:
    """`git status --porcelain=v1` 的一条记录。xy 为两字符状态码。"""

    xy: str
    path: str
    #: rename/copy 时的原始路径
    orig_path: str | None = None


@dataclass(frozen=True)
class CommitMetadata:
    sha: str
    author: str
    author_email: str
    committed_at: str
    subject: str


def _subcommand_of(args: list[str]) -> str | None:
    """跳过前置全局选项（-c k v 等），找到子命令名。"""
    i = 0
    while i < len(args):
        token = args[i]
        if token == "-c":
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token
    return None


class GitAdapter:
    """单个 Git 仓库的只读访问封装。"""

    def __init__(self, repo_root: Path, *, timeout: float = 10.0):
        self._root = Path(repo_root)
        self._timeout = timeout

    @property
    def repo_root(self) -> Path:
        return self._root

    @classmethod
    def detect(cls, project_root: str | Path, *, timeout: float = 10.0) -> GitAdapter | None:
        """探测 Git 仓库：成功返回 adapter，失败返回 None（non-Git 模式）。

        绝不自动 `git init`（06 §2）。
        """
        root = Path(project_root)
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=root,
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        toplevel = Path(proc.stdout.decode("utf-8", errors="replace").strip())
        return cls(toplevel, timeout=timeout)

    # ------------------------------------------------------------------ infra

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        allow_exit_codes: tuple[int, ...] = (0,),
    ) -> bytes:
        command = ["git", "-c", "core.quotepath=false", *args]
        subcommand = _subcommand_of(args)
        if subcommand is None or subcommand not in _READ_ONLY_SUBCOMMANDS:
            raise GitSafetyError(
                f"git subcommand {subcommand!r} is not in the read-only whitelist; "
                f"the adapter never mutates the repository"
            )
        try:
            proc = subprocess.run(
                command,
                cwd=cwd if cwd is not None else self._root,
                capture_output=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {subcommand} timed out after {self._timeout}s") from exc
        except OSError as exc:
            raise GitError(f"failed to execute git {subcommand}: {exc}") from exc
        if proc.returncode not in allow_exit_codes:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(
                f"git {subcommand} failed (exit={proc.returncode}): {detail}"
            )
        return proc.stdout

    def _run_text(self, args: list[str], **kwargs) -> str:
        return self._run(args, **kwargs).decode("utf-8", errors="replace")

    # ------------------------------------------------------------- repository

    def get_head_sha(self) -> str | None:
        """当前 HEAD SHA；空仓库（无提交）返回 None。"""
        try:
            out = self._run(["rev-parse", "HEAD"])
        except GitError:
            return None
        return out.decode("ascii").strip() or None

    def get_branch(self) -> str | None:
        """当前分支名；detached HEAD 返回 None。"""
        try:
            out = self._run(["symbolic-ref", "--short", "HEAD"])
        except GitError:
            return None
        return out.decode("utf-8", errors="replace").strip() or None

    # ----------------------------------------------------------------- status

    def get_status_entries(self) -> list[StatusEntry]:
        """工作区状态（含 untracked），已排除 .project-evolution/。"""
        out = self._run(["status", "--porcelain=v1", "-z", "-uall"])
        tokens = out.decode("utf-8", errors="surrogateescape").split("\0")
        entries: list[StatusEntry] = []
        i = 0
        while i < len(tokens):
            record = tokens[i]
            if not record:
                i += 1
                continue
            xy, rest = record[:2], record[3:]
            orig: str | None = None
            if xy and xy[0] in ("R", "C"):
                orig = tokens[i + 1] if i + 1 < len(tokens) else None
                i += 2
            else:
                i += 1
            path = normalize_path(rest)
            if is_excluded_path(path):
                continue
            if orig is not None:
                orig = normalize_path(orig)
                if is_excluded_path(orig):
                    continue
            entries.append(StatusEntry(xy=xy, path=path, orig_path=orig))
        return entries

    def get_status(self) -> list[str]:
        """与 HEAD 有差异的路径集合（排除 .project-evolution/）。"""
        return [entry.path for entry in self.get_status_entries()]

    # ------------------------------------------------------------------- diff

    def get_diff(
        self,
        *,
        base: str | None = None,
        staged: bool = False,
        paths: list[str] | None = None,
        find_renames: bool = False,
    ) -> str:
        args = ["diff"]
        if find_renames:
            args.append("--find-renames")
        if base is not None:
            args.append(base)
        if staged:
            args.append("--cached")
        if paths:
            args.append("--")
            args.extend(paths)
        return self._run_text(args)

    def get_diff_stat(
        self, *, base: str | None = None, staged: bool = False, paths: list[str] | None = None
    ) -> str:
        args = ["diff", "--stat"]
        if base is not None:
            args.append(base)
        if staged:
            args.append("--cached")
        if paths:
            args.append("--")
            args.extend(paths)
        return self._run_text(args).strip()

    def get_changed_files(
        self, *, base: str | None = None, staged: bool = False
    ) -> list[str]:
        args = ["diff", "--name-only"]
        if base is not None:
            args.append(base)
        if staged:
            args.append("--cached")
        text = self._run_text(args)
        return [
            line for line in text.splitlines() if line and not is_excluded_path(line.strip())
        ]

    def diff_no_index(self, path_a: Path | str, path_b: Path | str, *, cwd: Path) -> str:
        """`git diff --no-index` 两个工作文件（06 §5 逐文件 diff 的实现）。

        cwd 允许指向临时目录以便输出相对路径（仍是只读操作）。
        退出码 0/1 分别表示相同/有差异，均为成功。
        """
        return self._run_text(
            [
                "diff",
                "--no-index",
                "--",
                path_a.as_posix() if isinstance(path_a, Path) else path_a,
                path_b.as_posix() if isinstance(path_b, Path) else path_b,
            ],
            cwd=cwd,
            allow_exit_codes=(0, 1),
        )

    def find_renames_between(self, old_rev: str, new_rev: str) -> dict[str, str]:
        """`git diff --find-renames --name-status`：new_path -> old_path。"""
        out = self._run(
            ["diff", "--find-renames", "--name-status", "-z", old_rev, new_rev]
        )
        tokens = out.decode("utf-8", errors="surrogateescape").split("\0")
        renames: dict[str, str] = {}
        i = 0
        while i < len(tokens):
            record = tokens[i]
            if not record:
                i += 1
                continue
            status = record[0]
            path = normalize_path(record[2:]) if len(record) > 2 else ""
            if status in ("R", "C") and i + 1 < len(tokens):
                orig = normalize_path(tokens[i + 1])
                renames[path] = orig
                i += 2
            else:
                i += 1
        return renames

    # ------------------------------------------------------------- git objects

    def file_exists_in_rev(self, rev: str, path: str) -> bool:
        try:
            self._run(["cat-file", "-e", f"{rev}:{normalize_path(path)}"])
        except GitError:
            return False
        return True

    def show_file(self, rev: str, path: str) -> bytes:
        """读取某 revision 中某文件的内容（原始字节）。"""
        return self._run(["show", f"{rev}:{normalize_path(path)}"])

    def get_commit_metadata(self, sha: str) -> CommitMetadata:
        out = self._run(
            ["show", "-s", "--format=%H%x00%an%x00%ae%x00%aI%x00%s", sha]
        )
        sha_, author, email, at, subject = out.decode("utf-8", errors="replace").split("\0", 4)
        return CommitMetadata(
            sha=sha_,
            author=author,
            author_email=email,
            committed_at=at,
            subject=subject.strip(),
        )

    def get_tags(self) -> list[tuple[str, str]]:
        """[(tag_name, commit_sha)]，按 refname 排序。"""
        out = self._run(
            ["for-each-ref", "refs/tags", "--format=%(refname:short)%00%(objectname)"]
        )
        tags: list[tuple[str, str]] = []
        for line in out.decode("utf-8", errors="replace").splitlines():
            if not line:
                continue
            name, _, commit = line.partition("\0")
            tags.append((name, commit))
        return tags
