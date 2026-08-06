from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4


MAX_KUBECONFIG_BYTES = 256 * 1024
_MANAGED_FILENAME = re.compile(r"^[0-9a-f]{32}\.yaml$")


class KubeconfigFiles:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save_text(self, contents: str) -> str:
        if not contents.strip():
            raise ValueError("Kubeconfig file cannot be empty.")
        encoded = contents.encode("utf-8")
        if len(encoded) > MAX_KUBECONFIG_BYTES:
            raise ValueError("Kubeconfig file must be 256 KiB or smaller.")
        path: Path | None = None
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root.chmod(0o700)
            path = self.root / f"{uuid4().hex}.yaml"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
        except OSError as exc:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ValueError("Kubeconfig file could not be stored.") from exc
        return str(path)

    def remove(self, value: str) -> None:
        path = Path(value)
        try:
            root = self.root.resolve(strict=False)
            resolved = path.resolve(strict=False)
            if resolved.parent != root or not _MANAGED_FILENAME.fullmatch(resolved.name):
                return
            resolved.unlink(missing_ok=True)
        except OSError:
            return
