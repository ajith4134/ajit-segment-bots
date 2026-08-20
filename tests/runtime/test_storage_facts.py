"""On this box /tmp is tmpfs -- RAM with a filesystem interface.

State written there is not durable and memory measured there is measuring RAM
against RAM. Section 5 of the runtime spec records the round of measurement that
was lost to exactly this. These tests keep the guard honest.
"""

import pathlib

import pytest

from runtime.storage_facts import (
    VolatileStorageRefused,
    is_memory_backed_filesystem,
    read_filesystem_type,
    require_durable_directory,
)


def test_reads_the_filesystem_type_of_a_real_directory(durable_tmp_path):
    assert read_filesystem_type(durable_tmp_path) == "ext4"


def test_recognises_tmp_as_memory_backed_on_this_box():
    assert is_memory_backed_filesystem(pathlib.Path("/tmp")) is True


def test_recognises_the_home_filesystem_as_durable():
    assert is_memory_backed_filesystem(pathlib.Path.home()) is False


def test_require_durable_directory_returns_the_path_when_it_is_durable(durable_tmp_path):
    assert require_durable_directory(durable_tmp_path) == durable_tmp_path


def test_require_durable_directory_refuses_tmpfs_and_names_the_filesystem():
    with pytest.raises(VolatileStorageRefused) as refusal:
        require_durable_directory(pathlib.Path("/tmp"))
    assert "tmpfs" in str(refusal.value)


def test_resolves_the_filesystem_of_a_path_that_does_not_exist_yet(durable_tmp_path):
    unborn = durable_tmp_path / "not" / "created" / "yet"
    assert read_filesystem_type(unborn) == "ext4"
