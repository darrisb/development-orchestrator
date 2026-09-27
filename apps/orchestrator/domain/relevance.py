"""Relevance heuristics for context selection (build.md section 15).

Pure functions over paths and text: nothing here touches a filesystem, a
repository or a database. The context builder does the I/O and asks these
functions what any of it means, which keeps "why was this file included?"
answerable in a unit test rather than only against a real repository.

The heuristics are deliberately shallow. A language-server-grade import graph
would be more accurate, but it would also be one more thing that can be wrong
in a way nobody notices; a wrong answer here costs a few hundred wasted tokens
and is visible in ``context-manifest.json``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import PurePosixPath

#: Words too common in task titles to say anything about which file matters.
STOP_WORDS: frozenset[str] = frozenset(
    {
        "add", "all", "allow", "also", "and", "any", "are", "but", "can", "case",
        "change", "class", "code", "create", "data", "file", "files", "fix", "for",
        "from", "function", "get", "has", "have", "implement", "into", "its", "make",
        "method", "must", "new", "non", "not", "only", "out", "project", "remove",
        "return", "returns", "set", "should", "src", "support", "task", "test",
        "tests", "that", "the", "then", "this", "update", "use", "used", "using",
        "value", "when", "which", "while", "with", "work",
    }
)

#: Configuration and build files worth sending whatever the task says, because
#: they define how the coder's work will be built, linted and tested.
CONFIG_FILENAMES: tuple[str, ...] = (
    "package.json",
    "tsconfig.json",
    "pyproject.toml",
    "setup.cfg",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Makefile",
    "Dockerfile",
    ".eslintrc.json",
    ".eslintrc.js",
    "eslint.config.js",
    "vitest.config.ts",
    "jest.config.js",
    "pytest.ini",
    "tox.ini",
)

#: Extensions a coder can usefully read. Anything else is either binary or
#: noise, and neither belongs in a bounded prompt.
TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".c", ".cc", ".cfg", ".conf", ".cpp", ".cs", ".css", ".go", ".gradle", ".h",
        ".hpp", ".html", ".ini", ".java", ".js", ".json", ".jsx", ".kt", ".kts",
        ".md", ".mjs", ".php", ".pyi", ".py", ".rb", ".rs", ".scss", ".sh", ".sql",
        ".swift", ".toml", ".ts", ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml",
    }
)

#: Directories never worth walking: generated, vendored or version-control.
EXCLUDED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "dist", "build",
        "target", "out", "coverage", "__pycache__", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".next", ".nuxt", ".tox", "vendor", "site-packages",
    }
)

_TEST_PATH_PATTERNS: tuple[str, ...] = (
    "test_*",
    "*_test.*",
    "*.test.*",
    "*.spec.*",
    "*Test.java",
    "*Tests.cs",
)
_TEST_DIRECTORIES: frozenset[str] = frozenset(
    {"test", "tests", "__tests__", "spec", "specs", "testing"}
)

#: Paths that usually declare types or contracts rather than behaviour.
_INTERFACE_PATTERNS: tuple[str, ...] = (
    "*.d.ts",
    "*.pyi",
    "*/types.*",
    "*/types/*",
    "*/interfaces/*",
    "*/schemas/*",
    "*/schema.*",
    "*/models.py",
    "*/domain/*",
    "*/contracts/*",
    "*/api/*",
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_CAMEL_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")

_IMPORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # JS/TS: import ... from "x"; export ... from 'x'; require("x")
    re.compile(r"""(?:^|\s)(?:import|export)\s[^;\n]*?from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""(?:^|\s)import\s+['"]([^'"]+)['"]"""),
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),
    # Python: from a.b import c / import a.b. ``static`` is excluded because
    # Java's ``import static a.b.C;`` otherwise yields the keyword as a target.
    re.compile(r"""(?m)^\s*from\s+([.\w]+)\s+import\s"""),
    re.compile(r"""(?m)^\s*import\s+(?!static\b)([.\w]+)"""),
    # Java/Kotlin: import com.example.navigation.Tree;
    re.compile(r"""(?m)^\s*import\s+(?:static\s+)?([\w.]+)\s*;"""),
)

#: Go on its own, because a bare quoted string is not evidence of an import:
#: ``"name": "thing",`` in a JSON fixture and a string in a list literal both
#: look like one. A specifier counts only inside a real import statement or a
#: parenthesised import block, which means tracking where the block ends.
_GO_SINGLE_IMPORT = re.compile(r'''(?m)^\s*import\s+(?:[\w.]+\s+)?"([^"]+)"''')
_GO_IMPORT_BLOCK_START = re.compile(r"(?m)^\s*import\s*\($")
_GO_BLOCK_ENTRY = re.compile(r'''^\s*(?:[\w.]+\s+)?"([^"]+)"''')


def split_identifier(token: str) -> list[str]:
    """``navigationTree`` / ``navigation_tree`` / ``NavigationTree`` -> parts."""
    parts: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", token):
        if chunk:
            parts.extend(match.group(0) for match in _CAMEL_RE.finditer(chunk))
    return [part for part in parts if part]


def extract_keywords(*texts: str | None, minimum_length: int = 3) -> frozenset[str]:
    """Lower-cased significant words from task text, identifiers split apart.

    Both halves of ``navigationTree`` are keywords, so a task titled
    "Implement navigation tree" matches ``src/navigationTree.ts``.
    """
    keywords: set[str] = set()
    for text in texts:
        if not text:
            continue
        for match in _WORD_RE.finditer(text):
            token = match.group(0)
            for part in [token, *split_identifier(token)]:
                folded = part.casefold()
                if len(folded) >= minimum_length and folded not in STOP_WORDS:
                    keywords.add(folded)
    return frozenset(keywords)


def path_keywords(path: str) -> frozenset[str]:
    """Keywords a path itself contributes: directory names and the file stem."""
    pure = PurePosixPath(path)
    return extract_keywords(" ".join([*pure.parts[:-1], pure.stem]))


def normalise_path(path: str) -> str:
    """Repository-relative POSIX form, without ``./`` or a leading slash."""
    return PurePosixPath(path.strip().lstrip("/")).as_posix().removeprefix("./")


@lru_cache(maxsize=512)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to a regex.

    ``fnmatch`` is not usable directly: its ``*`` crosses directory
    separators, so ``src/*.ts`` would match ``src/deep/nested.ts`` and a
    declared pattern would quietly widen a task's scope. Here ``*`` and ``?``
    stop at ``/`` and only ``**`` spans directories.
    """
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif character == "*":
            parts.append("[^/]*")
            index += 1
        elif character == "?":
            parts.append("[^/]")
            index += 1
        elif character == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                parts.append(re.escape(character))
                index += 1
            else:
                body = pattern[index + 1 : end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                parts.append(f"[{body}]")
                index = end + 1
        else:
            parts.append(re.escape(character))
            index += 1
    return re.compile("".join(parts) + r"\Z")


def matches_pattern(path: str, pattern: str) -> bool:
    """Glob match where ``**`` spans directories and a bare name matches anywhere."""
    candidate = normalise_path(path)
    pattern = pattern.strip()
    if not pattern:
        return False
    if _compile_pattern(pattern).match(candidate):
        return True
    # "secrets/**" should also match the directory's own entry, and a bare
    # "package.json" should match at any depth.
    if pattern.endswith("/**") and _compile_pattern(pattern[:-3]).match(candidate):
        return True
    return "/" not in pattern and bool(
        _compile_pattern(pattern).match(PurePosixPath(candidate).name)
    )


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(matches_pattern(path, pattern) for pattern in patterns)


def is_excluded(path: str) -> bool:
    return any(part in EXCLUDED_DIRECTORIES for part in PurePosixPath(path).parts)


def is_text_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.suffix.casefold() in TEXT_EXTENSIONS or pure.name in CONFIG_FILENAMES


def is_test_path(path: str) -> bool:
    pure = PurePosixPath(normalise_path(path))
    if any(part.casefold() in _TEST_DIRECTORIES for part in pure.parts[:-1]):
        return True
    return any(fnmatch(pure.name, pattern) for pattern in _TEST_PATH_PATTERNS)


def is_config_path(path: str) -> bool:
    return PurePosixPath(normalise_path(path)).name in CONFIG_FILENAMES


def is_interface_path(path: str) -> bool:
    candidate = normalise_path(path)
    return any(matches_pattern(candidate, pattern) for pattern in _INTERFACE_PATTERNS)


def score_path(path: str, keywords: Iterable[str]) -> int:
    """How strongly ``path`` matches the task's keywords. Zero means no match."""
    wanted = {keyword.casefold() for keyword in keywords}
    if not wanted:
        return 0
    pure = PurePosixPath(normalise_path(path))
    stem_words = {word.casefold() for word in split_identifier(pure.stem)}
    directory_words = {
        word.casefold() for part in pure.parts[:-1] for word in split_identifier(part)
    }
    # A filename match is worth more than a directory match: every file under
    # src/navigation/ would otherwise score as highly as navigation.ts itself.
    return 3 * len(stem_words & wanted) + len(directory_words & wanted)


def rank_paths(
    paths: Iterable[str], keywords: Iterable[str], *, limit: int | None = None
) -> list[tuple[str, int]]:
    """Scoring paths, best first, ties broken by path so the result is stable."""
    wanted = frozenset(keyword.casefold() for keyword in keywords)
    scored = [
        (path, score_path(path, wanted))
        for path in sorted({normalise_path(candidate) for candidate in paths})
    ]
    ranked = sorted(
        (entry for entry in scored if entry[1] > 0),
        key=lambda entry: (-entry[1], entry[0]),
    )
    return ranked if limit is None else ranked[:limit]


def related_test_paths(source_path: str, candidates: Iterable[str]) -> list[str]:
    """Test files that name ``source_path``'s stem, e.g. ``navigation.test.ts``."""
    stem = PurePosixPath(normalise_path(source_path)).stem
    stem_words = {word.casefold() for word in split_identifier(stem)}
    if not stem_words:
        return []
    matched = [
        normalise_path(candidate)
        for candidate in candidates
        if is_test_path(candidate)
        and stem_words <= {word.casefold() for word in split_identifier(
            PurePosixPath(normalise_path(candidate)).stem
        )}
    ]
    return sorted(set(matched))


def extract_import_targets(source: str) -> list[str]:
    """Module specifiers imported by ``source``, in first-seen order.

    Package imports are returned too; resolving which of them exist in the
    repository is the caller's job, because only the caller knows what is on
    disk.
    """
    seen: dict[str, None] = {}
    for pattern in _IMPORT_PATTERNS:
        for match in pattern.finditer(source):
            seen.setdefault(match.group(1).strip(), None)
    for target in _go_import_targets(source):
        seen.setdefault(target, None)
    return list(seen)


def _go_import_targets(source: str) -> list[str]:
    """Go import specifiers, read only from where Go puts them."""
    targets: list[str] = []
    for match in _GO_SINGLE_IMPORT.finditer(source):
        targets.append(match.group(1).strip())
    lines = source.splitlines()
    inside = False
    for line in lines:
        if not inside:
            inside = _GO_IMPORT_BLOCK_START.match(line) is not None
            continue
        if line.strip().startswith(")"):
            inside = False
            continue
        entry = _GO_BLOCK_ENTRY.match(line)
        if entry is not None:
            targets.append(entry.group(1).strip())
    return targets


def resolve_import(specifier: str, *, from_path: str, known_paths: Sequence[str]) -> str | None:
    """Resolve ``specifier`` to a repository path, or ``None`` if it is external.

    Handles the two forms that actually matter for context selection: a
    relative JS/TS path and a dotted Python module. Anything that does not
    land on a known path is treated as a third-party package and dropped.
    """
    known = {normalise_path(path) for path in known_paths}
    base = PurePosixPath(normalise_path(from_path)).parent

    if specifier.startswith("."):
        if _is_python(from_path):
            candidate = _python_relative_base(specifier, base)
        else:
            candidate = _posix_join(base, specifier)
    elif _is_python(from_path):
        candidate = PurePosixPath(specifier.replace(".", "/"))
    elif PurePosixPath(from_path).suffix in {".java", ".kt", ".kts"}:
        java_path = specifier.replace(".", "/")
        matches = sorted(
            path
            for path in known
            if path.endswith(f"/{java_path}.java")
            or path.endswith(f"/{java_path}.kt")
        )
        return matches[0] if matches else None
    elif PurePosixPath(from_path).suffix == ".go":
        # Go imports packages, not files. Prefer the package path's directory
        # and a stable source file within it.
        package = specifier.strip("/")
        matches = sorted(
            path
            for path in known
            if path.endswith(".go")
            and (f"/{package}/" in f"/{path}" or f"/{package.rsplit('/', 1)[-1]}/" in f"/{path}")
        )
        return matches[0] if matches else None
    else:
        # A bare JS specifier is a package unless the repository happens to
        # contain that exact path (an alias or a monorepo workspace).
        candidate = PurePosixPath(normalise_path(specifier))

    return _first_existing(candidate, known)


def _is_python(path: str) -> bool:
    return PurePosixPath(path).suffix in {".py", ".pyi"}


def _posix_join(base: PurePosixPath, specifier: str) -> PurePosixPath:
    parts = list(base.parts)
    for part in PurePosixPath(specifier).parts:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def _python_relative_base(specifier: str, base: PurePosixPath) -> PurePosixPath:
    """``from ..domain.models import X`` inside ``a/b/c.py`` -> ``a/domain/models``."""
    leading = len(specifier) - len(specifier.lstrip("."))
    parts = list(base.parts)
    for _ in range(leading - 1):
        if parts:
            parts.pop()
    remainder = specifier[leading:]
    parts.extend(part for part in remainder.split(".") if part)
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def _first_existing(candidate: PurePosixPath, known: set[str]) -> str | None:
    stem = candidate.as_posix()
    if stem in {".", ""}:
        return None
    suffixes = ("", ".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".py", ".pyi")
    for suffix in suffixes:
        path = f"{stem}{suffix}"
        if path in known:
            return path
    for index in ("index.ts", "index.tsx", "index.js", "__init__.py"):
        path = f"{stem}/{index}"
        if path in known:
            return path
    return None
