"""Send a verified shallow pack to the offline workspace."""

from .coding_broker import clone_request
from .import_client import _deliver


def deliver_clone(socket_path, repo, commit, sha256):
    request = clone_request(repo, commit, sha256)
    return _deliver(socket_path, {"clone_repo": request.repo, "commit": request.commit, "sha256": request.sha256},
                    f"/work/repos/{repo}/{commit}", "cloned")
