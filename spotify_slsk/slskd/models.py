"""Shapes of the JSON slskd returns, and helpers for its peer paths."""
import re


def remote_basename(path):
    """Basename of a peer path. slskd paths use backslashes, which
    os.path.basename does not split on Linux — so the whole share path,
    folder names included, used to be scored and blacklisted."""
    return re.split(r"[\\/]", path or "")[-1]
