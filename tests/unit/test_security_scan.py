"""Security verification over a candidate diff (build.md section 19, phase H).

The checks that read the change itself: what the added lines contain and what
kind of file was added. The path, size and deletion checks belong to the scope
guard and are asserted in ``test_scope_guard.py``.
"""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.enums import ScopePolicyDecision
from apps.orchestrator.domain.git import ChangeType, DiffSummary, FileChange
from apps.orchestrator.domain.security import (
    SecurityFindingKind,
    added_lines,
    scan_candidate,
)


def summary(*files: FileChange) -> DiffSummary:
    return DiffSummary(files=files)


def changed(path: str, *, binary: bool = False) -> FileChange:
    return FileChange(
        path=path,
        change_type=ChangeType.ADDED,
        insertions=None if binary else 10,
        deletions=None if binary else 0,
    )


def diff_adding(*lines: str) -> str:
    body = "\n".join(f"+{line}" for line in lines)
    header = (
        "diff --git a/src/app.ts b/src/app.ts\n"
        "--- a/src/app.ts\n"
        "+++ b/src/app.ts\n"
        "@@ -1 +1,2 @@\n"
    )
    return f"{header}{body}\n"


# --- what is scanned ---------------------------------------------------------


def test_only_added_lines_are_scanned():
    """A secret being *removed* is the candidate doing the right thing.
    Flagging it would teach the coder not to remove one."""
    diff = (
        "diff --git a/config.ts b/config.ts\n"
        "--- a/config.ts\n"
        "+++ b/config.ts\n"
        '-const API_KEY = "sk-ZZfakefakefakefakefake1234";\n'
        "+const API_KEY = process.env.API_KEY;\n"
    )

    assessment = scan_candidate(diff, summary(changed("config.ts")))

    assert assessment.decision is ScopePolicyDecision.ALLOW
    assert assessment.findings == ()


def test_the_file_header_is_not_mistaken_for_an_added_line():
    lines = added_lines(diff_adding("const answer = 42;"))

    assert lines == ((5, "const answer = 42;"),)


# --- secret scanning ---------------------------------------------------------


def test_a_credential_shaped_added_line_blocks_the_candidate():
    assessment = scan_candidate(
        diff_adding('const key = "sk-ZZfakefakefakefakefake1234";'),
        summary(changed("src/app.ts")),
    )

    assert assessment.blocked
    assert assessment.findings[0].kind is SecurityFindingKind.SECRET_MATERIAL


def test_a_finding_never_repeats_the_credential_it_found():
    """The run record is read by people and sent to a reviewer. A scan that
    quotes the secret it found has published it a second time."""
    secret = "ghp_ZZfakefakefakefakefakefake123456"

    assessment = scan_candidate(diff_adding(f'token = "{secret}"'), summary(changed("a.py")))

    assert assessment.blocked
    for finding in assessment.findings:
        assert secret not in finding.detail
    assert secret not in assessment.summary()


def test_a_named_credential_assignment_is_found():
    assessment = scan_candidate(
        diff_adding('DATABASE_PASSWORD = "hunter2-not-a-real-password"'),
        summary(changed("settings.py")),
    )

    assert assessment.blocked


def test_ordinary_code_is_not_a_secret():
    assessment = scan_candidate(
        diff_adding(
            "export function navigate(target: string) {",
            "  return router.push(target);",
            "}",
        ),
        summary(changed("src/nav.ts")),
    )

    assert assessment.decision is ScopePolicyDecision.ALLOW
    assert assessment.scanned_lines == 3


# --- file-shaped findings ----------------------------------------------------


def test_committed_build_output_is_blocked():
    assessment = scan_candidate("", summary(changed("dist/bundle.js")))

    assert assessment.blocked
    assert assessment.findings[0].kind is SecurityFindingKind.GENERATED_ARTIFACT


def test_a_dependency_tree_is_blocked():
    assessment = scan_candidate("", summary(changed("node_modules/left-pad/index.js")))

    assert assessment.blocked
    assert assessment.findings[0].kind is SecurityFindingKind.GENERATED_ARTIFACT


def test_credential_material_is_blocked_by_its_name_alone():
    assessment = scan_candidate("", summary(changed("config/server.pem")))

    assert assessment.blocked
    assert assessment.findings[0].kind is SecurityFindingKind.FORBIDDEN_FILE


def test_a_binary_file_asks_for_a_human_rather_than_blocking():
    """Nothing here can read it, and neither can a reviewer -- but a fixture
    image is a legitimate change, so this is a question, not a refusal."""
    assessment = scan_candidate("", summary(changed("tests/fixtures/logo.png", binary=True)))

    assert assessment.decision is ScopePolicyDecision.REQUIRE_REVIEW
    assert assessment.findings[0].kind is SecurityFindingKind.UNEXPECTED_BINARY


