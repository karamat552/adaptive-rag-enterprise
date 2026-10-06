"""Target classification for measurement/bootstrap scripts.

Classifies the EFFECTIVE database target(s) of a script as PRODUCTION or
disposable/local, and refuses production for write-capable scripts with
NO override flag — mirroring the audit-tool fix (2026-10-05 lesson):
a script that can write must never silently trust the ambient .env target,
and there is deliberately no flag that permits a production write.

Classification compares (host, database) pairs, not hosts alone: a
disposable database on the SAME Neon host as production (the established
disposable pattern) must be allowed, while the production database itself
— under either the runtime or the admin identity — must be refused.
Writes are split across identities (runtime + admin), so EVERY effective
URL is classified and the most dangerous class wins.

Read-only measurements (measure_recall.py) do not refuse production;
they enforce READ ONLY at the Postgres level instead (see that script).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent


def _parse(url: str) -> Optional[Tuple[str, str]]:
    """(host, database) from a postgres URL; credentials are ignored."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").rpartition("@")[-1]
        path = (parsed.path or "").lstrip("/")
        return (host, path) if host else None
    except Exception:
        return None


def _production_targets() -> Set[Tuple[str, str]]:
    """(host, database) pairs of the production DB from .env — both the
    runtime and the admin identities, because writes split across them."""
    targets: Set[Tuple[str, str]] = set()
    env_path = REPO / ".env"
    if not env_path.exists():
        return targets
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        for var in ("DB_DATABASE_URL=", "DB_ADMIN_DATABASE_URL="):
            if line.startswith(var):
                parsed = _parse(line.split("=", 1)[1].strip().strip('"'))
                if parsed:
                    targets.add(parsed)
    return targets


def _is_local(url: str) -> bool:
    """localhost / 127.0.0.1 / ::1 / unix-socket targets are always local."""
    parsed = _parse(url)
    if not parsed:
        return False
    host = parsed[0].lower()
    return (host in ("localhost", "127.0.0.1", "::1")
            or host.startswith("/"))


def classify_targets(urls: Dict[str, str]) -> Tuple[str, List[str]]:
    """Classify every provided URL; return the most dangerous class and
    the offending URL labels. 'production' beats 'unknown-remote' beats
    'local-or-disposable'.

    FAIL-CLOSED on unknown remotes (the reviewer's pushback, 2026-10-07):
    the old version classified anything not matching .env's production
    pairs as 'local-or-disposable' — which fails OPEN when .env is missing
    or unparseable (the guard then passes everything). Now: a NON-local
    target that cannot be verified against .env's production pairs is
    'unknown-remote', and write-capable scripts refuse it — only an
    explicitly local target, or a remote verified against a readable .env,
    may be written."""
    prod = _production_targets()
    env_ok = bool(prod) or not (REPO / ".env").exists()
    offending: List[str] = []
    worst = "local-or-disposable"
    for label, url in sorted(urls.items()):
        parsed = _parse(url)
        if parsed and parsed in prod:
            offending.append(label)
            worst = "production"
        elif not env_ok and parsed and not _is_local(url):
            if worst != "production":
                offending.append(label)
                worst = "unknown-remote"
    return worst, offending


def refuse_production_writes(urls: Dict[str, str], script_name: str) -> None:
    """Hard refusal for write-capable scripts. There is deliberately NO
    flag to override this — a production write must never be one argument
    away (the audit-tool lesson). Unknown-remote targets fail closed too:
    if .env is missing or unparseable, a remote target cannot be verified
    as non-production, so it is refused."""
    worst, offending = classify_targets(urls)
    if worst == "production":
        print("REFUSED: the effective database target is PRODUCTION "
              f"({', '.join(offending)}).")
        print(f"  {script_name} writes; production must never be its target.")
        print("  Point it at a seeded disposable stack")
        print("  (scripts/local_stack_bootstrap.py --database-url ...).")
        print("  There is deliberately no flag to override this refusal.")
        sys.exit(1)
    if worst == "unknown-remote":
        print("REFUSED: the database target is a remote that could not be")
        print(f"  verified as non-production ({', '.join(offending)}) — .env is")
        print("  missing or has no readable DB URLs. Failing closed: writes")
        print("  to an unverifiable remote are refused.")
        sys.exit(1)


def banner_for_read_only(url: str) -> str:
    """A loud, honest banner for scripts that may target production
    READ-ONLY (measure_recall.py): names the target and the enforcement."""
    prod = _production_targets()
    parsed = _parse(url)
    is_prod = parsed in prod if parsed else False
    target = f"{parsed[0]}/{parsed[1]}" if parsed else "n/a"
    if is_prod:
        return (f"TARGET IS PRODUCTION ({target}) — read-only measurement; "
                "Postgres-enforced READ ONLY (self-checked)")
    return f"target: {target} (disposable/local)"
