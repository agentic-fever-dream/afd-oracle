"""Every catalogue entry has a builder, every module imports, and every builder is in the spec."""
from afd_oracle_ingest.catalog import IMPORT_ERRORS, load_all, spec


def test_all_modules_import():
    load_all()
    assert IMPORT_ERRORS == {}


def test_every_spec_entry_has_a_builder():
    missing = sorted(set(spec()["candidates"]) - set(load_all()))
    assert missing == [], f"{len(missing)} catalogue ids without a builder: {missing}"