def test_source_files_produce_no_findings():
    assessment = scan_candidate(
        "", summary(changed("src/nav.ts"), changed("tests/nav.test.ts"))
    )

    assert assessment.decision is ScopePolicyDecision.ALLOW
    assert assessment.complete


# --- honesty about what was not scanned --------------------------------------


def test_a_clipped_diff_is_reported_rather_than_scanned_quietly():
    """A "no secrets found" over material the scan never read is worse than
    no scan: it is a false assurance attached to the run record."""
    assessment = scan_candidate(
        diff_adding("const answer = 42;"), summary(changed("src/app.ts")), truncated=True
    )

    assert not assessment.complete
    assert assessment.decision is ScopePolicyDecision.REQUIRE_REVIEW
    assert assessment.findings[0].kind is SecurityFindingKind.DIFF_NOT_SCANNED


def test_a_blocking_finding_outranks_an_incomplete_scan():
    assessment = scan_candidate(
        diff_adding('key = "sk-ZZfakefakefakefakefake1234"'),
        summary(changed("src/app.ts")),
        truncated=True,
    )

    assert assessment.blocked
    assert len(assessment.blocking_findings) == 1


def test_a_private_key_block_is_caught_line_by_line():
    """The redactor matches a PEM block across its whole body; a diff is
    scanned one line at a time, so the header has to be conclusive on its own."""
    assessment = scan_candidate(
        diff_adding("-----BEGIN RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEAfake"),
        summary(changed("deploy/key.txt")),
    )

    assert assessment.blocked
    assert assessment.findings[0].kind is SecurityFindingKind.SECRET_MATERIAL


def test_reading_a_credential_from_the_environment_is_not_a_leak():
    """`API_KEY = process.env.API_KEY` is the coder doing the right thing."""
    assessment = scan_candidate(
        diff_adding("const apiKey = process.env.API_KEY;"), summary(changed("src/app.ts"))
    )

    assert assessment.decision is ScopePolicyDecision.ALLOW


def test_a_placeholder_value_is_not_a_leak():
    """A template that ships `PASSWORD=changeme` has not published anything.
    (The `.env.example` *path* is still refused -- see `test_scope_guard.py`
    and concern 5: `.env.*` is protected whatever it holds.)"""
    assessment = scan_candidate(
        diff_adding("REVIEW_API_KEY=changeme"), summary(changed("docs/setup.md"))
    )

    assert assessment.decision is ScopePolicyDecision.ALLOW


# --------------------------------------------- per-project generated patterns


def test_a_vendored_dependency_tree_is_blocked_by_default():
    """The default is right for most repositories: commit source, not output."""
    assessment = scan_candidate("", summary(changed("vendor/github.com/x/y.go")))

    assert assessment.decision is ScopePolicyDecision.BLOCK
    assert assessment.findings[0].kind is SecurityFindingKind.GENERATED_ARTIFACT


def test_a_project_that_deliberately_versions_its_build_output_can_say_so():
    """Concern 22: a Go project that vendors its dependencies had no way to
    pass verification while the pattern list was a module constant."""
    assessment = scan_candidate(
        "",
        summary(changed("vendor/github.com/x/y.go")),
        generated_path_exceptions=("vendor/**",),
    )

    assert assessment.decision is ScopePolicyDecision.ALLOW
    assert assessment.findings == ()


def test_an_exception_does_not_widen_to_other_generated_paths():
    """Declaring `vendor/` says nothing about `dist/`."""
    assessment = scan_candidate(
        "",
        summary(changed("dist/bundle.js")),
        generated_path_exceptions=("vendor/**",),
    )

    assert assessment.decision is ScopePolicyDecision.BLOCK


def test_an_exception_never_excuses_credential_material():
    """A declared generated path is still not a place to put a private key:
    the forbidden-file check is ordered ahead of the generated-path check."""
    assessment = scan_candidate(
        "",
        summary(changed("vendor/config/service-account.json")),
        generated_path_exceptions=("vendor/**",),
    )

    assert assessment.decision is ScopePolicyDecision.BLOCK
    assert assessment.findings[0].kind is SecurityFindingKind.FORBIDDEN_FILE


# ----------------------------- a name beside a bare word is not a credential
#
# Found by a real campaign. UI-002 run #2 built, tested and then failed the
# security scan alone, on two diff lines that read:
#
#     readonly totalTokens: number;
#
# ``TOKEN`` is a secret-name hint and must stay one, the ``named_value`` shape
# accepts ``:`` as a separator, and ``number`` is six characters, is no
# environment lookup and is no known placeholder. So every ingredient of a
# credential was present except a credential: what follows the colon is the
# *type* of the value, and the value is not on the line at all.
#
# The scanner does not try to recognise declarations -- that would mean
# knowing six languages' grammars. It asks whether there is enough evidence to
# block: a colon, an unquoted value and a bare identifier leave the *name* as
# the only thing suggesting a secret, and a name is not evidence of a value.
# These tests are therefore about where that evidence runs out, and about
# everything that still has enough of it.


