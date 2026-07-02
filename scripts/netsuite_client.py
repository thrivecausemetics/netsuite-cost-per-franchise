"""Minimal NetSuite SuiteQL client over REST with OAuth1 Token-Based Auth.

Mirrors the client used by thrivecausemetics/item-cost-variance:
- OAuth1 HMAC-SHA256 with realm = account ID (TBA).
- Long request timeout (240s) — NetSuite can be slow on analytic queries.
- Retries with exponential backoff on transient HTTP errors.
- limit/offset pagination so callers always get the complete result set.

Read-only by design: only the SuiteQL query endpoint is implemented.
"""

import json
import time

import requests
from requests_oauthlib import OAuth1

SUITEQL_PAGE_LIMIT = 1000  # REST SuiteQL maximum rows per request
REQUEST_TIMEOUT_SECONDS = 240
MAX_RETRIES = 4


class NetSuiteClient:
    def __init__(self, account_id, consumer_key, consumer_secret, token_id, token_secret):
        # URL slug lowercases and dash-ifies the account ID (e.g. 1234567_SB1 -> 1234567-sb1)
        slug = account_id.lower().replace("_", "-")
        self.url = f"https://{slug}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
        self.auth = OAuth1(
            consumer_key,
            client_secret=consumer_secret,
            resource_owner_key=token_id,
            resource_owner_secret=token_secret,
            signature_method="HMAC-SHA256",
            realm=account_id.upper(),
        )

    def suiteql(self, query):
        """Run a SuiteQL query and return ALL rows (follows pagination).

        Queries must not use server-side ORDER BY on large tables — it blows
        the NetSuite timeout. Sort in Python instead.
        """
        rows = []
        offset = 0
        while True:
            page = self._post(query, offset)
            rows.extend(page.get("items", []))
            if not page.get("hasMore"):
                return rows
            offset += SUITEQL_PAGE_LIMIT

    def _post(self, query, offset):
        params = {"limit": SUITEQL_PAGE_LIMIT, "offset": offset}
        headers = {"Prefer": "transient", "Content-Type": "application/json"}
        body = json.dumps({"q": query})
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                time.sleep(2 ** attempt)
            try:
                response = requests.post(
                    self.url,
                    params=params,
                    headers=headers,
                    data=body,
                    auth=self.auth,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                last_error = exc
                continue
            if response.status_code == 200:
                return response.json()
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = RuntimeError(
                    f"SuiteQL HTTP {response.status_code}: {response.text[:300]}"
                )
                continue
            raise RuntimeError(
                f"SuiteQL HTTP {response.status_code}: {response.text[:1000]}"
            )
        raise RuntimeError(f"SuiteQL failed after {MAX_RETRIES + 1} attempts: {last_error}")
