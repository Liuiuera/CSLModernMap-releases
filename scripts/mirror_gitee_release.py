import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


GITHUB = "https://api.github.com/repos/Liuiuera/CSLModernMap-releases"
GITEE = "https://gitee.com/api/v5/repos/liuiuiera/CSLModernMap-releases"
REQUEST_TIMEOUT = 60
TAG_SYNC_TIMEOUT = 600
ASSET_LIMIT = 100_000_000
CHUNK_SIZE = 1024 * 1024


def api(url, *, method="GET", headers=None, payload=None, token=None, missing_ok=False):
    if token:
        url += "?" + urlencode({"access_token": token})
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.load(response)
    except HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise RuntimeError(f"{method} {urlsplit(url).path} failed (HTTP {error.code}).") from None
    except (URLError, TimeoutError, HTTPException, ValueError):
        raise RuntimeError(f"{method} {urlsplit(url).path} did not return valid JSON.") from None


def github_tag_commit(tag, headers):
    ref = api(f"{GITHUB}/git/ref/tags/{quote(tag, safe='')}", headers=headers)["object"]
    while ref["type"] == "tag":
        ref = api(f"{GITHUB}/git/tags/{ref['sha']}", headers=headers)["object"]
    if ref["type"] != "commit":
        raise RuntimeError(f"GitHub tag {tag} does not point to a commit.")
    return ref["sha"]


def gitee_tag_commit(tag):
    page = 1
    while True:
        tags = api(f"{GITEE}/tags?per_page=100&page={page}")
        for candidate in tags:
            if candidate["name"] == tag:
                return candidate["commit"]["sha"]
        if len(tags) < 100:
            return None
        page += 1


def wait_for_tag(tag, expected, apply):
    deadline = time.monotonic() + TAG_SYNC_TIMEOUT
    while True:
        actual = gitee_tag_commit(tag)
        if actual == expected:
            return
        if actual:
            raise RuntimeError(f"Gitee tag {tag} points to {actual}, expected {expected}.")
        if not apply or time.monotonic() >= deadline:
            raise RuntimeError(f"Gitee tag {tag} has not arrived from GitHub.")
        print(f"Waiting for Gitee tag {tag} ...", flush=True)
        time.sleep(15)


def file_digest(asset, host, destination=None):
    name = asset["name"]
    url = asset["browser_download_url"]
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != host:
        raise RuntimeError(f"Unexpected download URL for {name!r}.")

    digest = hashlib.sha256()
    size = 0
    output = destination.open("wb") if destination else None
    try:
        with urlopen(url, timeout=REQUEST_TIMEOUT) as response:
            while chunk := response.read(CHUNK_SIZE):
                size += len(chunk)
                if size > asset["size"]:
                    raise RuntimeError(f"Unexpected download size for {name!r} from {host}.")
                digest.update(chunk)
                if output:
                    output.write(chunk)
    except (URLError, TimeoutError, HTTPException):
        raise RuntimeError(f"Could not download {name!r} from {host}.") from None
    finally:
        if output:
            output.close()

    if size != asset["size"]:
        raise RuntimeError(f"Unexpected download size for {name!r} from {host}.")
    return digest.hexdigest()


def github_digest(asset, destination=None):
    recorded = asset.get("digest")
    if destination is None and recorded and re.fullmatch(r"sha256:[0-9a-f]{64}", recorded):
        return recorded.removeprefix("sha256:")
    actual = file_digest(asset, "github.com", destination)
    if recorded and recorded != f"sha256:{actual}":
        raise RuntimeError(f"GitHub checksum does not match for {asset['name']!r}.")
    return actual


def list_attachments(release_id):
    return api(f"{GITEE}/releases/{release_id}/attach_files")


