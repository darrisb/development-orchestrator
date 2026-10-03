"""Secret redaction (build.md sections 12 and 36).

Command output becomes a run artifact and part of a reviewer's package, so a
credential printed by a build script has two ways off the machine. Both halves
of the redactor are tested here, and so is the thing that would make it useless:
masking so much that a build log stops being readable.
"""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.redaction import (
    PLACEHOLDER,
    Redactor,
    is_secret_name,
)

# --- known values ------------------------------------------------------------


def test_a_known_secret_is_masked_wherever_it_appears():
    redactor = Redactor.for_values(["hunter2-is-a-long-secret"])

    masked = redactor.redact(
        "connecting with hunter2-is-a-long-secret\nretry with hunter2-is-a-long-secret\n"
    )

    assert "hunter2-is-a-long-secret" not in masked
    assert masked.count(PLACEHOLDER) == 2


def test_a_longer_secret_is_masked_before_a_shorter_one_it_contains():
    """Masking the prefix first would leave the rest of the longer secret in
    the output."""
    redactor = Redactor.for_values(["prefix-secret", "prefix-secret-with-more"])

    assert redactor.redact("value=prefix-secret-with-more") == f"value={PLACEHOLDER}"


def test_a_value_too_short_to_be_a_secret_is_not_masked():
    """A two-character "secret" would match constantly and turn a build log
    into noise, and it is not protecting anything."""
    redactor = Redactor.for_values(["ab"])

    assert redactor.redact("ab is a common substring: abstract, absolute") == (
        "ab is a common substring: abstract, absolute"
    )


def test_a_redactor_does_not_print_its_secrets():
    redactor = Redactor.for_values(["hunter2-is-a-long-secret"])

    assert "hunter2" not in repr(redactor)


# --- shapes ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("API_TOKEN=abc123def456", "abc123def456"),
        ('{"apiKey": "abc123def456"}', "abc123def456"),
        ("Authorization: Bearer abcdefghijklmnopqrst", "abcdefghijklmnopqrst"),
        ("authorization: basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("using sk-abcdefghijklmnopqrstuvwxyz012345", "sk-abcdefghijklmnopqrstuvwxyz012345"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
        ("aws AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
    ],
)
def test_a_credential_this_process_never_knew_about_is_masked_by_shape(
    text: str, secret: str
):
    masked = Redactor().redact(text)

    assert secret not in masked
    assert PLACEHOLDER in masked


def test_a_private_key_block_is_masked_whole():
    text = (
        "writing key\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc\n"
        "-----END RSA PRIVATE KEY-----\ndone\n"
    )

    masked = Redactor().redact(text)

    assert "MIIEowIBAAKCAQEA" not in masked
    assert masked.startswith("writing key\n")
    assert masked.endswith("done\n")


def test_the_scheme_and_the_variable_name_survive_so_the_log_stays_readable():
    """An operator needs to see that an Authorization header was present, not
    just that something was removed."""
    masked = Redactor().redact("Authorization: Bearer abcdefghijklmnopqrst")

    assert masked == f"Authorization: Bearer {PLACEHOLDER}"


@pytest.mark.parametrize(
    "text",
    [
        "42 tests passed in 3.2s",
        "FAIL src/auth.test.ts > session token is rotated",
        "npm run compile: tsc --noEmit",
        "ERROR in src/navigation.ts:12:5 - Type 'string' is not assignable",
        "added 214 packages in 8s",
    ],
)
def test_ordinary_build_output_is_left_alone(text: str):
    """The failure mode that would get redaction switched off."""
    assert Redactor().redact(text) == text


def test_shape_masking_can_be_turned_off_for_a_deterministic_comparison():
    redactor = Redactor.for_values(["hunter2-is-a-long-secret"], mask_shapes=False)

    assert redactor.redact("API_TOKEN=printed-by-a-build") == "API_TOKEN=printed-by-a-build"


# --- names -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["API_KEY", "NPM_TOKEN", "DB_PASSWORD", "aws_secret_access_key", "AUTH_HEADER"]
)
def test_a_credential_shaped_variable_name_is_recognised(name: str):
    assert is_secret_name(name)


@pytest.mark.parametrize("name", ["PATH", "HOME", "CI", "NODE_ENV", "TZ"])
def test_an_ordinary_variable_name_is_not(name: str):
    assert not is_secret_name(name)


def test_an_environment_is_logged_with_its_secret_values_masked_by_name():
    """The name is the reliable signal for an environment: the value may be
    anything at all, including something that looks like ordinary text."""
    masked = Redactor().redact_mapping(
        {"PATH": "/usr/bin", "CI": "true", "NPM_TOKEN": "plain-looking-value"}
    )

    assert masked == {"PATH": "/usr/bin", "CI": "true", "NPM_TOKEN": PLACEHOLDER}


# ----------------------------------- redaction stays eager where the scan does not
#
# The candidate scanner stopped treating ``readonly totalTokens: number;`` as
# credential material, because blocking a candidate needs confidence that the
# line assigns a secret rather than declares a type. Redaction makes the
# opposite trade on purpose: masking an innocent value in a build log costs a
# word of readability, so it keeps the benefit of the doubt.
#
# These tests pin that asymmetry. If a later change moves the scanner's
# judgement down into ``SHAPE_PATTERNS``, they fail.


@pytest.mark.parametrize(
    ("text", "masked_part"),
    [
        ("readonly totalTokens: number;", "number"),
        ("  apiKey: string;", "string"),
        ("  authorization: HttpHeaders;", "HttpHeaders"),
    ],
)
def test_a_type_declaration_in_a_log_is_still_masked_eagerly(
    text: str, masked_part: str
):
    """The shared shapes did not get looser to make the scanner more accurate.

    A log line is not a candidate: there is no cost to masking this and no
    assurance to be had from leaving it, so the conservative half of the
    design is unchanged.
    """
    masked = Redactor().redact(text)

    assert PLACEHOLDER in masked
    assert masked_part not in masked


def test_the_name_hints_still_include_token():
    """Dropping ``TOKEN`` would have been the cheap fix and the wrong one: it
    is the hint that catches ``API_TOKEN=...`` in a printed environment."""
    assert is_secret_name("API_TOKEN")
    assert is_secret_name("totalTokens")
    assert PLACEHOLDER in Redactor().redact("API_TOKEN=Zx91fakefakenotrealvalue")
