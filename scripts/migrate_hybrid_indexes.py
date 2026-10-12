"""Explicit owned-document migration; preserves prior versions for rollback, no parser calls."""

import argparse
import json
from pathlib import Path

from backend.application.runtime import build_runtime
from backend.infrastructure.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--document-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--manifest", default="runtime/hybrid_migration.json")
    args = parser.parse_args()
    settings = Settings()
    runtime = build_runtime(settings)
    path = Path(args.manifest)
    try:
        user = runtime.auth.get_user_by_username(settings.auth_bootstrap_username)
        if user is None:
            raise RuntimeError("Authenticated migration owner is unavailable")
        if args.rollback:
            if not args.apply:
                raise RuntimeError("Rollback requires --apply")
            manifest = json.loads(path.read_text(encoding="utf-8"))
            for entry in manifest["documents"]:
                document = runtime.documents.get(entry["document_id"], user_id=user.user_id)
                old = runtime.versions.get(entry["old_version"])
                if (
                    document is None
                    or old is None
                    or document.status != "available"
                    or document.active_index_version != entry["new_version"]
                    or old.document_id != document.document_id
                    or old.status != "retired"
                ):
                    raise RuntimeError("Rollback manifest conflicts with current lifecycle")
                runtime.versions.mark_validated(old.index_version, chunk_count=old.chunk_count)
                if not runtime.registry.promote(old.index_version):
                    raise RuntimeError("Rollback promotion was refused")
            print(f"rollback_ok documents={len(manifest['documents'])}")
            return 0
        if not args.document_id:
            raise RuntimeError("Supply explicit owned --document-id; broad migration is forbidden")
        manifest = {
            "generation": "hybrid_v1",
            "analyzer": "chinese",
            "rrf_k": 60,
            "chunking": "preserved-source-structure",
            "documents": [],
        }
        if path.exists() and args.apply:
            prior = json.loads(path.read_text(encoding="utf-8"))
            if prior.get("generation") != "hybrid_v1":
                raise RuntimeError("Existing manifest has an incompatible generation")
            manifest = prior
        for document_id in dict.fromkeys(args.document_id):
            document = runtime.documents.get(document_id, user_id=user.user_id)
            if document is None or document.status != "available":
                raise RuntimeError("Selected document is unavailable")
            old = runtime.versions.get(document.active_index_version or "")
            if old is None:
                raise RuntimeError("Selected active version is unavailable")
            if old.collection_name.endswith("_hybrid_v1"):
                print(f"already_hybrid document={document_id}")
                continue
            if not args.apply:
                print(f"planned document={document_id} children={old.chunk_count}")
                continue
            outcome = runtime.ingestion.migrate_hybrid_index(
                user_id=user.user_id, document_id=document_id
            )
            job = runtime.jobs.get(outcome.ingestion_job_id)
            if job is None or job.status != "success":
                raise RuntimeError(
                    f"Migration failed safely: {job.failure_code if job else 'missing_job'}"
                )
            manifest["documents"].append(
                {
                    "document_id": document_id,
                    "old_version": old.index_version,
                    "new_version": outcome.index_version,
                    "job_id": outcome.ingestion_job_id,
                    "children": old.chunk_count,
                    "old_collection": old.collection_name,
                }
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"migrated document={document_id} children={old.chunk_count}")
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
