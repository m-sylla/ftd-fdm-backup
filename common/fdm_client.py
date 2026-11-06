"""
Thin client for the FTD device-local (FDM) REST API.

Covers: OAuth-style token auth, generic CRUD, a live-spec-backed endpoint
resolver (so the migration scripts don't hardcode resource paths that can
shift between FTD releases), name-based lookup to avoid duplicate objects,
and triggering/polling a deployment.

Reference: on the FTD box itself, https://<host>/#/api-explorer documents the
exact object model and endpoints for the running version. FDM_API_VERSION
below is "latest", which FDM resolves server-side to the newest API version
that build supports.

NOTE: this is a standalone copy, identical to ../../common/fdm_client.py at
the main asa-to-ftd-migration project root. Duplicated on purpose so that
backup_automation/ is a self-contained folder that can be handed to the
customer without the rest of the migration toolkit -- not shared code, so if
the original gets a fix/improvement, copy it over here by hand too.
"""
import json
import sys
import time
import warnings
from urllib.parse import urljoin

import requests
import urllib3

FDM_API_VERSION = "latest"


class FdmApiError(RuntimeError):
    pass


class FdmClient:
    def __init__(self, host, username, password, port=443, verify_ssl=False,
                 ca_bundle=None, dry_run=False):
        self.base_url = f"https://{host}:{port}/api/fdm/{FDM_API_VERSION}/"
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.dry_run = dry_run
        self.verify = ca_bundle if (verify_ssl and ca_bundle) else verify_ssl
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.session = requests.Session()
        self._access_token = None
        self._refresh_token = None
        self._apispec = None
        self._apispec_warning_shown = False

        if not dry_run:
            self._login()

    # ---------------------------------------------------------------- auth
    def _login(self):
        url = urljoin(self.base_url, "fdm/token")
        resp = self.session.post(
            url,
            json={"grant_type": "password", "username": self.username,
                  "password": self.password},
            verify=self.verify,
            timeout=30,
        )
        if resp.status_code != 200:
            raise FdmApiError(
                f"Login failed ({resp.status_code}): {resp.text}\n"
                "Check host/port/credentials, and that this account has API access."
            )
        data = resp.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token")
        self.session.headers.update({"Authorization": f"Bearer {self._access_token}"})

    def _refresh(self):
        url = urljoin(self.base_url, "fdm/token")
        resp = self.session.post(
            url,
            json={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
            verify=self.verify,
            timeout=30,
        )
        if resp.status_code != 200:
            # refresh token expired/rotated out too -> full re-login
            self._login()
            return
        data = resp.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self.session.headers.update({"Authorization": f"Bearer {self._access_token}"})

    # ------------------------------------------------------------- generic
    def request(self, method, path, json_body=None, params=None, _retried=False):
        url = urljoin(self.base_url, path.lstrip("/"))
        if self.dry_run:
            print(f"[DRY-RUN] {method} {url}")
            if json_body is not None:
                print(json.dumps(json_body, indent=2))
            # Return a fake object so callers can keep flowing during dry-run.
            fake = dict(json_body or {})
            fake.setdefault("id", f"dryrun-{abs(hash(url + json.dumps(json_body or {}))) % 10**8}")
            fake.setdefault("name", fake.get("name", "dry-run-object"))
            return fake

        resp = self.session.request(method, url, json=json_body, params=params,
                                     verify=self.verify, timeout=60)
        if resp.status_code == 401 and not _retried:
            self._refresh()
            return self.request(method, path, json_body, params, _retried=True)
        if resp.status_code >= 400:
            raise FdmApiError(f"{method} {path} -> {resp.status_code}: {resp.text}")
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, body):
        return self.request("POST", path, json_body=body)

    def put(self, path, body):
        return self.request("PUT", path, json_body=body)

    def delete(self, path):
        return self.request("DELETE", path)

    def get_all_pages(self, path, params=None, limit=100):
        params = dict(params or {})
        params["limit"] = limit
        offset = 0
        items = []
        while True:
            params["offset"] = offset
            page = self.get(path, params=params)
            data = page.get("items", [])
            items.extend(data)
            if len(data) < limit:
                break
            offset += limit
        return items

    # -------------------------------------------------------- object reuse
    def find_by_name(self, collection_path, name):
        """Return the existing object dict with this name (case/whitespace-insensitive), or None."""
        if self.dry_run:
            return None
        try:
            results = self.get_all_pages(collection_path, params={"filter": f"name:{name}"})
        except FdmApiError:
            return None
        return self._match_name(results, name)

    def _find_by_name_unfiltered(self, collection_path, name):
        """Full, unfiltered scan of the collection. Used as a fallback when the
        server rejects a create as a duplicate name but the filtered lookup in
        find_by_name() didn't turn it up -- which can happen if the `filter`
        query param's matching semantics differ from ours (case, whitespace)."""
        try:
            results = self.get_all_pages(collection_path)
        except FdmApiError:
            return None
        return self._match_name(results, name)

    @staticmethod
    def _match_name(items, name):
        target = name.strip().lower()
        for item in items:
            if str(item.get("name", "")).strip().lower() == target:
                return item
        return None

    def create_or_reuse(self, collection_path, body, skip_existing=True):
        if skip_existing:
            existing = self.find_by_name(collection_path, body["name"])
            if existing:
                print(f"  = '{body['name']}' already exists (id={existing['id']}), reusing")
                return existing
        self.validate_body_fields(collection_path, body)
        try:
            created = self.post(collection_path, body)
        except FdmApiError as e:
            if skip_existing and "duplicate name" in str(e).lower():
                # The pre-check above can still miss a real duplicate (filter
                # query semantics, case difference, etc.) -- the server's own
                # rejection is authoritative, so fall back to a full scan.
                existing = self._find_by_name_unfiltered(collection_path, body["name"])
                if existing:
                    print(f"  = '{body['name']}' already exists (id={existing['id']}, found "
                          f"via fallback scan after a duplicate-name rejection), reusing")
                    return existing
                print(f"  ! '{body['name']}' was rejected as a duplicate name, but no "
                      f"matching object was found in {collection_path} -- it likely already "
                      f"exists as a different object type (FDM may enforce unique names "
                      f"across kinds of objects); check FDM manually for an existing object "
                      f"named '{body['name']}'")
            raise
        print(f"  + created '{body['name']}' (id={created.get('id')})")
        return created

    # ------------------------------------------------------- schema sanity
    def get_schema_properties(self, path, method="post"):
        """Best-effort: top-level property names the live API expects for
        POST <path>, read from the device's own OpenAPI spec. Returns None if
        it can't be determined (spec unreachable, path not found, etc.)."""
        try:
            spec = self.get_apispec()
        except FdmApiError:
            return None
        paths = spec.get("paths", {})
        key = path if path in paths else f"/{path}" if f"/{path}" in paths else None
        if key is None:
            for p in paths:
                if p.lstrip("/") == path.lstrip("/"):
                    key = p
                    break
        if key is None:
            return None
        op = paths.get(key, {}).get(method, {})
        schema_ref = None
        for param in op.get("parameters", []):
            if param.get("in") == "body":
                schema_ref = param.get("schema", {}).get("$ref")
        if not schema_ref:
            schema_ref = (op.get("requestBody", {}).get("content", {})
                          .get("application/json", {}).get("schema", {}).get("$ref"))
        if not schema_ref:
            return None
        def_name = schema_ref.split("/")[-1]
        defs = spec.get("definitions") or spec.get("components", {}).get("schemas", {})
        schema = defs.get(def_name)
        if not schema:
            return None
        return set(schema.get("properties", {}).keys())

    def validate_body_fields(self, path, body, method="post"):
        if self.dry_run:
            return
        props = self.get_schema_properties(path, method)
        if props is None:
            return
        unknown = set(body.keys()) - props
        if unknown:
            print(f"  ! warning: field(s) {sorted(unknown)} not in the live schema for "
                  f"POST {path} -- expected one of: {sorted(props)}. The field may have been "
                  f"renamed in this FTD version; check https://{self.host}/#/api-explorer "
                  f"before trusting the result.")

    # --------------------------------------------------------- live spec
    def get_apispec(self):
        if self._apispec is None:
            if self.dry_run:
                # In dry-run without a live device we can't fetch the spec;
                # resolve_endpoint() falls back to the built-in defaults.
                self._apispec = {"paths": {}}
            else:
                # Try a couple of known locations -- which one serves it (and
                # whether it's JSON at all rather than the web UI's SPA HTML
                # shell for an unmatched path) varies by build.
                candidates = [
                    f"https://{self.host}:{self.port}/apispec/en-us.json",
                    f"https://{self.host}:{self.port}/api/fdm/{FDM_API_VERSION}/apispec/en-us.json",
                ]
                spec = None
                last_err = None
                for url in candidates:
                    try:
                        resp = self.session.get(url, verify=self.verify, timeout=30)
                        if resp.status_code != 200:
                            last_err = f"{resp.status_code} from {url}"
                            continue
                        spec = resp.json()
                        break
                    except ValueError as e:
                        # Non-JSON 200 response (e.g. the SPA's index.html for
                        # an unmatched path) -- try the next candidate.
                        last_err = f"non-JSON response from {url} ({e})"
                        continue
                    except requests.exceptions.RequestException as e:
                        last_err = f"{url}: {e}"
                        continue
                if spec is None:
                    raise FdmApiError(
                        f"Could not fetch a usable API spec ({last_err}). Live endpoint/field "
                        f"validation is disabled for this run -- falling back to this "
                        f"toolkit's hardcoded default paths/field names, which may not all be "
                        f"correct for your build. Object creation itself still works; only the "
                        f"extra safety checks are skipped."
                    )
                self._apispec = spec
        return self._apispec

    def resolve_endpoint(self, default_path, keywords):
        """
        Confirm `default_path` (or find the real one) by checking the live
        OpenAPI spec for a POST path containing all `keywords`. Falls back to
        default_path if the spec can't be fetched or nothing matches -- the
        default is the documented FDM API path as of FTD 7.6.
        """
        if self.dry_run:
            return default_path  # no live device to check the spec against
        try:
            spec = self.get_apispec()
        except FdmApiError as e:
            if not self._apispec_warning_shown:
                print(f"  ! warning: {e}")
                self._apispec_warning_shown = True
            return default_path
        paths = spec.get("paths", {})
        if default_path in paths or f"/{default_path}" in paths:
            return default_path
        for p, ops in paths.items():
            if "post" in ops and all(k.lower() in p.lower() for k in keywords):
                return p.lstrip("/")
        print(f"  ! warning: could not confirm endpoint for {keywords} in live API spec, "
              f"using default '{default_path}' -- verify in /#/api-explorer if this fails")
        return default_path

    # --------------------------------------------------------------- deploy
    def deploy(self, wait=True, poll_interval=5, timeout=600):
        if self.dry_run:
            print("[DRY-RUN] would trigger deployment now")
            return
        job = self.post("operational/deploy", {})
        job_id = job.get("id")
        print(f"Deployment triggered (job id={job_id})")
        if not wait:
            return job
        start = time.time()
        while time.time() - start < timeout:
            status = self.get(f"operational/deploy/{job_id}")
            state = status.get("state")
            print(f"  deploy state: {state}")
            if state in ("DEPLOYED", "FAILED"):
                if state == "FAILED":
                    raise FdmApiError(f"Deployment failed: {json.dumps(status, indent=2)}")
                return status
            time.sleep(poll_interval)
        raise FdmApiError("Timed out waiting for deployment to finish")


def load_config(path="config.yaml"):
    import yaml
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return cfg


def build_client(cfg):
    ftd = cfg["ftd"]
    dry_run = cfg.get("options", {}).get("dry_run", True)
    if dry_run:
        print("=== DRY RUN MODE: no changes will be sent to the FTD ===")
    return FdmClient(
        host=ftd["host"],
        username=ftd["username"],
        password=ftd["password"],
        port=ftd.get("port", 443),
        verify_ssl=ftd.get("verify_ssl", False),
        ca_bundle=ftd.get("ca_bundle"),
        dry_run=dry_run,
    )
