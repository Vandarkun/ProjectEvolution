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


def _subtree_scope(project_root: str | Path, toplevel: str | Path) -> str:
    """项目根相对 repo root 的 posix 前缀；相等时返回空串。

    :raises ValueError: 项目根不在仓库顶层之下（异常布局，调用方降级）。
    """
    rel = Path(project_root).resolve().relative_to(Path(toplevel).resolve())
    text = rel.as_posix()
    return "" if text == "." else text


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
    """单个 Git 仓库的只读访问封装。

    ``scope`` 是项目根相对 repo root 的 posix 前缀（项目根 == repo root 时为空）：
    项目目录嵌套在更大的仓库里时，一切采集（status / diff / log）都用
    pathspec 限定在该子树内，父仓库其余部分的变更绝不混入本项目证据
    （06 §5：无法可靠归属时不伪造）。记录的路径统一为 repo 相对路径。
    """

    def __init__(self, repo_root: Path, *, scope: str = "", timeout: float = 10.0):
        self._root = Path(repo_root)
        self._scope = scope.strip("/") if scope else ""
        self._timeout = timeout

    @property
    def repo_root(self) -> Path:
        return self._root

    @property
    def scope(self) -> str:
        return self._scope

    @classmethod
    def detect(cls, project_root: str | Path, *, timeout: float = 10.0) -> GitAdapter | None:
        """探测 Git 仓库：成功返回 adapter，失败返回 None（non-Git 模式）。

        绝不自动 `git init`（06 §2）。项目根不是仓库顶层时按子树 scope
        采集；路径关系无法判定（跨盘符等异常布局）时返回 None 明确降级。
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
        try:
            scope = _subtree_scope(root, toplevel)
        except (OSError, ValueError):
            return None
        return cls(toplevel, scope=scope, timeout=timeout)

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

    def _pathspec_args(self) -> list[str]:
        """把采集限定在项目子树内的 pathspec 参数（scope 为空 = 整个仓库）。"""
        return ["--", self._scope] if self._scope else []

    def _log_pathspec_args(self) -> list[str]:
        """git log 专用 pathspec：限定 scope 且排除 PEL 目录。

        只动过 ``.project-evolution/`` 记账文件的提交不是项目代码证据，
        通过 ``:(exclude)`` 魔法前缀在 git 侧直接过滤（06 §8 自指风险）。
        """
        pel = f"{self._scope}/{PEL_EXCLUDED_DIR}" if self._scope else PEL_EXCLUDED_DIR
        spec = [self._scope] if self._scope else []
        spec.append(f":(exclude){pel}")
        return ["--", *spec]

    def _is_excluded(self, path: str) -> bool:
        """scope 感知的 .project-evolution/ 排除（子树内 PEL 目录带 scope 前缀）。"""
        normalized = normalize_path(path)
        if is_excluded_path(normalized):
            return True
        if not self._scope:
            return False
        prefix = f"{self._scope}/{PEL_EXCLUDED_DIR}"
        return normalized == prefix or normalized.startswith(prefix + "/")

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
        """工作区状态（含 untracked），限定项目 scope，已排除 .project-evolution/。"""
        out = self._run(["status", "--porcelain=v1", "-z", "-uall", *self._pathspec_args()])
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
            if self._is_excluded(path):
                continue
            if orig is not None:
                orig = normalize_path(orig)
                if self._is_excluded(orig):
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
        args.extend(self._pathspec_args())
        text = self._run_text(args)
        return [
            line for line in text.splitlines() if line and not self._is_excluded(line.strip())
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
        """`git diff --find-renames --name-status`：new_path -> old_path（限 scope）。"""
        out = self._run(
            [
                "diff", "--find-renames", "--name-status", "-z",
                old_rev, new_rev, *self._pathspec_args(),
            ]
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

    def changed_paths_between(self, old_rev: str, new_rev: str) -> list[str]:
        """两个 revision 之间有差异的路径（限 scope，排除 .project-evolution/）。

        06 §4：HEAD 区间是快照对比的补充来源——两次快照之间被 commit 的
        改动不会出现在任何一方快照的 files 里（采集时工作区可能已干净）。
        """
        out = self._run(
            ["diff", "--name-only", "-z", old_rev, new_rev, *self._pathspec_args()]
        )
        tokens = out.decode("utf-8", errors="surrogateescape").split("\0")
        return [
            path for path in map(normalize_path, tokens) if path and not self._is_excluded(path)
        ]

    def list_commits_between(self, old_rev: str, new_rev: str) -> list[CommitMetadata]:
        """`old..new` 之间触及项目 scope 的提交（06 §9：checkpoint 记录的 commit）。

        按提交时间正序（旧 → 新）返回，与 Ledger 的 seq 语义一致；
        只动过 ``.project-evolution/`` 的提交被排除（见 ``_log_pathspec_args``）。

        :raises GitError: git log 执行失败（调用方决定是否降级）。
        """
        out = self._run(
            [
                "log", "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x1e",
                f"{old_rev}..{new_rev}", *self._log_pathspec_args(),
            ]
        )
        text = out.decode("utf-8", errors="replace")
        commits: list[CommitMetadata] = []
        for record in text.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, author, email, at, subject = record.split("\0", 4)
            commits.append(
                CommitMetadata(
                    sha=sha,
                    author=author,
                    author_email=email,
                    committed_at=at,
                    subject=subject,
                )
            )
        commits.reverse()  # git log 按新 → 旧输出，翻转为时间正序
        return commits

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