def upload_attachment(release_id, asset, path, token):
    boundary = f"gitee-{uuid.uuid4().hex}"
    name = asset["name"].replace("\\", "\\\\").replace('"', '\\"')
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")

    def multipart(source):
        yield prefix
        while chunk := source.read(CHUNK_SIZE):
            yield chunk
        yield suffix

    url = f"{GITEE}/releases/{release_id}/attach_files"
    url += "?" + urlencode({"access_token": token})
    with path.open("rb") as source:
        request = Request(url, data=multipart(source), method="POST", headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(prefix) + path.stat().st_size + len(suffix)),
        })
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"Gitee upload of {asset['name']!r} failed (HTTP {error.code}).") from None
        except (URLError, TimeoutError, HTTPException, ValueError):
            raise RuntimeError(f"Gitee upload of {asset['name']!r} did not complete.") from None


def sync_release(tag, apply, github_token, gitee_token):
    github_headers = {"Authorization": f"Bearer {github_token}"} if github_token else {}
    encoded_tag = quote(tag, safe="")
    source = api(f"{GITHUB}/releases/tags/{encoded_tag}", headers=github_headers)
    if source["draft"]:
        raise RuntimeError(f"GitHub release {tag} is still a draft.")
    if not source["name"]:
        raise RuntimeError(f"GitHub release {tag} has no title.")
    commit = github_tag_commit(tag, github_headers)
    wait_for_tag(tag, commit, apply)
    target_url = f"{GITEE}/releases/tags/{encoded_tag}"
    target = api(target_url, missing_ok=True)
    existing = {
        asset["name"]: asset
        for asset in (list_attachments(target["id"]) if target else [])
    }
    metadata = {
        "name": source["name"],
        "body": source["body"] or "",
        "prerelease": source["prerelease"],
    }
    metadata_changed = not target or any(target.get(key) != value for key, value in metadata.items())

    with tempfile.TemporaryDirectory(prefix="gitee-release-") as temporary_dir:
        pending = []
        for index, asset in enumerate(source["assets"]):
            name = asset["name"]
            if asset["state"] != "uploaded" or not 0 < asset["size"] <= ASSET_LIMIT:
                raise RuntimeError(f"GitHub attachment {name!r} is incomplete or exceeds the Gitee limit.")
            if "\r" in name or "\n" in name:
                raise RuntimeError(f"Invalid attachment name {name!r}.")
            if match := existing.get(name):
                if match["size"] != asset["size"] or (
                    file_digest(match, "gitee.com") != github_digest(asset)
                ):
                    raise RuntimeError(f"Gitee already has another file named {name!r}; resolve the conflict first.")
                print(f"Verified existing attachment: {name}")
                continue
            print(f"Missing Gitee attachment: {name}")
            if apply:
                path = Path(temporary_dir) / str(index)
                pending.append((asset, path, github_digest(asset, path)))

        action = "update" if target else "create"
        if not apply:
            print(f"Dry run: {'would ' + action if metadata_changed else 'no change to'} release {tag}.")
            return
        if not target:
            target = api(f"{GITEE}/releases", method="POST", token=gitee_token, payload={
                **metadata, "tag_name": tag, "target_commitish": commit,
            })
        elif metadata_changed:
            api(f"{GITEE}/releases/{target['id']}", method="PATCH", token=gitee_token, payload=metadata)

        for asset, path, _ in pending:
            upload_attachment(target["id"], asset, path, gitee_token)
            print(f"Uploaded {asset['name']}")

        published = api(target_url)
        if not published or any(published.get(key) != value for key, value in metadata.items()):
            raise RuntimeError(f"Could not verify Gitee metadata for {tag}.")
        if pending:
            uploaded = {asset["name"]: asset for asset in list_attachments(target["id"])}
            for asset, _, digest in pending:
                match = uploaded.get(asset["name"])
                if not match or file_digest(match, "gitee.com") != digest:
                    raise RuntimeError(f"Could not verify Gitee checksum for {asset['name']!r}.")
        print(f"Gitee release {tag} matches GitHub.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("tag", help="A published GitHub release tag.")
    parser.add_argument("--apply", action="store_true", help="Write changes to Gitee.")
    args = parser.parse_args()
    gitee_token = os.environ.get("GITEE_RELEASE_TOKEN")
    if args.apply and not gitee_token:
        parser.error("GITEE_RELEASE_TOKEN is required to write to Gitee.")
    try:
        sync_release(args.tag, args.apply, os.environ.get("GH_TOKEN"), gitee_token)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
