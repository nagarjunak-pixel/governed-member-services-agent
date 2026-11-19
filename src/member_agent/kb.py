"""Tenant-isolated knowledge-base loader and retriever for plan documents.

Security & governance invariants:
- Separate per-tenant indexes; retrieval for tenant A never touches tenant B.
- Folder name and YAML front-matter `tenant` must agree; mismatches are quarantined.
- Product-code filtering prevents mixing benefit designs (e.g. Choice PPO vs Value HDHP).
- Plan-year filtering defaults to the active plan year unless another year is requested.
- Prompt-injection directives embedded in KB documents are stripped at ingest time.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"[a-z0-9]+")
INJECTION_LINE_RE = re.compile(
    r"(?:processing\s+note\s+for\s+ai|ai\s+coding\s+assistant|do\s+not\s+mention\s+this\s+note|"
    r"ignore\s+previous\s+instructions|system\s+prompt)",
    re.IGNORECASE,
)


def _tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def _parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw
    meta: dict[str, str] = {}
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
        if ":" in lines[idx]:
            key, val = lines[idx].split(":", 1)
            meta[key.strip().lower()] = val.strip()
    if end_idx is None:
        return {}, raw
    body = "\n".join(lines[end_idx + 1 :]).strip()
    return meta, body


def _strip_injection_sections(body: str) -> tuple[str, bool]:
    """Remove any Markdown section or line containing AI-assistant prompt-injection directives."""
    lines = body.splitlines()
    kept: list[str] = []
    skipping_section = False
    stripped_any = False

    for line in lines:
        if line.startswith("## "):
            skipping_section = False
        if INJECTION_LINE_RE.search(line):
            stripped_any = True
            # If the previous kept line was a heading just for this note, drop it too.
            while kept and kept[-1].strip() == "":
                kept.pop()
            if kept and kept[-1].startswith("## "):
                kept.pop()
            skipping_section = True
            continue
        if not skipping_section:
            kept.append(line)

    return "\n".join(kept).strip(), stripped_any


@dataclass(frozen=True)
class KBDocument:
    doc_id: str
    tenant: str
    plan_year: int
    effective_date: str
    product_code: str
    product: str
    title: str
    content: str
    tokens: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class QuarantinedDocument:
    relative_path: str
    folder_tenant: str
    front_matter_tenant: str | None
    reason: str


class TenantKnowledgeBase:
    """In-memory per-tenant Markdown knowledge base with strict isolation checks."""

    def __init__(self, kb_dir: Path, default_plan_year: int = 2026) -> None:
        self.kb_dir = kb_dir
        self.default_plan_year = default_plan_year
        self._docs_by_tenant: dict[str, list[KBDocument]] = {}
        self.quarantined_documents: list[QuarantinedDocument] = []
        self.sanitised_documents: list[str] = []
        self._load()

    def _load(self) -> None:
        if not self.kb_dir.is_dir():
            return
        for tenant_dir in sorted(p for p in self.kb_dir.iterdir() if p.is_dir()):
            folder_tenant = tenant_dir.name
            tenant_docs = self._docs_by_tenant.setdefault(folder_tenant, [])

            for md_path in sorted(tenant_dir.glob("*.md")):
                rel_path = str(md_path.relative_to(self.kb_dir))
                raw = md_path.read_text(encoding="utf-8")
                meta, body = _parse_front_matter(raw)
                fm_tenant = meta.get("tenant")

                if not fm_tenant or fm_tenant != folder_tenant:
                    self.quarantined_documents.append(
                        QuarantinedDocument(
                            relative_path=rel_path,
                            folder_tenant=folder_tenant,
                            front_matter_tenant=fm_tenant,
                            reason=(
                                f"Tenant mismatch: located in '{folder_tenant}/' "
                                f"but front-matter declares tenant='{fm_tenant}'."
                            ),
                        )
                    )
                    continue

                clean_body, had_injection = _strip_injection_sections(body)
                if had_injection:
                    self.sanitised_documents.append(rel_path)

                title = md_path.stem
                for line in clean_body.splitlines():
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break

                plan_year = int(meta.get("plan_year", self.default_plan_year))
                product_code = (meta.get("product_code") or "ALL").upper()
                doc = KBDocument(
                    doc_id=rel_path,
                    tenant=folder_tenant,
                    plan_year=plan_year,
                    effective_date=meta.get("effective_date", ""),
                    product_code=product_code,
                    product=meta.get("product", ""),
                    title=title,
                    content=clean_body,
                    tokens=tuple(_tokenize(f"{title} {meta.get('product', '')} {clean_body}")),
                )
                tenant_docs.append(doc)

    def list_documents(self, tenant: str) -> list[KBDocument]:
        return list(self._docs_by_tenant.get(tenant, []))

    def retrieve(
        self,
        *,
        tenant: str,
        query: str,
        product_code: str | None = None,
        plan_year: int | None = None,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        """Retrieve the most relevant KB documents strictly within `tenant`."""
        docs = self._docs_by_tenant.get(tenant, [])
        if not docs:
            return []

        target_year = plan_year or self.default_plan_year
        norm_product = product_code.upper() if product_code else None

        # Step 1: Filter by product_code (matching product + plan-wide ALL docs)
        if norm_product:
            product_filtered = [d for d in docs if d.product_code in (norm_product, "ALL")]
        else:
            product_filtered = list(docs)

        # Step 2: Filter by plan_year. Keep exact year for product docs; for ALL docs,
        # prefer target_year and fall back to latest <= target_year if no doc exists for target_year.
        candidates: list[KBDocument] = []
        for d in product_filtered:
            if d.plan_year == target_year:
                candidates.append(d)
            elif d.product_code == "ALL" and d.plan_year <= target_year:
                # Only include if there isn't a newer version for the same base stem
                has_exact = any(
                    other.product_code == "ALL"
                    and other.plan_year == target_year
                    and other.title == d.title
                    for other in product_filtered
                )
                if not has_exact:
                    candidates.append(d)

        if not candidates:
            candidates = product_filtered

        query_tokens = _tokenize(query)
        scored = self._score_bm25(candidates, query_tokens, preferred_product=norm_product)
        results: list[dict[str, Any]] = []
        for doc, score in scored[:top_k]:
            results.append(
                {
                    "doc_id": doc.doc_id,
                    "tenant": doc.tenant,
                    "plan_year": doc.plan_year,
                    "effective_date": doc.effective_date,
                    "product_code": doc.product_code,
                    "product": doc.product,
                    "title": doc.title,
                    "score": round(score, 4),
                    "content": doc.content,
                }
            )
        return results

    @staticmethod
    def _score_bm25(
        docs: list[KBDocument],
        query_tokens: list[str],
        *,
        preferred_product: str | None = None,
    ) -> list[tuple[KBDocument, float]]:
        if not docs:
            return []
        n_docs = len(docs)
        avg_len = sum(len(d.tokens) for d in docs) / max(n_docs, 1)
        k1 = 1.5
        b = 0.75

        df: dict[str, int] = {}
        for tok in set(query_tokens):
            df[tok] = sum(1 for d in docs if tok in d.tokens)

        scored: list[tuple[KBDocument, float]] = []
        for doc in docs:
            score = 0.0
            doc_len = len(doc.tokens) or 1
            tf_counts: dict[str, int] = {}
            for tok in doc.tokens:
                tf_counts[tok] = tf_counts.get(tok, 0) + 1

            title_lower = doc.title.lower()
            for tok in query_tokens:
                tf = tf_counts.get(tok, 0)
                if tf == 0:
                    continue
                idf = math.log((n_docs - df[tok] + 0.5) / (df[tok] + 0.5) + 1.0)
                denom = tf + k1 * (1.0 - b + b * (doc_len / max(avg_len, 1.0)))
                score += idf * ((tf * (k1 + 1.0)) / denom)
                if tok in title_lower:
                    score += 0.5

            # Small tie-breaker favoring product-specific benefit summaries when product is known
            if preferred_product and doc.product_code == preferred_product:
                score += 0.25

            scored.append((doc, score))

        scored.sort(key=lambda pair: (-pair[1], pair[0].doc_id))
        return scored
