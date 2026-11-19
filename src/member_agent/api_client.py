"""HTTP client for the tenant member-services APIs and third-party provider directory.

Handles:
- Strict tenant-to-bearer-token binding (compensating for upstream servers that do not verify token-to-path match).
- Claims pagination and 429 Retry-After backoff.
- Retry-safe grievance creation using deterministic Idempotency-Key + canonical JSON bytes across 503 retries.
- Third-party provider directory calls using X-Api-Key only (never forwarding tenant bearer tokens).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx


class APIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class TenantAuthError(APIError):
    pass


@dataclass(frozen=True)
class GrievanceWriteResult:
    grievance: dict[str, Any]
    idempotency_key: str
    attempts: int
    status_code: int
    idempotent_replayed: bool


class MemberServicesClient:
    """Thin, resilient HTTP client for member-services and provider-directory endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        tenant_tokens: dict[str, str],
        directory_api_key: str,
        timeout_s: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._tenant_tokens = dict(tenant_tokens)
        self._directory_api_key = directory_api_key
        self._sleep = sleep_fn
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _tenant_headers(self, tenant: str) -> dict[str, str]:
        token = self._tenant_tokens.get(tenant)
        if not token:
            raise TenantAuthError(f"No API bearer token configured for tenant '{tenant}'.", status_code=401)
        return {"Authorization": f"Bearer {token}"}

    def _assert_tenant_path(self, tenant: str, path: str) -> None:
        expected_prefix = f"/tenants/{tenant}/"
        if not path.startswith(expected_prefix):
            raise TenantAuthError(
                f"Tenant isolation violation: path '{path}' does not belong to tenant '{tenant}'.",
                status_code=403,
            )

    def _get_with_rate_limit_retry(
        self,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        max_retries: int = 5,
    ) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            resp = self._http.get(path, headers=headers, params=params)
            if resp.status_code == 429 and attempt <= max_retries:
                retry_after_raw = resp.headers.get("Retry-After", "1")
                try:
                    wait_s = max(float(retry_after_raw), 0.1)
                except ValueError:
                    wait_s = 1.0
                self._sleep(min(wait_s, 10.0))
                continue
            return resp

    def get_health(self) -> dict[str, Any]:
        resp = self._http.get("/health")
        resp.raise_for_status()
        return resp.json()

    def get_member(self, tenant: str, member_id: str) -> dict[str, Any] | None:
        path = f"/tenants/{tenant}/members/{member_id}"
        self._assert_tenant_path(tenant, path)
        resp = self._http.get(path, headers=self._tenant_headers(tenant))
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise APIError(f"get_member failed ({resp.status_code}): {resp.text}", status_code=resp.status_code)
        return resp.json()

    def get_benefits(self, tenant: str, member_id: str) -> dict[str, Any] | None:
        path = f"/tenants/{tenant}/members/{member_id}/benefits"
        self._assert_tenant_path(tenant, path)
        resp = self._http.get(path, headers=self._tenant_headers(tenant))
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise APIError(f"get_benefits failed ({resp.status_code}): {resp.text}", status_code=resp.status_code)
        return resp.json()

    def list_claims(self, tenant: str, member_id: str, page_size: int = 5) -> list[dict[str, Any]]:
        headers = self._tenant_headers(tenant)
        path: str | None = f"/tenants/{tenant}/members/{member_id}/claims"
        params: dict[str, Any] | None = {"page_size": min(max(page_size, 1), 5)}
        claims: list[dict[str, Any]] = []

        while path:
            self._assert_tenant_path(tenant, path)
            resp = self._get_with_rate_limit_retry(path, headers=headers, params=params)
            if resp.status_code == 404:
                return []
            if resp.status_code >= 400:
                raise APIError(f"list_claims failed ({resp.status_code}): {resp.text}", status_code=resp.status_code)
            payload = resp.json()
            claims.extend(payload.get("data", []))
            next_path = payload.get("next")
            if next_path:
                # Subsequent cursor URLs already carry query parameters
                path = next_path
                params = None
            else:
                path = None
        return claims

    def get_claim(self, tenant: str, claim_id: str) -> dict[str, Any] | None:
        path = f"/tenants/{tenant}/claims/{claim_id}"
        self._assert_tenant_path(tenant, path)
        resp = self._get_with_rate_limit_retry(path, headers=self._tenant_headers(tenant))
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise APIError(f"get_claim failed ({resp.status_code}): {resp.text}", status_code=resp.status_code)
        return resp.json()

    def list_grievances(self, tenant: str, member_id: str) -> list[dict[str, Any]]:
        path = f"/tenants/{tenant}/members/{member_id}/grievances"
        self._assert_tenant_path(tenant, path)
        resp = self._http.get(path, headers=self._tenant_headers(tenant))
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise APIError(f"list_grievances failed ({resp.status_code}): {resp.text}", status_code=resp.status_code)
        return resp.json().get("data", [])

    def create_grievance(
        self,
        tenant: str,
        member_id: str,
        *,
        category: str,
        description: str,
        expedite_requested: bool,
        idempotency_key: str,
        max_retries: int = 4,
    ) -> GrievanceWriteResult:
        """Create a grievance with deterministic Idempotency-Key and canonical JSON bytes.

        Safe under partial failures (e.g. server commits write then returns 503):
        retrying with identical bytes and Idempotency-Key replays the original record
        rather than creating a duplicate.
        """
        path = f"/tenants/{tenant}/members/{member_id}/grievances"
        self._assert_tenant_path(tenant, path)
        headers = {
            **self._tenant_headers(tenant),
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        canonical_bytes = json.dumps(
            {
                "category": category,
                "description": description.strip(),
                "expedite_requested": bool(expedite_requested),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        last_err: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = self._http.post(path, headers=headers, content=canonical_bytes)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = exc
                if attempt < max_retries:
                    self._sleep(0.25 * (2 ** (attempt - 1)))
                    continue
                raise APIError(f"Grievance write transport failure after {attempt} attempts: {exc}") from exc

            if resp.status_code in (200, 201):
                replayed = resp.headers.get("Idempotent-Replayed", "").lower() == "true"
                return GrievanceWriteResult(
                    grievance=resp.json(),
                    idempotency_key=idempotency_key,
                    attempts=attempt,
                    status_code=resp.status_code,
                    idempotent_replayed=replayed,
                )

            if resp.status_code in (502, 503, 504) and attempt < max_retries:
                self._sleep(0.25 * (2 ** (attempt - 1)))
                continue

            err_body: dict[str, Any] = {}
            try:
                err_body = resp.json()
            except ValueError:
                pass
            raise APIError(
                f"create_grievance failed ({resp.status_code}): {err_body.get('message', resp.text)}",
                status_code=resp.status_code,
                code=err_body.get("error"),
            )

        raise APIError(f"create_grievance exhausted retries: {last_err}")

    def search_providers(
        self,
        *,
        query: str | None = None,
        specialty: str | None = None,
        zip_code: str | None = None,
        network_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        if not self._directory_api_key:
            raise APIError("DIRECTORY_API_KEY is not configured.", status_code=401)
        params: dict[str, Any] = {"limit": min(max(limit, 1), 50)}
        if query:
            params["query"] = query
        if specialty:
            params["specialty"] = specialty
        if zip_code:
            params["zip"] = zip_code
        if network_id:
            params["network_id"] = network_id

        resp = self._http.get(
            "/directory/v3/providers",
            headers={"X-Api-Key": self._directory_api_key},
            params=params,
        )
        if resp.status_code >= 400:
            raise APIError(f"search_providers failed ({resp.status_code}): {resp.text}", status_code=resp.status_code)
        return resp.json()

    def get_provider(self, npi: str) -> dict[str, Any] | None:
        if not self._directory_api_key:
            raise APIError("DIRECTORY_API_KEY is not configured.", status_code=401)
        resp = self._http.get(
            f"/directory/v3/providers/{npi}",
            headers={"X-Api-Key": self._directory_api_key},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise APIError(f"get_provider failed ({resp.status_code}): {resp.text}", status_code=resp.status_code)
        return resp.json()
