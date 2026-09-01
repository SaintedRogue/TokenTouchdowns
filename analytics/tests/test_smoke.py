from tt import __version__


def test_package_imports_and_declares_a_version():
    assert isinstance(__version__, str)
    assert __version__
