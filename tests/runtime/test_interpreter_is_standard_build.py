"""The interpreter this project runs on is a decision, not an accident.

Section 15.1 of the runtime spec chose the standard CPython 3.14 build over the
free-threaded one, because five packages this project wants publish a cp314 wheel
and no cp314t wheel, and this box has no C compiler to fall back on. A venv
rebuilt on the wrong interpreter would not fail loudly anywhere else -- it would
fail weeks later, at an install, with a message about a missing 'cc'.
"""

import sys
import sysconfig

import runtime


def test_interpreter_is_the_standard_build_not_free_threaded():
    assert sysconfig.get_config_var("Py_GIL_DISABLED") == 0, (
        "this venv is the free-threaded build; spec section 15.1 chose the standard "
        "build. Rebuild with: uv venv --python 3.14.4 .venv"
    )


def test_interpreter_is_python_3_14():
    assert sys.version_info[:2] == (3, 14)


def test_runtime_package_is_importable():
    assert runtime.__name__ == "runtime"
