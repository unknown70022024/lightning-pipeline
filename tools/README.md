# tools/

本地维护脚本，不参与管线运行。

| 脚本 | 用途 |
|---|---|
| `api_push.py` | 通过 GitHub Git Data API 推送。**本机 `github.com:443` 不通**（HTTP 000），只有 `api.github.com` 可达，所以 `git push` 用不了，改用 REST API。 |
| `set_secrets.py` | 把 `~/.lightning.env` 里的 EUMETSAT 密钥加密写入 GitHub Actions Secrets，免去网页操作。 |

用法：

```bash
python tools/api_push.py unknown70022024/lightning-pipeline
python tools/set_secrets.py unknown70022024/lightning-pipeline
```

两者都从 `~/.gh-token` 读 PAT（600 权限，仓库外）。
