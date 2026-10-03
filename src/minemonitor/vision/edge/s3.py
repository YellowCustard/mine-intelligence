"""A minimal S3 ``PUT`` client (AWS Signature V4, stdlib only) for evidence stills → MinIO.

The edge node needs exactly one object-store operation — upload a small evidence still — so it
signs requests itself rather than pulling boto3/botocore (tens of MB) or the MinIO SDK into the
edge image. Path-style addressing (``<endpoint>/<bucket>/<key>``), which is what MinIO serves.

Signing follows the published SigV4 algorithm and is verified in the tests against AWS's own
worked example, so a bug here fails CI rather than a commissioning visit.

The secret key is used only to derive the signature; it is never logged or put in a header.
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime

from minemonitor import net

_ALGO = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class S3Error(Exception):
    """The object store refused or could not be reached; the caller retries later."""


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _uri_encode(s: str, *, keep_slash: bool) -> str:
    return urllib.parse.quote(s, safe="/~" if keep_slash else "~")


def sign_v4(
    *,
    method: str,
    host: str,
    path: str,
    query: Mapping[str, str] | None,
    headers: Mapping[str, str],
    payload_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    when: datetime,
    service: str = "s3",
) -> dict[str, str]:
    """Return ``headers`` plus ``host``, ``x-amz-date``, ``x-amz-content-sha256`` and the
    ``Authorization`` header for one request. ``path`` is the unencoded object path."""
    if when.tzinfo is None:
        raise ValueError("when must be timezone-aware")
    when = when.astimezone(UTC)
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    day = when.strftime("%Y%m%d")
    all_headers = {k.lower(): str(v).strip() for k, v in headers.items()}
    all_headers["host"] = host
    all_headers["x-amz-date"] = amz_date
    all_headers["x-amz-content-sha256"] = payload_sha256
    signed = sorted(all_headers)
    canonical_headers = "".join(f"{k}:{all_headers[k]}\n" for k in signed)
    signed_headers = ";".join(signed)
    canonical_query = "&".join(
        f"{_uri_encode(k, keep_slash=False)}={_uri_encode(v, keep_slash=False)}"
        for k, v in sorted((query or {}).items())
    )
    canonical_request = "\n".join(
        [
            method,
            _uri_encode(path, keep_slash=True),
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_sha256,
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [_ALGO, amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    k = _hmac(("AWS4" + secret_key).encode(), day)
    k = _hmac(k, region)
    k = _hmac(k, service)
    k = _hmac(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
    all_headers["authorization"] = (
        f"{_ALGO} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return all_headers


class S3Uploader:
    """Upload objects to one bucket on an S3-compatible endpoint (MinIO), path-style."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        *,
        region: str = "us-east-1",
        timeout_s: float = 10.0,
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("endpoint must be http(s)://host[:port]")
        if not bucket or not access_key or not secret_key:
            raise ValueError("bucket, access key and secret key are required")
        self.base = f"{parsed.scheme}://{parsed.netloc}"
        self.host = parsed.netloc
        self.bucket = bucket
        self.region = region
        self.timeout_s = timeout_s
        self._access_key = access_key
        self._secret_key = secret_key

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        """PUT ``data`` at ``key``; return its ``s3://`` URI. Raises :class:`S3Error`."""
        path = f"/{self.bucket}/{key.lstrip('/')}"
        headers = sign_v4(
            method="PUT",
            host=self.host,
            path=path,
            query=None,
            headers={"content-type": content_type},
            payload_sha256=hashlib.sha256(data).hexdigest(),
            access_key=self._access_key,
            secret_key=self._secret_key,
            region=self.region,
            when=datetime.now(UTC),
        )
        headers.pop("host")  # urllib sets Host from the URL; identical value
        req = urllib.request.Request(
            self.base + _uri_encode(path, keep_slash=True), data=data, headers=headers, method="PUT"
        )
        try:
            with net.urlopen(req, timeout=self.timeout_s) as resp:
                if resp.status not in (200, 201, 204):
                    raise S3Error(f"PUT {key}: HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            raise S3Error(f"PUT {key}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise S3Error(f"PUT {key}: {exc}") from exc
        return self.uri(key)
