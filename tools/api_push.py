#!/usr/bin/env python3
"""通过 GitHub Git Data API 推送本地仓库（github.com 的 git 端口不通时用）。

api.github.com 可达，但 github.com:443 连不上，所以 git push 用不了。
这里用 REST API 逐个上传 blob/tree/commit，保留完整提交历史。
"""
import base64
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

TOKEN = pathlib.Path.home().joinpath(".gh-token").read_text().strip()
REPO = sys.argv[1] if len(sys.argv) > 1 else "unknown70022024/lightning-pipeline"
BRANCH = "main"
API = "https://api.github.com"


def api(method: str, path: str, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        API + path, method=method, data=body,
        headers={"Authorization": f"token {TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace")
        print(f"  !! {method} {path} -> HTTP {exc.code}: {detail}")
        raise


def git(*args) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          check=True).stdout


def bootstrap_empty_repo() -> None:
    """GitHub 的 Git Data API 拒绝在零提交的仓库上建 blob/tree。

    必须先通过 Contents API 造一个根提交，仓库才"非空"。
    这是 GitHub 的硬限制，不是我们的 bug。
    """
    payload = {
        "message": "chore: 初始化仓库\n\n"
                   "GitHub 的 Git Data API 要求仓库至少有一个提交才能创建 "
                   "blob/tree，所以先落一个根提交，随后的提交会立即覆盖整棵树。",
        "content": base64.b64encode(b"# lightning-pipeline\n").decode(),
    }
    api("PUT", f"/repos/{REPO}/contents/README.md", payload)
    print("  已创建根提交（空仓库引导）")


def main() -> int:
    parent = None
    try:
        parent = api("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}")["object"]["sha"]
        print(f"远端分支 {BRANCH} 已存在，追加模式，parent={parent[:9]}")
        shas = git("rev-list", "--reverse", f"{parent}..HEAD").split()
    except urllib.error.HTTPError:
        # 仓库为空（409）或分支不存在（404）都走这里
        shas = git("rev-list", "--reverse", "HEAD").split()
        try:
            api("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}")
        except urllib.error.HTTPError:
            pass
        try:
            has_commits = bool(api("GET", f"/repos/{REPO}/commits?per_page=1"))
        except urllib.error.HTTPError:
            has_commits = False        # 空仓库对 /commits 也返回 409
        if not has_commits:
            bootstrap_empty_repo()
            parent = api("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}")["object"]["sha"]
            print(f"  根提交 = {parent[:9]}")

    if not shas:
        print("没有需要推送的提交")
        return 0

    blob_cache: dict[str, str] = {}
    print(f"共 {len(shas)} 个提交，仓库 {REPO}")

    for n, sha in enumerate(shas, 1):
        message = git("log", "-1", "--format=%B", sha).rstrip()
        subject = message.splitlines()[0]
        entries = []
        for line in git("ls-tree", "-r", sha).splitlines():
            meta, path = line.split("\t", 1)
            mode, _typ, bhash = meta.split()
            if bhash not in blob_cache:
                content = subprocess.run(["git", "cat-file", "blob", bhash],
                                         capture_output=True, check=True).stdout
                res = api("POST", f"/repos/{REPO}/git/blobs", {
                    "content": base64.b64encode(content).decode(),
                    "encoding": "base64"})
                blob_cache[bhash] = res["sha"]
            entries.append({"path": path, "mode": mode, "type": "blob",
                            "sha": blob_cache[bhash]})

        tree = api("POST", f"/repos/{REPO}/git/trees",
                   {"tree": entries})["sha"]
        commit_body = {"message": message, "tree": tree}
        if parent:
            commit_body["parents"] = [parent]
        parent = api("POST", f"/repos/{REPO}/git/commits", commit_body)["sha"]
        print(f"  [{n}/{len(shas)}] {parent[:9]} {subject[:56]} "
              f"({len(entries)} 文件)")

    try:
        api("POST", f"/repos/{REPO}/git/refs",
            {"ref": f"refs/heads/{BRANCH}", "sha": parent})
        mode = "新建分支"
    except urllib.error.HTTPError:
        api("PATCH", f"/repos/{REPO}/git/refs/heads/{BRANCH}",
            {"sha": parent, "force": True})
        mode = "更新分支"
    print(f"\n{mode} {BRANCH} -> {parent[:9]}")
    print(f"https://github.com/{REPO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
