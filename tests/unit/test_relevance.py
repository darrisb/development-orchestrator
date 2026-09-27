"""Relevance heuristics for context selection (build.md section 15)."""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.relevance import (
    extract_import_targets,
    extract_keywords,
    is_config_path,
    is_excluded,
    is_interface_path,
    is_test_path,
    is_text_path,
    matches_pattern,
    normalise_path,
    rank_paths,
    related_test_paths,
    resolve_import,
    score_path,
    split_identifier,
)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("navigationTree", ["navigation", "Tree"]),
        ("navigation_tree", ["navigation", "tree"]),
        ("NavigationTree", ["Navigation", "Tree"]),
        ("HTTPServer", ["HTTP", "Server"]),
        ("nav-tree.test", ["nav", "tree", "test"]),
    ],
)
def test_identifiers_split_into_their_parts(token: str, expected: list[str]):
    assert split_identifier(token) == expected


def test_keywords_drop_words_that_say_nothing_about_which_file_matters():
    keywords = extract_keywords("Implement the navigation tree for the sidebar")

    assert {"navigation", "tree", "sidebar"} <= keywords
    # "implement" and "the" appear in half of all task titles.
    assert "implement" not in keywords
    assert "the" not in keywords


def test_a_camel_case_filename_matches_a_spaced_task_title():
    keywords = extract_keywords("Implement navigation tree")

    assert score_path("src/navigationTree.ts", keywords) > 0


def test_a_filename_match_outranks_a_directory_match():
    keywords = extract_keywords("navigation")

    assert score_path("src/navigation.ts", keywords) > score_path(
        "src/navigation/unrelated.ts", keywords
    )


def test_ranking_is_stable_for_equal_scores():
    keywords = {"navigation"}
    paths = ["src/b/navigation.ts", "src/a/navigation.ts"]

    assert [path for path, _ in rank_paths(paths, keywords)] == [
        "src/a/navigation.ts",
        "src/b/navigation.ts",
    ]
    assert rank_paths(reversed(paths), keywords) == rank_paths(paths, keywords)


def test_paths_that_match_nothing_are_not_ranked_at_all():
    assert rank_paths(["src/unrelated.ts"], {"navigation"}) == []


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_nav.py",
        "src/nav.test.ts",
        "src/__tests__/nav.ts",
        "src/nav.spec.ts",
        "src/nav_test.go",
    ],
)
def test_test_files_are_recognised_across_conventions(path: str):
    assert is_test_path(path)


def test_a_source_file_is_not_a_test_because_it_says_test():
    assert not is_test_path("src/testHarnessClient.ts")


def test_configuration_and_interface_paths_are_recognised():
    assert is_config_path("package.json")
    assert is_config_path("backend/pyproject.toml")
    assert is_interface_path("src/types.ts")
    assert is_interface_path("src/api/routes.ts")
    assert not is_interface_path("src/main.ts")


def test_generated_directories_are_excluded_and_binaries_are_not_text():
    assert is_excluded("node_modules/left-pad/index.js")
    assert is_excluded("src/__pycache__/x.pyc")
    assert not is_excluded("src/app.ts")
    assert is_text_path("src/app.ts")
    assert not is_text_path("assets/logo.png")


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("src/a/b.ts", "src/**", True),
        ("src", "src/**", True),
        ("deep/nested/package.json", "package.json", True),
        (".env", ".env", True),
        ("src/a/b.ts", "src/*.ts", False),
        ("./src/a.ts", "src/a.ts", True),
    ],
)
def test_pattern_matching_handles_the_manifest_forms(path, pattern, expected):
    assert matches_pattern(path, pattern) is expected


def test_paths_normalise_to_repository_relative_posix_form():
    assert normalise_path("./src/a.ts") == "src/a.ts"
    assert normalise_path("/src/a.ts") == "src/a.ts"


def test_related_tests_are_found_by_name():
    candidates = ["src/navigation.ts", "tests/navigation.test.ts", "tests/other.test.ts"]

    assert related_test_paths("src/navigation.ts", candidates) == ["tests/navigation.test.ts"]


def test_import_targets_are_extracted_in_order_without_duplicates():
    source = (
        "import { a } from './tree';\n"
        "import './styles.css';\n"
        "const b = require('../lib/b');\n"
        "export { c } from './tree';\n"
    )

    assert extract_import_targets(source) == ["./tree", "./styles.css", "../lib/b"]


def test_python_imports_are_extracted():
    source = "from apps.domain import scope\nimport apps.services.git\n"

    assert extract_import_targets(source) == ["apps.domain", "apps.services.git"]


def test_java_imports_are_extracted_without_the_static_keyword():
    """Concern 30: a Java repository found nothing at all before these
    patterns existed, so the reviewer saw the diff and nothing around it."""
    source = (
        "package com.example.nav;\n"
        "\n"
        "import static org.junit.Assert.assertEquals;\n"
        "import com.example.navigation.Tree;\n"
    )

    assert extract_import_targets(source) == [
        "com.example.navigation.Tree",
        "org.junit.Assert.assertEquals",
    ]


def test_go_imports_are_extracted_from_a_parenthesised_block():
    source = (
        "package main\n"
        "\n"
        "import (\n"
        '\t"fmt"\n'
        '\tnav "example/project/navigation"\n'
        ")\n"
    )

    assert extract_import_targets(source) == ["fmt", "example/project/navigation"]


def test_a_single_go_import_is_extracted():
    assert extract_import_targets('import "example/project/nav"') == [
        "example/project/nav"
    ]


def test_a_quoted_string_outside_a_go_import_block_is_not_an_import():
    """A bare quoted string is not evidence: a JSON fixture and a string in a
    list literal both look like a Go import entry to a line-shaped pattern."""
    source = (
        "package main\n"
        "\n"
        "import (\n"
        '\t"fmt"\n'
        ")\n"
        "\n"
        'var path = "not/an/import"\n'
    )

    assert extract_import_targets(source) == ["fmt"]


def test_a_json_document_yields_no_import_targets():
    assert extract_import_targets('{\n  "name": "thing",\n  "main": "index.js"\n}\n') == []


def test_relative_javascript_imports_resolve_to_repository_files():
    known = ["src/tree.ts", "src/widgets/index.tsx", "src/styles.css"]

    assert resolve_import("./tree", from_path="src/nav.ts", known_paths=known) == "src/tree.ts"
    assert (
        resolve_import("./widgets", from_path="src/nav.ts", known_paths=known)
        == "src/widgets/index.tsx"
    )
    assert resolve_import("./styles.css", from_path="src/nav.ts", known_paths=known) is not None


def test_python_imports_resolve_relative_and_absolute():
    known = ["apps/domain/models.py", "apps/services/git.py", "apps/domain/__init__.py"]

    assert (
        resolve_import("..domain.models", from_path="apps/services/x.py", known_paths=known)
        == "apps/domain/models.py"
    )
    assert (
        resolve_import("apps.services.git", from_path="apps/main.py", known_paths=known)
        == "apps/services/git.py"
    )


def test_a_third_party_package_resolves_to_nothing():
    assert resolve_import("react", from_path="src/nav.ts", known_paths=["src/tree.ts"]) is None
    assert resolve_import("os.path", from_path="apps/x.py", known_paths=["apps/y.py"]) is None
