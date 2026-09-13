"""The one secret that has to be readable again."""
from __future__ import annotations

import pytest

from git_synapse import vault


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(vault.ENV_KEY, "a-passphrase-someone-chose")
    return "ghp_supersecrettokenvalue"


def test_a_sealed_token_comes_back_and_the_ciphertext_does_not_contain_it(keyed):
    sealed = vault.seal(keyed)
    assert keyed not in sealed
    assert vault.open_(sealed) == keyed


def test_the_same_token_seals_differently_every_time(keyed):
    """Fernet carries a random IV."""
    assert vault.seal(keyed) != vault.seal(keyed)


def test_a_real_fernet_key_is_used_as_given(monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv(vault.ENV_KEY, key)
    assert vault.open_(vault.seal("ghp_x_but_long_enough")) == "ghp_x_but_long_enough"


def test_without_a_key_nothing_can_be_stored(monkeypatch):
    """The feature is opt-in, and the failure is a refusal rather than a plaintext write."""
    monkeypatch.delenv(vault.ENV_KEY, raising=False)
    assert vault.available() is False
    with pytest.raises(vault.VaultError, match=vault.ENV_KEY):
        vault.seal("ghp_x")


def test_seal_refuses_an_empty_secret(keyed):
    with pytest.raises(vault.VaultError):
        vault.seal("")


@pytest.mark.parametrize("stored", [None, "", "not-ciphertext", "gAAAAABtruncated"])
def test_anything_unreadable_opens_as_empty_rather_than_raising(keyed, stored):
    """A rotated key or a row from another deployment must not take a discovery run down: the caller's fallback is the environment credential, which works."""
    assert vault.open_(stored) == ""


def test_a_rotated_key_does_not_raise(keyed, monkeypatch):
    sealed = vault.seal(keyed)
    monkeypatch.setenv(vault.ENV_KEY, "a-different-passphrase-entirely")
    assert vault.open_(sealed) == ""


def test_the_hint_identifies_without_revealing(keyed):
    hint = vault.hint(keyed)
    assert hint.startswith("ghp_") and hint.endswith("alue")
    assert keyed not in hint
    # Too short to have a middle worth hiding: show nothing at all.
    assert vault.hint("short") == "••••"
    assert vault.hint("") == "••••"


def test_available_is_true_once_a_key_is_set(keyed):
    """Asked before the form offers to keep a token at all."""
    assert vault.available() is True
