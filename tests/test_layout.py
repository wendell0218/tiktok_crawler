import argparse
import ast
import importlib
import sys
from pathlib import Path

import pytest

from dycrawler import options


ROOT = Path(__file__).resolve().parents[1]
ENTRIES = ("crawl_keyword", "crawl_user")
HELPERS = {
    "install": "python -m pip install -r requirements.txt",
    "login": "python -m dycrawler login",
    "search": "python -m dycrawler search",
    "download": "python -m dycrawler download",
    "refresh": "python -m dycrawler refresh",
    "test": "python -m pytest -q",
}


def test_two_parallel_entry_pairs_and_no_old_entry():
    assert {path.name for path in ROOT.glob("*.sh")} == {f"{name}.sh" for name in ENTRIES}
    assert not (ROOT / "crawl.py").exists()
    for name in ENTRIES:
        assert (ROOT / f"{name}.py").is_file()
        assert f"python {name}.py " in (ROOT / f"{name}.sh").read_text()


def test_auxiliary_scripts_are_complete_and_grouped():
    assert {path.stem for path in (ROOT / "scripts").glob("*.sh")} == set(HELPERS)
    for name, command in HELPERS.items():
        script = (ROOT / "scripts" / f"{name}.sh").read_text()
        assert script.startswith("cd ")
        assert "conda activate douyin-crawler" in script
        assert command in script
        assert not (ROOT / f"{name}.sh").exists()


@pytest.mark.parametrize("name,description", [
    ("crawl_keyword", "按关键词搜索并下载抖音视频"),
    ("crawl_user", "下载指定用户可公开访问的抖音视频"),
])
def test_parallel_entries_have_chinese_help(name, description, monkeypatch, capsys):
    entry = importlib.import_module(name)
    monkeypatch.setattr(sys, "argv", [f"{name}.py", "--help"])
    with pytest.raises(SystemExit) as error:
        entry.main()
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert description in output
    assert "并发下载数" in output
    assert "--count" in output and "--output-dir" in output


@pytest.mark.parametrize("name", ENTRIES)
def test_entries_use_shared_options_without_importing_each_other(name):
    entry = importlib.import_module(name)
    for validator in ("positive_integer", "quality_value", "nonnegative_number"):
        assert getattr(entry, validator) is getattr(options, validator)
    tree = ast.parse((ROOT / f"{name}.py").read_text())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module.split(".")[0])
    assert not set(imported) & {"crawl", *ENTRIES}


@pytest.mark.parametrize("validator,value,expected", [
    ("positive_integer", "1", 1),
    ("positive_integer", "200", 200),
    ("quality_value", " BEST ", "best"),
    ("quality_value", "worst", "worst"),
    ("quality_value", "720", "720p"),
    ("quality_value", "1080P", "1080p"),
    ("nonnegative_number", "0", 0.0),
    ("nonnegative_number", "2.5", 2.5),
])
def test_shared_options_preserve_accepted_values(validator, value, expected):
    assert getattr(options, validator)(value) == expected


@pytest.mark.parametrize("validator,value", [
    ("positive_integer", "0"),
    ("positive_integer", "-1"),
    ("quality_value", "0p"),
    ("quality_value", "abc"),
    ("nonnegative_number", "-1"),
])
def test_shared_options_preserve_rejected_values(validator, value):
    with pytest.raises(argparse.ArgumentTypeError):
        getattr(options, validator)(value)