def ts_declaration(*lines: str):
    """Scan added lines as a TypeScript source file."""
    return scan_candidate(diff_adding(*lines), summary(changed("src/state.ts")))


def test_a_typed_token_property_is_not_credential_material():
    """The campaign line, exactly as it appeared in the diff."""
    assessment = ts_declaration("  readonly totalTokens: number;")

    assert assessment.decision is ScopePolicyDecision.ALLOW
    assert assessment.findings == ()


@pytest.mark.parametrize(
    "line",
    [
        # The reported line and its family: a credential-shaped name, a colon,
        # and a bare word where a secret would be. No list of type names is
        # consulted, so a project's own types are covered by the same rule as
        # the language's.
        "  readonly totalTokens: number;",
        "  apiKey: string;",
        "  totalTokens: TokenCount;",
        "  credentialMode: RequestCredentials[];",
        "  tokenStream: Observable<string>;",
        "  authInstant: java.time.Instant;",
        # No terminator and no modifier either: the evidence is absent whatever
        # surrounds the match.
        "export function rotate(apiKey: string) {",
        # Names that a built-in-type dictionary would have got wrong, because
        # digits in a type name are not digits in a secret.
        "  authCount: int32;",
        "  oauthToken: OAuth2Token;",
        "  keyEncoder: Base64Encoder;",
        # The ``auth_header`` shape reaches the same false conclusion about an
        # Angular property, so it is held to the same rule.
        "  authorization: HttpHeaders;",
    ],
)
def test_a_name_beside_a_bare_identifier_does_not_block(line: str):
    assessment = ts_declaration(line)

    assert assessment.decision is ScopePolicyDecision.ALLOW, line
    assert assessment.findings == (), line


# ------------------------------- and every high-confidence shape still blocks


@pytest.mark.parametrize(
    ("line", "why"),
    [
        # Synthetic throughout: none of these is a real credential.
        #
        # ``=`` is an assignment wherever it is spelled that way.
        ("TOKEN=Zx91fakefakenotrealvalue", "named assignment"),
        ("export AUTH_SECRET=Zx91fakefakenotrealvalue", "named assignment"),
        ('DATABASE_PASSWORD = "hunter2-not-a-real-password"', "named assignment"),
        # A quoted value is data, not an annotation.
        ('  "api_token": "Zx91fakefakenotrealvalue",', "quoted JSON value"),
        ("  password: 'Zx91fakefakenotrealvalue'", "quoted value"),
        # Unquoted after a colon, but credential punctuation cannot be spelled
        # as an identifier.
        ("api_token: Zx91-fakefake-notreal-value;", "punctuated value"),
        ("secret_key: Zx91fakefake/notreal+value;", "punctuated value"),
        # An Authorization scheme puts the value after the scheme, not after
        # the colon.
        (
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.ZZfakebodyZZfake.ZZfakesigZZfake",
            "auth header",
        ),
        ("authorization: basic dXNlcjpwYXNzd29yZA==", "auth header"),
        # The content-identifying shapes are outside the rule and act as its
        # backstop: these block even though the line is otherwise excused.
        (
            "  authToken: eyJhbGciOiJIUzI1NiJ9.ZZfakebodyZZfake.ZZfakesigZZfake;",
            "JWT backstop on an excused line",
        ),
        # Not AWS's own ``AKIAIOSFODNN7EXAMPLE``: that contains "EXAMPLE" and
        # is correctly allowed by the placeholder rule, which predates this.
        ("  aws_access_key: AKIAZZ91FAKEFAKEFAKE;", "AWS prefix backstop"),
        ('const key = "sk-ZZfakefakefakefakefake1234";', "provider prefix"),
        ("token = 'ghp_ZZfakefakefakefakefakefake123456'", "provider prefix"),
    ],
)
def test_high_confidence_credential_material_still_blocks(line: str, why: str):
    assessment = ts_declaration(line)

    assert assessment.blocked, f"{why}: {line}"
    assert assessment.findings[0].kind is SecurityFindingKind.SECRET_MATERIAL
    # The finding must not republish what it found (see the test above).
    assert "Zx91fakefake" not in assessment.summary(), line


def test_a_declaration_does_not_excuse_a_private_key_on_the_same_line():
    """PEM detection is outside the rule entirely."""
    assessment = ts_declaration(
        "  readonly key: string = '-----BEGIN RSA PRIVATE KEY-----';"
    )

    assert assessment.blocked
    assert assessment.findings[0].kind is SecurityFindingKind.SECRET_MATERIAL


def test_the_name_itself_is_never_what_excuses_a_line():
    """``TOKEN`` remains a secret-name hint and ``totalTokens`` is on no
    allow-list. The same name blocks or allows according to whether there is
    evidence of a value beside it."""
    assert ts_declaration("  totalTokens: number;").decision is ScopePolicyDecision.ALLOW
    assert ts_declaration("totalTokens=Zx91fakefakenotrealvalue").blocked
