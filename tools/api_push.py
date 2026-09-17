#!/usr/bin/env python3
"""通过 GitHub Git Data API 推送本地仓库。

为什么不用 git push
-------------------
本机 github.com:443 **完全不通**（curl 返回 HTTP 000），只有 api.github.com
可达。所以 git 的 smart-HTTP 协议用不了，只能走 REST API 逐个上传
blob / tree / commit。

行为
----
把本地**全部**提交重放成一个新的线性历史，然后强制更新远端分支。
内容和提交信息与本地一致，但 sha 不同（GitHub 重新计算）。对一个小型测试
仓库来说，这比维护本地/远端 sha 映射表简单得多。

注意：这是强制更新。远端若有别人推的提交会被覆盖。
"""
import base64
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

TOKEN = pathlib.Path.home().joinpath(".gh-token").read_text().strip()
REPO = sys.argv[1] if len(sys.argv) > 1 else "unknown70022024/lightning-pipeline"
BRANCH = sys.argv[2] if len(sys.argv) > 2 else "main"
API = "https://api.github.com"


def api(method, path, data=None, quiet=False):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        API + path, method=method, data=body,
        headers={"Authorization": "token " + TOKEN,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        if not quiet:
            print("  !! %s %s -> HTTP %s: %s" % (method, path, exc.code, detail))
        raise


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          check=True).stdout


def repo_is_empty():
    try:
        return not api("GET", "/repos/%s/commits?per_page=1" % REPO, quiet=True)
    except urllib.error.HTTPError:
        return True          # 空仓库对 /commits 返回 409


def bootstrap():
    """Git Data API 拒绝在零提交的仓库上建 blob/tree，必须先有根提交。

    这个提交随后会被孤立（分支指向重放出来的真实历史），只是为了解锁 API。
    """
    api("PUT", "/repos/%s/contents/README.md" % REPO, {
        "message": "chore: bootstrap（空仓库需要根提交才能用 Git Data API）",
        "content": base64.b64encode(b"# placeholder\n").decode(),
    })
    print("  已创建引导提交（稍后会被孤立）")


def main():
    shas = git("rev-list", "--reverse", "HEAD").split()
    if not shas:
        print("本地没有提交")
        return 0
    if repo_is_empty():
        bootstrap()

    blob_cache = {}
    print("重放 %d 个提交 -> %s (%s)" % (len(shas), REPO, BRANCH))
    parent = None

    for n, sha in enumerate(shas, 1):
        message = git("log", "-1", "--format=%B", sha).rstrip()
        subject = message.splitlines()[0]
        entries = []
        for line in git("ls-tree", "-r", sha).splitlines():
            meta, path = line.split("\t", 1)
            mode, _type, bhash = meta.split()
            if bhash not in blob_cache:
                content = subprocess.run(["git", "cat-file", "blob", bhash],
                                         capture_output=True, check=True).stdout
                blob_cache[bhash] = api("POST", "/repos/%s/git/blobs" % REPO, {
                    "content": base64.b64encode(content).decode(),
                    "encoding": "base64"})["sha"]
            entries.append({"path": path, "mode": mode, "type": "blob",
                            "sha": blob_cache[bhash]})

        tree = api("POST", "/repos/%s/git/trees" % REPO, {"tree": entries})["sha"]
        body = {"message": message, "tree": tree}
        if parent:
            body["parents"] = [parent]
        parent = api("POST", "/repos/%s/git/commits" % REPO, body)["sha"]
        print("  [%d/%d] %s %s (%d 文件)" % (n, len(shas), parent[:9],
                                             subject[:58], len(entries)))

    try:
        api("POST", "/repos/%s/git/refs" % REPO,
            {"ref": "refs/heads/" + BRANCH, "sha": parent}, quiet=True)
        action = "新建"
    except urllib.error.HTTPError:
        api("PATCH", "/repos/%s/git/refs/heads/%s" % (REPO, BRANCH),
            {"sha": parent, "force": True})
        action = "强制更新"
    print("\n%s %s -> %s" % (action, BRANCH, parent[:9]))
    print("https://github.com/" + REPO)
    return 0


if __name__ == "__main__":
    sys.exit(main())
