"""pytest only: skip the tests that start a real program when that program isn't installed.

Every other external program the tests touch (makemkvcon, ffmpeg, mkvmerge, tesseract,
dovi_tool) is faked, but these tests copy real files with rsync. Run directly with
`python3 discshelf/test_x.py` they still fail loudly instead, which is what a developer wants.
"""
import os
import shutil

import pytest

NEEDS = {
    "test_encode.py": ["rsync"],
    "test_pipeline.py": ["rsync"],
    "test_queue.py": ["rsync"],
    "test_transfer.py": ["rsync"],
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        missing = [t for t in NEEDS.get(os.path.basename(str(item.fspath)), []) if not shutil.which(t)]
        if missing:
            item.add_marker(pytest.mark.skip(reason=f"{', '.join(missing)} is not installed"))
